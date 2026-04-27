# MRQ Launcher Baseline Behavior

## Scope
This document captures the current runtime and UI behavior of the MRQ Launcher before architecture hardening and Qt migration work. It is a baseline snapshot for parity validation, not a redesign proposal.

## Source of truth
This baseline is derived from the current implementation in `code/mrq_launcher.py` (app version `1.6.9`) and reflects behavior as currently implemented, including implementation-coupled details, rather than desired future behavior.

## Core runtime model
- **Task list**: `self.settings.tasks` stores `RenderTask` objects (uproject, level, sequence, preset, output_dir, notes, added_at, enabled).
- **State list**: `self.state` is a parallel list keyed by task index and stores runtime/UI state (`status`, `progress`, `start`, `end`).
- **Runtime queue**: `self.runtime_q` is a `queue.Queue[RenderTask]` holding pending task objects to process.
- **Worker loop**: `_run_queue()` starts one background worker thread (if not already running). The worker consumes from `runtime_q`, launches Unreal processes, updates state via UI queue, and applies retry/failure policy.
- **Current process tracking**: `self.current_process` stores the active `subprocess.Popen` instance. `self._current_global_idx` tracks the currently running task index for UI/status mapping.
- **Stop-current vs stop-all**:
  - Stop current uses `cancel_current_requested` + terminate/kill of current process.
  - Stop all sets `stop_all`, invokes stop current, and clears pending runtime queue entries.
- **UI ownership of runtime logic**: Queue construction, status transitions, duplication checks, task edit/delete interaction with pending queue, and failure policy are implemented in the Tkinter app class (not a separate runtime service).

## User-visible workflows

### Render Selected
- **Trigger**: `Render Selected` button/menu, bound to `run_selected()`.
- **Task source**: Current tree selection (`_collect(only_selected=True)`), preserving selected order from tree selection IDs.
- **If nothing selected**: Shows info dialog: "Select at least one task in the table." and returns.
- **If render already active**: Does not start another worker/process; enqueues selected tasks via `_enqueue_tasks()`.
- **Status changes**:
  - On enqueue: selected complete/non-duplicate tasks are set to `Queued`.
  - On execution: `Rendering HH:MM:SS` then `Done (...)`, `Failed (rc=...)`, `Cancelled`, or `Skipped (policy)` depending on outcome.
- **Queue interaction**: Uses shared `runtime_q`; duplicate/incomplete filtering is applied during enqueue.

### Render Enabled
- **Trigger**: `Render Enabled` button/menu, bound to `run_enabled()`.
- **Task source**: All enabled tasks (`_collect(only_enabled=True)`).
- **If nothing enabled**: Shows info dialog: "No enabled tasks to run." and returns.
- **If render already active**: Enqueues enabled tasks via `_enqueue_tasks()` (no parallel second process).
- **Status changes**: Same enqueue/run transitions as Render Selected.
- **Queue interaction**: Appends eligible tasks to shared runtime queue with duplicate/incomplete filtering.

### Render All
- **Trigger**: `Render All` button/menu, bound to `run_all()`.
- **Task source**: All tasks (`_collect()`), regardless of enabled flag at collection step.
- **If no tasks**:
  - `_run_queue()` shows "No tasks to run" only when both provided task list is empty and runtime queue is empty and worker is not running.
- **If render already active**: Enqueues all provided tasks via `_enqueue_tasks()`.
- **Status changes**: Same enqueue/run transitions as above; disabled tasks may still be skipped as incomplete only if required fields missing, but enabled flag does not block enqueue in `run_all()` itself.
- **Queue interaction**: Shared queue; duplicate/incomplete checks still apply.

### Add Task(s) to Queue
- **Trigger**: Menu `Add Task(s) to Queue` and `Queue Selected` button, bound to `enqueue_selected_or_enabled()`.
- **Selected vs enabled fallback**:
  - If selection exists: enqueue selected tasks.
  - If no selection: enqueue all enabled tasks.
- **If worker idle**: Enqueues tasks, then calls `_run_queue([])` to start worker on already-populated queue.
- **If worker already active**: Only enqueues; does not spawn another worker.
- **Queue status updates**: Successfully queued tasks become `Queued`; duplicates/incomplete tasks are logged as skipped.

### Stop Current Render
- **Trigger**: `Stop Current Render` (full UI), `Stop Current` (minimal UI), menu item, bound to `cancel_current()`.
- **Graceful terminate**: Sets `cancel_current_requested = True`, calls `current_process.terminate()`, logs request.
- **Kill-after-timeout fallback**: Waits up to `kill_timeout_s`; if still alive, calls `kill()` and logs kill-after-timeout.
- **Impact on current task only**: Current process is stopped; worker marks that task `Cancelled` when it detects cancellation.
- **Impact on rest of queue**: Queue continues unless `stop_all` is also set; cancelled current task does not automatically cancel pending tasks.

