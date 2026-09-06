import ast
import importlib.util
import pathlib
import queue
import tempfile
import threading
import time
import unittest


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


class FakeProcess:
    def __init__(self, return_code: int):
        self.return_code = return_code
        self.stdout = iter(())

    def poll(self):
        return self.return_code

    def wait(self):
        return self.return_code


class FakeProcessController:
    def __init__(self, return_codes, on_launch=None):
        self.return_codes = iter(return_codes)
        self.on_launch = on_launch
        self.launches = []

    def launch(self, cmd):
        self.launches.append(cmd[0])
        if self.on_launch:
            self.on_launch(len(self.launches))
        return FakeProcess(next(self.return_codes))

    def clear_if_current(self, process):
        pass

    def is_active(self):
        return False

    def stop_current(self, timeout_s):
        return False


class WorkerHarness:
    _worker_loop = extract_qt_shell_method("_worker_loop")
    _worker_loop_body = extract_qt_shell_method("_worker_loop_body")

    def __init__(self, tasks, return_codes, retries=0, policy="retry_then_next", on_launch=None):
        self.settings = mrq.AppSettings(tasks=tasks, retries=retries, fail_policy=policy)
        self.state = [mrq.default_task_state() for _ in tasks]
        self.ui_events = queue.Queue()
        self.logs = []
        self.worker_gate = mrq.RuntimeWorkerGate()
        self.stop_all = False
        self.cancel_current_requested = False
        self._runtime_session_id = 1
        self.process_controller = FakeProcessController(return_codes, on_launch=on_launch)
        self.tempdir = tempfile.TemporaryDirectory()
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

    def _task_logfile(self, task):
        return str(pathlib.Path(self.tempdir.name) / f"{task.sequence}.log")

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
        self.ui_events.put(mrq.TaskRuntimeEvent(
            event_type=event_type,
            status=status,
            progress=progress,
            start=start,
            end=end,
            task=task,
            session_id=self._runtime_session_id if session_id is None else session_id,
        ))

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
    def test_worker_error_releases_active_task_and_continues_pending(self):
        tasks = [make_task(i) for i in range(2)]
        harness = WorkerHarness(tasks, [0])
        self.addCleanup(harness.close)
        original_logfile = harness._task_logfile
        calls = 0

        def fail_first_logfile(task):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("controlled logfile failure")
            return original_logfile(task)

        harness._task_logfile = fail_first_logfile
        harness.run()

        self.assertEqual(harness.state[0]["status"], "Failed (worker error)")
        self.assertTrue(harness.state[1]["status"].startswith(mrq.TaskRuntimeStatus.DONE))
        self.assertFalse(harness.runtime_queue.has_work())

    def test_retry_runs_same_task_then_succeeds(self):
        task = make_task(1)
        harness = WorkerHarness([task], [1, 0], retries=1)
        self.addCleanup(harness.close)
        harness.run()

        self.assertEqual(harness.process_controller.launches, [task.sequence, task.sequence])
        self.assertTrue(harness.state[0]["status"].startswith(mrq.TaskRuntimeStatus.DONE))

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
