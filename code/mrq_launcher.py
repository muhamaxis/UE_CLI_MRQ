import os
import subprocess
import json
import queue
import threading
import time
import sys
import shlex
from dataclasses import dataclass, asdict, field
from typing import List, Optional, Any
from datetime import datetime, timedelta

# -------------------------------------------------
# App meta
# -------------------------------------------------

APP_VERSION = "1.10.26"

UI_THEME = {
    "bg": "#111318",
    "panel": "#171B22",
    "panel_alt": "#1D232C",
    "panel_soft": "#202733",
    "border": "#2A3340",
    "text": "#E7ECF3",
    "muted": "#9CA8B7",
    "accent": "#4EA1FF",
    "accent_hover": "#6BB2FF",
    "accent_soft": "#223A56",
    "success": "#2D6A4F",
    "warning": "#8A6A2F",
    "danger": "#8B3A46",
    "danger_hover": "#A64957",
    "panel_soft_hover": "#2B3442",
    "entry": "#10141A",
}

STATUS_PILL_THEME = {
    "ready": {"bg": "#173A28", "text": "#8FE6B0", "border": "#24573A"},
    "queued": {"bg": "#3A3116", "text": "#F0C85B", "border": "#655523"},
    "rendering": {"bg": "#1E315B", "text": "#A8C9FF", "border": "#35518E"},
    "done": {"bg": "#2A3444", "text": "#E7ECF3", "border": "#3A4557"},
    "failed": {"bg": "#47232A", "text": "#FF9AA9", "border": "#6F313D"},
    "disabled": {"bg": "#2A313B", "text": "#C9D2DD", "border": "#404B59"},
    "skipped": {"bg": "#3A2A4B", "text": "#D7C7FF", "border": "#5C4777"},
}

APP_ICON_RELATIVE_PATH = "resources/app_icon.ico"
APP_HEADER_LOGO_RELATIVE_PATH = "resources/mrq_launcher_logo_167.png"
# -------------------------------------------------
# Helpers
# -------------------------------------------------

def resource_path(relative_path: str) -> str:
    """Return an absolute resource path for source runs and PyInstaller builds."""
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, relative_path)
    app_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    return os.path.join(app_root, relative_path)


def app_icon_path() -> str:
    """Return the preferred application icon path."""
    return resource_path(APP_ICON_RELATIVE_PATH)


def app_header_logo_path() -> str:
    """Return the header logo path used by full-size launcher views."""
    return resource_path(APP_HEADER_LOGO_RELATIVE_PATH)


