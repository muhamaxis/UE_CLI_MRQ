import ast
import importlib.util
import os
import pathlib
import queue
import tempfile
import threading
import time
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "code" / "mrq_launcher.py"
SPEC = importlib.util.spec_from_file_location("mrq_launcher", SOURCE)
mrq = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(mrq)


def make_task(number: int) -> mrq.RenderTask:
    name = f"Task{number:03d}"
    return mrq.RenderTask(
        uproject="Project.uproject",
        level=f"/Game/{name}.{name}",
        sequence=name,
        preset="/Game/Preset.Preset",
    )


def extract_qt_shell_method(name: str):
    """Load worker code without importing PySide6 or opening a window."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "QtMRQShell":
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == name:
                    module = ast.Module(body=[child], type_ignores=[])
                    ast.fix_missing_locations(module)
                    namespace = dict(mrq.__dict__)
                    exec(compile(module, str(SOURCE), "exec"), namespace)
                    return namespace[name]
    raise AssertionError(f"QtMRQShell.{name} was not found")


def extract_nested_class_method(class_name: str, name: str):
    """Load a nested Qt helper method without importing or constructing Qt widgets."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == name:
                    module = ast.Module(body=[child], type_ignores=[])
                    ast.fix_missing_locations(module)
                    namespace = dict(mrq.__dict__)
                    exec(compile(module, str(SOURCE), "exec"), namespace)
                    return namespace[name]
    raise AssertionError(f"{class_name}.{name} was not found")


class RuntimeQueueCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tasks = []
        self.statuses = []
        self.logs = []
        self.coordinator = mrq.RuntimeQueueCoordinator(
            lambda task: self.tasks.index(task) if task in self.tasks else None,
            lambda task, status: self.statuses.append((task, status)),
            self.logs.append,
        )

    def test_one_hundred_tasks_keep_order_and_identity(self):
        self.tasks = [make_task(i) for i in range(100)]
        self.assertTrue(self.coordinator.enqueue_tasks(self.tasks))

        consumed = []
        while not self.coordinator.empty():
            task = self.coordinator.get(timeout=0.01)
            consumed.append(task)
            self.coordinator.complete(task)

        self.assertEqual(consumed, self.tasks)
        self.assertEqual(len({id(task) for task in consumed}), 100)
        self.assertFalse(self.coordinator.has_work())

    def test_concurrent_duplicate_enqueue_is_atomic(self):
        self.tasks = [make_task(i) for i in range(100)]
        barrier = threading.Barrier(8)

        def enqueue_same_batch():
            barrier.wait()
            self.coordinator.enqueue_tasks(self.tasks, mark_queued=False)

        threads = [threading.Thread(target=enqueue_same_batch) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        pending = self.coordinator.pending_tasks()
        self.assertEqual(pending, self.tasks)
        self.assertEqual(len({id(task) for task in pending}), 100)

    def test_snapshot_remove_and_dequeue_never_requeue_behind_later_work(self):
        self.tasks = [make_task(i) for i in range(3)]
        self.coordinator.enqueue_tasks(self.tasks, mark_queued=False)
        first = self.coordinator.get(timeout=0.01)

        self.assertIs(first, self.tasks[0])
        self.assertEqual(self.coordinator.task_identity_set(), {id(task) for task in self.tasks})
        self.assertEqual(self.coordinator.remove_tasks([self.tasks[1]]), 1)
        self.assertEqual(self.coordinator.pending_tasks(), [self.tasks[2]])
        self.coordinator.complete(first)
        self.assertIs(self.coordinator.get(timeout=0.01), self.tasks[2])

    def test_reorder_changes_pending_but_not_active(self):
        self.tasks = [make_task(i) for i in range(4)]
        self.coordinator.enqueue_tasks(self.tasks, mark_queued=False)
        active = self.coordinator.get(timeout=0.01)
        self.coordinator.reorder_pending([self.tasks[3], self.tasks[2], self.tasks[1], self.tasks[0]])

        self.assertIs(self.coordinator.active_task(), active)
        self.assertEqual(self.coordinator.pending_tasks(), [self.tasks[3], self.tasks[2], self.tasks[1]])

    def test_second_dequeue_is_rejected_while_task_is_active(self):
        self.tasks = [make_task(i) for i in range(2)]
        self.coordinator.enqueue_tasks(self.tasks, mark_queued=False)
        self.coordinator.get(timeout=0.01)

        with self.assertRaisesRegex(RuntimeError, "another render task is active"):
            self.coordinator.get(timeout=0.01)

    def test_removing_active_task_can_be_rejected_atomically(self):
        self.tasks = [make_task(i) for i in range(2)]
        self.coordinator.enqueue_tasks(self.tasks, mark_queued=False)
        active = self.coordinator.get(timeout=0.01)

        self.assertEqual(self.coordinator.remove_tasks([active], reject_active=True), -1)
        self.assertIs(self.coordinator.active_task(), active)
        self.assertEqual(self.coordinator.pending_tasks(), [self.tasks[1]])

    def test_queued_status_is_emitted_before_waiting_worker_can_dequeue(self):
        task = make_task(1)
        callback_started = threading.Event()
        callback_release = threading.Event()
        dequeued = threading.Event()
        event_order = []

        def status_callback(_task, _status):
            event_order.append("queued")
            callback_started.set()
            callback_release.wait(timeout=5)

        coordinator = mrq.RuntimeQueueCoordinator(lambda _task: 0, status_callback, self.logs.append)

        def consume():
            coordinator.get(timeout=5)
            event_order.append("dequeued")
            dequeued.set()

        consumer = threading.Thread(target=consume)
        producer = threading.Thread(target=lambda: coordinator.enqueue_tasks([task]))
        consumer.start()
        producer.start()
        self.assertTrue(callback_started.wait(timeout=5))
        self.assertFalse(dequeued.is_set())
        callback_release.set()
        producer.join(timeout=5)
        consumer.join(timeout=5)

        self.assertEqual(event_order, ["queued", "dequeued"])


class RuntimeIdentityTests(unittest.TestCase):
    def test_delayed_event_follows_task_after_reorder(self):
        first, second = make_task(1), make_task(2)
        tasks = [first, second]
        states = [mrq.default_task_state(), mrq.default_task_state()]
        event = mrq.TaskRuntimeEvent(
            mrq.TaskRuntimeEventType.TASK_FINISHED,
            status="Done (00:00:01)",
            progress=100,
            task=first,
            session_id=7,
        )

        tasks.reverse()
        states.reverse()
        self.assertTrue(mrq.apply_task_runtime_event(tasks, states, event, current_session_id=7))
        self.assertEqual(states[1]["status"], "Done (00:00:01)")
        self.assertEqual(states[0]["status"], mrq.TaskRuntimeStatus.READY)

    def test_removed_task_and_stale_session_events_are_ignored(self):
        task = make_task(1)
        state = [mrq.default_task_state()]
        stale = mrq.TaskRuntimeEvent(
            mrq.TaskRuntimeEventType.TASK_FAILED,
            status="Failed",
            task=task,
            session_id=1,
        )
        self.assertFalse(mrq.apply_task_runtime_event([task], state, stale, current_session_id=2))
        self.assertFalse(mrq.apply_task_runtime_event([], [], stale, current_session_id=1))
        self.assertEqual(state[0]["status"], mrq.TaskRuntimeStatus.READY)

    def test_persisted_task_has_no_runtime_identity_fields(self):
        task = make_task(1)
        payload = mrq.PersistenceRepository.task_to_payload(task)
        self.assertEqual(
            set(payload),
            {"uproject", "level", "sequence", "preset", "output_dir", "notes", "added_at", "enabled"},
        )


class OrderingContractTests(unittest.TestCase):
    class Harness:
        _sort_tasks_by_session_order = extract_qt_shell_method("_sort_tasks_by_session_order")
        _collect = extract_qt_shell_method("_collect")

        def __init__(self, tasks, order, selected):
            self.settings = mrq.AppSettings(tasks=tasks)
            self.queue_order_by_task_id = {id(task): position for task, position in order}
            self._selected = selected

        def selected_indices(self):
            return list(self._selected)

    def test_selected_enabled_and_all_share_order_contract(self):
        first, second, third = make_task(1), make_task(2), make_task(3)
        third.enabled = False
        harness = self.Harness(
            [first, second, third],
            [(first, 2), (second, 1)],
            selected=[0, 1, 2],
        )

        expected = [second, first, third]
        self.assertEqual(harness._collect(only_selected=True), expected)
        self.assertEqual(harness._collect(only_enabled=True), [second, first])
        self.assertEqual(harness._collect(), expected)


class RuntimeWorkerGateTests(unittest.TestCase):
    def test_only_one_concurrent_worker_reservation_succeeds(self):
        gate = mrq.RuntimeWorkerGate()
        barrier = threading.Barrier(32)
        results = []
        lock = threading.Lock()

        def reserve():
            barrier.wait()
            result = gate.try_reserve(True)
            with lock:
                results.append(result)

        threads = [threading.Thread(target=reserve) for _ in range(32)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(results.count(True), 1)
        self.assertTrue(gate.running)

    def test_completion_boundary_reserves_restart_atomically(self):
        gate = mrq.RuntimeWorkerGate()
        self.assertTrue(gate.try_reserve(True))
        self.assertTrue(gate.release_and_reserve_restart(has_pending=True, stopped=False))
        self.assertTrue(gate.running)
        self.assertFalse(gate.try_reserve(True))


class RenderSessionJournalTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.primary = pathlib.Path(self.tempdir.name) / "portable" / "mrq_logs"
        self.fallback = pathlib.Path(self.tempdir.name) / "user" / "mrq_logs"

    def journal(self, session_id, tasks, started_at=1_800_000_000.125):
        return mrq.RenderSessionJournal(
            session_id,
            tasks,
            commit_cl="CL-12345",
            primary_dir=str(self.primary),
            fallback_dir=str(self.fallback),
            started_at=started_at,
        )

    def test_snapshot_is_immutable_and_contains_only_actual_session_tasks(self):
        first, omitted, selected_disabled = make_task(1), make_task(2), make_task(3)
        selected_disabled.enabled = False
        journal = self.journal(1, [first, selected_disabled])
        first.sequence = "MutatedAfterStart"
        now = time.time()
        journal.record_event(mrq.TaskRuntimeEvent(
            mrq.TaskRuntimeEventType.TASK_FINISHED,
            task=first,
            session_id=1,
            status="Done (00:00:01)",
            start=now - 1,
            end=now,
        ))
        path = journal.checkpoint(final=True)
        text = pathlib.Path(path).read_text(encoding="utf-8")

        self.assertIn("Task001", text)
        self.assertNotIn("MutatedAfterStart", text)
        self.assertNotIn("Task002", text)
        self.assertIn("Task003", text)
        self.assertIn(journal.session_uid, text)

    def test_short_sessions_never_share_a_filename(self):
        first = self.journal(1, [make_task(1)], started_at=1_800_000_000.0)
        second = self.journal(2, [make_task(2)], started_at=1_800_000_000.0)
        first_path = first.checkpoint(final=True)
        second_path = second.checkpoint(final=True)

        self.assertNotEqual(first_path, second_path)
        self.assertTrue(os.path.exists(first_path))
        self.assertTrue(os.path.exists(second_path))

    def test_atomic_replace_failure_removes_temporary_file(self):
        target = self.primary / "Queue_Log_test.log"
        with mock.patch.object(mrq.os, "replace", side_effect=OSError("controlled replace failure")):
            with self.assertRaisesRegex(OSError, "controlled replace failure"):
                mrq.atomic_write_text(str(target), "summary\n")
        self.assertFalse(target.exists())
        self.assertEqual(list(self.primary.glob("*.tmp")), [])

    def test_portable_root_uses_executable_location_not_working_directory(self):
        with mock.patch.object(mrq.sys, "frozen", True, create=True), mock.patch.object(
            mrq.sys,
            "executable",
            r"D:\Portable\MRQ\MRQLauncherCLI.exe",
        ):
            self.assertEqual(mrq.preferred_logs_dir(), r"D:\Portable\MRQ\mrq_logs")

    def test_primary_mkdir_open_write_and_replace_failures_use_fallback(self):
        original = mrq.atomic_write_text
        for stage in ("mkdir", "open", "write", "replace"):
            with self.subTest(stage=stage):
                journal = self.journal(len(stage), [make_task(len(stage))], started_at=1_800_000_000.0 + len(stage))

                def fail_primary(path, text, failure_stage=stage):
                    if os.path.commonpath([os.path.abspath(path), os.path.abspath(self.primary)]) == os.path.abspath(self.primary):
                        raise OSError(f"controlled {failure_stage} failure")
                    return original(path, text)

                with mock.patch.object(mrq, "atomic_write_text", side_effect=fail_primary):
                    path = journal.checkpoint()
                self.assertEqual(os.path.dirname(path), str(self.fallback))
                self.assertTrue(journal.using_fallback)

    def test_real_primary_mkdir_failure_falls_back(self):
        blocker = pathlib.Path(self.tempdir.name) / "not_a_directory"
        blocker.write_text("blocked", encoding="utf-8")
        journal = mrq.RenderSessionJournal(
            8,
            [make_task(1)],
            primary_dir=str(blocker / "mrq_logs"),
            fallback_dir=str(self.fallback),
        )

        path = journal.checkpoint()

        self.assertEqual(os.path.dirname(path), str(self.fallback))
        self.assertTrue(journal.using_fallback)

    def test_all_save_failures_report_every_full_candidate_path_and_can_retry(self):
        journal = self.journal(7, [make_task(1)])
        with mock.patch.object(mrq, "atomic_write_text", side_effect=PermissionError("read only")):
            with self.assertRaises(mrq.SessionLogError) as raised:
                journal.checkpoint()
        self.assertIn(str(self.primary), str(raised.exception))
        self.assertIn(str(self.fallback), str(raised.exception))

        path = journal.checkpoint()
        self.assertTrue(os.path.exists(path))
        self.assertEqual(journal.last_error, "")

    def test_partial_checkpoint_survives_new_session_and_stale_events(self):
        first_task = make_task(1)
        first = self.journal(1, [first_task])
        first.record_event(mrq.TaskRuntimeEvent(
            mrq.TaskRuntimeEventType.TASK_FAILED,
            task=first_task,
            session_id=1,
            status="Failed to start",
            end=time.time(),
        ))
        first_path = first.checkpoint()
        before = pathlib.Path(first_path).read_text(encoding="utf-8")

        second_task = make_task(2)
        second = self.journal(2, [second_task])
        self.assertFalse(second.record_event(mrq.TaskRuntimeEvent(
            mrq.TaskRuntimeEventType.TASK_FINISHED,
            task=second_task,
            session_id=1,
            status="Done (00:00:01)",
        )))
        second_path = second.checkpoint(final=True)

        self.assertEqual(pathlib.Path(first_path).read_text(encoding="utf-8"), before)
        self.assertNotEqual(first_path, second_path)


class QueueLogCompatibilityTests(unittest.TestCase):
    class Harness:
        _safe_int = extract_nested_class_method("QtQueueLogViewer", "_safe_int")
        _parse_queue_log = extract_nested_class_method("QtQueueLogViewer", "_parse_queue_log")

    def test_legacy_queue_log_remains_readable(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = pathlib.Path(tempdir) / "Queue_Log_legacy.log"
            path.write_text(
                "Level / Sequence / Preset / Start / End / Duration\n"
                "Map / Shot / Preset / 10:00:00 / 10:00:05 / 00:00:05\n",
                encoding="utf-8",
            )
            rows, headers = self.Harness()._parse_queue_log(str(path))

        self.assertEqual(headers, ["Level", "Sequence", "Preset", "Start", "End", "Duration"])
        self.assertEqual(rows[0]["Order"], "1")
        self.assertEqual(rows[0]["Status"], "Done")
        self.assertEqual(rows[0]["Running Time"], "00:00:05")


class FakeProcess:
    def __init__(self, return_code: int, stdout_lines=(), stdout_delay=0.0):
        self.return_code = return_code
        self.stdout = self._stdout(stdout_lines, stdout_delay)

    @staticmethod
    def _stdout(lines, delay):
        for line in lines:
            if delay:
                time.sleep(delay)
            yield line

    def poll(self):
        return self.return_code

    def wait(self):
        return self.return_code


class FakeProcessController:
    def __init__(self, return_codes, on_launch=None, stdout_lines=(), stdout_delay=0.0):
        self.return_codes = iter(return_codes)
        self.on_launch = on_launch
        self.stdout_lines = tuple(stdout_lines)
        self.stdout_delay = stdout_delay
        self.launches = []

    def launch(self, cmd):
        self.launches.append(cmd[0])
        if self.on_launch:
            self.on_launch(len(self.launches))
        result = next(self.return_codes)
        if isinstance(result, BaseException):
            raise result
        return FakeProcess(result, self.stdout_lines, self.stdout_delay)

    def clear_if_current(self, process):
        pass

    def is_active(self):
        return False

    def stop_current(self, timeout_s):
        return False


class WorkerHarness:
    _worker_loop = extract_qt_shell_method("_worker_loop")
    _worker_loop_body = extract_qt_shell_method("_worker_loop_body")

    def __init__(
        self,
        tasks,
        return_codes,
        retries=0,
        policy="retry_then_next",
        on_launch=None,
        stdout_lines=(),
        stdout_delay=0.0,
    ):
        self.settings = mrq.AppSettings(tasks=tasks, retries=retries, fail_policy=policy)
        self.state = [mrq.default_task_state() for _ in tasks]
        self.ui_events = queue.Queue()
        self.logs = []
        self.worker_gate = mrq.RuntimeWorkerGate()
        self.stop_all = False
        self.cancel_current_requested = False
        self._runtime_session_id = 1
        self.process_controller = FakeProcessController(
            return_codes,
            on_launch=on_launch,
            stdout_lines=stdout_lines,
            stdout_delay=stdout_delay,
        )
        self.tempdir = tempfile.TemporaryDirectory()
        self._session_journals = {
            1: mrq.RenderSessionJournal(
                1,
                tasks,
                primary_dir=str(pathlib.Path(self.tempdir.name) / "logs"),
                fallback_dir=str(pathlib.Path(self.tempdir.name) / "fallback"),
            )
        }
        self.runtime_queue = mrq.RuntimeQueueCoordinator(
            self._find_task_index_by_identity,
            self._set_status_from_core,
            self._append_log,
        )

    def close(self):
        self.tempdir.cleanup()

    def _find_task_index_by_identity(self, task):
        return mrq.find_task_index_by_identity(self.settings.tasks, task)

    def _set_status_from_core(self, task, status):
        self._put_runtime_event("status", task, status=status)

    def _append_log(self, message):
        self.logs.append(message)

    def _build_command_for_task(self, task):
        return [task.sequence]

    def _task_logfile(self, task, session_id=None, attempt=1):
        return self._session_journals[session_id or self._runtime_session_id].task_log_path(task, attempt)

    def _extract_progress(self, line):
        return None

    def _put_runtime_event(
        self,
        event_type,
        task=None,
        session_id=None,
        status=None,
        progress=None,
        start=None,
        end=None,
    ):
        event = mrq.TaskRuntimeEvent(
            event_type=event_type,
            status=status,
            progress=progress,
            start=start,
            end=end,
            task=task,
            session_id=self._runtime_session_id if session_id is None else session_id,
        )
        journal = self._session_journals.get(event.session_id)
        terminal = journal.record_event(event) if journal else False
        self.ui_events.put(event)
        if terminal:
            self._checkpoint_session(event.session_id)

    def _checkpoint_session(self, session_id, final=False):
        journal = self._session_journals.get(session_id)
        return journal.checkpoint(final=final) if journal else None

    def run(self):
        self.runtime_queue.enqueue_tasks(self.settings.tasks)
        self.worker_gate.try_reserve(True)
        self._worker_loop()
        deadline = time.monotonic() + 5
        while self.worker_gate.running and time.monotonic() < deadline:
            time.sleep(0.01)
        if self.worker_gate.running:
            raise AssertionError("worker did not finish")
        while not self.ui_events.empty():
            mrq.apply_task_runtime_event(
                self.settings.tasks,
                self.state,
                self.ui_events.get_nowait(),
                self._runtime_session_id,
            )


class WorkerPolicyTests(unittest.TestCase):
    def test_task_log_path_error_does_not_strand_worker_or_skip_pending(self):
        tasks = [make_task(i) for i in range(2)]
        harness = WorkerHarness(tasks, [0, 0])
        self.addCleanup(harness.close)
        original_logfile = harness._task_logfile
        calls = 0

        def fail_first_logfile(task, session_id=None, attempt=1):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("controlled logfile failure")
            return original_logfile(task, session_id=session_id, attempt=attempt)

        harness._task_logfile = fail_first_logfile
        harness.run()

        self.assertTrue(harness.state[0]["status"].startswith(mrq.TaskRuntimeStatus.DONE))
        self.assertTrue(harness.state[1]["status"].startswith(mrq.TaskRuntimeStatus.DONE))
        self.assertFalse(harness.runtime_queue.has_work())
        self.assertIn("Task log path unavailable", harness._session_journals[1].rows()[0]["attempts"][0]["io_error"])

    def test_retry_runs_same_task_then_succeeds(self):
        task = make_task(1)
        harness = WorkerHarness([task], [1, 0], retries=1)
        self.addCleanup(harness.close)
        harness.run()

        self.assertEqual(harness.process_controller.launches, [task.sequence, task.sequence])
        self.assertTrue(harness.state[0]["status"].startswith(mrq.TaskRuntimeStatus.DONE))
        attempts = harness._session_journals[1].rows()[0]["attempts"]
        self.assertEqual([entry["exit_code"] for entry in attempts], [1, 0])
        self.assertNotEqual(attempts[0]["log_path"], attempts[1]["log_path"])
        summary = pathlib.Path(harness._session_journals[1].summary_path).read_text(encoding="utf-8")
        self.assertIn("A001", summary)
        self.assertIn("A002", summary)

    def test_launch_failure_is_terminal_and_does_not_block_next_task(self):
        tasks = [make_task(1), make_task(2)]
        harness = WorkerHarness(tasks, [OSError("controlled launch failure"), 0])
        self.addCleanup(harness.close)
        harness.run()

        self.assertEqual(harness.state[0]["status"], "Failed to start")
        self.assertTrue(harness.state[1]["status"].startswith(mrq.TaskRuntimeStatus.DONE))
        first_attempt = harness._session_journals[1].rows()[0]["attempts"][0]
        self.assertIn("controlled launch failure", first_attempt["error"])

    def test_worker_exception_is_recovered_and_finalized(self):
        tasks = [make_task(1), make_task(2)]
        harness = WorkerHarness(tasks, [0])
        self.addCleanup(harness.close)
        calls = 0

        def fail_first_build(task):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("controlled worker failure")
            return [task.sequence]

        harness._build_command_for_task = fail_first_build
        harness.run()

        self.assertEqual(harness.state[0]["status"], "Failed (worker error)")
        self.assertTrue(harness.state[1]["status"].startswith(mrq.TaskRuntimeStatus.DONE))
        self.assertFalse(harness.worker_gate.running)
        self.assertTrue(harness._session_journals[1].finalized)

    def test_worker_waits_for_slow_stdout_tail_before_closing_task_log(self):
        task = make_task(1)
        harness = WorkerHarness(
            [task],
            [0],
            stdout_lines=("first line\n", "final buffered line\n"),
            stdout_delay=0.15,
        )
        self.addCleanup(harness.close)
        started = time.monotonic()
        harness.run()

        attempt = harness._session_journals[1].rows()[0]["attempts"][0]
        text = pathlib.Path(attempt["log_path"]).read_text(encoding="utf-8")
        self.assertGreaterEqual(time.monotonic() - started, 0.25)
        self.assertIn("final buffered line", text)
        self.assertLess(text.index("final buffered line"), text.index("END:"))

    def test_skip_next_skips_exactly_one_pending_task(self):
        tasks = [make_task(i) for i in range(3)]
        harness = WorkerHarness(tasks, [1, 0], policy="skip_next")
        self.addCleanup(harness.close)
        harness.run()

        self.assertEqual(harness.process_controller.launches, [tasks[0].sequence, tasks[2].sequence])
        self.assertTrue(harness.state[0]["status"].startswith(mrq.TaskRuntimeStatus.FAILED))
        self.assertEqual(harness.state[1]["status"], mrq.TaskRuntimeStatus.SKIPPED_POLICY)
        self.assertTrue(harness.state[2]["status"].startswith(mrq.TaskRuntimeStatus.DONE))

    def test_stop_current_continues_remaining_queue(self):
        tasks = [make_task(i) for i in range(2)]
        harness = None

        def cancel_first(launch_count):
            if launch_count == 1:
                harness.cancel_current_requested = True

        harness = WorkerHarness(tasks, [1, 0], on_launch=cancel_first)
        self.addCleanup(harness.close)
        harness.run()

        self.assertEqual(harness.state[0]["status"], mrq.TaskRuntimeStatus.CANCELLED)
        self.assertTrue(harness.state[1]["status"].startswith(mrq.TaskRuntimeStatus.DONE))

    def test_stop_all_cancels_active_and_every_pending_task(self):
        tasks = [make_task(i) for i in range(3)]
        harness = None

        def stop_on_first_launch(launch_count):
            if launch_count == 1:
                harness.cancel_current_requested = True
                harness.stop_all = True

        harness = WorkerHarness(tasks, [1], on_launch=stop_on_first_launch)
        self.addCleanup(harness.close)
        harness.run()

        self.assertEqual(harness.process_controller.launches, [tasks[0].sequence])
        self.assertEqual(harness.state[0]["status"], mrq.TaskRuntimeStatus.CANCELLED)
        self.assertEqual(harness.state[1]["status"], mrq.TaskRuntimeStatus.CANCELLED_QUEUE)
        self.assertEqual(harness.state[2]["status"], mrq.TaskRuntimeStatus.CANCELLED_QUEUE)


if __name__ == "__main__":
    unittest.main()