### Stop All
- **Trigger**: `Stop All` (buttons) / `Cancel All` (menu label), bound to `cancel_all()`.
- **Stop flag behavior**: Sets `stop_all = True`.
- **Relationship to Stop Current**: Calls `cancel_current()` as part of stop-all.
- **Clearing pending runtime queue items**: Calls `_clear_pending_runtime_queue("Cancelled (queue)")` immediately; worker also clears pending items on exit path when `stop_all` is set.
- **Resulting task statuses**:
  - Running task typically becomes `Cancelled` (if cancellation flag path is reached).
  - Pending queued tasks are set to `Cancelled (queue)`.

## Queue and identity rules
- **Identity model**: Queue deduplication and queued/running detection are based on **object identity** (`id(task)`), not dataclass value equality.
- **Duplicate protection**:
  - `_task_identity_set_from_runtime_queue()` captures IDs of currently running task and all currently queued items.
  - `_enqueue_tasks()` skips tasks whose object ID is already in that set.
- **Queued/running recognition**:
  - Running task is inferred from `_current_global_idx` and task object at that index.
  - Queued tasks are inferred by draining/rebuilding `runtime_q` temporarily and collecting object IDs.
- **Edit interaction with queued items**:
  - Editing selected task removes pending queue entries for the old object via `_remove_tasks_from_runtime_queue([old_task])`, then replaces task object in list.
- **Delete interaction with queued items**:
  - Removing task(s) also removes matching queued objects from runtime queue before deleting table/state rows.
- **Disable interaction with queued items**:
  - Toggling tasks to disabled resets their state and removes matching queued objects.
  - `set_enabled_all(False)` removes queued entries for all tasks.
- **Value-equal duplicates**:
  - Distinct task objects with identical field values are treated as different queue entries unless they share object identity.

## Status model as currently implemented
- **Raw runtime state strings** (stored in `self.state[idx]["status"]`) include at least:
  - `Ready`
  - `Queued`
  - `Rendering HH:MM:SS`
  - `Done` / `Done (HH:MM:SS)`
  - `Failed (rc=...)`
  - `Cancelled`
  - `Cancelled (queue)`
  - `Skipped (policy)`
- **Presentation mapping**:
  - `get_status_display(raw, enabled)` maps disabled tasks to `Disabled` regardless of raw status.
  - Raw statuses beginning with `Cancelled` are displayed as `Failed` in the status pill/text mapping.
  - `Skipped...` maps to `Skipped`; `Rendering...` maps to `Rendering`; exact `Queued` maps to `Queued`; otherwise default is `Ready`.
- **Status kinds for color/theme**: derived by `get_status_kind()`, similarly mapping `Cancelled` + `Failed` into `failed` styling.
- **Progress coupling** (`_drain_queues`):
  - `Done*` sets progress 100.
  - `Queued`, `Ready`, `Cancelled`, `Cancelled (queue)` reset progress to 0.

## Minimal Mode
- **Hidden UI regions**:
  - Main header panel, bottom panel (inspector/preview/log region), status bar.
  - Queue section header, queue toolbar, queue hint frame, queue stats frame, horizontal scrollbar.
  - App menu replaced by empty menu.
- **Shown in Minimal Mode**:
  - Minimal header/footer widgets and main task tree remain.
- **Columns visible**: `status`, `level`, `sequence`, `preset`, `runtime`.
- **Disabled-task filtering**:
  - `refresh_tree()` omits disabled tasks when `minimal_mode` is true.
- **Progress/session info shown**:
  - Minimal footer includes current task/status/progress/session values maintained by shared status variables.
- **Layout/geometry behavior**:
  - On enter: stores prior geometry, applies minimal minsize, refreshes tree, computes compact geometry based on visible rows/content.
  - On exit: restores full layout and previous stored geometry when available.
  - Includes fallback restore logging paths if layout switch fails.

## Persistence behavior
- **Queue JSON (`save_to_json` / `load_from_json`)**:
  - Saves runtime options (`ue_cmd`, retries, fail policy, kill timeout, render/window settings, auto minimal, extra CLI) plus full task list (`asdict`).
  - Load updates UI vars and settings from file with defaults from current settings when fields are missing.
  - Task defaults on load when absent: `enabled=True`, `notes=""`, `added_at=current_task_timestamp()`.
  - Loading queue JSON rebuilds `self.state` to default state entries for all tasks.
- **Single task JSON (`save_selected_tasks_dialog`, `load_tasks_dialog`)**:
  - Save one or multiple selected tasks as task JSON file(s).
  - Load supports both a single-task dict and a dict containing `tasks` array.
  - Task import requires required keys: `uproject`, `level`, `sequence`, `preset`.
  - Imported tasks also apply same missing-field defaults (`enabled`, `notes`, `added_at`).

## Logging behavior
- **Per-task log files**:
  - Created under `mrq_logs/` with timestamped filename + `level__sequence__preset` suffix.
  - Each task log records `CMD`, `START`, streamed process output, `END`, and `EXIT`.