def user_settings_path() -> str:
    """Return a writable per-user settings path for launcher workflow preferences."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        folder = os.path.join(base, "MRQLauncher")
    elif sys.platform == "darwin":
        folder = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "MRQLauncher")
    else:
        folder = os.path.join(os.path.expanduser("~"), ".config", "MRQLauncher")
    return os.path.join(folder, "user_settings.json")


def detect_default_unreal_cmd() -> str:
    candidates = [
        # Use forward slashes to avoid escaping issues in Python strings
        "C:/Program Files/Epic Games/UE_5.6/Engine/Binaries/Win64/UnrealEditor-Cmd.exe",
        "C:/Program Files/Epic Games/UE_5.5/Engine/Binaries/Win64/UnrealEditor-Cmd.exe",
        "C:/Program Files/Epic Games/UE_5.4/Engine/Binaries/Win64/UnrealEditor-Cmd.exe",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return ""


def fs_to_soft_object(uasset_path: str) -> str:
    """Path to .uasset/.umap inside Content folder → SoftObjectPath.
    Expected: .../<Project>/Content/<Rel>/<Asset>.uasset
    Result: /Game/<Rel>/<Asset>.<Asset>
    """
    norm = os.path.normpath(uasset_path)
    if not norm.lower().endswith((".uasset", ".umap")):
        raise ValueError("Select a .uasset/.umap file")
    parts = norm.split(os.sep)
    if "Content" not in parts:
        raise ValueError("Path must contain the project's 'Content' folder.")
    idx = len(parts) - 1 - parts[::-1].index("Content")
    rel_parts = parts[idx + 1:]
    asset_name = os.path.splitext(rel_parts[-1])[0]
    rel_dir = rel_parts[:-1]
    game_path = "/Game"
    if rel_dir:
        # Build the path using forward slashes
        game_path += "/" + "/".join(rel_dir)
    return f"{game_path}/{asset_name}.{asset_name}"


def soft_name(soft_path: str) -> str:
    if not soft_path:
        return "?"
    return soft_path.split(".")[-1]



def soft_object_to_editor_path(soft_path: str) -> str:
    """Return a shorter asset path for editor fields without changing stored data."""
    value = (soft_path or "").strip()
    if not value:
        return value

    object_path, separator, asset_name = value.rpartition(".")
    if not separator or "/" not in object_path:
        return value

    path_asset_name = object_path.rsplit("/", 1)[-1]
    if asset_name == path_asset_name:
        return object_path
    return value


def editor_path_to_soft_object(editor_path: str) -> str:
    """Convert a shortened /Game asset path back to a SoftObjectPath."""
    value = (editor_path or "").strip()
    if not value.startswith("/Game/"):
        return value

    leaf = value.rsplit("/", 1)[-1]
    if not leaf or "." in leaf:
        return value
    return f"{value}.{leaf}"


def _soft_path_object_part(soft_path: str) -> str:
    """Return the package/object path part used for local file validation."""
    value = (soft_path or "").strip().replace("\\", "/")
    if not value:
        return value
    object_path, separator, asset_name = value.rpartition(".")
    if separator and object_path.startswith("/") and asset_name:
        return object_path
    return value


def soft_path_to_local_asset_candidates(project_path: str, soft_path: str, extensions: tuple[str, ...]) -> tuple[list[str], Optional[str]]:
    """Build local Content candidates for /Game paths without touching JSON data."""
    object_path = _soft_path_object_part(soft_path)
    if not object_path.startswith("/Game/"):
        return [], object_path.split("/", 2)[1] if object_path.startswith("/") and "/" in object_path[1:] else object_path

    project_root = os.path.dirname(os.path.abspath(project_path))
    relative = object_path[len("/Game/"):].strip("/")
    if not relative:
        return [], None
    base_path = os.path.join(project_root, "Content", *relative.split("/"))
    return [base_path + ext for ext in extensions], None


@dataclass
class TaskValidationResult:
    """Transient task validation state; never persisted to queue JSON."""

    status: str
    message: str = ""
    details: List[str] = field(default_factory=list)

    @property
    def is_blocking(self) -> bool:
        return self.status in ("Incomplete", "Invalid")

    @property
    def display_text(self) -> str:
        parts = [f"Validation: {self.status}"]
        if self.message:
            parts.append(self.message)
        if self.details:
            parts.extend(self.details[:4])
        return "\n".join(parts)


def basic_task_validation(task) -> TaskValidationResult:
    """Validate required task fields only; no filesystem checks."""
    missing = []
    if not getattr(task, "uproject", ""):
        missing.append("Project")
    if not getattr(task, "level", ""):
        missing.append("Level")
    if not getattr(task, "sequence", ""):
        missing.append("Sequence")
    if not getattr(task, "preset", ""):
        missing.append("Preset")
    if missing:
        return TaskValidationResult("Incomplete", "Missing required field(s): " + ", ".join(missing))
    return TaskValidationResult("Not checked", "Path validation runs when a queue JSON is loaded.")


def validate_task_paths(task) -> TaskValidationResult:
    """Validate a loaded queue task against local files without changing JSON structure."""
    basic = basic_task_validation(task)
    if basic.status == "Incomplete":
        return basic

    project_path = os.path.abspath(os.path.normpath(task.uproject))
    errors = []
    warnings = []

    if not project_path.lower().endswith(".uproject"):
        errors.append(f"Project is not a .uproject file: {task.uproject}")
    elif not os.path.isfile(project_path):
        errors.append(f"Project file not found: {task.uproject}")

    if not errors:
        checks = (
            ("Level", task.level, (".umap",)),
            ("Sequence", task.sequence, (".uasset",)),
            ("Preset", task.preset, (".uasset",)),
        )
        for label, soft_path, extensions in checks:
            candidates, mount = soft_path_to_local_asset_candidates(project_path, soft_path, extensions)
            if mount is not None:
                warnings.append(f"{label}: unsupported mount point, skipped local check: {soft_path}")
                continue
            if not candidates or not any(os.path.isfile(candidate) for candidate in candidates):
                expected = candidates[0] if candidates else soft_path
                errors.append(f"{label} asset not found: {expected}")

    if errors:
        return TaskValidationResult("Invalid", "Local files are missing.", errors)
    if warnings:
        return TaskValidationResult("Unknown", "Some paths could not be checked locally.", warnings)
    return TaskValidationResult("Ready", "All local project and asset files were found.")


def summarize_validation_results(results: List[TaskValidationResult]) -> str:
    """Return a compact validation summary for log output."""
    counts = {"Ready": 0, "Invalid": 0, "Incomplete": 0, "Unknown": 0, "Not checked": 0}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    return " | ".join(f"{key}: {counts[key]}" for key in ("Ready", "Invalid", "Incomplete", "Unknown") if counts.get(key, 0))


def validation_status_color(status: str) -> str:
    """Return the shared color for queue validation indicators."""
    palette = {
        "Ready": "#35D04F",
        "Incomplete": "#F0B429",
        "Invalid": "#FF453A",
        "Unknown": "#6E7F91",
        "Not checked": "#4B5563",
    }
    return palette.get(status or "Not checked", palette["Not checked"])


def validation_status_tooltip(result: Optional[TaskValidationResult]) -> str:
    """Return a compact validation text for UI tooltips or diagnostics."""
    if result is None:
        return "Validation: -"
    return result.display_text


class TaskRuntimeStatus:
    """Canonical runtime status values shared by UI shells."""

    READY = "Ready"
    QUEUED = "Queued"
    RENDERING = "Rendering"
    DONE = "Done"
    FAILED = "Failed"
    CANCELLED = "Cancelled"
    CANCELLED_QUEUE = "Cancelled (queue)"
    SKIPPED_POLICY = "Skipped (policy)"


class TaskRuntimeEventType:
    """Canonical task/queue event names emitted by runtime services."""

    TASK_QUEUED = "task_queued"
    TASK_STARTED = "task_started"
    PROGRESS_UPDATED = "progress_updated"
    TASK_FINISHED = "task_finished"
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"
    QUEUE_CLEARED = "queue_cleared"
    QUEUE_COMPLETED = "queue_completed"


@dataclass
class TaskRuntimeEvent:
    """UI-agnostic runtime event consumed by the current Tk shell and future Qt shell."""

    event_type: str
    task_index: Optional[int] = None
    status: Optional[str] = None
    progress: Optional[int] = None
    start: Optional[float] = None
    end: Optional[float] = None
    payload: Any = None


def default_task_state() -> dict:
    return {"status": TaskRuntimeStatus.READY, "progress": None, "start": None, "end": None}


def current_task_timestamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def format_state_time_display(value: Optional[float]) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromtimestamp(float(value))
    except Exception:
        return "—"
    return dt.strftime("%H:%M:%S")


def format_duration_hms(value: Optional[float]) -> str:
    if value is None:
        return "—"
    total_seconds = max(0, int(round(float(value))))
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def format_runtime_display(state: dict) -> str:
    start = state.get("start")
    end = state.get("end")
    if start:
        if end:
            return format_duration_hms(end - start)
        if str(state.get("status", "")).startswith("Rendering"):
            return format_duration_hms(time.time() - start)
    return "—"


def format_added_display(value: str) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value)
    except Exception:
        return value

    now = datetime.now()
    day_delta = (now.date() - dt.date()).days
    if day_delta == 0:
        return f"Today {dt:%H:%M}"
    if day_delta == 1:
        return f"Yesterday {dt:%H:%M}"
    if 1 < day_delta <= 6:
        return f"{day_delta} days ago"
    return dt.strftime("%Y-%m-%d %H:%M")


def get_status_display(status: str, enabled: bool) -> str:
    if not enabled:
        return "Disabled"
    status = (status or "Ready").strip()
    if status.startswith("Cancelled"):
        return "Failed"
    if status.startswith("Failed"):
        return "Failed"
    if status.startswith("Done"):
        return "Done"
    if status.startswith("Rendering"):
        return "Rendering"
    if status.startswith("Skipped"):
        return "Skipped"
    if status == "Queued":
        return "Queued"
    return "Ready"

def get_status_kind(status: str, enabled: bool) -> str:
    if not enabled:
        return "disabled"
    status = (status or "Ready").strip()
    if status.startswith("Cancelled") or status.startswith("Failed"):
        return "failed"
    if status.startswith("Done"):
        return "done"
    if status.startswith("Rendering"):
        return "rendering"
    if status.startswith("Skipped"):
        return "skipped"
    if status == "Queued":
        return "queued"
    return "ready"


def get_queue_log_status(status: str, enabled: bool) -> str:
    """Return a persisted queue-log status without hiding cancellation state."""
    if not enabled:
        return "Disabled"
    status = (status or TaskRuntimeStatus.READY).strip()
    if status.startswith(TaskRuntimeStatus.CANCELLED):
        return TaskRuntimeStatus.CANCELLED
    if status.startswith(TaskRuntimeStatus.FAILED):
        return TaskRuntimeStatus.FAILED
    if status.startswith(TaskRuntimeStatus.DONE):
        return TaskRuntimeStatus.DONE
    if status.startswith(TaskRuntimeStatus.RENDERING):
        return TaskRuntimeStatus.RENDERING
    if status.startswith(TaskRuntimeStatus.SKIPPED_POLICY):
        return TaskRuntimeStatus.SKIPPED_POLICY
    if status == TaskRuntimeStatus.QUEUED:
        return TaskRuntimeStatus.QUEUED
    return TaskRuntimeStatus.READY



# -------------------------------------------------
# Data
# -------------------------------------------------

@dataclass
class RenderTask:
    uproject: str = ""
    level: str = ""
    sequence: str = ""
    preset: str = ""
    output_dir: str = ""  # Optional override for output directory
    notes: str = ""
    added_at: str = field(default_factory=current_task_timestamp)
    enabled: bool = True


@dataclass
class AppSettings:
    ue_cmd: str = field(default_factory=detect_default_unreal_cmd)
    tasks: List[RenderTask] = field(default_factory=list)
    retries: int = 0  # automatic retries on non-zero exit code
    fail_policy: str = "retry_then_next"  # retry_then_next | skip_next | stop_queue
    kill_timeout_s: int = 10  # timeout for graceful cancel before kill
    windowed: bool = True
    resx: int = 1280
    resy: int = 720
    no_texture_streaming: bool = True
    extra_cli: str = ""  # free-form string for additional arguments
    auto_minimal_on_render: bool = True


class PersistenceError(Exception):
    """Raised when persisted queue or task payloads are invalid."""


class UserSettingsRepository:
    """Stores launcher workflow preferences outside render queue JSON files."""

    MAX_RECENT_QUEUES = 10
    DEFAULTS = {
        "recent_queues": [],
        "last_queue": "",
        "auto_load_last_queue": False,
    }

    @classmethod
    def load(cls) -> dict:
        path = user_settings_path()
        data = dict(cls.DEFAULTS)
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except FileNotFoundError:
            return data
        except Exception:
            return data
        if not isinstance(payload, dict):
            return data
        recent = payload.get("recent_queues", [])
        if isinstance(recent, list):
            data["recent_queues"] = cls._normalize_recent(recent)
        last_queue = payload.get("last_queue", "")
        if isinstance(last_queue, str):
            data["last_queue"] = os.path.normpath(last_queue) if last_queue else ""
        data["auto_load_last_queue"] = bool(payload.get("auto_load_last_queue", False))
        return data

    @classmethod
    def save(cls, data: dict) -> None:
        path = user_settings_path()
        payload = {
            "recent_queues": cls._normalize_recent(data.get("recent_queues", [])),
            "last_queue": str(data.get("last_queue", "") or ""),
            "auto_load_last_queue": bool(data.get("auto_load_last_queue", False)),
        }
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    @classmethod
    def register_queue(cls, data: dict, path: str) -> dict:
        normalized = os.path.normpath(path)
        recent = [p for p in cls._normalize_recent(data.get("recent_queues", [])) if os.path.normcase(p) != os.path.normcase(normalized)]
        recent.insert(0, normalized)
        data["recent_queues"] = recent[:cls.MAX_RECENT_QUEUES]
        data["last_queue"] = normalized
        return data

    @classmethod
    def clear_recent(cls, data: dict) -> dict:
        data["recent_queues"] = []
        data["last_queue"] = ""
        return data

    @classmethod
    def _normalize_recent(cls, paths: list) -> list:
        result = []
        seen = set()
        for path in paths:
            if not isinstance(path, str) or not path.strip():
                continue
            normalized = os.path.normpath(path.strip())
            key = os.path.normcase(normalized)
            if key in seen:
                continue
            seen.add(key)
            result.append(normalized)
            if len(result) >= cls.MAX_RECENT_QUEUES:
                break
        return result


class PersistenceRepository:
    """Centralized JSON persistence for queue settings and render tasks."""

    TASK_FIELDS = {field.name for field in RenderTask.__dataclass_fields__.values()}
    REQUIRED_TASK_FIELDS = ("uproject", "level", "sequence", "preset")
    QUEUE_CONFIG_FIELDS = (
        "ue_cmd",
        "retries",
        "fail_policy",
        "kill_timeout_s",
        "windowed",
        "resx",
        "resy",
        "no_texture_streaming",
        "auto_minimal_on_render",
        "extra_cli",
    )

    @classmethod
    def load_queue(cls, path: str, defaults: AppSettings) -> tuple[dict, List[RenderTask]]:
        payload = cls._read_json_object(path)
        raw_tasks = payload.get("tasks", [])
        if not isinstance(raw_tasks, list):
            raise PersistenceError("Queue file field 'tasks' must be a list.")

        config = cls._normalize_queue_config(payload, defaults)
        tasks = [cls._task_from_payload(item, require_required=False) for item in raw_tasks]
        return config, tasks

    @classmethod
    def save_queue(cls, path: str, config: dict, tasks: List[RenderTask]) -> None:
        payload = {key: config.get(key) for key in cls.QUEUE_CONFIG_FIELDS}
        payload["tasks"] = [cls.task_to_payload(task) for task in tasks]
        cls._write_json(path, payload)

    @classmethod
    def load_task_file(cls, path: str) -> List[RenderTask]:
        payload = cls._read_json_object(path)
        if cls._looks_like_task_payload(payload):
            return [cls._task_from_payload(payload, require_required=True)]
        if "tasks" in payload:
            raw_tasks = payload.get("tasks", [])
            if not isinstance(raw_tasks, list):
                raise PersistenceError("Task file field 'tasks' must be a list.")
            tasks = []
            for item in raw_tasks:
                if cls._looks_like_task_payload(item):
                    tasks.append(cls._task_from_payload(item, require_required=True))
            return tasks
        raise PersistenceError("Task file must contain a task payload or a 'tasks' array.")

    @classmethod
    def save_task(cls, path: str, task: RenderTask) -> None:
        cls._write_json(path, cls.task_to_payload(task))

    @classmethod
    def task_to_payload(cls, task: RenderTask) -> dict:
        return asdict(task)

    @classmethod
    def _read_json_object(cls, path: str) -> dict:
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as exc:
            raise PersistenceError(str(exc)) from exc
        if not isinstance(payload, dict):
            raise PersistenceError("JSON root must be an object.")
        return payload

    @classmethod
    def _write_json(cls, path: str, payload: dict) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    @classmethod
    def _normalize_queue_config(cls, payload: dict, defaults: AppSettings) -> dict:
        return {
            "ue_cmd": str(payload.get("ue_cmd", defaults.ue_cmd)),
            "retries": cls._int_or_default(payload.get("retries", defaults.retries), defaults.retries),
            "fail_policy": str(payload.get("fail_policy", defaults.fail_policy)),
            "kill_timeout_s": cls._int_or_default(payload.get("kill_timeout_s", defaults.kill_timeout_s), defaults.kill_timeout_s),
            "windowed": bool(payload.get("windowed", defaults.windowed)),
            "resx": cls._int_or_default(payload.get("resx", defaults.resx), defaults.resx),
            "resy": cls._int_or_default(payload.get("resy", defaults.resy), defaults.resy),
            "no_texture_streaming": bool(payload.get("no_texture_streaming", defaults.no_texture_streaming)),
            "auto_minimal_on_render": bool(payload.get("auto_minimal_on_render", defaults.auto_minimal_on_render)),
            "extra_cli": str(payload.get("extra_cli", defaults.extra_cli)),
        }

    @classmethod
    def _task_from_payload(cls, payload: dict, require_required: bool) -> RenderTask:
        if not isinstance(payload, dict):
            raise PersistenceError("Task payload must be an object.")
        if require_required:
            missing = [key for key in cls.REQUIRED_TASK_FIELDS if not payload.get(key)]
            if missing:
                raise PersistenceError(f"Task payload is missing required field(s): {', '.join(missing)}.")

        data = {key: payload[key] for key in cls.TASK_FIELDS if key in payload}
        data.setdefault("enabled", True)
        data.setdefault("notes", "")
        data.setdefault("added_at", current_task_timestamp())
        return RenderTask(**data)

    @classmethod
    def _looks_like_task_payload(cls, payload: object) -> bool:
        return isinstance(payload, dict) and all(key in payload for key in cls.REQUIRED_TASK_FIELDS)

    @staticmethod
    def _int_or_default(value, default: int) -> int:
        try:
            return int(value)
        except Exception:
            return int(default)


class RuntimeQueueCoordinator:
    """Owns pending render queue operations independently from Tk widgets."""

    def __init__(self, task_index_resolver, current_task_getter, status_callback, log_callback):
        self._pending: "queue.Queue[RenderTask]" = queue.Queue()
        self._task_index_resolver = task_index_resolver
        self._current_task_getter = current_task_getter
        self._status_callback = status_callback
        self._log_callback = log_callback

    def empty(self) -> bool:
        return self._pending.empty()

    def get(self, timeout: Optional[float] = None) -> RenderTask:
        if timeout is None:
            return self._pending.get()
        return self._pending.get(timeout=timeout)

    def task_identity_set(self) -> set:
        queued_ids = set()
        running_task = self._current_task_getter()
        if running_task is not None:
            queued_ids.add(id(running_task))

        kept = []
        while True:
            try:
                task = self._pending.get_nowait()
            except queue.Empty:
                break
            kept.append(task)
            queued_ids.add(id(task))

        for task in kept:
            self._pending.put(task)
        return queued_ids

    def enqueue_tasks(self, tasks: List[RenderTask], mark_queued: bool = True, log_prefix: str = "[+] Added ") -> bool:
        if not tasks:
            return False

        queued_ids = self.task_identity_set()
        count = 0
        skipped_duplicates = 0
        skipped_incomplete = 0

        for task in tasks:
            if not all([task.uproject, task.level, task.sequence, task.preset]):
                skipped_incomplete += 1
                continue
            if id(task) in queued_ids:
                skipped_duplicates += 1
                continue

            self._pending.put(task)
            queued_ids.add(id(task))
            if mark_queued:
                task_index = self._task_index_resolver(task)
                if task_index is not None:
                    self._status_callback(task_index, "Queued")
            count += 1

        if count:
            self._log_callback(f"{log_prefix}{count} task(s) to queue")
        if skipped_duplicates:
            self._log_callback(f"[Queue] Skipped {skipped_duplicates} task(s): already queued or rendering.")
        if skipped_incomplete:
            self._log_callback(f"[Queue] Skipped {skipped_incomplete} incomplete task(s).")

        return bool(count or skipped_duplicates or skipped_incomplete)

    def clear_pending(self, status_text: str = TaskRuntimeStatus.CANCELLED_QUEUE) -> int:
        removed = 0
        while True:
            try:
                task = self._pending.get_nowait()
            except queue.Empty:
                break
            task_index = self._task_index_resolver(task)
            if task_index is not None:
                self._status_callback(task_index, status_text)
            removed += 1

        if removed:
            self._log_callback(f"[Cancel] Removed {removed} queued task(s).")
        return removed

    def remove_tasks(self, tasks_to_remove: List[RenderTask]) -> int:
        if not tasks_to_remove:
            return 0

        to_remove_ids = {id(task) for task in tasks_to_remove}
        kept = []
        removed = 0
        while True:
            try:
                task = self._pending.get_nowait()
            except queue.Empty:
                break
            if id(task) in to_remove_ids:
                removed += 1
                continue
            kept.append(task)

        for task in kept:
            self._pending.put(task)

        if removed:
            self._log_callback(f"[Tasks] Removed {removed} queued item(s) from runtime queue.")
        return removed


class RenderProcessController:
    """Owns the active Unreal subprocess lifecycle independently from UI widgets."""

    def __init__(self, log_callback):
        self.current_process: Optional[subprocess.Popen] = None
        self._log_callback = log_callback

    def is_active(self) -> bool:
        return bool(self.current_process and self.current_process.poll() is None)

    def launch(self, cmd: List[str]) -> subprocess.Popen:
        if self.is_active():
            raise RuntimeError("A render process is already running.")
        self.current_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        return self.current_process

    def clear_if_current(self, process: Optional[subprocess.Popen]) -> None:
        if process is not None and self.current_process is process:
            self.current_process = None

    def stop_current(self, timeout_s: int) -> bool:
        process = self.current_process
        if not process or process.poll() is not None:
            self._log_callback("[Cancel] No running process")
            return False

        process.terminate()
        self._log_callback("[Cancel] Stop current render requested…")
        if timeout_s > 0:
            try:
                process.wait(timeout=timeout_s)
            except Exception:
                process.kill()
                self._log_callback("[Cancel] Kill current render after timeout")
        return True


def app_settings_to_queue_config(settings: AppSettings) -> dict:
    """Return the persisted queue config subset from an AppSettings snapshot."""
    return {
        "ue_cmd": settings.ue_cmd,
        "retries": int(settings.retries),
        "fail_policy": settings.fail_policy,
        "kill_timeout_s": int(settings.kill_timeout_s),
        "windowed": bool(settings.windowed),
        "resx": int(settings.resx),
        "resy": int(settings.resy),
        "no_texture_streaming": bool(settings.no_texture_streaming),
        "auto_minimal_on_render": bool(settings.auto_minimal_on_render),
        "extra_cli": settings.extra_cli,
    }


def build_unreal_command(settings: AppSettings, task: RenderTask) -> List[str]:
    """Build the effective Unreal command from shared app settings and task data."""
    cmd = [
        settings.ue_cmd or "<UnrealEditor-Cmd.exe>",
        task.uproject or "<uproject>",
        task.level.split(".")[0] if task.level else "<map>",
        "-game",
        f'-LevelSequence="{task.sequence or "<sequence>"}"',
        f'-MoviePipelineConfig="{task.preset or "<preset>"}"',
        "-log",
    ]
    cmd.append("-windowed" if settings.windowed else "-fullscreen")
    cmd += [f"-ResX={int(settings.resx)}", f"-ResY={int(settings.resy)}"]
    if settings.no_texture_streaming:
        cmd.append("-notexturestreaming")
    extra = (settings.extra_cli or "").strip()
    if extra:
        cmd += shlex.split(extra)
    if task.output_dir:
        cmd.append(f'-OutputDirectory="{task.output_dir}"')
    return cmd


def build_unreal_command_preview(settings: AppSettings, task: RenderTask) -> str:
    """Build a display-friendly command preview from shared command data."""
    return (" " + "\\" + "\n").join(build_unreal_command(settings, task))

# -------------------------------------------------
# Optional Qt shell preview
# -------------------------------------------------

def run_qt_shell() -> int:
    """Launch the PySide6 queue workspace without replacing the Tkinter launcher."""
    try:
        from PySide6.QtCore import QEvent, Qt, QTimer, QSize
        from PySide6.QtGui import QColor, QBrush, QIcon, QPalette, QFont, QPainter, QPen, QPixmap
        from PySide6.QtWidgets import (
            QApplication, QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
            QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPushButton,
            QHeaderView, QMenu, QSizePolicy, QSpinBox, QSplitter, QStatusBar, QStyle, QStyledItemDelegate, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
        )
    except ImportError as exc:
        print("PySide6 is required for the Qt shell. Install it with: pip install PySide6")
        print(f"Import error: {exc}")
        return 1

    def apply_qt_dark_theme(app: QApplication) -> None:
        """Apply an Apple-inspired dark production theme to the Qt shell."""
        try:
            app.setStyle("Fusion")
        except Exception:
            pass

        apple = {
            "bg": "#0B0D12",
            "card": "#151820",
            "card_alt": "#1A1F2A",
            "control": "#202634",
            "control_hover": "#2A3344",
            "field": "#0F1218",
            "border": "#2B3444",
            "border_soft": "#202838",
            "text": "#F5F7FA",
            "muted": "#9AA6B8",
            "muted2": "#728096",
            "accent": "#0A84FF",
            "accent_hover": "#2696FF",
            "accent_soft": "#123B67",
            "danger": "#FF453A",
            "success": "#32D74B",
        }

        palette = QPalette()
        palette.setColor(QPalette.Window, QColor(apple["bg"]))
        palette.setColor(QPalette.WindowText, QColor(apple["text"]))
        palette.setColor(QPalette.Base, QColor(apple["field"]))
        palette.setColor(QPalette.AlternateBase, QColor(apple["card_alt"]))
        palette.setColor(QPalette.ToolTipBase, QColor(apple["card_alt"]))
        palette.setColor(QPalette.ToolTipText, QColor(apple["text"]))
        palette.setColor(QPalette.Text, QColor(apple["text"]))
        palette.setColor(QPalette.Button, QColor(apple["control"]))
        palette.setColor(QPalette.ButtonText, QColor(apple["text"]))
        palette.setColor(QPalette.BrightText, QColor("#FFFFFF"))
        palette.setColor(QPalette.Highlight, QColor(apple["accent"]))
        palette.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
        palette.setColor(QPalette.Disabled, QPalette.Text, QColor(apple["muted2"]))
        palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(apple["muted2"]))
        app.setPalette(palette)

        app.setStyleSheet(f"""
            QWidget {{
                background-color: {apple['bg']};
                color: {apple['text']};
                font-family: "Segoe UI";
                font-size: 10pt;
                selection-background-color: {apple['accent']};
                selection-color: #FFFFFF;
            }}
            QFrame {{
                background: transparent;
                border: none;
            }}
            QFrame#Card {{
                background-color: {apple['card']};
                border: 1px solid {apple['border_soft']};
                border-radius: 14px;
            }}
            QFrame#OptionStrip, QFrame#ToolbarStrip {{
                background-color: {apple['card_alt']};
                border: 1px solid {apple['border_soft']};
                border-radius: 12px;
            }}
            QFrame#CommandSettingsBody, QFrame#DiagnosticsLogBody {{
                background: transparent;
                border: none;
            }}
            QSplitter {{
                background-color: transparent;
            }}
            QSplitter::handle {{
                background-color: {apple['border_soft']};
                border-radius: 3px;
            }}
            QSplitter::handle:horizontal {{
                width: 8px;
                margin: 8px 0px;
            }}
            QSplitter::handle:hover {{
                background-color: {apple['accent']};
            }}
            QLabel {{
                background: transparent;
                border: none;
            }}
            QLabel#TitleLabel {{
                color: {apple['text']};
                font-size: 22px;
                font-weight: 800;
            }}
            QLabel#SectionTitle {{
                color: {apple['text']};
                font-size: 13px;
                font-weight: 700;
                padding-bottom: 2px;
            }}
            QLabel#SubtitleLabel, QLabel#MutedLabel {{
                color: {apple['muted']};
                font-size: 9pt;
            }}
            QLabel#InspectorField {{
                background-color: {apple['field']};
                color: {apple['text']};
                border: 1px solid {apple['border_soft']};
                border-radius: 10px;
                padding: 8px 10px;
                line-height: 120%;
            }}
            QPushButton {{
                background-color: {apple['control']};
                color: {apple['text']};
                border: 1px solid {apple['border_soft']};
                border-radius: 10px;
                padding: 7px 13px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                background-color: {apple['control_hover']};
                border-color: {apple['border']};
            }}
            QPushButton:pressed {{
                background-color: {apple['accent_soft']};
            }}
            QPushButton[role="primary"] {{
                background-color: {apple['accent']};
                border-color: {apple['accent']};
                color: #FFFFFF;
            }}
            QPushButton[role="primary"]:hover {{
                background-color: {apple['accent_hover']};
                border-color: {apple['accent_hover']};
            }}
            QPushButton[role="warning"] {{
                background-color: #C76A1D;
                border-color: #E07B24;
                color: #FFFFFF;
            }}
            QPushButton[role="warning"]:hover {{
                background-color: #E07B24;
                border-color: #F08A2A;
            }}
            QPushButton[role="danger"] {{
                background-color: #3A1F24;
                border-color: #6B2B32;
                color: #FFB3B0;
            }}
            QPushButton[role="danger"]:hover {{
                background-color: #5A2730;
                border-color: #A64957;
                color: #FFFFFF;
            }}
            QPushButton[role="danger"]:pressed {{
                background-color: #7A303B;
                border-color: #C85A67;
                color: #FFFFFF;
            }}
            QPushButton[role="ghost"] {{
                background-color: transparent;
                border-color: transparent;
                color: {apple['muted']};
            }}
            QPushButton#DisclosureButton {{
                min-width: 34px;
                max-width: 34px;
                min-height: 34px;
                max-height: 34px;
                padding: 0px;
                border-radius: 10px;
                font-weight: 800;
            }}
            QLabel#CommandSummary {{
                color: {apple['text']};
                font-size: 10pt;
                padding-top: 3px;
            }}
            QLineEdit, QTextEdit, QSpinBox, QComboBox {{
                background-color: {apple['field']};
                color: {apple['text']};
                border: 1px solid {apple['border_soft']};
                border-radius: 9px;
                padding: 6px 8px;
            }}
            QLineEdit:focus, QTextEdit:focus, QSpinBox:focus, QComboBox:focus {{
                border-color: {apple['accent']};
            }}
            QSpinBox::up-button, QSpinBox::down-button {{
                width: 0px;
                border: none;
            }}
            QLineEdit::placeholder {{
                color: {apple['muted2']};
            }}
            QCheckBox {{
                background: transparent;
                border: none;
                spacing: 8px;
                color: {apple['text']};
            }}
            QCheckBox::indicator {{
                width: 16px;
                height: 16px;
                border-radius: 5px;
                border: 1px solid {apple['border']};
                background-color: {apple['field']};
            }}
            QCheckBox::indicator:checked {{
                background-color: {apple['accent']};
                border-color: {apple['accent']};
            }}
            QTableWidget {{
                background-color: {apple['card']};
                alternate-background-color: {apple['card_alt']};
                color: {apple['text']};
                gridline-color: transparent;
                border: 1px solid {apple['border_soft']};
                border-radius: 12px;
                padding: 4px;
            }}
            QHeaderView::section {{
                background-color: {apple['card_alt']};
                color: {apple['muted']};
                border: none;
                border-bottom: 1px solid {apple['border_soft']};
                padding: 8px 10px;
                font-weight: 700;
            }}
            QTableWidget::item {{
                border: none;
                padding: 7px 9px;
            }}
            QTableWidget::item:selected {{
                background-color: {apple['accent_soft']};
                color: #FFFFFF;
            }}
            QListWidget {{
                background-color: {apple['card']};
                color: {apple['text']};
                border: 1px solid {apple['border_soft']};
                border-radius: 12px;
                padding: 4px;
                outline: none;
            }}
            QListWidget::item {{
                background: transparent;
                border: none;
            }}
            QListWidget::item:selected {{
                background: transparent;
                border: none;
            }}
            QStatusBar {{
                background-color: {apple['card']};
                color: {apple['muted']};
                border-top: 1px solid {apple['border_soft']};
            }}
            QMenu {{
                background-color: {apple['card_alt']};
                color: {apple['text']};
                border: 1px solid {apple['border']};
                border-radius: 8px;
                padding: 6px;
            }}
            QMenu::item {{
                padding: 7px 28px 7px 18px;
                border-radius: 6px;
            }}
            QMenu::item:selected {{
                background-color: {apple['accent_soft']};
                color: #FFFFFF;
            }}
            QMenu::item:disabled {{
                color: {apple['muted2']};
            }}
            QMenu::separator {{
                height: 1px;
                background-color: {apple['border_soft']};
                margin: 6px 4px;
            }}
            QScrollBar:vertical {{
                background: {apple['card']};
                width: 12px;
                margin: 2px;
            }}
            QScrollBar::handle:vertical {{
                background: {apple['control_hover']};
                border-radius: 6px;
                min-height: 28px;
            }}
            QScrollBar:horizontal {{
                background: {apple['card']};
                height: 12px;
                margin: 2px;
            }}
            QScrollBar::handle:horizontal {{
                background: {apple['control_hover']};
                border-radius: 6px;
                min-width: 28px;
            }}
            QScrollBar::add-line, QScrollBar::sub-line {{
                width: 0px;
                height: 0px;
            }}
        """)

    class QtStatusPillDelegate(QStyledItemDelegate):
        """Draw rounded status pills in the Qt queue table."""

        def paint(self, painter: QPainter, option, index) -> None:
            text = str(index.data(Qt.DisplayRole) or "")
            bg = index.data(Qt.UserRole + 1) or STATUS_PILL_THEME["ready"]["bg"]
            fg = index.data(Qt.UserRole + 2) or STATUS_PILL_THEME["ready"]["text"]
            border = index.data(Qt.UserRole + 3) or STATUS_PILL_THEME["ready"]["border"]

            painter.save()
            painter.setRenderHint(QPainter.Antialiasing, True)

            row_bg = QColor("#151820")
            if option.state & QStyle.State_Selected:
                row_bg = QColor("#123B67")
            elif index.row() % 2:
                row_bg = QColor("#1A1F2A")
            painter.fillRect(option.rect, row_bg)

            rect = option.rect.adjusted(10, 7, -10, -7)
            min_width = 92
            if rect.width() > min_width:
                rect.setWidth(min(rect.width(), max(min_width, 22 + len(text) * 8)))

            painter.setPen(QPen(QColor(border), 1))
            painter.setBrush(QBrush(QColor(bg)))
            painter.drawRoundedRect(rect, 7, 7)

            font = QFont(option.font)
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QColor(fg))
            painter.drawText(rect, Qt.AlignCenter, text)
            painter.restore()


    class QtOrderBadgeDelegate(QStyledItemDelegate):
        """Draw compact orange queue order badges."""

        def paint(self, painter: QPainter, option, index) -> None:
            text = str(index.data(Qt.DisplayRole) or "").strip()

            painter.save()
            painter.setRenderHint(QPainter.Antialiasing, True)

            row_bg = QColor("#151820")
            if option.state & QStyle.State_Selected:
                row_bg = QColor("#123B67")
            elif index.row() % 2:
                row_bg = QColor("#1A1F2A")
            painter.fillRect(option.rect, row_bg)

            if text:
                size = max(22, min(option.rect.height() - 10, 28))
                width = size if len(text) <= 2 else min(option.rect.width() - 14, size + (len(text) - 2) * 7)
                x = option.rect.x() + max(7, (option.rect.width() - width) // 2)
                y = option.rect.y() + max(5, (option.rect.height() - size) // 2)
                rect = option.rect.__class__(x, y, width, size)
                shadow_rect = option.rect.__class__(x, y + 2, width, size)

                painter.setPen(Qt.NoPen)
                painter.setBrush(QBrush(QColor(0, 0, 0, 70)))
                painter.drawRoundedRect(shadow_rect, 6, 6)

                painter.setPen(QPen(QColor("#FFB15A"), 1))
                painter.setBrush(QBrush(QColor("#E8791A")))
                painter.drawRoundedRect(rect, 6, 6)

                inner = rect.adjusted(2, 2, -2, -2)
                painter.setPen(QPen(QColor("#F08A2A"), 1))
                painter.setBrush(QBrush(QColor("#C75F12")))
                painter.drawRoundedRect(inner, 4, 4)

                font = QFont(option.font)
                font.setBold(True)
                painter.setFont(font)
                painter.setPen(QColor("#FFFFFF"))
                painter.drawText(rect, Qt.AlignCenter, text)

            painter.restore()


    class QtValidationDotDelegate(QStyledItemDelegate):
        """Draw a compact color-coded validation dot."""

        def paint(self, painter: QPainter, option, index) -> None:
            color = index.data(Qt.UserRole + 4) or validation_status_color("Not checked")

            painter.save()
            painter.setRenderHint(QPainter.Antialiasing, True)

            row_bg = QColor("#151820")
            if option.state & QStyle.State_Selected:
                row_bg = QColor("#123B67")
            elif index.row() % 2:
                row_bg = QColor("#1A1F2A")
            painter.fillRect(option.rect, row_bg)

            size = max(10, min(option.rect.height() - 16, 14))
            x = option.rect.x() + max(0, (option.rect.width() - size) // 2)
            y = option.rect.y() + max(0, (option.rect.height() - size) // 2)
            rect = option.rect.__class__(x, y, size, size)
            painter.setPen(QPen(QColor(color), 1))
            painter.setBrush(QBrush(QColor(color)))
            painter.drawEllipse(rect)
            painter.restore()


    class QtTaskEditor(QDialog):
        """Qt editor for one render task."""

        def __init__(self, parent, task: Optional[RenderTask] = None):
            super().__init__(parent)
            self.setWindowTitle("Task Editor")
            self.result: Optional[RenderTask] = None
            self.source_task = task
            layout = QVBoxLayout(self)
            form = QGridLayout()
            layout.addLayout(form)

            self.project_edit = QLineEdit(task.uproject if task else "")
            self.level_edit = QLineEdit(soft_object_to_editor_path(task.level) if task else "")
            self.sequence_edit = QLineEdit(soft_object_to_editor_path(task.sequence) if task else "")
            self.preset_edit = QLineEdit(soft_object_to_editor_path(task.preset) if task else "")

            rows = (
                ("Project (.uproject)", self.project_edit, self._browse_project),
                ("Map", self.level_edit, self._browse_level),
                ("Level Sequence", self.sequence_edit, self._browse_sequence),
                ("MRQ Preset", self.preset_edit, self._browse_preset),
            )
            for row, (label_text, edit, browse_cb) in enumerate(rows):
                form.addWidget(QLabel(label_text), row, 0)
                form.addWidget(edit, row, 1)
                button = QPushButton("Browse")
                button.clicked.connect(browse_cb)
                form.addWidget(button, row, 2)

            buttons = QHBoxLayout()
            buttons.addStretch(1)
            ok_button = self._styled_dialog_button("OK", "primary")
            ok_button.clicked.connect(self._accept)
            cancel_button = self._styled_dialog_button("Cancel", "danger")
            cancel_button.clicked.connect(self.reject)
            buttons.addWidget(ok_button)
            buttons.addWidget(cancel_button)
            buttons.addStretch(1)
            layout.addLayout(buttons)
            self.resize(760, 185)

        def _styled_dialog_button(self, text: str, role: str) -> QPushButton:
            button = QPushButton(text)
            button.setProperty("role", role)
            button.setMinimumWidth(110)
            button.setMinimumHeight(34)
            return button

        def _browse_project(self) -> None:
            path, _ = QFileDialog.getOpenFileName(self, "Select .uproject", "", "Unreal Project (*.uproject);;All Files (*.*)")
            if path:
                self.project_edit.setText(path)

        def _browse_level(self) -> None:
            self._browse_soft_object(self.level_edit, "Select MAP .umap/.uasset", "Unreal Map/Asset (*.umap *.uasset);;All Files (*.*)")

        def _browse_sequence(self) -> None:
            self._browse_soft_object(self.sequence_edit, "Select LevelSequence .uasset", "Unreal Asset (*.uasset);;All Files (*.*)")

        def _browse_preset(self) -> None:
            self._browse_soft_object(self.preset_edit, "Select MRQ Preset .uasset", "Unreal Asset (*.uasset);;All Files (*.*)")

        def _browse_soft_object(self, edit: QLineEdit, title: str, file_filter: str) -> None:
            path, _ = QFileDialog.getOpenFileName(self, title, "", file_filter)
            if not path:
                return
            try:
                edit.setText(soft_object_to_editor_path(fs_to_soft_object(path)))
            except Exception as exc:
                QMessageBox.critical(self, title, str(exc))

        def _accept(self) -> None:
            task = RenderTask(
                uproject=self.project_edit.text().strip(),
                level=editor_path_to_soft_object(self.level_edit.text()),
                sequence=editor_path_to_soft_object(self.sequence_edit.text()),
                preset=editor_path_to_soft_object(self.preset_edit.text()),
                output_dir=(self.source_task.output_dir if self.source_task else ""),
                notes=(self.source_task.notes if self.source_task else ""),
                added_at=(self.source_task.added_at if self.source_task else current_task_timestamp()),
                enabled=(self.source_task.enabled if self.source_task else True),
            )
            if not all([task.uproject, task.level, task.sequence, task.preset]):
                QMessageBox.critical(self, "Validation", "Fill in all required fields.")
                return
            self.result = task
            self.accept()

    class QtQueueLogListDelegate(QStyledItemDelegate):
        """Draw queue log list items with compact colored status counters."""

        COUNTER_COLORS = {
            "Done": "#32D74B",
            "Failed": "#FF453A",
            "Cancelled": "#FF9F0A",
            "Skipped": "#BF5AF2",
            "Incomplete": "#AAB2C0",
        }

        def sizeHint(self, option, index) -> QSize:
            return QSize(280, 72)

        def paint(self, painter: QPainter, option, index) -> None:
            data = index.data(Qt.UserRole + 1) or {}
            filename = data.get("filename", str(index.data(Qt.DisplayRole) or ""))
            created = data.get("created", "")
            stats = data.get("stats", {})
            total = int(stats.get("Total", 0) or 0)

            painter.save()
            painter.setRenderHint(QPainter.Antialiasing, True)
            rect = option.rect.adjusted(0, 0, -1, -1)
            if option.state & QStyle.State_Selected:
                bg = QColor("#123B67")
            elif option.state & QStyle.State_MouseOver:
                bg = QColor("#1A1F2A")
            else:
                bg = QColor("#151820")
            painter.fillRect(rect, bg)

            painter.setPen(QPen(QColor("#202838"), 1))
            painter.drawLine(rect.bottomLeft(), rect.bottomRight())

            left = rect.left() + 14
            right = rect.right() - 14
            top = rect.top() + 10

            title_font = QFont(option.font)
            title_font.setPointSize(max(9, title_font.pointSize()))
            painter.setFont(title_font)
            painter.setPen(QColor("#F5F7FA"))
            painter.drawText(left, top, max(80, right - left - 74), 22, Qt.AlignLeft | Qt.AlignVCenter, filename)

            painter.setPen(QColor("#D8E0EA"))
            painter.drawText(right - 72, top, 72, 22, Qt.AlignRight | Qt.AlignVCenter, f"{total} tasks")

            meta_font = QFont(option.font)
            meta_font.setPointSize(max(8, meta_font.pointSize() - 1))
            painter.setFont(meta_font)
            painter.setPen(QColor("#AAB4C3"))
            painter.drawText(left, top + 28, max(80, right - left - 120), 20, Qt.AlignLeft | Qt.AlignVCenter, created)

            counter_x = right
            for key in ("Incomplete", "Skipped", "Cancelled", "Failed", "Done"):
                value = str(stats.get(key, 0) or 0)
                width = max(18, painter.fontMetrics().horizontalAdvance(value) + 8)
                counter_x -= width
                painter.setPen(QColor(self.COUNTER_COLORS.get(key, "#AAB2C0")))
                painter.drawText(counter_x, top + 28, width, 20, Qt.AlignRight | Qt.AlignVCenter, value)

            painter.restore()


    class QtQueueLogViewer(QDialog):
        """Browse saved queue logs as sortable Minimal Mode snapshots."""

        COLUMNS = ("Order", "Status", "Level", "Sequence", "Preset", "Running Time", "Start", "End")

        def __init__(self, parent, logs_dir: str):
            super().__init__(parent)
            self.logs_dir = logs_dir
            self.current_log_path: Optional[str] = None
            self.setWindowTitle("Queue Logs")
            icon_path = app_icon_path()
            if os.path.exists(icon_path):
                self.setWindowIcon(QIcon(icon_path))
            self.resize(1240, 760)
            self._build_ui()
            self.refresh_logs()

        def _mark_button(self, button: QPushButton, role: str = "secondary") -> QPushButton:
            button.setProperty("role", role)
            return button

        def _build_ui(self) -> None:
            root = QHBoxLayout(self)
            root.setContentsMargins(14, 14, 14, 14)
            root.setSpacing(10)

            left_panel = QFrame(self)
            left_panel.setObjectName("Card")
            left_panel.setMinimumWidth(340)
            left_panel.setMaximumWidth(430)
            left_layout = QVBoxLayout(left_panel)
            left_layout.setSpacing(8)

            title_row = QHBoxLayout()
            title_block = QVBoxLayout()
            title = QLabel("Queue Logs")
            title.setObjectName("SectionTitle")
            self.total_label = QLabel("Total logs: 0")
            self.total_label.setObjectName("MutedLabel")
            title_block.addWidget(title)
            title_block.addWidget(self.total_label)
            title_row.addLayout(title_block, 1)
            refresh_button = self._mark_button(QPushButton("Refresh"))
            refresh_button.clicked.connect(self.refresh_logs)
            title_row.addWidget(refresh_button)
            left_layout.addLayout(title_row)

            self.search_edit = QLineEdit(self)
            self.search_edit.setPlaceholderText("Search logs...")
            self.search_edit.textChanged.connect(self.refresh_logs)
            left_layout.addWidget(self.search_edit)

            self.log_list = QListWidget(self)
            self.log_list.setMouseTracking(True)
            self.log_list.setItemDelegate(QtQueueLogListDelegate(self.log_list))
            self.log_list.currentItemChanged.connect(self._on_log_selected)
            left_layout.addWidget(self.log_list, 1)

            left_buttons = QHBoxLayout()
            open_folder = self._mark_button(QPushButton("Open Log Folder"))
            open_folder.clicked.connect(self.open_log_folder)
            delete_log = self._mark_button(QPushButton("Delete Log..."), "danger")
            delete_log.clicked.connect(self.delete_selected_log)
            left_buttons.addWidget(open_folder)
            left_buttons.addWidget(delete_log)
            left_layout.addLayout(left_buttons)
            root.addWidget(left_panel)

            right_panel = QFrame(self)
            right_panel.setObjectName("Card")
            right_layout = QVBoxLayout(right_panel)
            right_layout.setSpacing(10)

            top_row = QHBoxLayout()
            title_col = QVBoxLayout()
            self.log_title = QLabel("Select a queue log")
            self.log_title.setObjectName("TitleLabel")
            self.log_meta = QLabel("No log selected")
            self.log_meta.setObjectName("MutedLabel")
            title_col.addWidget(self.log_title)
            title_col.addWidget(self.log_meta)
            top_row.addLayout(title_col, 1)
            copy_button = self._mark_button(QPushButton("Copy to Clipboard"))
            copy_button.clicked.connect(self.copy_selected_log)
            export_button = self._mark_button(QPushButton("Export..."))
            export_button.clicked.connect(self.export_selected_log)
            top_row.addWidget(export_button)
            top_row.addWidget(copy_button)
            right_layout.addLayout(top_row)

            self.metrics_row = QHBoxLayout()
            self.metric_labels = {}
            metric_value_colors = {
                "Total": "#F5F7FA",
                "Done": "#32D74B",
                "Failed": "#FF453A",
                "Cancelled": "#FF9F0A",
                "Skipped": "#BF5AF2",
                "Incomplete": "#AAB2C0",
            }
            for key in ("Total", "Done", "Failed", "Cancelled", "Skipped", "Incomplete"):
                metric = QFrame(self)
                metric.setObjectName("ToolbarStrip")
                metric_layout = QVBoxLayout(metric)
                metric_layout.setContentsMargins(10, 8, 10, 8)
                label = QLabel(key)
                label.setAlignment(Qt.AlignCenter)
                label.setStyleSheet("background: transparent; border: none; color: #F5F7FA;")
                value = QLabel("0")
                value.setAlignment(Qt.AlignCenter)
                value_font = value.font()
                value_font.setPointSize(value_font.pointSize() + 6)
                value_font.setBold(True)
                value.setFont(value_font)
                value.setStyleSheet(f"background: transparent; border: none; color: {metric_value_colors[key]};")
                metric_layout.addWidget(label)
                metric_layout.addWidget(value)
                self.metric_labels[key] = value
                self.metrics_row.addWidget(metric)
            right_layout.addLayout(self.metrics_row)

            self.table = QTableWidget(0, len(self.COLUMNS), self)
            self.table.setHorizontalHeaderLabels(list(self.COLUMNS))
            self.table.setAlternatingRowColors(True)
            self.table.verticalHeader().setVisible(False)
            self.table.verticalHeader().setDefaultSectionSize(34)
            self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
            self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            self.table.setShowGrid(False)
            self.table.horizontalHeader().setStretchLastSection(False)
            self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
            self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
            self.table.setItemDelegateForColumn(0, QtOrderBadgeDelegate(self.table))
            self.table.setItemDelegateForColumn(1, QtStatusPillDelegate(self.table))
            self.table.setColumnWidth(0, 70)
            self.table.setColumnWidth(1, 120)
            right_layout.addWidget(self.table, 1)

            summary_label = QLabel("Log Summary")
            summary_label.setObjectName("SectionTitle")
            right_layout.addWidget(summary_label)
            self.summary_view = QTextEdit(self)
            self.summary_view.setReadOnly(True)
            self.summary_view.setMinimumHeight(120)
            right_layout.addWidget(self.summary_view)
            root.addWidget(right_panel, 1)

        def _queue_log_files(self) -> List[str]:
            try:
                files = [
                    os.path.join(self.logs_dir, name)
                    for name in os.listdir(self.logs_dir)
                    if name.startswith("Queue_Log_") and name.endswith(".log")
                ]
            except FileNotFoundError:
                return []
            except Exception:
                return []
            files.sort(key=lambda path: os.path.basename(path), reverse=True)
            return files

        def _log_created_display(self, path: str) -> str:
            try:
                return datetime.fromtimestamp(os.path.getmtime(path)).strftime("%d %b %Y %H:%M:%S")
            except Exception:
                return "Unknown time"

        def refresh_logs(self) -> None:
            query = self.search_edit.text().strip().lower() if self.search_edit else ""
            current = self.current_log_path
            self.log_list.clear()
            files = self._queue_log_files()
            visible = [path for path in files if not query or query in os.path.basename(path).lower()]
            self.total_label.setText(f"Total logs: {len(files)}")
            for path in visible:
                rows, _headers = self._parse_queue_log(path)
                stats = self._stats_for_rows(rows)
                item = QListWidgetItem(os.path.basename(path))
                item.setSizeHint(QSize(0, 72))
                item.setData(Qt.UserRole, path)
                item.setData(Qt.UserRole + 1, {
                    "filename": os.path.basename(path),
                    "created": self._log_created_display(path),
                    "stats": stats,
                })
                self.log_list.addItem(item)
                if current and os.path.abspath(path) == os.path.abspath(current):
                    self.log_list.setCurrentItem(item)
            if self.log_list.count() and self.log_list.currentRow() < 0:
                self.log_list.setCurrentRow(0)
            if not visible:
                self._clear_log_view()

        def _clear_log_view(self) -> None:
            self.current_log_path = None
            self.log_title.setText("Select a queue log")
            self.log_meta.setText("No log selected")
            self.table.setRowCount(0)
            self.summary_view.setPlainText("")
            for value in self.metric_labels.values():
                value.setText("0")

        def _on_log_selected(self, current: Optional[QListWidgetItem], _previous: Optional[QListWidgetItem]) -> None:
            if current is None:
                self._clear_log_view()
                return
            path = current.data(Qt.UserRole)
            if isinstance(path, str):
                self.load_log(path)

        def _parse_queue_log(self, path: str) -> tuple[List[dict], List[str]]:
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    lines = [line.strip() for line in handle.readlines() if line.strip()]
            except Exception:
                return [], []
            if not lines:
                return [], []
            headers = [part.strip() for part in lines[0].split(" / ")]
            rows = []
            for row_index, line in enumerate(lines[1:], start=1):
                parts = [part.strip() for part in line.split(" / ")]
                if headers == ["Level", "Sequence", "Preset", "Start", "End", "Duration"] and len(parts) >= 6:
                    row = {
                        "Order": str(row_index),
                        "Status": "Done" if parts[4] else "Incomplete",
                        "Level": parts[0],
                        "Sequence": parts[1],
                        "Preset": parts[2],
                        "Running Time": parts[5],
                        "Start": parts[3],
                        "End": parts[4],
                    }
                else:
                    row = {name: (parts[i] if i < len(parts) else "") for i, name in enumerate(headers)}
                    for name in self.COLUMNS:
                        row.setdefault(name, "")
                    if not row.get("Order"):
                        row["Order"] = str(row_index)
                rows.append(row)
            rows.sort(key=lambda row: self._safe_int(row.get("Order")))
            return rows, headers

        def _safe_int(self, value) -> int:
            try:
                return int(str(value).strip())
            except Exception:
                return 10**9

        def _duration_to_seconds(self, value: str) -> int:
            parts = str(value or "").strip().split(":")
            if len(parts) != 3:
                return 0
            try:
                hours, minutes, seconds = [int(part) for part in parts]
            except Exception:
                return 0
            return max(0, hours * 3600 + minutes * 60 + seconds)

        def _total_runtime_display(self, rows: List[dict]) -> str:
            total = sum(self._duration_to_seconds(row.get("Running Time", "")) for row in rows)
            return format_duration_hms(total)

        def _status_bucket(self, status: str) -> str:
            clean = (status or "").strip()
            if clean.startswith("Done"):
                return "Done"
            if clean.startswith("Failed"):
                return "Failed"
            if clean.startswith("Cancelled"):
                return "Cancelled"
            if clean.startswith("Skipped"):
                return "Skipped"
            if clean in ("", "Ready", "Queued", "Rendering", "Disabled", "Incomplete") or clean.startswith("Rendering"):
                return "Incomplete"
            return "Incomplete"

        def _stats_for_rows(self, rows: List[dict]) -> dict:
            stats = {"Total": len(rows), "Done": 0, "Failed": 0, "Cancelled": 0, "Skipped": 0, "Incomplete": 0}
            for row in rows:
                bucket = self._status_bucket(row.get("Status", ""))
                if bucket in stats:
                    stats[bucket] += 1
            return stats

        def _status_colors(self, status: str) -> tuple[str, str, str]:
            clean = (status or "").strip()
            if clean.startswith("Done"):
                return "#143A26", "#7DFFA2", "#236C43"
            if clean.startswith("Failed"):
                return "#47232A", "#FF6B68", "#6F313D"
            if clean.startswith("Cancelled"):
                return "#4A3015", "#FFB340", "#8A5A1F"
            if clean.startswith("Skipped"):
                return "#3A2A4B", "#D7C7FF", "#5C4777"
            if clean.startswith("Rendering"):
                return "#1E315B", "#A8C9FF", "#35518E"
            if clean == "Queued":
                return "#3A3116", "#F0C85B", "#655523"
            return "#173A28", "#8FE6B0", "#24573A"

        def load_log(self, path: str) -> None:
            rows, headers = self._parse_queue_log(path)
            self.current_log_path = path
            self.log_title.setText(os.path.basename(path))
            try:
                created = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                created = "Unknown time"
            self.log_meta.setText(f"{created}  •  Sorted by Order")
            stats = self._stats_for_rows(rows)
            for key, value in stats.items():
                if key in self.metric_labels:
                    self.metric_labels[key].setText(str(value))

            self.table.setRowCount(len(rows))
            for row_index, row_data in enumerate(rows):
                for column_index, column_name in enumerate(self.COLUMNS):
                    item = QTableWidgetItem(str(row_data.get(column_name, "")))
                    if column_name == "Order":
                        item.setTextAlignment(Qt.AlignCenter)
                    if column_name == "Status":
                        bg, fg, border = self._status_colors(str(row_data.get("Status", "")))
                        item.setData(Qt.UserRole + 1, bg)
                        item.setData(Qt.UserRole + 2, fg)
                        item.setData(Qt.UserRole + 3, border)
                    self.table.setItem(row_index, column_index, item)
            self.table.resizeColumnsToContents()
            self.table.setColumnWidth(0, 70)
            self.table.setColumnWidth(1, 120)
            if headers == ["Level", "Sequence", "Preset", "Start", "End", "Duration"]:
                compatibility = "[Logs] Legacy queue log format detected. Order and Status were reconstructed for display."
            else:
                compatibility = "[Logs] Queue log format: Order / Status / Level / Sequence / Preset / Running Time / Start / End"
            self.summary_view.setPlainText(
                "\n".join([
                    compatibility,
                    f"[Logs] File: {os.path.basename(path)}",
                    f"[Logs] Total tasks: {stats['Total']}",
                    f"[Logs] Total render time: {self._total_runtime_display(rows)}",
                    f"[Logs] Done: {stats['Done']} | Failed: {stats['Failed']} | Cancelled: {stats['Cancelled']} | Skipped: {stats['Skipped']} | Incomplete: {stats['Incomplete']}",
                ])
            )

        def _selected_log_text(self) -> str:
            if not self.current_log_path:
                return ""
            try:
                with open(self.current_log_path, "r", encoding="utf-8") as handle:
                    return handle.read()
            except Exception:
                return ""

        def copy_selected_log(self) -> None:
            text = self._selected_log_text()
            if text:
                QApplication.clipboard().setText(text)

        def export_selected_log(self) -> None:
            if not self.current_log_path:
                return
            target, _ = QFileDialog.getSaveFileName(
                self,
                "Export Queue Log",
                os.path.basename(self.current_log_path),
                "Log Files (*.log);;Text Files (*.txt);;All Files (*.*)",
            )
            if not target:
                return
            try:
                with open(self.current_log_path, "r", encoding="utf-8") as source, open(target, "w", encoding="utf-8") as dest:
                    dest.write(source.read())
            except Exception as exc:
                QMessageBox.critical(self, "Export Queue Log", str(exc))

        def open_log_folder(self) -> None:
            os.makedirs(self.logs_dir, exist_ok=True)
            self._open_path(self.logs_dir)

        def delete_selected_log(self) -> None:
            if not self.current_log_path:
                return
            name = os.path.basename(self.current_log_path)
            reply = QMessageBox.question(self, "Delete Queue Log", f"Delete {name}?")
            if reply != QMessageBox.Yes:
                return
            try:
                os.remove(self.current_log_path)
            except Exception as exc:
                QMessageBox.critical(self, "Delete Queue Log", str(exc))
                return
            self.current_log_path = None
            self.refresh_logs()

        def _open_path(self, path: str) -> None:
            try:
                if os.name == "nt":
                    os.startfile(path)
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", path])
                else:
                    subprocess.Popen(["xdg-open", path])
            except Exception as exc:
                QMessageBox.critical(self, "Open Path", str(exc))


    class QtMRQShell(QMainWindow):
        """Qt queue workspace backed by shared task/settings/core models."""

        COLUMNS = ("Order", "Status", "Validate", "Level", "Sequence", "Preset", "Running Time", "Start", "End")

        def __init__(self):
            super().__init__()
            self.settings = AppSettings()
            self.user_settings = UserSettingsRepository.load()
            self.current_queue_path = self.user_settings.get("last_queue", "")
            self.state: List[dict] = []
            self.validation_results: List[Optional[TaskValidationResult]] = []
            self.ui_events: "queue.Queue[TaskRuntimeEvent]" = queue.Queue()
            self.log_events: "queue.Queue[str]" = queue.Queue()
            self.process_controller = RenderProcessController(self._append_log)
            self.runtime_queue = RuntimeQueueCoordinator(
                self._find_task_index_by_identity,
                self._current_running_task_for_queue,
                self._set_status_from_core,
                self._append_log,
            )
            self.worker_running = False
            self.queue_order_by_task_id: dict[int, int] = {}
            self.stop_all = False
            self.cancel_current_requested = False
            self._current_global_idx: Optional[int] = None
            self._session_started_at: Optional[float] = None
            self.table = None
            self.filter_edit = None
            self.ue_path_edit = None
            self.retries_spin = None
            self.fail_policy_combo = None
            self.kill_timeout_spin = None
            self.windowed_check = None
            self.resx_spin = None
            self.resy_spin = None
            self.no_texture_streaming_check = None
            self.auto_minimal_check = None
            self.extra_cli_edit = None
            self.command_settings_panel = None
            self.command_settings_body = None
            self.command_settings_toggle = None
            self.command_settings_summary = None
            self.command_settings_expanded = False
            self.diagnostics_log_panel = None
            self.diagnostics_log_body = None
            self.diagnostics_log_toggle = None
            self.diagnostics_log_summary = None
            self.diagnostics_log_expanded = False
            self.command_preview = None
            self.log_view = None
            self.current_task_label = None
            self.current_status_label = None
            self.session_time_label = None
            self.minimal_current_task_label = None
            self.minimal_current_status_label = None
            self.minimal_session_time_label = None
            self.header_panel = None
            self.main_splitter = None
            self.queue_panel = None
            self.inspector_panel = None
            self.diagnostics_panel = None
            self.minimal_header = None
            self.minimal_footer = None
            self.queue_title_label = None
            self.queue_toolbar_panel = None
            self.minimal_mode = False
            self._normal_geometry = None
            self.queue_log_viewer = None
            self.inspector_labels = {}
            self.setWindowTitle(f"MRQ Launcher (Qt Shell) ver {APP_VERSION}")
            icon_path = app_icon_path()
            if os.path.exists(icon_path):
                self.setWindowIcon(QIcon(icon_path))
            self.resize(1420, 860)
            self.full_minimum_size = (1120, 680)
            self.minimal_minimum_size = (560, 360)
            self.setMinimumSize(*self.full_minimum_size)
            self._build_ui()
            self.refresh_queue_view()
            self.event_timer = QTimer(self)
            self.event_timer.timeout.connect(self._drain_runtime_events)
            self.event_timer.start(100)
            QTimer.singleShot(0, self._auto_load_last_queue_if_enabled)

        def _build_ui(self) -> None:
            root = QWidget(self)
            root_layout = QVBoxLayout(root)
            root_layout.setContentsMargins(18, 18, 18, 18)
            root_layout.setSpacing(14)
            self.setCentralWidget(root)
            self.header_panel = self._build_header()
            root_layout.addWidget(self.header_panel)
            self.minimal_header = self._build_minimal_header()
            self.minimal_header.setVisible(False)
            root_layout.addWidget(self.minimal_header)
            self.main_splitter = QSplitter(Qt.Horizontal, root)
            self.main_splitter.setHandleWidth(8)
            self.main_splitter.setChildrenCollapsible(False)
            self.main_splitter.setOpaqueResize(True)
            self.queue_panel = self._build_queue_area()
            self.inspector_panel = self._build_inspector_area()
            self.queue_panel.setMinimumWidth(520)
            self.inspector_panel.setMinimumWidth(280)
            self.inspector_panel.setMaximumWidth(560)
            self.main_splitter.addWidget(self.queue_panel)
            self.main_splitter.addWidget(self.inspector_panel)
            self.main_splitter.setStretchFactor(0, 1)
            self.main_splitter.setStretchFactor(1, 0)
            self.main_splitter.setSizes([980, 360])
            root_layout.addWidget(self.main_splitter, 1)
            self.diagnostics_panel = self._build_diagnostics_area()
            root_layout.addWidget(self.diagnostics_panel)
            self.minimal_footer = self._build_minimal_footer()
            self.minimal_footer.setVisible(False)
            root_layout.addWidget(self.minimal_footer)
            status = QStatusBar(self)
            status.showMessage("Qt runtime workspace ready.")
            self.setStatusBar(status)

        def _panel(self) -> QFrame:
            panel = QFrame(self)
            panel.setObjectName("Card")
            panel.setFrameShape(QFrame.StyledPanel)
            return panel

        def _mark_button(self, button: QPushButton, role: str = "secondary") -> QPushButton:
            """Assign a visual role used by the shared Qt stylesheet."""
            button.setProperty("role", role)
            return button

        def _section_label(self, text: str) -> QLabel:
            label = QLabel(text)
            label.setObjectName("SectionTitle")
            return label

        def _muted_label(self, text: str) -> QLabel:
            label = QLabel(text)
            label.setObjectName("MutedLabel")
            return label

        def _status_colors_for_task(self, task: RenderTask, state: dict) -> tuple[str, str, str]:
            """Return row/status colors matching the existing launcher status palette."""
            kind = get_status_kind(state.get("status", TaskRuntimeStatus.READY), task.enabled)
            palette = STATUS_PILL_THEME.get(kind, STATUS_PILL_THEME["ready"])
            return palette["bg"], palette["text"], palette["border"]

        def _apply_status_item_style(self, item: QTableWidgetItem, task: RenderTask, state: dict, column: int) -> None:
            """Apply compact dark table styling without changing runtime state."""
            bg, fg, border = self._status_colors_for_task(task, state)
            if column == 0:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
                item.setTextAlignment(Qt.AlignCenter)
                return
            if column == 1:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
                item.setTextAlignment(Qt.AlignCenter)
                item.setData(Qt.UserRole + 1, bg)
                item.setData(Qt.UserRole + 2, fg)
                item.setData(Qt.UserRole + 3, border)
                return
            if column == 2:
                item.setTextAlignment(Qt.AlignCenter)
                return
            else:
                if not task.enabled:
                    item.setForeground(QBrush(QColor(UI_THEME["muted"])))
                elif state.get("status", "").startswith(TaskRuntimeStatus.RENDERING):
                    item.setForeground(QBrush(QColor("#A8C9FF")))
                else:
                    item.setForeground(QBrush(QColor(UI_THEME["text"])))

        def _build_header(self) -> QFrame:
            panel = self._panel()
            layout = QVBoxLayout(panel)

            header_row = QHBoxLayout()
            header_row.setSpacing(12)
            layout.addLayout(header_row)

            logo_path = app_header_logo_path()
            if os.path.exists(logo_path):
                logo_pixmap = QPixmap(logo_path)
                if not logo_pixmap.isNull():
                    logo_label = QLabel(panel)
                    logo_size = 134
                    logo_label.setFixedSize(logo_size, logo_size)
                    logo_label.setPixmap(logo_pixmap.scaled(
                        logo_size, logo_size, Qt.KeepAspectRatio, Qt.SmoothTransformation
                    ))
                    logo_label.setAlignment(Qt.AlignCenter)
                    header_row.addWidget(logo_label, 0, Qt.AlignLeft | Qt.AlignTop)

            right_column = QVBoxLayout()
            right_column.setSpacing(10)
            header_row.addLayout(right_column, 1)

            top_row = QHBoxLayout()
            right_column.addLayout(top_row)

            title_block = QVBoxLayout()
            title = QLabel("MRQ Launcher CLI")
            title.setObjectName("TitleLabel")
            subtitle = self._muted_label("Qt runtime workspace")
            title_block.addWidget(title)
            title_block.addWidget(subtitle)
            title_block.addStretch(1)
            top_row.addLayout(title_block, 1)
            load_button = self._mark_button(QPushButton("Load Queue ▾"))
            load_button.clicked.connect(self.show_recent_queue_menu)
            save_button = self._mark_button(QPushButton("Save Queue"))
            save_button.clicked.connect(self.save_queue_dialog)
            minimal_button = self._mark_button(QPushButton("Minimal Mode"), "primary")
            minimal_button.clicked.connect(self.enter_minimal_mode)
            top_row.addWidget(load_button)
            top_row.addWidget(save_button)
            top_row.addWidget(minimal_button)
            self._build_render_options_panel(right_column)
            return panel

        def _build_render_options_panel(self, parent_layout: QVBoxLayout) -> None:
            options_panel = QFrame(self)
            options_panel.setObjectName("OptionStrip")
            self.command_settings_panel = options_panel
            options_panel.setMinimumHeight(224)
            options_shell = QVBoxLayout(options_panel)
            options_shell.setContentsMargins(14, 12, 14, 14)
            options_shell.setSpacing(12)

            title_row = QHBoxLayout()
            title_row.setSpacing(10)
            self.command_settings_toggle = QPushButton("▾", options_panel)
            self.command_settings_toggle.setObjectName("DisclosureButton")
            self.command_settings_toggle.clicked.connect(self._toggle_command_settings_panel)
            title_row.addWidget(self.command_settings_toggle)

            title_col = QVBoxLayout()
            title_col.setSpacing(0)
            title_col.addWidget(self._section_label("Command Settings"))
            self.command_settings_summary = QLabel(options_panel)
            self.command_settings_summary.setObjectName("CommandSummary")
            title_col.addWidget(self.command_settings_summary)
            title_row.addLayout(title_col, 1)
            options_shell.addLayout(title_row)

            self.command_settings_body = QFrame(options_panel)
            self.command_settings_body.setObjectName("CommandSettingsBody")
            self.command_settings_body.setMinimumHeight(142)
            options_layout = QGridLayout(self.command_settings_body)
            options_layout.setContentsMargins(0, 0, 0, 0)
            options_layout.setHorizontalSpacing(14)
            options_layout.setVerticalSpacing(12)
            for row in range(4):
                options_layout.setRowMinimumHeight(row, 34)

            self.ue_path_edit = QLineEdit(self.settings.ue_cmd, self.command_settings_body)
            self.ue_path_edit.textChanged.connect(self._on_ue_path_changed)
            browse_button = self._mark_button(QPushButton("Browse", self.command_settings_body))
            browse_button.clicked.connect(self.browse_unreal_cmd)

            self.retries_spin = QSpinBox(options_panel)
            self.retries_spin.setRange(0, 3)
            self.fail_policy_combo = QComboBox(options_panel)
            self.fail_policy_combo.addItems(("retry_then_next", "skip_next", "stop_queue"))
            self.kill_timeout_spin = QSpinBox(options_panel)
            self.kill_timeout_spin.setRange(0, 3600)
            self.kill_timeout_spin.setSuffix(" s")
            self.windowed_check = QCheckBox("Windowed", options_panel)
            self.resx_spin = QSpinBox(options_panel)
            self.resx_spin.setRange(1, 32768)
            self.resy_spin = QSpinBox(options_panel)
            self.resy_spin.setRange(1, 32768)
            self.no_texture_streaming_check = QCheckBox("No texture streaming", options_panel)
            self.auto_minimal_check = QCheckBox("Auto minimal on render", options_panel)
            self.extra_cli_edit = QLineEdit(options_panel)
            self.extra_cli_edit.setPlaceholderText("Additional Unreal command-line arguments")

            for control in (
                self.ue_path_edit,
                browse_button,
                self.retries_spin,
                self.fail_policy_combo,
                self.kill_timeout_spin,
                self.resx_spin,
                self.resy_spin,
                self.extra_cli_edit,
            ):
                control.setMinimumHeight(32)
            for spin in (self.retries_spin, self.kill_timeout_spin, self.resx_spin, self.resy_spin):
                spin.setButtonSymbols(QSpinBox.NoButtons)

            options_layout.addWidget(QLabel("UnrealEditor-Cmd.exe"), 0, 0)
            options_layout.addWidget(self.ue_path_edit, 0, 1, 1, 5)
            options_layout.addWidget(browse_button, 0, 6)

            options_layout.addWidget(QLabel("Retries"), 1, 0)
            options_layout.addWidget(self.retries_spin, 1, 1)
            options_layout.addWidget(QLabel("On fail"), 1, 2)
            options_layout.addWidget(self.fail_policy_combo, 1, 3)
            options_layout.addWidget(QLabel("Kill timeout"), 1, 4)
            options_layout.addWidget(self.kill_timeout_spin, 1, 5)
            options_layout.addWidget(self.windowed_check, 1, 6)

            options_layout.addWidget(QLabel("ResX"), 2, 0)
            options_layout.addWidget(self.resx_spin, 2, 1)
            options_layout.addWidget(QLabel("ResY"), 2, 2)
            options_layout.addWidget(self.resy_spin, 2, 3)
            options_layout.addWidget(self.no_texture_streaming_check, 2, 4, 1, 2)
            options_layout.addWidget(self.auto_minimal_check, 2, 6)

            options_layout.addWidget(QLabel("Extra CLI"), 3, 0)
            options_layout.addWidget(self.extra_cli_edit, 3, 1, 1, 6)
            options_shell.addWidget(self.command_settings_body)
            parent_layout.addWidget(options_panel)

            self._apply_settings_to_option_controls()
            self._apply_command_settings_collapsed_state()
            self._connect_option_control_signals()

        def _toggle_command_settings_panel(self) -> None:
            self.command_settings_expanded = not self.command_settings_expanded
            self._apply_command_settings_collapsed_state()

        def _apply_command_settings_collapsed_state(self) -> None:
            if self.command_settings_body:
                self.command_settings_body.setVisible(self.command_settings_expanded)
            if self.command_settings_panel:
                if self.command_settings_expanded:
                    self.command_settings_panel.setMinimumHeight(224)
                    self.command_settings_panel.setMaximumHeight(16777215)
                else:
                    self.command_settings_panel.setMinimumHeight(72)
                    self.command_settings_panel.setMaximumHeight(82)
            if self.command_settings_toggle:
                self.command_settings_toggle.setText("▾" if self.command_settings_expanded else "▸")
            self._update_command_settings_summary()
            if self.command_settings_panel:
                self.command_settings_panel.updateGeometry()

        def _update_command_settings_summary(self) -> None:
            if not self.command_settings_summary:
                return
            ue_name = os.path.basename(self.settings.ue_cmd) if self.settings.ue_cmd else "UnrealEditor-Cmd.exe not set"
            window_mode = "Windowed" if self.settings.windowed else "Fullscreen"
            nts = "NTS on" if self.settings.no_texture_streaming else "NTS off"
            extra = " + Extra CLI" if (self.settings.extra_cli or "").strip() else ""
            self.command_settings_summary.setText(
                f"{ue_name}  •  {window_mode}  •  {self.settings.resx}×{self.settings.resy}  •  {self.settings.fail_policy}  •  {nts}{extra}"
            )

        def _option_control_widgets(self) -> List[QWidget]:
            return [
                widget for widget in (
                    self.retries_spin,
                    self.fail_policy_combo,
                    self.kill_timeout_spin,
                    self.windowed_check,
                    self.resx_spin,
                    self.resy_spin,
                    self.no_texture_streaming_check,
                    self.auto_minimal_check,
                    self.extra_cli_edit,
                )
                if widget is not None
            ]

        def _apply_settings_to_option_controls(self) -> None:
            widgets = self._option_control_widgets()
            ue_was_blocked = False
            if self.ue_path_edit:
                ue_was_blocked = self.ue_path_edit.blockSignals(True)
            for widget in widgets:
                widget.blockSignals(True)
            try:
                if self.ue_path_edit:
                    self.ue_path_edit.setText(self.settings.ue_cmd)
                if self.retries_spin:
                    self.retries_spin.setValue(int(self.settings.retries))
                if self.fail_policy_combo:
                    index = self.fail_policy_combo.findText(self.settings.fail_policy)
                    self.fail_policy_combo.setCurrentIndex(index if index >= 0 else 0)
                if self.kill_timeout_spin:
                    self.kill_timeout_spin.setValue(int(self.settings.kill_timeout_s))
                if self.windowed_check:
                    self.windowed_check.setChecked(bool(self.settings.windowed))
                if self.resx_spin:
                    self.resx_spin.setValue(int(self.settings.resx))
                if self.resy_spin:
                    self.resy_spin.setValue(int(self.settings.resy))
                if self.no_texture_streaming_check:
                    self.no_texture_streaming_check.setChecked(bool(self.settings.no_texture_streaming))
                if self.auto_minimal_check:
                    self.auto_minimal_check.setChecked(bool(self.settings.auto_minimal_on_render))
                if self.extra_cli_edit:
                    self.extra_cli_edit.setText(self.settings.extra_cli)
            finally:
                for widget in widgets:
                    widget.blockSignals(False)
                if self.ue_path_edit:
                    self.ue_path_edit.blockSignals(ue_was_blocked)
            self._update_command_settings_summary()
            self._update_command_preview()

        def _sync_option_controls_to_settings(self) -> None:
            if self.ue_path_edit:
                self.settings.ue_cmd = self.ue_path_edit.text().strip()
            if self.retries_spin:
                self.settings.retries = int(self.retries_spin.value())
            if self.fail_policy_combo:
                self.settings.fail_policy = self.fail_policy_combo.currentText()
            if self.kill_timeout_spin:
                self.settings.kill_timeout_s = int(self.kill_timeout_spin.value())
            if self.windowed_check:
                self.settings.windowed = bool(self.windowed_check.isChecked())
            if self.resx_spin:
                self.settings.resx = int(self.resx_spin.value())
            if self.resy_spin:
                self.settings.resy = int(self.resy_spin.value())
            if self.no_texture_streaming_check:
                self.settings.no_texture_streaming = bool(self.no_texture_streaming_check.isChecked())
            if self.auto_minimal_check:
                self.settings.auto_minimal_on_render = bool(self.auto_minimal_check.isChecked())
            if self.extra_cli_edit:
                self.settings.extra_cli = self.extra_cli_edit.text().strip()
            self._update_command_settings_summary()

        def _connect_option_control_signals(self) -> None:
            for spin in (self.retries_spin, self.kill_timeout_spin, self.resx_spin, self.resy_spin):
                if spin:
                    spin.valueChanged.connect(self._on_render_options_changed)
            for check in (self.windowed_check, self.no_texture_streaming_check, self.auto_minimal_check):
                if check:
                    check.toggled.connect(self._on_render_options_changed)
            if self.fail_policy_combo:
                self.fail_policy_combo.currentTextChanged.connect(self._on_render_options_changed)
            if self.extra_cli_edit:
                self.extra_cli_edit.textChanged.connect(self._on_render_options_changed)

        def _on_render_options_changed(self, *_args) -> None:
            self._sync_option_controls_to_settings()
            self._update_command_preview()

        def _validate_current_render_options(self) -> bool:
            self._sync_option_controls_to_settings()
            try:
                shlex.split(self.settings.extra_cli or "")
            except ValueError as exc:
                QMessageBox.critical(self, "Render Options", f"Invalid Extra CLI: {exc}")
                return False
            return True

        def _build_minimal_header(self) -> QFrame:
            panel = self._panel()
            layout = QHBoxLayout(panel)
            title_block = QVBoxLayout()
            title = self._section_label("Minimal Mode")
            subtitle = self._muted_label("Execution only view with compact columns.")
            title_block.addWidget(title)
            title_block.addWidget(subtitle)
            layout.addLayout(title_block, 1)
            stop_current = self._mark_button(QPushButton("Stop Current"))
            stop_current.clicked.connect(self.cancel_current)
            stop_all = self._mark_button(QPushButton("Stop All"), "danger")
            stop_all.clicked.connect(self.cancel_all)
            exit_button = self._mark_button(QPushButton("Exit Minimal Mode"), "primary")
            exit_button.clicked.connect(self.exit_minimal_mode)
            layout.addWidget(stop_current)
            layout.addWidget(stop_all)
            layout.addWidget(exit_button)
            return panel

        def _build_queue_area(self) -> QFrame:
            panel = self._panel()
            layout = QVBoxLayout(panel)
            self.queue_title_label = self._section_label("Render Queue")
            layout.addWidget(self.queue_title_label)
            self.queue_toolbar_panel = QFrame(panel)
            self.queue_toolbar_panel.setObjectName("ToolbarStrip")
            toolbar = QHBoxLayout(self.queue_toolbar_panel)
            toolbar.setContentsMargins(10, 10, 10, 10)
            toolbar.setSpacing(8)
            for text, callback in (
                ("Add Job", self.add_task_dialog),
                ("Load Task(s)", self.load_task_dialog),
                ("Save Selected", self.save_selected_tasks_dialog),
                ("Edit", self.edit_selected_task),
                ("Duplicate", self.duplicate_selected),
                ("Remove", self.remove_selected),
                ("Move Up", lambda: self.move_selected(-1)),
                ("Move Down", lambda: self.move_selected(1)),
                ("Toggle All", self.toggle_all_ready_disabled),
            ):
                button = self._mark_button(QPushButton(text))
                button.clicked.connect(callback)
                toolbar.addWidget(button)
            toolbar.addStretch(1)
            toolbar.addWidget(QLabel("Filter"))
            self.filter_edit = QLineEdit(panel)
            self.filter_edit.setPlaceholderText("Filter tasks")
            self.filter_edit.textChanged.connect(self.refresh_queue_view)
            toolbar.addWidget(self.filter_edit)
            layout.addWidget(self.queue_toolbar_panel)
            self.table = QTableWidget(0, len(self.COLUMNS), panel)
            self.table.setHorizontalHeaderLabels(list(self.COLUMNS))
            self.table.setAlternatingRowColors(True)
            self.table.verticalHeader().setVisible(False)
            self.table.verticalHeader().setDefaultSectionSize(38)
            self.table.horizontalHeader().setStretchLastSection(False)
            self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
            self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
            self.table.setShowGrid(False)
            self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
            self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
            self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            self.table.setItemDelegateForColumn(0, QtOrderBadgeDelegate(self.table))
            self.table.setItemDelegateForColumn(1, QtStatusPillDelegate(self.table))
            self.table.setItemDelegateForColumn(2, QtValidationDotDelegate(self.table))
            self.table.setColumnWidth(0, 68)
            self.table.setColumnWidth(1, 116)
            self.table.setColumnWidth(2, 74)
            self.table.setContextMenuPolicy(Qt.CustomContextMenu)
            self.table.customContextMenuRequested.connect(self._on_table_context_menu)
            self.table.itemSelectionChanged.connect(self._on_selection_changed)
            self.table.doubleClicked.connect(self._on_table_double_clicked)
            self.table.installEventFilter(self)
            layout.addWidget(self.table, 1)
            return panel

        def _add_context_action(self, menu: QMenu, label: str, callback, enabled: bool = True):
            """Add a context-menu action with consistent enabled-state handling."""
            action = menu.addAction(label)
            action.setEnabled(enabled)
            if enabled:
                action.triggered.connect(callback)
            return action

        def _on_table_context_menu(self, pos) -> None:
            """Select the row under the cursor and show the task context menu."""
            if not self.table:
                return

            item = self.table.itemAt(pos)
            if item is not None:
                row = item.row()
                row_indices = {
                    self.table.item(row, col).data(Qt.UserRole)
                    for col in range(self.table.columnCount())
                    if self.table.item(row, col) is not None
                }
                selected = set(self.selected_indices())
                if not row_indices.intersection(selected):
                    self.table.clearSelection()
                    self.table.selectRow(row)
            elif not self.selected_indices():
                self.table.clearSelection()

            selected_count = len(self.selected_indices())
            has_selection = selected_count > 0
            can_move_single = selected_count == 1
            has_tasks = bool(self.settings.tasks)

            menu = QMenu(self.table)
            self._add_context_action(menu, "Add Task", self.add_task_dialog)
            self._add_context_action(menu, "Edit Task", self.edit_selected_task, has_selection)
            self._add_context_action(menu, "Duplicate Task", self.duplicate_selected, has_selection)
            self._add_context_action(menu, "Remove Task(s)", self.remove_selected, has_selection)
            menu.addSeparator()
            self._add_context_action(menu, "Move Up", lambda: self.move_selected(-1), can_move_single)
            self._add_context_action(menu, "Move Down", lambda: self.move_selected(1), can_move_single)
            self._add_context_action(menu, "Toggle All Ready/Disabled", self.toggle_all_ready_disabled, has_tasks)
            menu.addSeparator()
            section = menu.addAction("Task Save/Load")
            section.setEnabled(False)
            self._add_context_action(menu, "Load Task(s)...", self.load_task_dialog)
            self._add_context_action(menu, "Save Selected Task(s)...", self.save_selected_tasks_dialog, has_selection)
            menu.addSeparator()
            self._add_context_action(menu, "Load Queue...", self.load_queue_dialog)
            self._add_context_action(menu, "Save Queue...", self.save_queue_dialog)
            self._add_context_action(menu, "Save Queue Log...", self.save_queue_log)
            menu.addSeparator()
            self._add_context_action(menu, "Clear Status", self.clear_status_selected, has_selection)
            menu.exec(self.table.viewport().mapToGlobal(pos))

        def _build_inspector_area(self) -> QFrame:
            panel = self._panel()
            layout = QVBoxLayout(panel)
            layout.setSpacing(8)
            layout.addWidget(self._section_label("Job Inspector"))
            for key, text in (
                ("job", "Job Name: No selection"), ("enabled", "Enabled: -"),
                ("project", "Project: -"), ("level", "Level: -"),
                ("sequence", "Sequence: -"), ("preset", "Preset: -"),
                ("validation", "Validation: -"),
            ):
                label = QLabel(text)
                label.setObjectName("InspectorField")
                label.setWordWrap(True)
                label.setMinimumWidth(0)
                label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
                self.inspector_labels[key] = label
                layout.addWidget(label)
            layout.addStretch(1)
            validate_button = self._mark_button(QPushButton("Validate"), "primary")
            validate_button.clicked.connect(self.validate_queue_tasks)
            layout.addWidget(validate_button)
            fix_button = self._mark_button(QPushButton("Fix Project Path"))
            fix_button.clicked.connect(self.fix_project_path_for_queue)
            layout.addWidget(fix_button)
            return panel

        def _build_diagnostics_area(self) -> QFrame:
            panel = self._panel()
            layout = QVBoxLayout(panel)
            layout.setSpacing(10)

            controls = QHBoxLayout()
            controls.setSpacing(8)
            for text, callback, role in (
                ("Render Enabled", self.render_enabled, "warning"),
                ("Render Selected", self.render_selected, "warning"),
                ("Queue Selected", self.queue_selected_or_enabled, "secondary"),
                ("Clear Status", self.clear_status_selected, "secondary"),
                ("Stop Current Render", self.cancel_current, "secondary"),
            ):
                button = self._mark_button(QPushButton(text), role)
                button.clicked.connect(callback)
                controls.addWidget(button)
            stop_all = self._mark_button(QPushButton("Stop All"), "danger")
            stop_all.clicked.connect(self.cancel_all)
            controls.addWidget(stop_all)
            controls.addStretch(1)
            for text, callback in (
                ("Save Queue Log", self.save_queue_log),
                ("Queue Logs", self.open_last_queue_log),
            ):
                button = self._mark_button(QPushButton(text))
                button.clicked.connect(callback)
                controls.addWidget(button)
            layout.addLayout(controls)

            status_row = QHBoxLayout()
            self.current_task_label = QLabel("Current task: Idle")
            self.current_status_label = QLabel("Status: Idle")
            self.session_time_label = QLabel("Session total: 00:00:00")
            status_row.addWidget(self.current_task_label)
            status_row.addWidget(self.current_status_label)
            status_row.addStretch(1)
            status_row.addWidget(self.session_time_label)
            layout.addLayout(status_row)

            self.diagnostics_log_panel = QFrame(panel)
            self.diagnostics_log_panel.setObjectName("ToolbarStrip")
            log_shell = QVBoxLayout(self.diagnostics_log_panel)
            log_shell.setContentsMargins(10, 10, 10, 10)
            log_shell.setSpacing(8)

            log_title_row = QHBoxLayout()
            log_title_row.setSpacing(10)
            self.diagnostics_log_toggle = QPushButton("▸", self.diagnostics_log_panel)
            self.diagnostics_log_toggle.setObjectName("DisclosureButton")
            self.diagnostics_log_toggle.clicked.connect(self._toggle_diagnostics_log_panel)
            log_title_row.addWidget(self.diagnostics_log_toggle)
            title_col = QVBoxLayout()
            title_col.setSpacing(0)
            title_col.addWidget(self._section_label("Command Preview & Log"))
            self.diagnostics_log_summary = QLabel("Collapsed • command preview and live render log")
            self.diagnostics_log_summary.setObjectName("CommandSummary")
            title_col.addWidget(self.diagnostics_log_summary)
            log_title_row.addLayout(title_col, 1)
            copy_button = self._mark_button(QPushButton("Copy Command"))
            copy_button.clicked.connect(self.copy_command_preview)
            log_title_row.addWidget(copy_button)
            log_shell.addLayout(log_title_row)

            self.diagnostics_log_body = QFrame(self.diagnostics_log_panel)
            self.diagnostics_log_body.setObjectName("DiagnosticsLogBody")
            diagnostics = QHBoxLayout(self.diagnostics_log_body)
            diagnostics.setContentsMargins(0, 0, 0, 0)
            diagnostics.setSpacing(8)
            self.command_preview = QTextEdit(self.diagnostics_log_body)
            self.command_preview.setReadOnly(True)
            self.command_preview.setPlaceholderText("Select a task to inspect the generated command line.")
            self.command_preview.setMinimumHeight(150)
            self.log_view = QTextEdit(self.diagnostics_log_body)
            self.log_view.setReadOnly(True)
            self.log_view.setMinimumHeight(150)
            diagnostics.addWidget(self.command_preview, 1)
            diagnostics.addWidget(self.log_view, 1)
            log_shell.addWidget(self.diagnostics_log_body)
            layout.addWidget(self.diagnostics_log_panel)
            self._apply_diagnostics_log_collapsed_state()
            return panel

        def _toggle_diagnostics_log_panel(self) -> None:
            self.diagnostics_log_expanded = not self.diagnostics_log_expanded
            self._apply_diagnostics_log_collapsed_state()

        def _apply_diagnostics_log_collapsed_state(self) -> None:
            if self.diagnostics_log_body:
                self.diagnostics_log_body.setVisible(self.diagnostics_log_expanded)
            if self.diagnostics_log_toggle:
                self.diagnostics_log_toggle.setText("▾" if self.diagnostics_log_expanded else "▸")
            if self.diagnostics_log_summary:
                self.diagnostics_log_summary.setText(
                    "Expanded • command preview and live render log"
                    if self.diagnostics_log_expanded
                    else "Collapsed • command preview and live render log"
                )
            if self.diagnostics_log_panel:
                self.diagnostics_log_panel.setMinimumHeight(230 if self.diagnostics_log_expanded else 66)
                self.diagnostics_log_panel.setMaximumHeight(16777215 if self.diagnostics_log_expanded else 76)
                self.diagnostics_log_panel.updateGeometry()

        def _build_minimal_footer(self) -> QFrame:
            panel = self._panel()
            layout = QHBoxLayout(panel)
            self.minimal_current_task_label = QLabel("Current task: Idle")
            self.minimal_current_status_label = QLabel("Status: Idle")
            self.minimal_session_time_label = QLabel("Session total: 00:00:00")
            layout.addWidget(self.minimal_session_time_label)
            layout.addWidget(QLabel("•"))
            layout.addWidget(self.minimal_current_task_label, 1)
            layout.addWidget(self.minimal_current_status_label)
            return panel

        def enter_minimal_mode(self) -> None:
            if self.minimal_mode:
                return
            self.minimal_mode = True
            self._normal_geometry = self.saveGeometry()
            self.setMinimumSize(*self.minimal_minimum_size)
            if self.header_panel:
                self.header_panel.setVisible(False)
            if self.minimal_header:
                self.minimal_header.setVisible(True)
            if self.inspector_panel:
                self.inspector_panel.setVisible(False)
            if self.diagnostics_panel:
                self.diagnostics_panel.setVisible(False)
            if self.queue_title_label:
                self.queue_title_label.setVisible(False)
            if self.queue_toolbar_panel:
                self.queue_toolbar_panel.setVisible(False)
            if self.minimal_footer:
                self.minimal_footer.setVisible(True)
            if self.statusBar():
                self.statusBar().setVisible(False)
            self._set_minimal_columns(True)
            self.refresh_queue_view()
            self._resize_minimal_window()

        def exit_minimal_mode(self) -> None:
            if not self.minimal_mode:
                return
            self.minimal_mode = False
            self.setMinimumSize(*self.full_minimum_size)
            if self.header_panel:
                self.header_panel.setVisible(True)
            if self.minimal_header:
                self.minimal_header.setVisible(False)
            if self.inspector_panel:
                self.inspector_panel.setVisible(True)
            if self.diagnostics_panel:
                self.diagnostics_panel.setVisible(True)
            if self.queue_title_label:
                self.queue_title_label.setVisible(True)
            if self.queue_toolbar_panel:
                self.queue_toolbar_panel.setVisible(True)
            if self.minimal_footer:
                self.minimal_footer.setVisible(False)
            if self.statusBar():
                self.statusBar().setVisible(True)
            self._set_minimal_columns(False)
            self.refresh_queue_view()
            if self._normal_geometry:
                self.restoreGeometry(self._normal_geometry)
            self.statusBar().showMessage("Qt runtime workspace ready.")

        def toggle_minimal_mode(self) -> None:
            if self.minimal_mode:
                self.exit_minimal_mode()
            else:
                self.enter_minimal_mode()

        def _set_minimal_columns(self, enabled: bool) -> None:
            if not self.table:
                return
            # Minimal Mode keeps Running Time visible and hides non-essential order/timestamp columns.
            order_column = self.COLUMNS.index("Order")
            start_column = self.COLUMNS.index("Start")
            end_column = self.COLUMNS.index("End")
            for column in (order_column, start_column, end_column):
                self.table.setColumnHidden(column, enabled)

        def _resize_minimal_window(self) -> None:
            visible_rows = self.table.rowCount() if self.table else 0
            row_count = max(6, min(visible_rows, 12))
            width = 820
            height = max(340, min(600, 160 + row_count * 30))
            self.resize(width, height)

        def _ensure_state(self) -> None:
            while len(self.state) < len(self.settings.tasks):
                self.state.append(default_task_state())
            if len(self.state) > len(self.settings.tasks):
                self.state = self.state[:len(self.settings.tasks)]
            while len(self.validation_results) < len(self.settings.tasks):
                self.validation_results.append(None)
            if len(self.validation_results) > len(self.settings.tasks):
                self.validation_results = self.validation_results[:len(self.settings.tasks)]
            self._prune_queue_order()

        def _prune_queue_order(self) -> None:
            valid_ids = {id(task) for task in self.settings.tasks}
            for task_id in list(self.queue_order_by_task_id):
                if task_id not in valid_ids:
                    self.queue_order_by_task_id.pop(task_id, None)

        def _queue_order_for_task(self, task: RenderTask) -> Optional[int]:
            return self.queue_order_by_task_id.get(id(task))

        def _next_queue_order(self) -> int:
            return max(self.queue_order_by_task_id.values(), default=0) + 1

        def _ordered_task_indices(self) -> List[int]:
            ordered = []
            for idx, task in enumerate(self.settings.tasks):
                order = self._queue_order_for_task(task)
                if task.enabled and order is not None:
                    ordered.append((order, idx))
            return [idx for _order, idx in sorted(ordered)]

        def _sort_tasks_by_session_order(self, tasks: List[RenderTask]) -> List[RenderTask]:
            positions = {id(task): pos for pos, task in enumerate(tasks)}
            return sorted(
                tasks,
                key=lambda task: (
                    self.queue_order_by_task_id.get(id(task), 10**9),
                    positions.get(id(task), 10**9),
                ),
            )

        def _assign_order_to_task(self, task: RenderTask) -> None:
            if id(task) not in self.queue_order_by_task_id:
                self.queue_order_by_task_id[id(task)] = self._next_queue_order()

        def _remove_order_from_task(self, task: RenderTask) -> None:
            self.queue_order_by_task_id.pop(id(task), None)

        def _rebuild_order_for_enabled_tasks(self) -> None:
            """Create session-only queue order from enabled tasks in list order."""
            self.queue_order_by_task_id.clear()
            order = 1
            for idx, task in enumerate(self.settings.tasks):
                state = self.state[idx] if idx < len(self.state) else default_task_state()
                if not task.enabled or state.get("status", "Ready").startswith(TaskRuntimeStatus.RENDERING):
                    continue
                self.queue_order_by_task_id[id(task)] = order
                order += 1

        def _compact_queue_order(self) -> None:
            ordered = []
            for idx, task in enumerate(self.settings.tasks):
                order = self.queue_order_by_task_id.get(id(task))
                if task.enabled and order is not None:
                    ordered.append((order, idx, task))
            self.queue_order_by_task_id.clear()
            for new_order, (_old_order, _idx, task) in enumerate(sorted(ordered), start=1):
                self.queue_order_by_task_id[id(task)] = new_order

        def _visible_task_indices(self) -> List[int]:
            query = self.filter_edit.text().strip().lower() if self.filter_edit else ""
            visible = []
            source_indices = self._ordered_task_indices() if self.minimal_mode else list(range(len(self.settings.tasks)))
            for idx in source_indices:
                task = self.settings.tasks[idx]
                state = self.state[idx] if idx < len(self.state) else default_task_state()
                order = self._queue_order_for_task(task)
                if self.minimal_mode and order is None:
                    continue
                haystack = " ".join([
                    str(order or ""), task.uproject, task.level, task.sequence,
                    task.preset, task.output_dir, state.get("status", "Ready"),
                ]).lower()
                if not query or query in haystack:
                    visible.append(idx)
            return visible

        def refresh_queue_view(self) -> None:
            self._ensure_state()
            if not self.table:
                return
            selected_indices = set(self.selected_indices())
            visible_indices = self._visible_task_indices()
            self.table.setRowCount(len(visible_indices))
            for row, task_index in enumerate(visible_indices):
                task = self.settings.tasks[task_index]
                state = self.state[task_index] if task_index < len(self.state) else default_task_state()
                order = self._queue_order_for_task(task)
                validation = self._validation_for_index(task_index)
                values = (
                    str(order) if order is not None else "",
                    get_status_display(state.get("status", "Ready"), task.enabled),
                    "",
                    soft_name(task.level), soft_name(task.sequence), soft_name(task.preset),
                    format_runtime_display(state), format_state_time_display(state.get("start")),
                    format_state_time_display(state.get("end")),
                )
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setData(Qt.UserRole, task_index)
                    if column == 2:
                        item.setData(Qt.UserRole + 4, validation_status_color(validation.status if validation else "Not checked"))
                        item.setToolTip(validation_status_tooltip(validation))
                    self._apply_status_item_style(item, task, state, column)
                    self.table.setItem(row, column, item)
            self.table.resizeColumnsToContents()
            self.table.setColumnWidth(0, 68)
            self.table.setColumnWidth(1, 116)
            self.table.setColumnWidth(2, 74)
            for row, task_index in enumerate(visible_indices):
                if task_index in selected_indices:
                    self.table.selectRow(row)
            self._update_inspector()
            self._update_command_preview()
            self._update_status_bar()

        def _task_index_for_table_row(self, row: int) -> Optional[int]:
            """Resolve a visible table row to the underlying task index."""
            if not self.table or row < 0 or row >= self.table.rowCount():
                return None
            item = self.table.item(row, 0)
            if item is None:
                return None
            task_index = item.data(Qt.UserRole)
            return task_index if isinstance(task_index, int) else None

        def selected_indices(self) -> List[int]:
            if not self.table:
                return []
            rows = []
            selection_model = self.table.selectionModel()
            if selection_model is not None:
                rows = [index.row() for index in selection_model.selectedRows()]
            if not rows:
                rows = sorted({item.row() for item in self.table.selectedItems()})

            indices = []
            seen = set()
            for row in rows:
                task_index = self._task_index_for_table_row(row)
                if task_index is not None and task_index not in seen:
                    seen.add(task_index)
                    indices.append(task_index)
            return sorted(indices)

        def _selected_task(self) -> Optional[RenderTask]:
            indices = self.selected_indices()
            if not indices:
                return None
            idx = indices[0]
            return self.settings.tasks[idx] if 0 <= idx < len(self.settings.tasks) else None

        def _collect(self, only_enabled: bool = False, only_selected: bool = False) -> List[RenderTask]:
            if only_selected:
                tasks = [self.settings.tasks[idx] for idx in self.selected_indices()]
                return self._sort_tasks_by_session_order(tasks)
            if only_enabled:
                tasks = [task for task in self.settings.tasks if task.enabled]
                return self._sort_tasks_by_session_order(tasks)
            return list(self.settings.tasks)

        def _find_task_index_by_identity(self, task: RenderTask) -> Optional[int]:
            for i, existing in enumerate(self.settings.tasks):
                if existing is task:
                    return i
            return None

        def _current_running_task_for_queue(self) -> Optional[RenderTask]:
            if self._current_global_idx is not None and 0 <= self._current_global_idx < len(self.settings.tasks):
                return self.settings.tasks[self._current_global_idx]
            return None

        def _set_status_from_core(self, idx: int, text: str) -> None:
            self.ui_events.put(TaskRuntimeEvent(
                event_type=self._event_type_for_status(text),
                task_index=idx,
                status=text,
            ))

        def _append_log(self, message: str) -> None:
            self.log_events.put(message)

        def _event_type_for_status(self, status: str) -> str:
            status = status or TaskRuntimeStatus.READY
            if status == TaskRuntimeStatus.QUEUED:
                return TaskRuntimeEventType.TASK_QUEUED
            if status.startswith(TaskRuntimeStatus.RENDERING):
                return TaskRuntimeEventType.TASK_STARTED
            if status.startswith(TaskRuntimeStatus.DONE):
                return TaskRuntimeEventType.TASK_FINISHED
            if status.startswith(TaskRuntimeStatus.FAILED):
                return TaskRuntimeEventType.TASK_FAILED
            if status.startswith(TaskRuntimeStatus.CANCELLED):
                return TaskRuntimeEventType.TASK_CANCELLED
            return "task_status_changed"

        def _apply_runtime_event(self, event: TaskRuntimeEvent) -> bool:
            idx = event.task_index
            if idx is None or not (0 <= idx < len(self.state)):
                return False
            if event.start is not None:
                self.state[idx]["start"] = event.start
            if event.end is not None:
                self.state[idx]["end"] = event.end
            if event.status is not None:
                self.state[idx]["status"] = event.status
                if event.status.startswith(TaskRuntimeStatus.DONE):
                    self.state[idx]["progress"] = 100
                elif event.status in (TaskRuntimeStatus.QUEUED, TaskRuntimeStatus.READY, TaskRuntimeStatus.CANCELLED, TaskRuntimeStatus.CANCELLED_QUEUE):
                    self.state[idx]["progress"] = 0
            if event.progress is not None:
                self.state[idx]["progress"] = event.progress
            return True

        def _drain_runtime_events(self) -> None:
            changed = False
            try:
                while True:
                    message = self.log_events.get_nowait()
                    if self.log_view:
                        self.log_view.append(message)
            except queue.Empty:
                pass
            try:
                while True:
                    event = self.ui_events.get_nowait()
                    changed = self._apply_runtime_event(event) or changed
                    if event.event_type == TaskRuntimeEventType.QUEUE_COMPLETED:
                        self.save_queue_log(auto=True)
            except queue.Empty:
                pass
            self._update_session_runtime()
            if changed:
                self.refresh_queue_view()
            else:
                self._update_status_bar()

        def _on_selection_changed(self) -> None:
            self._update_inspector()
            self._update_command_preview()
            self._update_status_bar()

        def _validation_for_index(self, idx: Optional[int]) -> Optional[TaskValidationResult]:
            if idx is None or not (0 <= idx < len(self.settings.tasks)):
                return None
            if idx < len(self.validation_results) and self.validation_results[idx] is not None:
                return self.validation_results[idx]
            return basic_task_validation(self.settings.tasks[idx])

        def _validate_loaded_queue_tasks(self) -> None:
            self.validation_results = [validate_task_paths(task) for task in self.settings.tasks]
            summary = summarize_validation_results(self.validation_results)
            if summary:
                self._append_log(f"[Validation] Queue validation: {summary}")
            for idx, result in enumerate(self.validation_results, start=1):
                if result.status in ("Invalid", "Incomplete"):
                    self._append_log(f"[Validation] Task {idx} {result.status}: {result.message}")
                    for detail in result.details:
                        self._append_log(f"[Validation]   {detail}")

        def validate_queue_tasks(self) -> None:
            """Run local path validation for all current queue tasks on demand."""
            if not self.settings.tasks:
                QMessageBox.information(self, "Validate", "No tasks to validate.")
                return
            self._validate_loaded_queue_tasks()
            self.refresh_queue_view()
            self._update_inspector()
            self._update_status_bar()
            self._append_log("[Validation] Manual validation completed.")
            self.statusBar().showMessage("Validation completed.", 5000)

        def _validation_for_enqueue(self, task: RenderTask) -> TaskValidationResult:
            """Validate a task before it can enter the runtime queue."""
            idx = self._find_task_index_by_identity(task)
            if idx is None:
                return validate_task_paths(task)
            result = self._validation_for_index(idx)
            if result is None or result.status == "Not checked":
                result = validate_task_paths(task)
                self.validation_results[idx] = result
            return result

        def _filter_tasks_by_loaded_validation(self, tasks: List[RenderTask]) -> List[RenderTask]:
            eligible = []
            skipped = 0
            validation_changed = False
            for task in tasks:
                idx = self._find_task_index_by_identity(task)
                before = self.validation_results[idx] if idx is not None and idx < len(self.validation_results) else None
                result = self._validation_for_enqueue(task)
                if before is None or before.status == "Not checked":
                    validation_changed = True
                if result is not None and result.is_blocking:
                    skipped += 1
                    self._append_log(f"[Validation] Skipped {soft_name(task.sequence)}: {result.status} - {result.message}")
                    for detail in result.details:
                        self._append_log(f"[Validation]   {detail}")
                    continue
                if result is not None and result.status == "Unknown":
                    self._append_log(f"[Validation] Warning for {soft_name(task.sequence)}: {result.message}")
                eligible.append(task)
            if skipped:
                self._append_log(f"[Validation] Skipped {skipped} task(s) because validation is blocking.")
            if validation_changed:
                self._append_log("[Validation] Queue candidates were validated before enqueue.")
            return eligible

        def _task_can_enter_session_queue(self, idx: int) -> bool:
            """Return True when a task is allowed to become Ready/Queued."""
            if not (0 <= idx < len(self.settings.tasks)):
                return False
            task = self.settings.tasks[idx]
            result = self._validation_for_enqueue(task)
            if result.is_blocking:
                self._append_log(f"[Validation] Cannot add {soft_name(task.sequence)} to queue: {result.status} - {result.message}")
                for detail in result.details:
                    self._append_log(f"[Validation]   {detail}")
                self.statusBar().showMessage("Task was not added: validation is blocking.", 5000)
                return False
            if result.status == "Unknown":
                self._append_log(f"[Validation] Warning for {soft_name(task.sequence)}: {result.message}")
            return True

        def fix_project_path_for_queue(self) -> None:
            if not self.settings.tasks:
                QMessageBox.information(self, "Fix Project Path", "Load a queue first.")
                return
            new_project, _ = QFileDialog.getOpenFileName(self, "Select replacement .uproject", "", "Unreal Project (*.uproject);;All Files (*.*)")
            if not new_project:
                return
            if not os.path.isfile(new_project) or not new_project.lower().endswith(".uproject"):
                QMessageBox.critical(self, "Fix Project Path", "Select a valid .uproject file.")
                return

            selected = self._selected_task()
            source = selected or (self.settings.tasks[0] if self.settings.tasks else None)
            target_name = os.path.basename(source.uproject) if source and source.uproject else ""
            candidates = [task for task in self.settings.tasks if not target_name or os.path.basename(task.uproject) == target_name]
            if not candidates:
                candidates = list(self.settings.tasks)
            reply = QMessageBox.question(self, "Fix Project Path", f"Update project path for {len(candidates)} task(s)?")
            if reply != QMessageBox.Yes:
                return

            normalized = new_project.replace("\\", "/")
            for task in candidates:
                task.uproject = normalized
            self._validate_loaded_queue_tasks()
            self.refresh_queue_view()
            self._append_log(f"[Validation] Project path relinked for {len(candidates)} task(s): {normalized}")

        def _update_inspector(self) -> None:
            task = self._selected_task()
            if task is None:
                values = {
                    "job": "Job Name: No selection", "enabled": "Enabled: -", "project": "Project: -",
                    "level": "Level: -", "sequence": "Sequence: -", "preset": "Preset: -",
                    "validation": "Validation: -",
                }
            else:
                idx = self._find_task_index_by_identity(task)
                validation = self._validation_for_index(idx)
                values = {
                    "job": f"Job Name: {soft_name(task.sequence)}", "enabled": f"Enabled: {'Yes' if task.enabled else 'No'}",
                    "project": f"Project: {task.uproject or '-'}",
                    "level": f"Level: {soft_object_to_editor_path(task.level) or '-'}",
                    "sequence": f"Sequence: {soft_object_to_editor_path(task.sequence) or '-'}",
                    "preset": f"Preset: {soft_object_to_editor_path(task.preset) or '-'}",
                    "validation": validation.display_text if validation else "Validation: -",
                }
            for key, value in values.items():
                label = self.inspector_labels.get(key)
                if label:
                    label.setText(value)

        def _build_command_preview_for_task(self, task: RenderTask) -> str:
            try:
                return build_unreal_command_preview(self.settings, task)
            except ValueError as exc:
                return f"Command preview error: invalid Extra CLI.\n{exc}"

        def _build_command_for_task(self, task: RenderTask) -> List[str]:
            return build_unreal_command(self.settings, task)

        def _update_command_preview(self) -> None:
            if not self.command_preview:
                return
            self._sync_option_controls_to_settings()
            task = self._selected_task()
            if task is None and self._current_global_idx is not None and 0 <= self._current_global_idx < len(self.settings.tasks):
                task = self.settings.tasks[self._current_global_idx]
            self.command_preview.setPlainText(
                self._build_command_preview_for_task(task)
                if task else "Select a task to inspect the generated command line."
            )

        def _update_status_bar(self) -> None:
            queued = sum(1 for state in self.state if state.get("status") == TaskRuntimeStatus.QUEUED)
            running = sum(1 for state in self.state if state.get("status", "").startswith(TaskRuntimeStatus.RENDERING))
            failed = sum(1 for state in self.state if state.get("status", "").startswith((TaskRuntimeStatus.FAILED, TaskRuntimeStatus.CANCELLED)))
            done = sum(1 for state in self.state if state.get("status", "").startswith(TaskRuntimeStatus.DONE))
            enabled = sum(1 for task in self.settings.tasks if task.enabled)
            ordered = len(self._ordered_task_indices())
            self.statusBar().showMessage(f"Tasks: {len(self.settings.tasks)} | Enabled: {enabled} | Ordered: {ordered} | Queued: {queued} | Running: {running} | Done: {done} | Failed: {failed}")
            current_idx = self._current_global_idx if self._current_global_idx is not None else (self.selected_indices()[0] if self.selected_indices() else None)
            if current_idx is not None and 0 <= current_idx < len(self.settings.tasks):
                task = self.settings.tasks[current_idx]
                state = self.state[current_idx]
                current_task_text = f"Current task: {soft_name(task.sequence)}"
                current_status_text = f"Status: {state.get('status', 'Ready')}"
            else:
                current_task_text = "Current task: Idle"
                current_status_text = "Status: Idle"

            self.current_task_label.setText(current_task_text)
            self.current_status_label.setText(current_status_text)
            if self.minimal_current_task_label:
                self.minimal_current_task_label.setText(current_task_text)
            if self.minimal_current_status_label:
                self.minimal_current_status_label.setText(current_status_text)

        def _update_session_runtime(self) -> None:
            for idx, state in enumerate(self.state):
                if state.get("start") and not state.get("end") and state.get("status", "").startswith(TaskRuntimeStatus.RENDERING):
                    # Keep the running-time column live without waiting for another render event.
                    pass
            total = 0
            for state in self.state:
                start = state.get("start")
                end = state.get("end")
                if start and end:
                    total += max(0, int(end - start))
            if self._current_global_idx is not None and 0 <= self._current_global_idx < len(self.state):
                state = self.state[self._current_global_idx]
                if state.get("start") and self.process_controller.is_active():
                    total += max(0, int(time.time() - state["start"]))
            h, rem = divmod(total, 3600)
            m, s = divmod(rem, 60)
            session_text = f"Session total: {h:02d}:{m:02d}:{s:02d}"
            self.session_time_label.setText(session_text)
            if self.minimal_session_time_label:
                self.minimal_session_time_label.setText(session_text)

        def _save_user_settings(self) -> None:
            try:
                UserSettingsRepository.save(self.user_settings)
            except Exception as exc:
                self._append_log(f"[Recent] Failed to save user settings: {exc}")

        def _on_user_settings_changed(self, checked: bool = False) -> None:
            self.user_settings["auto_load_last_queue"] = bool(checked)
            self._save_user_settings()

        def _register_recent_queue(self, path: str) -> None:
            self.user_settings = UserSettingsRepository.register_queue(self.user_settings, path)
            self.current_queue_path = self.user_settings.get("last_queue", "")
            self._save_user_settings()

        def show_recent_queue_menu(self) -> None:
            menu = QMenu(self)
            recent = UserSettingsRepository._normalize_recent(self.user_settings.get("recent_queues", []))
            if recent:
                for path in recent:
                    label = os.path.basename(path) or path
                    if os.path.normcase(path) == os.path.normcase(self.current_queue_path or ""):
                        label = f"✓ {label}"
                    action = menu.addAction(label)
                    action.setToolTip(path)
                    action.triggered.connect(lambda _checked=False, p=path: self.load_queue_path(p))
                menu.addSeparator()
            else:
                empty_action = menu.addAction("No recent queues")
                empty_action.setEnabled(False)
                menu.addSeparator()
            menu.addAction("Open Queue File...", self.load_queue_dialog)
            auto_load_action = menu.addAction("Auto Load Last Queue")
            auto_load_action.setCheckable(True)
            auto_load_action.setChecked(bool(self.user_settings.get("auto_load_last_queue", False)))
            auto_load_action.toggled.connect(self._on_user_settings_changed)
            menu.addAction("Clear Recent Queues", self.clear_recent_queues)
            menu.exec(self.cursor().pos())

        def clear_recent_queues(self) -> None:
            self.user_settings = UserSettingsRepository.clear_recent(self.user_settings)
            self.current_queue_path = ""
            self._save_user_settings()
            self._append_log("[Recent] Recent queue list cleared.")

        def _auto_load_last_queue_if_enabled(self) -> None:
            if not bool(self.user_settings.get("auto_load_last_queue", False)):
                return
            path = self.user_settings.get("last_queue", "")
            if path:
                self.load_queue_path(path, silent=True)

        def load_queue_path(self, path: str, silent: bool = False) -> bool:
            if not os.path.isfile(path):
                msg = f"Queue file not found: {path}"
                if silent:
                    self._append_log(f"[Recent] {msg}")
                else:
                    QMessageBox.critical(self, "Load Queue", msg)
                return False
            try:
                config, tasks = PersistenceRepository.load_queue(path, self.settings)
            except PersistenceError as exc:
                if silent:
                    self._append_log(f"[Recent] Failed to load last queue: {exc}")
                else:
                    QMessageBox.critical(self, "Load Queue", str(exc))
                return False
            for key, value in config.items():
                if hasattr(self.settings, key):
                    setattr(self.settings, key, value)
            self._apply_settings_to_option_controls()
            self.settings.tasks = tasks
            self.state = [default_task_state() for _ in self.settings.tasks]
            self._validate_loaded_queue_tasks()
            self.runtime_queue.clear_pending(TaskRuntimeStatus.CANCELLED_QUEUE)
            self._rebuild_order_for_enabled_tasks()
            self.refresh_queue_view()
            self._register_recent_queue(path)
            self._append_log(f"[Recent] Loaded queue: {path}")
            return True

        def load_queue_dialog(self) -> None:
            path, _ = QFileDialog.getOpenFileName(self, "Load Queue", "", "JSON (*.json);;All Files (*.*)")
            if path:
                self.load_queue_path(path)

        def save_queue_dialog(self) -> None:
            path, _ = QFileDialog.getSaveFileName(self, "Save Queue", "", "JSON (*.json);;All Files (*.*)")
            if not path:
                return
            if not path.lower().endswith(".json"):
                path += ".json"
            self._sync_option_controls_to_settings()
            config = {key: getattr(self.settings, key) for key in PersistenceRepository.QUEUE_CONFIG_FIELDS}
            try:
                PersistenceRepository.save_queue(path, config, self.settings.tasks)
            except Exception as exc:
                QMessageBox.critical(self, "Save Queue", str(exc))
                return
            self._register_recent_queue(path)
            self._append_log(f"[Qt] Saved queue: {path}")

        def _default_task_filename(self, task: RenderTask) -> str:
            return f"{soft_name(task.level)}__{soft_name(task.sequence)}__{soft_name(task.preset)}.task.json"

        def add_task_dialog(self) -> None:
            dialog = QtTaskEditor(self)
            if dialog.exec() != QDialog.Accepted or dialog.result is None:
                return
            self.settings.tasks.append(dialog.result)
            self.state.append(default_task_state())
            self.validation_results.append(None)
            self.refresh_queue_view()
            self._select_task_index(len(self.settings.tasks) - 1)
            self._append_log("[Qt] Added task.")

        def load_task_dialog(self) -> None:
            paths, _ = QFileDialog.getOpenFileNames(self, "Load Task JSON(s)", "", "JSON (*.json);;All Files (*.*)")
            if not paths:
                return
            loaded = 0
            for path in paths:
                try:
                    tasks = PersistenceRepository.load_task_file(path)
                except PersistenceError as exc:
                    QMessageBox.critical(self, "Load Task", f"{os.path.basename(path)}: {exc}")
                    continue
                self.settings.tasks.extend(tasks)
                self.validation_results.extend([None for _ in tasks])
                loaded += len(tasks)
            self._ensure_state()
            self.refresh_queue_view()
            self._append_log(f"[Qt] Loaded {loaded} task(s).")

        def save_selected_tasks_dialog(self) -> None:
            indices = self.selected_indices()
            if not indices:
                QMessageBox.warning(self, "Save Task", "Select at least one task in the table.")
                return
            if len(indices) == 1:
                task = self.settings.tasks[indices[0]]
                path, _ = QFileDialog.getSaveFileName(
                    self,
                    "Save Task JSON",
                    self._default_task_filename(task),
                    "JSON (*.json);;All Files (*.*)",
                )
                if not path:
                    return
                if not path.lower().endswith(".json"):
                    path += ".json"
                try:
                    PersistenceRepository.save_task(path, task)
                except Exception as exc:
                    QMessageBox.critical(self, "Save Task", str(exc))
                    return
                self._append_log(f"[Qt] Saved task: {os.path.basename(path)}")
                return

            folder = QFileDialog.getExistingDirectory(self, "Select folder to save tasks")
            if not folder:
                return
            saved = 0
            for idx in indices:
                task = self.settings.tasks[idx]
                path = os.path.join(folder, self._default_task_filename(task))
                try:
                    PersistenceRepository.save_task(path, task)
                    saved += 1
                except Exception as exc:
                    QMessageBox.critical(self, "Save Task", f"{os.path.basename(path)}: {exc}")
                    return
            self._append_log(f"[Qt] Saved {saved} task file(s) to {folder}")

        def duplicate_selected(self) -> None:
            for idx in sorted(self.selected_indices(), reverse=True):
                clone_data = asdict(self.settings.tasks[idx])
                clone_data["added_at"] = current_task_timestamp()
                self.settings.tasks.insert(idx + 1, RenderTask(**clone_data))
                self.state.insert(idx + 1, default_task_state())
                self.validation_results.insert(idx + 1, None)
            self.refresh_queue_view()

        def remove_selected(self) -> None:
            indices = self.selected_indices()
            if not indices:
                return
            removed_tasks = [self.settings.tasks[idx] for idx in indices]
            self.runtime_queue.remove_tasks(removed_tasks)
            for task in removed_tasks:
                self.queue_order_by_task_id.pop(id(task), None)
            for idx in sorted(indices, reverse=True):
                del self.settings.tasks[idx]
                del self.state[idx]
                if idx < len(self.validation_results):
                    del self.validation_results[idx]
            self.refresh_queue_view()

        def move_selected(self, delta: int) -> None:
            indices = self.selected_indices()
            if len(indices) != 1:
                return
            idx = indices[0]
            new_idx = idx + delta
            if not (0 <= new_idx < len(self.settings.tasks)):
                return
            self.settings.tasks[idx], self.settings.tasks[new_idx] = self.settings.tasks[new_idx], self.settings.tasks[idx]
            self.state[idx], self.state[new_idx] = self.state[new_idx], self.state[idx]
            self.validation_results[idx], self.validation_results[new_idx] = self.validation_results[new_idx], self.validation_results[idx]
            self.refresh_queue_view()
            self._select_task_index(new_idx)

        def _select_task_index(self, task_index: int) -> None:
            for row in range(self.table.rowCount()):
                item = self.table.item(row, 0)
                if item and item.data(Qt.UserRole) == task_index:
                    self.table.selectRow(row)
                    break

        def _on_table_double_clicked(self, index=None) -> None:
            task_index = self._task_index_for_table_row(index.row()) if index is not None and index.isValid() else None
            if task_index is None:
                self.toggle_selected()
                return
            self.toggle_task_indices([task_index])

        def eventFilter(self, source, event) -> bool:
            if source is self.table and event.type() == QEvent.KeyPress and event.key() == Qt.Key_Space:
                self.toggle_selected()
                return True
            return super().eventFilter(source, event)

        def toggle_task_indices(self, indices: List[int]) -> None:
            """Toggle selected tasks between Disabled and Ready with session queue order."""
            unique_indices = [idx for idx in sorted(set(indices)) if 0 <= idx < len(self.settings.tasks)]
            if not unique_indices:
                return

            disabled_tasks = []
            enabled_count = 0
            disabled_count = 0
            changed = False
            self._ensure_state()

            for idx in unique_indices:
                state = self.state[idx] if idx < len(self.state) else default_task_state()
                if state.get("status", "Ready").startswith(TaskRuntimeStatus.RENDERING):
                    continue

                task = self.settings.tasks[idx]
                task_id = id(task)
                has_order = task_id in self.queue_order_by_task_id
                is_active = bool(task.enabled) or has_order

                if is_active:
                    task.enabled = False
                    self.queue_order_by_task_id.pop(task_id, None)
                    disabled_tasks.append(task)
                    disabled_count += 1
                else:
                    if not self._task_can_enter_session_queue(idx):
                        continue
                    task.enabled = True
                    self.queue_order_by_task_id[task_id] = self._next_queue_order()
                    enabled_count += 1

                self.state[idx] = default_task_state()
                changed = True

            if disabled_tasks:
                self.runtime_queue.remove_tasks(disabled_tasks)
            if changed:
                self._compact_queue_order()
                self.refresh_queue_view()
                self._append_log(f"[Qt] Toggle selected: enabled {enabled_count}, disabled {disabled_count} task(s).")

        def toggle_selected(self) -> None:
            self.toggle_task_indices(self.selected_indices())

        def toggle_all_ready_disabled(self) -> None:
            """Toggle all non-rendering tasks between Disabled and ordered Ready state."""
            if not self.settings.tasks:
                return
            if self.worker_running or self.process_controller.is_active():
                QMessageBox.information(self, "Toggle All", "Stop the active render before rebuilding the queue order.")
                return

            self._ensure_state()
            toggleable_indices = []
            for idx, _task in enumerate(self.settings.tasks):
                state = self.state[idx] if idx < len(self.state) else default_task_state()
                if state.get("status", "Ready").startswith(TaskRuntimeStatus.RENDERING):
                    continue
                toggleable_indices.append(idx)

            if not toggleable_indices:
                return

            # If every toggleable task is already active and ordered, the next
            # Toggle All press disables all of them. Otherwise it rebuilds the
            # session-only render order from the current list order.
            all_active_and_ordered = all(
                self.settings.tasks[idx].enabled
                and id(self.settings.tasks[idx]) in self.queue_order_by_task_id
                for idx in toggleable_indices
            )

            if all_active_and_ordered:
                disabled_tasks = []
                for idx in toggleable_indices:
                    task = self.settings.tasks[idx]
                    task.enabled = False
                    self.state[idx] = default_task_state()
                    disabled_tasks.append(task)
                self.queue_order_by_task_id.clear()
                self.runtime_queue.remove_tasks(disabled_tasks)
                self.refresh_queue_view()
                self._append_log(f"[Qt] Toggle All disabled {len(disabled_tasks)} task(s) and cleared session order.")
                return

            self.runtime_queue.clear_pending(TaskRuntimeStatus.CANCELLED_QUEUE)
            self.queue_order_by_task_id.clear()
            order = 1
            skipped = 0
            for idx in toggleable_indices:
                task = self.settings.tasks[idx]
                if not self._task_can_enter_session_queue(idx):
                    skipped += 1
                    continue
                task.enabled = True
                self.state[idx] = default_task_state()
                self.queue_order_by_task_id[id(task)] = order
                order += 1
            self.refresh_queue_view()
            self._append_log(f"[Qt] Toggle All enabled and ordered {order - 1} task(s).")
            if skipped:
                self._append_log(f"[Validation] Toggle All skipped {skipped} task(s) because validation is blocking.")

        def render_selected(self) -> None:
            tasks = self._collect(only_selected=True)
            if not tasks:
                QMessageBox.information(self, "Render Selected", "Select at least one task in the table.")
                return
            self._run_queue(tasks)

        def render_enabled(self) -> None:
            tasks = self._collect(only_enabled=True)
            if not tasks:
                QMessageBox.information(self, "Render Enabled", "No enabled tasks to run.")
                return
            self._run_queue(tasks)

        def render_all(self) -> None:
            self._run_queue(self._collect())

        def append_selected_to_render_queue(self) -> None:
            tasks = self._collect(only_selected=True)
            if not tasks:
                QMessageBox.information(self, "Append Selected", "Select at least one task in the table.")
                return
            if not (self.worker_running or self.process_controller.is_active()):
                QMessageBox.information(self, "Append Selected", "Start a render first, then append selected tasks to the active queue.")
                return
            self._enqueue_tasks(tasks)

        def queue_selected_or_enabled(self) -> None:
            tasks = self._collect(only_selected=True)
            if not tasks:
                tasks = self._collect(only_enabled=True)
            if not tasks:
                QMessageBox.information(self, "Queue Selected", "Nothing to enqueue: select tasks or enable some tasks.")
                return
            self._run_queue(tasks)

        def _enqueue_tasks(self, tasks: List[RenderTask]) -> bool:
            tasks = self._filter_tasks_by_loaded_validation(tasks)
            changed = self.runtime_queue.enqueue_tasks(tasks, mark_queued=True, log_prefix="[Qt] Queued ")
            if changed or not tasks:
                self.refresh_queue_view()
                self._update_inspector()
                self._update_status_bar()
            return changed

        def _run_queue(self, tasks: List[RenderTask]) -> None:
            if not self._validate_current_render_options():
                return
            if not self.settings.ue_cmd or not os.path.exists(self.settings.ue_cmd):
                QMessageBox.critical(self, "Render", "Specify a valid path to UnrealEditor-Cmd.exe.")
                return
            if tasks:
                self._enqueue_tasks(tasks)
            if self.worker_running:
                return
            if self.runtime_queue.empty():
                QMessageBox.information(self, "Render", "No valid tasks to render. Check the Validate column and Job Inspector details.")
                return
            self.stop_all = False
            self.cancel_current_requested = False
            threading.Thread(target=self._worker_loop, daemon=True).start()

        def _worker_loop(self) -> None:
            self.worker_running = True
            retries = int(self.settings.retries)
            policy = self.settings.fail_policy
            skip_next_pending = 0
            local_counter = 0
            self._append_log("[Qt] Queue worker started.")
            while True:
                if self.stop_all and self.runtime_queue.empty():
                    break
                try:
                    task = self.runtime_queue.get(timeout=0.5)
                except queue.Empty:
                    if self.runtime_queue.empty():
                        break
                    continue
                local_counter += 1
                if self.stop_all:
                    break
                task_index = self._find_task_index_by_identity(task)
                if task_index is None:
                    continue
                if skip_next_pending > 0:
                    skip_next_pending -= 1
                    self._set_status_from_core(task_index, TaskRuntimeStatus.SKIPPED_POLICY)
                    self._append_log(f"[Qt] [{local_counter}] Skipped by fail policy.")
                    continue
                if not all([task.uproject, task.level, task.sequence, task.preset]):
                    self._append_log(f"[Qt] [{local_counter}] Skipped incomplete task.")
                    continue

                attempt = 0
                while attempt <= retries and not self.stop_all:
                    attempt += 1
                    try:
                        cmd = self._build_command_for_task(task)
                    except ValueError as exc:
                        self.ui_events.put(TaskRuntimeEvent(TaskRuntimeEventType.TASK_FAILED, task_index, "Failed (invalid Extra CLI)", 0, end=time.time()))
                        self._append_log(f"[Qt] [{local_counter}] Invalid Extra CLI: {exc}")
                        break
                    start_time = time.time()
                    start_dt = datetime.now()
                    logfile = self._task_logfile(task)
                    log_fp = None
                    self._current_global_idx = task_index
                    self.ui_events.put(TaskRuntimeEvent(TaskRuntimeEventType.TASK_STARTED, task_index, "Rendering 00:00:00", 0, start_time))
                    self._append_log(f"[Qt] [{local_counter}] Start try {attempt}/{retries + 1}: {' '.join(cmd)}")
                    try:
                        log_fp = open(logfile, "a", encoding="utf-8")
                        log_fp.write(f"CMD: {' '.join(cmd)}\n")
                        log_fp.write(f"START: {start_dt.strftime('%Y-%m-%d %H:%M:%S')}\n")
                        process = self.process_controller.launch(cmd)
                    except Exception as exc:
                        if log_fp:
                            try:
                                log_fp.write(f"FAILED TO START: {exc}\n")
                                log_fp.close()
                            except Exception:
                                pass
                        self._append_log(f"[Qt] [{local_counter}] Failed to start: {exc}")
                        break

                    def pump_stdout(proc: subprocess.Popen, idx: int, fp) -> None:
                        try:
                            if proc.stdout:
                                for line in proc.stdout:
                                    if self.stop_all:
                                        break
                                    clean_line = line.rstrip()
                                    self._append_log(clean_line)
                                    if fp:
                                        fp.write(clean_line + "\n")
                                    progress = self._extract_progress(clean_line)
                                    if progress is not None:
                                        self.ui_events.put(TaskRuntimeEvent(TaskRuntimeEventType.PROGRESS_UPDATED, idx, progress=progress))
                        except Exception as exc:
                            self._append_log(f"[Qt pump] {exc}")
                        finally:
                            if fp:
                                try:
                                    fp.flush()
                                except Exception:
                                    pass

                    pump_thread = threading.Thread(target=pump_stdout, args=(process, task_index, log_fp), daemon=True)
                    pump_thread.start()
                    while process.poll() is None and not self.stop_all:
                        elapsed = int(time.time() - start_time)
                        h, rem = divmod(elapsed, 3600)
                        m, s = divmod(rem, 60)
                        self.ui_events.put(TaskRuntimeEvent(TaskRuntimeEventType.TASK_STARTED, task_index, f"Rendering {h:02d}:{m:02d}:{s:02d}"))
                        time.sleep(1.0)
                    rc = process.wait()
                    self.process_controller.clear_if_current(process)
                    end_time = time.time()
                    end_dt = datetime.now()
                    try:
                        pump_thread.join(timeout=0.2)
                    except Exception:
                        pass
                    if log_fp:
                        try:
                            log_fp.write(f"END: {end_dt.strftime('%Y-%m-%d %H:%M:%S')}\n")
                            log_fp.write(f"EXIT: {rc}\n")
                            log_fp.close()
                        except Exception:
                            pass
                    self.ui_events.put(TaskRuntimeEvent(TaskRuntimeEventType.PROGRESS_UPDATED, task_index, end=end_time))
                    self._append_log(f"[Qt] [{local_counter}] Exit code: {rc}")

                    if self.cancel_current_requested:
                        self.cancel_current_requested = False
                        self.ui_events.put(TaskRuntimeEvent(TaskRuntimeEventType.TASK_CANCELLED, task_index, TaskRuntimeStatus.CANCELLED, 0, end=end_time))
                        break
                    if rc == 0:
                        duration = format_duration_hms(end_time - start_time)
                        self.ui_events.put(TaskRuntimeEvent(TaskRuntimeEventType.TASK_FINISHED, task_index, f"Done ({duration})", 100, end=end_time))
                        break
                    if policy == "stop_queue":
                        self.ui_events.put(TaskRuntimeEvent(TaskRuntimeEventType.TASK_FAILED, task_index, f"Failed (rc={rc})", end=end_time))
                        self.stop_all = True
                        break
                    if attempt <= retries:
                        self._append_log(f"[Qt] [{local_counter}] Will retry.")
                    else:
                        self.ui_events.put(TaskRuntimeEvent(TaskRuntimeEventType.TASK_FAILED, task_index, f"Failed (rc={rc})", end=end_time))
                        if policy == "skip_next":
                            skip_next_pending = 1
                        break
                if self.stop_all:
                    break
            if self.stop_all:
                self.runtime_queue.clear_pending(TaskRuntimeStatus.CANCELLED_QUEUE)
            self._current_global_idx = None
            self.worker_running = False
            self.ui_events.put(TaskRuntimeEvent(TaskRuntimeEventType.QUEUE_COMPLETED))
            self._append_log("[Qt] Queue complete.")

        def _logs_dir(self) -> str:
            return os.path.join(os.getcwd(), "mrq_logs")

        def _task_logfile(self, task: RenderTask) -> str:
            base = f"{soft_name(task.level)}__{soft_name(task.sequence)}__{soft_name(task.preset)}"
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            logs_dir = self._logs_dir()
            os.makedirs(logs_dir, exist_ok=True)
            return os.path.join(logs_dir, f"{timestamp}_{base}.log")

        def _queue_log_default_path(self) -> str:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            logs_dir = self._logs_dir()
            os.makedirs(logs_dir, exist_ok=True)
            return os.path.join(logs_dir, f"Queue_Log_{timestamp}.log")

        def _format_hms(self, seconds: Optional[int]) -> str:
            if seconds is None:
                return ""
            seconds = max(0, int(seconds))
            hours, rem = divmod(seconds, 3600)
            minutes, secs = divmod(rem, 60)
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"

        def _collect_queue_log_rows(self) -> List[str]:
            """Build queue log rows from the same data shown in Minimal Mode."""
            rows = ["Order / Status / Level / Sequence / Preset / Running Time / Start / End"]
            ordered_indices = self._ordered_task_indices()
            if not ordered_indices:
                ordered_indices = list(range(len(self.settings.tasks)))
            ordered_indices = sorted(
                ordered_indices,
                key=lambda idx: self._queue_order_for_task(self.settings.tasks[idx]) or idx + 1,
            )
            for idx in ordered_indices:
                task = self.settings.tasks[idx]
                state = self.state[idx] if idx < len(self.state) else default_task_state()
                order = self._queue_order_for_task(task) or idx + 1
                rows.append(
                    " / ".join([
                        str(order),
                        get_queue_log_status(state.get("status", "Ready"), task.enabled),
                        soft_name(task.level),
                        soft_name(task.sequence),
                        soft_name(task.preset),
                        format_runtime_display(state),
                        format_state_time_display(state.get("start")),
                        format_state_time_display(state.get("end")),
                    ])
                )
            return rows

        def save_queue_log(self, auto: bool = False) -> Optional[str]:
            try:
                path = self._queue_log_default_path()
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("\n".join(self._collect_queue_log_rows()) + "\n")
                label = "auto-saved" if auto else "saved"
                self._append_log(f"[Qt Logs] Queue summary {label}: {os.path.basename(path)}")
                return path
            except Exception as exc:
                if auto:
                    self._append_log(f"[Qt Logs] Failed to auto-save queue summary: {exc}")
                else:
                    QMessageBox.critical(self, "Save Queue Log", str(exc))
                return None

        def open_last_queue_log(self) -> None:
            """Open the queue log browser and select the newest saved queue log."""
            logs_dir = self._logs_dir()
            os.makedirs(logs_dir, exist_ok=True)
            try:
                if self.queue_log_viewer is None or not self.queue_log_viewer.isVisible():
                    self.queue_log_viewer = QtQueueLogViewer(self, logs_dir)
                else:
                    self.queue_log_viewer.refresh_logs()
                self.queue_log_viewer.show()
                self.queue_log_viewer.raise_()
                self.queue_log_viewer.activateWindow()
            except Exception as exc:
                QMessageBox.critical(self, "Queue Logs", str(exc))

        def _open_path(self, path: str) -> None:
            try:
                if os.name == "nt":
                    os.startfile(path)
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", path])
                else:
                    subprocess.Popen(["xdg-open", path])
            except Exception as exc:
                QMessageBox.critical(self, "Open Path", str(exc))

        def open_logs_folder(self) -> None:
            logs_dir = self._logs_dir()
            os.makedirs(logs_dir, exist_ok=True)
            self._open_path(logs_dir)

        def open_last_log_for_selected(self) -> None:
            task = self._selected_task()
            if task is None:
                QMessageBox.information(self, "Open Last Log", "Select a task first.")
                return
            logs_dir = self._logs_dir()
            base = f"{soft_name(task.level)}__{soft_name(task.sequence)}__{soft_name(task.preset)}"
            try:
                files = [
                    name for name in os.listdir(logs_dir)
                    if name.endswith(".log") and name.endswith(f"{base}.log")
                ]
            except FileNotFoundError:
                files = []
            except Exception as exc:
                QMessageBox.critical(self, "Open Last Log", str(exc))
                return
            if not files:
                QMessageBox.information(self, "Open Last Log", "No logs found for selected task.")
                return
            files.sort(reverse=True)
            self._open_path(os.path.join(logs_dir, files[0]))

        def clear_status_selected(self) -> None:
            indices = self.selected_indices()
            if not indices:
                QMessageBox.information(self, "Clear Status", "Select at least one task to clear its status.")
                return
            for idx in indices:
                if 0 <= idx < len(self.state):
                    self.state[idx] = default_task_state()
            self.refresh_queue_view()
            self._append_log(f"[Qt Status] Cleared status for {len(indices)} task(s).")

        def cancel_current(self) -> None:
            if self.process_controller.is_active():
                self.cancel_current_requested = True
                self.process_controller.stop_current(int(self.settings.kill_timeout_s))
            else:
                self._append_log("[Qt] No running process.")

        def cancel_all(self) -> None:
            self.stop_all = True
            self.cancel_current()
            self.runtime_queue.clear_pending(TaskRuntimeStatus.CANCELLED_QUEUE)
            self.refresh_queue_view()
            self._append_log("[Qt] Stop-all requested.")

        def copy_command_preview(self) -> None:
            if self.command_preview:
                QApplication.clipboard().setText(self.command_preview.toPlainText())
                self._append_log("[Qt] Command preview copied to clipboard.")

        def browse_unreal_cmd(self) -> None:
            path, _ = QFileDialog.getOpenFileName(
                self,
                "Select UnrealEditor-Cmd.exe",
                "",
                "UnrealEditor-Cmd (UnrealEditor-Cmd.exe);;Executable (*.exe);;All Files (*.*)",
            )
            if path and self.ue_path_edit:
                self.ue_path_edit.setText(path)

        def _on_ue_path_changed(self, value: str) -> None:
            self.settings.ue_cmd = value.strip()
            self._update_command_preview()

        def edit_selected_task(self) -> None:
            indices = self.selected_indices()
            if not indices:
                QMessageBox.information(self, "Edit", "Select one task to edit.")
                return
            idx = indices[0]
            if not (0 <= idx < len(self.settings.tasks)):
                return
            old_task = self.settings.tasks[idx]
            dialog = QtTaskEditor(self, old_task)
            if dialog.exec() != QDialog.Accepted or dialog.result is None:
                return
            previous_order = self.queue_order_by_task_id.pop(id(old_task), None)
            self.runtime_queue.remove_tasks([old_task])
            self.settings.tasks[idx] = dialog.result
            if idx < len(self.validation_results):
                self.validation_results[idx] = None
            if previous_order is not None:
                self.queue_order_by_task_id[id(dialog.result)] = previous_order
                dialog.result.enabled = True
            self.state[idx] = default_task_state()
            self.refresh_queue_view()
            self._select_task_index(idx)
            self._append_log("[Qt] Edited selected task.")


        def _extract_progress(self, line: str) -> Optional[int]:
            if "%" in line:
                i = line.find("%")
                j = i - 1
                while j >= 0 and line[j].isdigit():
                    j -= 1
                digits = line[j + 1:i]
                if digits.isdigit():
                    value = int(digits)
                    if 0 <= value <= 100:
                        return value
            return None

    app = QApplication.instance() or QApplication(sys.argv)
    icon_path = app_icon_path()
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
    apply_qt_dark_theme(app)
    window = QtMRQShell()
    window.show()
    return app.exec()

# -------------------------------------------------
# Entrypoint
# -------------------------------------------------

if __name__ == "__main__":
    if "--tk" in sys.argv:
        print("The legacy Tk UI has been removed. MRQ Launcher now uses the Qt UI only.")
    raise SystemExit(run_qt_shell())