- **Queue summary log**:
  - `save_queue_log()` writes `mrq_logs/Queue_Log_<timestamp>.log` with rows:
    `Level / Sequence / Preset / Start / End / Duration`.
  - Uses per-task state start/end timestamps to compute duration.
- **Live UI log**:
  - `_log()` enqueues lines to `log_queue`; `_drain_queues()` appends to Tk text widget.
  - Worker and queue operations emit operational messages to this stream.
- **Open last log**:
  - `open_last_log_for_selected()` finds latest `.log` matching selected task basename and opens via OS default app.

## Command preview behavior
- Preview text is generated by `_build_command_preview_for_task(task)`.
- Inputs affecting preview:
  - UE command path (`var_ue`)
  - task fields: `uproject`, `level`, `sequence`, `preset`, `output_dir`
  - render options: windowed/fullscreen, `ResX`, `ResY`, `-notexturestreaming`, extra CLI (`shlex.split`)
- If no selected task, preview shows instructional placeholder text.
- If no selection but a task is currently running (`_current_global_idx`), preview uses that task.

## Failure policy behavior
- `retry_then_next`:
  - Retries failed process up to configured `retries`; if exhausted, marks task failed and continues to next queue item.
- `skip_next`:
  - After retries exhausted on a task, marks it failed and sets `skip_next_pending = 1`.
  - Next dequeued task is marked `Skipped (policy)` and not executed.
- `stop_queue`:
  - On failure (non-zero rc), marks current task failed, sets `stop_all = True`, and exits queue loop.
  - Pending tasks are cleared with `Cancelled (queue)` on stop-all handling.

## Edge cases
Checklist of currently implemented behavior:
- [x] **No selected task**: Render Selected shows info dialog and does not enqueue/run.
- [x] **No enabled tasks**: Render Enabled shows info dialog and does not enqueue/run.
- [x] **Incomplete task**: Enqueue path skips incomplete tasks and logs skip count.
- [x] **Enqueue while rendering**: Render actions enqueue only; no second worker/process is started.
- [x] **Duplicate task already queued**: Duplicate (same object identity) is skipped and logged.
- [x] **Delete queued task**: Delete removes pending queue entries for those task objects before deleting rows.
- [x] **Edit queued task**: Edit removes pending old object from queue, then replaces with edited object.
- [x] **Disable queued task**: Disabling removes pending queue entries for those tasks.
- [x] **Cancel current render**: Current process terminate/kill path sets cancellation flag; task becomes Cancelled.
- [x] **Stop all during queue execution**: stop flag set, current cancelled, pending queue cleared to Cancelled (queue).
- [x] **Minimal Mode with disabled tasks**: Disabled tasks are filtered out of tree.
- [x] **Queue empty while worker idle**: `_run_queue([])` with empty runtime queue shows "No tasks to run" and returns.

## Known ambiguities / implementation-coupled behavior
- `run_all()` does not filter by `enabled`; it passes all tasks to enqueue logic. Disabled tasks can still be considered, subject to completeness/duplicate checks.
- Status display maps raw `Cancelled*` to visible `Failed`, while some internal logic still uses `Cancelled` text directly.
- Identity-based queue semantics rely on object references surviving list edits; replacing task objects changes dedupe behavior.
- Runtime queue inspection for dedupe temporarily drains and requeues pending items; behavior depends on queue contents being object references and this procedure remaining single-thread-safe in current model.
- `_run_queue([])` doubles as "start worker for existing queued items" and "show no tasks dialog" entrypoint, coupling UI action flow to runtime queue state.
- Worker loop uses UI variables directly (e.g., retries/policy/options captured at queue start; command options evaluated per attempt), coupling runtime behavior to current Tk variable model.

## Acceptance checklist for future parity
- [ ] Render Selected behavior preserved (selection required, active-render enqueue fallback, status transitions).
- [ ] Render Enabled behavior preserved (enabled-only source, empty-enabled dialog behavior, active-render enqueue fallback).
- [ ] Render All behavior preserved (all-task source and active-render enqueue fallback).
- [ ] Enqueue while rendering preserved (single worker/process model).
- [ ] Duplicate protection preserved (queued/running exclusion semantics).
- [ ] Stop Current preserved (terminate + kill-timeout fallback and queue-continuation behavior).
- [ ] Stop All preserved (stop flag, stop-current chaining, pending queue clear/status effects).
- [ ] Failure policies preserved (`retry_then_next`, `skip_next`, `stop_queue`).
- [ ] Edit/delete queued task behavior preserved (pending queue removal semantics).
- [ ] Save/load preserved (queue JSON + task JSON + missing-field defaults).
- [ ] Command preview preserved (inputs and formatting behavior).
- [ ] Queue log export preserved (`Queue_Log_*.log` schema and source timestamps).
- [ ] Minimal Mode disabled-task filtering preserved.
