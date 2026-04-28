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

import tkinter as tk
from tkinter import font as tkfont
from tkinter import filedialog, messagebox, StringVar
from tkinter import ttk

# -------------------------------------------------
# App meta
# -------------------------------------------------

APP_VERSION = "1.9.8"

UI_THEME = {
    "bg": "#111318",
    "panel": "#171B22",
    "panel_alt": "#1D232C",
    "panel_soft": "#202733",
    "border": "#2A3340",
    "text": "#E7ECF3",
    "muted": "#9CA8B7",
    "accent": "#4EA1FF",
    "accent_soft": "#223A56",
    "success": "#2D6A4F",
    "warning": "#8A6A2F",
    "danger": "#8B3A46",
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
# -------------------------------------------------
# Helpers
# -------------------------------------------------

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


def configure_windows_dpi_awareness() -> None:
    """Enable system DPI awareness on Windows before creating the Tk root."""
    if sys.platform != "win32":
        return
    try:
        ctypes = __import__("ctypes")
        user32 = ctypes.windll.user32
        try:
            user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
            return
        except Exception:
            pass
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            return
        except Exception:
            pass
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
    except Exception:
        pass

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


# -------------------------------------------------
# Task Editor (single window for selecting paths)
# -------------------------------------------------

class TaskEditor(tk.Toplevel):
    def __init__(self, master, task: Optional[RenderTask] = None):
        super().__init__(master)
        self.title("Task Editor")
        # Allow resizing of the task editor window
        self.resizable(True, True)
        self.result: Optional[RenderTask] = None
        self.source_task = task
        self.configure(bg=UI_THEME["panel"])

        self.var_uproj = StringVar(value=(task.uproject if task else ""))
        self.var_level = StringVar(value=(task.level if task else ""))
        self.var_seq = StringVar(value=(task.sequence if task else ""))
        self.var_preset = StringVar(value=(task.preset if task else ""))
        self.var_output_dir = StringVar(value=(task.output_dir if task else ""))

        frm = tk.Frame(self, padx=10, pady=10, bg=UI_THEME["panel"])
        frm.pack(fill=tk.BOTH, expand=True)

        def row(lbl, var, browse_cb=None, hint: str = ""):
            # Generic row builder for labeled entry with optional Browse and hint
            r = tk.Frame(frm, bg=UI_THEME["panel"])
            r.pack(fill="x", pady=3)
            tk.Label(r, text=lbl, width=20, anchor="w", bg=UI_THEME["panel"], fg=UI_THEME["text"]).pack(side=tk.LEFT)
            tk.Entry(r, textvariable=var, width=70, bg=UI_THEME["entry"], fg=UI_THEME["text"], insertbackground=UI_THEME["text"], relief=tk.FLAT, bd=0, highlightthickness=1, highlightbackground=UI_THEME["border"], highlightcolor=UI_THEME["accent"]).pack(side=tk.LEFT, padx=5)
            if browse_cb:
                tk.Button(r, text="Browse", command=browse_cb, bg=UI_THEME["panel_soft"], fg=UI_THEME["text"], activebackground=UI_THEME["panel_soft"], activeforeground=UI_THEME["text"], relief=tk.FLAT, bd=0, padx=10, pady=4).pack(side=tk.LEFT)
            if hint:
                tk.Label(frm, text=hint, fg=UI_THEME["muted"], bg=UI_THEME["panel"]).pack(anchor="w")

        def pick_uproj():
            p = filedialog.askopenfilename(title="Select .uproject", filetypes=[("Unreal Project", "*.uproject")])
            if p:
                self.var_uproj.set(p)

        def pick_level():
            p = filedialog.askopenfilename(title="Select MAP .umap/.uasset", filetypes=[("Unreal Map/Asset", "*.umap *.uasset")])
            if p:
                try:
                    self.var_level.set(fs_to_soft_object(p))
                except Exception as e:
                    messagebox.showerror("Level error", str(e))

        def pick_seq():
            p = filedialog.askopenfilename(title="Select LevelSequence .uasset", filetypes=[("Unreal Asset", "*.uasset")])
            if p:
                try:
                    self.var_seq.set(fs_to_soft_object(p))
                except Exception as e:
                    messagebox.showerror("Sequence error", str(e))

        def pick_preset():
            p = filedialog.askopenfilename(title="Select MRQ Preset .uasset", filetypes=[("Unreal Asset", "*.uasset")])
            if p:
                try:
                    self.var_preset.set(fs_to_soft_object(p))
                except Exception as e:
                    messagebox.showerror("Preset error", str(e))

        def pick_output_dir():
            p = filedialog.askdirectory(title="Select Output Directory")
            if p:
                # Normalize to use forward slashes so UE CLI handles it consistently
                self.var_output_dir.set(p.replace("\\", "/"))

        row("Project (.uproject)", self.var_uproj, pick_uproj)
        row("Map (SoftObjectPath)", self.var_level, pick_level, "e.g.: /Game/Maps/MyMap.MyMap")
        row("Level Sequence", self.var_seq, pick_seq, "e.g.: /Game/Cinematics/Shot.Shot")
        row("MRQ Preset", self.var_preset, pick_preset, "e.g.: /Game/Cinematics/MoviePipeline/Presets/High.High")
        row(
            "Output Directory",
            self.var_output_dir,
            pick_output_dir,
            "Optional. If empty, the path from MRQ Preset will be used."
        )

        btn = tk.Frame(frm, bg=UI_THEME["panel"])
        btn.pack(fill="x", pady=10)
        tk.Button(btn, text="OK", command=self.on_ok, bg=UI_THEME["accent"], fg="#FFFFFF", activebackground=UI_THEME["accent"], activeforeground="#FFFFFF", relief=tk.FLAT, bd=0, padx=12, pady=6).pack(side=tk.LEFT, padx=4)
        tk.Button(btn, text="Cancel", command=self.destroy, bg=UI_THEME["panel_soft"], fg=UI_THEME["text"], activebackground=UI_THEME["panel_soft"], activeforeground=UI_THEME["text"], relief=tk.FLAT, bd=0, padx=12, pady=6).pack(side=tk.LEFT)

    def on_ok(self):
        t = RenderTask(
            uproject=self.var_uproj.get().strip(),
            level=self.var_level.get().strip(),
            sequence=self.var_seq.get().strip(),
            preset=self.var_preset.get().strip(),
            output_dir=self.var_output_dir.get().strip(),
            notes=(self.source_task.notes if self.source_task else ""),
            added_at=(self.source_task.added_at if self.source_task else current_task_timestamp()),
            enabled=True,
        )
        if not (t.uproject and t.level and t.sequence and t.preset):
            messagebox.showerror("Validation", "Fill in all fields.")
            return
        self.result = t
        self.destroy()

# -------------------------------------------------
# Main App (thread-safe log + statuses)
# -------------------------------------------------

class MRQLauncher(tk.Tk):
    def __init__(self):
        configure_windows_dpi_awareness()
        super().__init__()
        self.ui_scale = self._detect_ui_scale()
        self._apply_tk_scaling()
        # Window title with version
        self.title(f"MRQ Launcher (CLI) ver {APP_VERSION}")
        self.geometry(f"{self._s(1480)}x{self._s(920)}")
        self.resizable(True, True)
        self.minsize(self._s(1280), self._s(760))
        self.full_mode_minsize = (self._s(1280), self._s(760))
        self.minimal_mode_minsize = (self._s(560), self._s(360))
        self.settings = AppSettings()
        self.process_controller = RenderProcessController(self._log)
        self._current_global_idx: Optional[int] = None
        self.stop_all = False
        self.cancel_current_requested = False
        self.minimal_mode = False
        self._full_mode_geometry: Optional[str] = None
        self.session_total_var = StringVar(value="Session total: 00:00:00")
        self.current_task_var = StringVar(value="Current task: Idle")
        self.current_status_var = StringVar(value="Status: Idle")
        self.current_task_time_var = StringVar(value="Task time: —")
        self.current_progress_var = StringVar(value="0%")
        self.render_progress_value = tk.DoubleVar(value=0.0)
        self.tree_columns = ("status", "level", "sequence", "preset", "runtime", "start", "end")
        self.tree_column_titles = {
            "status": "Status",
            "level": "Level",
            "sequence": "Sequence",
            "preset": "Preset",
            "runtime": "Running Time",
            "start": "Start",
            "end": "End",
        }
        self.tree_column_defaults = {
            "status": self._s(180),
            "level": self._s(150),
            "sequence": self._s(220),
            "preset": self._s(320),
            "runtime": self._s(130),
            "start": self._s(120),
            "end": self._s(120),
        }
        self.full_tree_columns = self.tree_columns
        self.minimal_tree_columns = ("status", "level", "sequence", "preset", "runtime")
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.ui_queue: "queue.Queue[tuple]" = queue.Queue()
        self.state: List[dict] = []  # {status, progress, start, end}
        # --- New: shared runtime task queue and worker flag
        self.runtime_queue = RuntimeQueueCoordinator(
            self._find_task_index_by_identity,
            self._current_running_task_for_queue,
            self._set_status_async,
            self._log,
        )
        self.worker_running: bool = False
        self.render_action_buttons = []
        self.render_mode_hint_var = StringVar(value="Ready to render.")
        self.command_preview: Optional[tk.Text] = None
        self.inspector_vars = {}
        self.status_pill_widgets = {}
        self._empty_menu = tk.Menu(self, tearoff=0)
        self._build_ui()
        self.after(50, self._drain_queues)
        # Periodic update for the session total time label
        self.after(500, self._tick_session_total)


    def _detect_ui_scale(self) -> float:
        try:
            dpi = float(self.winfo_fpixels("1i"))
            return max(1.0, min(3.0, dpi / 96.0))
        except Exception:
            return 1.0

    def _apply_tk_scaling(self) -> None:
        try:
            dpi = 96.0 * self.ui_scale
            self.tk.call("tk", "scaling", dpi / 72.0)
        except Exception:
            pass

    def _s(self, value: int) -> int:
        return max(1, int(round(value * self.ui_scale)))

    # UI
    def _build_ui(self):
        self._configure_styles()
        self._build_menu()

        self.shell = tk.Frame(self, bg=UI_THEME["bg"])
        self.shell.pack(fill=tk.BOTH, expand=True)

        self.header_panel = self._create_panel(self.shell, padx=14, pady=12)
        self.header_panel.pack(fill=tk.X, padx=self._s(12), pady=(self._s(12), self._s(8)))
        self._build_header(self.header_panel)

        self.body = tk.Frame(self.shell, bg=UI_THEME["bg"])
        self.body.pack(fill=tk.BOTH, expand=True, padx=self._s(12), pady=(0, self._s(8)))

        self.upper_body = tk.Frame(self.body, bg=UI_THEME["bg"])
        self.upper_body.pack(fill=tk.BOTH, expand=True)

        self.queue_panel = self._create_panel(self.upper_body, padx=12, pady=12)
        self.queue_panel.pack(fill=tk.BOTH, expand=True)
        self._build_queue_workspace(self.queue_panel)

        self.bottom_panel = self._create_panel(self.body, padx=12, pady=12)
        self.bottom_panel.pack(fill=tk.BOTH, expand=False, pady=(self._s(8), 0))
        self._build_bottom_panel(self.bottom_panel)

        self.status_bar = tk.Frame(self.shell, bg=UI_THEME["panel_alt"], highlightthickness=1, highlightbackground=UI_THEME["border"])
        self.status_bar.pack(fill=tk.X, padx=self._s(12), pady=(0, self._s(12)))
        self._build_status_bar(self.status_bar)

        self.refresh_tree()
        self._update_engine_labels()
        self._update_inspector()
        self._update_command_preview()
        self._update_status_summary()

    def _configure_styles(self):
        self.configure(bg=UI_THEME["bg"])

        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(
            "Treeview",
            background=UI_THEME["panel"],
            fieldbackground=UI_THEME["panel"],
            foreground=UI_THEME["text"],
            bordercolor=UI_THEME["border"],
            lightcolor=UI_THEME["border"],
            darkcolor=UI_THEME["border"],
            rowheight=self._s(30),
            relief="flat",
        )
        style.map(
            "Treeview",
            background=[("selected", UI_THEME["accent_soft"])],
            foreground=[("selected", "#FFFFFF")],
        )
        style.configure(
            "Treeview.Heading",
            background=UI_THEME["panel_soft"],
            foreground=UI_THEME["text"],
            bordercolor=UI_THEME["border"],
            relief="flat",
            padding=(self._s(8), self._s(6)),
        )
        style.map("Treeview.Heading", background=[("active", UI_THEME["panel_alt"])])

        style.configure(
            "TScrollbar",
            background=UI_THEME["panel_alt"],
            troughcolor=UI_THEME["entry"],
            bordercolor=UI_THEME["border"],
            arrowcolor=UI_THEME["text"],
        )
        style.configure(
            "Dark.TCombobox",
            fieldbackground=UI_THEME["entry"],
            background=UI_THEME["panel_soft"],
            foreground=UI_THEME["text"],
            bordercolor=UI_THEME["border"],
            lightcolor=UI_THEME["border"],
            darkcolor=UI_THEME["border"],
            arrowcolor=UI_THEME["text"],
        )

        style.configure(
            "Dark.Horizontal.TProgressbar",
            troughcolor=UI_THEME["entry"],
            background=UI_THEME["accent"],
            bordercolor=UI_THEME["border"],
            lightcolor=UI_THEME["accent"],
            darkcolor=UI_THEME["accent"],
        )

    def _build_menu(self):
        menubar = tk.Menu(self, bg=UI_THEME["panel"], fg=UI_THEME["text"], activebackground=UI_THEME["panel_soft"], activeforeground=UI_THEME["text"])

        m_task = tk.Menu(menubar, tearoff=0, bg=UI_THEME["panel"], fg=UI_THEME["text"], activebackground=UI_THEME["panel_soft"], activeforeground=UI_THEME["text"])
        m_task.add_command(label="Add Task", command=self.add_task)
        m_task.add_command(label="Edit Task", command=self.edit_task)
        m_task.add_command(label="Duplicate Task", command=self.duplicate_task)
        menubar.add_cascade(label="Task", menu=m_task)

        m_sel = tk.Menu(menubar, tearoff=0, bg=UI_THEME["panel"], fg=UI_THEME["text"], activebackground=UI_THEME["panel_soft"], activeforeground=UI_THEME["text"])
        m_sel.add_command(label="Enable All Tasks", command=lambda: self.set_enabled_all(True))
        m_sel.add_command(label="Disable All Tasks", command=lambda: self.set_enabled_all(False))
        m_sel.add_command(label="Remove Task(s)", command=self.remove_task)
        m_sel.add_command(label="Remove Unchecked Tasks", command=self.remove_unchecked_tasks)
        m_sel.add_command(label="Toggle Selection", command=self.toggle_selected)
        m_sel.add_separator()
        m_sel.add_command(label="Move Up", command=lambda: self.move_selected(-1))
        m_sel.add_command(label="Move Down", command=lambda: self.move_selected(1))
        menubar.add_cascade(label="Selections", menu=m_sel)

        m_run = tk.Menu(menubar, tearoff=0, bg=UI_THEME["panel"], fg=UI_THEME["text"], activebackground=UI_THEME["panel_soft"], activeforeground=UI_THEME["text"])
        m_run.add_command(label="Render All", command=self.run_all)
        m_run.add_command(label="Render Selected", command=self.run_selected)
        m_run.add_command(label="Render Checked", command=self.run_enabled)
        m_run.add_command(label="Add Task(s) to Queue", command=self.enqueue_selected_or_enabled)
        m_run.add_separator()
        m_run.add_command(label="Clear Status", command=self.clear_status_selected)
        m_run.add_command(label="Stop Current Render", command=self.cancel_current)
        m_run.add_command(label="Cancel All", command=self.cancel_all)
        menubar.add_cascade(label="Render", menu=m_run)

        m_save = tk.Menu(menubar, tearoff=0, bg=UI_THEME["panel"], fg=UI_THEME["text"], activebackground=UI_THEME["panel_soft"], activeforeground=UI_THEME["text"])
        m_save.add_command(label="Load Task(s)", command=self.load_tasks_dialog)
        m_save.add_command(label="Save Selected Task(s)", command=self.save_selected_tasks_dialog)
        m_save.add_separator()
        m_save.add_command(label="Load Queue", command=self.load_json_dialog)
        m_save.add_command(label="Save Queue", command=self.save_json_dialog)
        m_save.add_command(label="Save Queue Log", command=self.save_queue_log)
        menubar.add_cascade(label="Save", menu=m_save)

        self.menubar = menubar
        self.config(menu=menubar)

    def _create_panel(self, parent, width=None, height=None, padx=0, pady=0):
        frame = tk.Frame(
            parent,
            bg=UI_THEME["panel"],
            highlightthickness=1,
            highlightbackground=UI_THEME["border"],
            padx=self._s(padx),
            pady=self._s(pady),
        )
        if width is not None:
            frame.configure(width=width)
        if height is not None:
            frame.configure(height=height)
        return frame

    def _make_button(self, parent, text, command, variant="secondary", width=None):
        palette = {
            "primary": (UI_THEME["accent"], "#FFFFFF"),
            "danger": (UI_THEME["danger"], "#FFFFFF"),
            "secondary": (UI_THEME["panel_soft"], UI_THEME["text"]),
        }
        bg, fg = palette.get(variant, palette["secondary"])
        btn = tk.Button(
            parent,
            text=text,
            command=command,
            width=width,
            bg=bg,
            fg=fg,
            activebackground=bg,
            activeforeground=fg,
            relief=tk.FLAT,
            bd=0,
            padx=self._s(10),
            pady=self._s(7),
            highlightthickness=0,
            cursor="hand2",
        )
        return btn

    def _make_entry(self, parent, textvariable=None, width=None):
        entry = tk.Entry(
            parent,
            textvariable=textvariable,
            width=width,
            bg=UI_THEME["entry"],
            fg=UI_THEME["text"],
            insertbackground=UI_THEME["text"],
            relief=tk.FLAT,
            bd=0,
            highlightthickness=1,
            highlightbackground=UI_THEME["border"],
            highlightcolor=UI_THEME["accent"],
        )
        return entry

    def _section_title(self, parent, title, subtitle=""):
        wrapper = tk.Frame(parent, bg=UI_THEME["panel"])
        wrapper.pack(fill=tk.X, pady=(0, 10))
        tk.Label(wrapper, text=title, bg=UI_THEME["panel"], fg=UI_THEME["text"], font=("Segoe UI", 14, "bold")).pack(anchor="w")
        if subtitle:
            tk.Label(wrapper, text=subtitle, bg=UI_THEME["panel"], fg=UI_THEME["muted"], font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 0))
        return wrapper

    def _build_header(self, parent):
        top = tk.Frame(parent, bg=UI_THEME["panel"])
        top.pack(fill=tk.X)

        title_block = tk.Frame(top, bg=UI_THEME["panel"])
        title_block.pack(side=tk.LEFT, fill=tk.Y)
        tk.Label(title_block, text="MRQ Launcher CLI", bg=UI_THEME["panel"], fg=UI_THEME["text"], font=("Segoe UI", 16, "bold")).pack(anchor="w")
        tk.Label(
            title_block,
            text="Dark Studio Console",
            bg=UI_THEME["panel"],
            fg=UI_THEME["muted"],
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(2, 0))

        actions = tk.Frame(top, bg=UI_THEME["panel"])
        actions.pack(side=tk.RIGHT)
        self._make_button(actions, "Minimal Mode", self.toggle_minimal_mode).pack(side=tk.LEFT, padx=(0, 6))
        self._make_button(actions, "Load Queue", self.load_json_dialog).pack(side=tk.LEFT, padx=(0, 6))
        self._make_button(actions, "Save Queue", self.save_json_dialog).pack(side=tk.LEFT, padx=(0, 6))
        self._make_button(actions, "Save Queue Log", self.save_queue_log).pack(side=tk.LEFT)

        path_row = tk.Frame(parent, bg=UI_THEME["panel"])
        path_row.pack(fill=tk.X, pady=(14, 0))

        tk.Label(path_row, text="UnrealEditor-Cmd.exe", bg=UI_THEME["panel"], fg=UI_THEME["muted"], font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(0, 10))
        self.var_ue = StringVar(value=self.settings.ue_cmd)
        self.ue_entry = self._make_entry(path_row, textvariable=self.var_ue)
        self.ue_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._make_button(path_row, "Browse", self.browse_ue, width=10).pack(side=tk.LEFT, padx=(8, 0))

        self.var_retries = tk.IntVar(value=self.settings.retries)
        self.var_policy = StringVar(value=self.settings.fail_policy)
        self.var_kill_timeout = tk.IntVar(value=self.settings.kill_timeout_s)
        self.var_windowed = tk.BooleanVar(value=self.settings.windowed)
        self.var_resx = tk.IntVar(value=self.settings.resx)
        self.var_resy = tk.IntVar(value=self.settings.resy)
        self.var_nts = tk.BooleanVar(value=self.settings.no_texture_streaming)
        self.var_extra = StringVar(value=self.settings.extra_cli)
        self.var_auto_minimal = tk.BooleanVar(value=self.settings.auto_minimal_on_render)

        opts = tk.Frame(parent, bg=UI_THEME["panel"])
        opts.pack(fill=tk.X, pady=(12, 0))

        tk.Label(opts, text="Retries", bg=UI_THEME["panel"], fg=UI_THEME["muted"], font=("Segoe UI", 9)).pack(side=tk.LEFT)
        tk.Spinbox(opts, from_=0, to=3, width=3, textvariable=self.var_retries, bg=UI_THEME["entry"], fg=UI_THEME["text"], buttonbackground=UI_THEME["panel_soft"], insertbackground=UI_THEME["text"], relief=tk.FLAT).pack(side=tk.LEFT, padx=(6, 12))

        tk.Label(opts, text="On fail", bg=UI_THEME["panel"], fg=UI_THEME["muted"], font=("Segoe UI", 9)).pack(side=tk.LEFT)
        ttk.Combobox(opts, textvariable=self.var_policy, width=16, state="readonly", style="Dark.TCombobox",
                     values=("retry_then_next", "skip_next", "stop_queue")).pack(side=tk.LEFT, padx=(6, 12))

        tk.Label(opts, text="Kill timeout s", bg=UI_THEME["panel"], fg=UI_THEME["muted"], font=("Segoe UI", 9)).pack(side=tk.LEFT)
        tk.Spinbox(opts, from_=0, to=120, width=4, textvariable=self.var_kill_timeout, bg=UI_THEME["entry"], fg=UI_THEME["text"], buttonbackground=UI_THEME["panel_soft"], insertbackground=UI_THEME["text"], relief=tk.FLAT).pack(side=tk.LEFT, padx=(6, 12))

        tk.Checkbutton(opts, text="Windowed", variable=self.var_windowed, bg=UI_THEME["panel"], fg=UI_THEME["text"], selectcolor=UI_THEME["entry"], activebackground=UI_THEME["panel"], activeforeground=UI_THEME["text"]).pack(side=tk.LEFT)
        tk.Label(opts, text="ResX", bg=UI_THEME["panel"], fg=UI_THEME["muted"], font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(12, 0))
        tk.Spinbox(opts, from_=320, to=16384, width=6, textvariable=self.var_resx, bg=UI_THEME["entry"], fg=UI_THEME["text"], buttonbackground=UI_THEME["panel_soft"], insertbackground=UI_THEME["text"], relief=tk.FLAT).pack(side=tk.LEFT, padx=(6, 12))
        tk.Label(opts, text="ResY", bg=UI_THEME["panel"], fg=UI_THEME["muted"], font=("Segoe UI", 9)).pack(side=tk.LEFT)
        tk.Spinbox(opts, from_=240, to=16384, width=6, textvariable=self.var_resy, bg=UI_THEME["entry"], fg=UI_THEME["text"], buttonbackground=UI_THEME["panel_soft"], insertbackground=UI_THEME["text"], relief=tk.FLAT).pack(side=tk.LEFT, padx=(6, 12))
        tk.Checkbutton(opts, text="No Texture Streaming", variable=self.var_nts, bg=UI_THEME["panel"], fg=UI_THEME["text"], selectcolor=UI_THEME["entry"], activebackground=UI_THEME["panel"], activeforeground=UI_THEME["text"]).pack(side=tk.LEFT, padx=(0, 12))
        tk.Checkbutton(opts, text="Auto Minimal On Render", variable=self.var_auto_minimal, bg=UI_THEME["panel"], fg=UI_THEME["text"], selectcolor=UI_THEME["entry"], activebackground=UI_THEME["panel"], activeforeground=UI_THEME["text"]).pack(side=tk.LEFT, padx=(0, 12))

        tk.Label(opts, text="Extra CLI", bg=UI_THEME["panel"], fg=UI_THEME["muted"], font=("Segoe UI", 9)).pack(side=tk.LEFT)
        self.extra_entry = self._make_entry(opts, textvariable=self.var_extra, width=28)
        self.extra_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))

        tracked_vars = (
            self.var_ue,
            self.var_retries,
            self.var_policy,
            self.var_kill_timeout,
            self.var_windowed,
            self.var_resx,
            self.var_resy,
            self.var_nts,
            self.var_auto_minimal,
            self.var_extra,
        )
        for tracked in tracked_vars:
            tracked.trace_add("write", self._on_runtime_options_changed)

    def _build_sidebar(self, parent):
        self._section_title(parent, "Navigation", "Production workspace")

        for name in ("Queue", "Presets", "Profiles", "Settings", "Logs", "About"):
            variant = "primary" if name == "Queue" else "secondary"
            btn = self._make_button(parent, name, command=lambda n=name: None, variant=variant)
            btn.pack(fill=tk.X, pady=3)

        tk.Frame(parent, bg=UI_THEME["border"], height=1).pack(fill=tk.X, pady=12)

        self._section_title(parent, "Engine", "Runtime context")
        self.sidebar_engine_state = StringVar(value="Detected")
        self.sidebar_engine_version = StringVar(value="Version: ?")
        self.sidebar_engine_path = StringVar(value="Path not set")

        tk.Label(parent, textvariable=self.sidebar_engine_state, bg=UI_THEME["panel"], fg=UI_THEME["text"], font=("Segoe UI", 10, "bold")).pack(anchor="w")
        tk.Label(parent, textvariable=self.sidebar_engine_version, bg=UI_THEME["panel"], fg=UI_THEME["muted"], font=("Segoe UI", 9)).pack(anchor="w", pady=(4, 0))
        self.sidebar_path_label = tk.Label(
            parent,
            textvariable=self.sidebar_engine_path,
            bg=UI_THEME["panel"],
            fg=UI_THEME["muted"],
            font=("Segoe UI", 8),
            justify=tk.LEFT,
            wraplength=140,
        )
        self.sidebar_path_label.pack(anchor="w", pady=(6, 0))

    def _build_queue_workspace(self, parent):
        self.queue_section_header = self._section_title(parent, "Render Queue", "Main operational surface")

        self.minimal_header = tk.Frame(parent, bg=UI_THEME["panel"])
        minimal_title = tk.Frame(self.minimal_header, bg=UI_THEME["panel"])
        minimal_title.pack(side=tk.LEFT, fill=tk.Y)
        tk.Label(minimal_title, text="Minimal Mode", bg=UI_THEME["panel"], fg=UI_THEME["text"], font=("Segoe UI", 14, "bold")).pack(anchor="w")
        tk.Label(minimal_title, text="Execution only view with compact columns.", bg=UI_THEME["panel"], fg=UI_THEME["muted"], font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 0))
        self._make_button(self.minimal_header, "Exit Minimal Mode", self.exit_minimal_mode).pack(side=tk.RIGHT)
        self._make_button(self.minimal_header, "Stop All", self.cancel_all, variant="danger").pack(side=tk.RIGHT, padx=(0, 6))
        self._make_button(self.minimal_header, "Stop Current", self.cancel_current).pack(side=tk.RIGHT, padx=(0, 6))

        self.queue_toolbar = tk.Frame(parent, bg=UI_THEME["panel"])
        self.queue_toolbar.pack(fill=tk.X, pady=(0, 10))

        self.queue_hint_frame = tk.Frame(parent, bg=UI_THEME["panel"])
        self.queue_hint_frame.pack(fill=tk.X, pady=(0, 10))
        tk.Label(self.queue_hint_frame, textvariable=self.render_mode_hint_var, bg=UI_THEME["panel"], fg=UI_THEME["muted"], font=("Segoe UI", 9, "italic")).pack(anchor="w")

        left = tk.Frame(self.queue_toolbar, bg=UI_THEME["panel"])
        left.pack(side=tk.LEFT)
        self._make_button(left, "Add Job", self.add_task).pack(side=tk.LEFT, padx=(0, 6))
        self._make_button(left, "Edit", self.edit_task).pack(side=tk.LEFT, padx=(0, 6))
        self._make_button(left, "Duplicate", self.duplicate_task).pack(side=tk.LEFT, padx=(0, 6))
        self._make_button(left, "Remove", self.remove_task).pack(side=tk.LEFT, padx=(0, 6))
        self._make_button(left, "Move Up", lambda: self.move_selected(-1)).pack(side=tk.LEFT, padx=(0, 6))
        self._make_button(left, "Move Down", lambda: self.move_selected(1)).pack(side=tk.LEFT, padx=(0, 6))
        self._make_button(left, "Toggle", self.toggle_selected).pack(side=tk.LEFT)

        right = tk.Frame(self.queue_toolbar, bg=UI_THEME["panel"])
        right.pack(side=tk.RIGHT, fill=tk.X)
        tk.Label(right, text="Filter", bg=UI_THEME["panel"], fg=UI_THEME["muted"], font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(0, 8))
        self.var_task_filter = StringVar()
        self.var_task_filter.trace_add("write", lambda *_: self.refresh_tree())
        self.filter_entry = self._make_entry(right, textvariable=self.var_task_filter, width=24)
        self.filter_entry.pack(side=tk.LEFT)

        self.queue_stats_frame = tk.Frame(parent, bg=UI_THEME["panel"])
        self.queue_stats_frame.pack(fill=tk.X, pady=(0, 10))
        self.queue_stats_var = StringVar(value="Total: 0 | Visible: 0 | Enabled: 0 | Selected: 0")
        tk.Label(self.queue_stats_frame, textvariable=self.queue_stats_var, bg=UI_THEME["panel"], fg=UI_THEME["muted"], font=("Segoe UI", 9)).pack(side=tk.LEFT)

        self.tree_shell = tk.Frame(parent, bg=UI_THEME["panel"])
        self.tree_shell.pack(fill=tk.BOTH, expand=True)

        self.tree = ttk.Treeview(self.tree_shell, columns=self.tree_columns, show="headings", selectmode="extended")
        for name in self.tree_columns:
            self.tree.heading(name, text=self.tree_column_titles[name])
            self.tree.column(name, width=self.tree_column_defaults[name], anchor="w", stretch=True)

        self.tree.tag_configure("status_ready", foreground=UI_THEME["text"])
        self.tree.tag_configure("status_queued", foreground="#FFD28A")
        self.tree.tag_configure("status_rendering", foreground="#F0B35A", font=("Segoe UI", 9, "bold"))
        self.tree.tag_configure("status_done", foreground="#8BE2B5")
        self.tree.tag_configure("status_failed", foreground="#FF9AA9")
        self.tree.tag_configure("status_disabled", foreground=UI_THEME["muted"])
        self.tree.tag_configure("status_skipped", foreground="#D7C7FF")

        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.tree.bind("<Double-1>", self.on_tree_dblclick)
        self.tree.bind("<space>", self.on_space_toggle)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_selection_changed)
        self.tree.bind("<Configure>", lambda _e: self._queue_tree_refresh())
        self.tree.bind("<MouseWheel>", lambda _e: self.after_idle(self._refresh_status_pills))
        self.tree.bind("<Button-4>", lambda _e: self.after_idle(self._refresh_status_pills))
        self.tree.bind("<Button-5>", lambda _e: self.after_idle(self._refresh_status_pills))

        self.ctx_task = tk.Menu(self, tearoff=0, bg=UI_THEME["panel"], fg=UI_THEME["text"], activebackground=UI_THEME["panel_soft"], activeforeground=UI_THEME["text"])
        self.ctx_task.add_command(label="Add Task", command=self.add_task)
        self.ctx_task.add_command(label="Edit Task", command=self.edit_task)
        self.ctx_task.add_command(label="Duplicate Task", command=self.duplicate_task)
        self.ctx_task.add_command(label="Remove Task(s)", command=self.remove_task)
        self.ctx_task.add_separator()
        self.ctx_task.add_command(label="Move Up", command=lambda: self.move_selected(-1))
        self.ctx_task.add_command(label="Move Down", command=lambda: self.move_selected(1))
        self.ctx_task.add_separator()
        self.ctx_task.add_command(label="Load Task(s)...", command=self.load_tasks_dialog)
        self.ctx_task.add_command(label="Save Selected Task(s)...", command=self.save_selected_tasks_dialog)
        self.ctx_task.add_separator()
        self.ctx_task.add_command(label="Load Queue...", command=self.load_json_dialog)
        self.ctx_task.add_command(label="Save Queue...", command=self.save_json_dialog)
        self.ctx_task.add_command(label="Save Queue Log...", command=self.save_queue_log)
        self.ctx_task.add_separator()
        self.ctx_task.add_command(label="Clear Status", command=self.clear_status_selected)
        self.tree.bind("<Button-3>", self._on_tree_right_click)
        self.tree.bind("<Control-Button-1>", self._on_tree_right_click)

        sb = ttk.Scrollbar(self.tree_shell, orient="vertical", command=self._on_tree_yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        self.queue_hscroll = ttk.Scrollbar(parent, orient="horizontal", command=self._on_tree_xview)
        self.tree.configure(xscrollcommand=self.queue_hscroll.set)
        self.queue_hscroll.pack(fill=tk.X, pady=(8, 0))

        self.minimal_footer = tk.Frame(parent, bg=UI_THEME["panel"])
        footer_left = tk.Frame(self.minimal_footer, bg=UI_THEME["panel"])
        footer_left.pack(side=tk.LEFT)
        tk.Label(footer_left, textvariable=self.session_total_var, bg=UI_THEME["panel"], fg=UI_THEME["muted"], font=("Segoe UI", 10)).pack(side=tk.LEFT)
        tk.Label(footer_left, text="  •  ", bg=UI_THEME["panel"], fg=UI_THEME["muted"], font=("Segoe UI", 10)).pack(side=tk.LEFT)
        tk.Label(footer_left, textvariable=self.current_task_time_var, bg=UI_THEME["panel"], fg=UI_THEME["muted"], font=("Segoe UI", 10)).pack(side=tk.LEFT)

        footer_center = tk.Frame(self.minimal_footer, bg=UI_THEME["panel"])
        footer_center.pack(side=tk.LEFT, expand=True)
        ttk.Progressbar(
            footer_center,
            variable=self.render_progress_value,
            maximum=100.0,
            length=self._s(220),
            style="Dark.Horizontal.TProgressbar",
        ).pack(side=tk.LEFT)
        tk.Label(footer_center, textvariable=self.current_progress_var, bg=UI_THEME["panel"], fg=UI_THEME["text"], font=("Segoe UI", 9, "bold"), padx=10).pack(side=tk.LEFT)

        footer_right = tk.Frame(self.minimal_footer, bg=UI_THEME["panel"])
        footer_right.pack(side=tk.RIGHT)
        tk.Label(footer_right, textvariable=self.current_task_var, bg=UI_THEME["panel"], fg=UI_THEME["text"], font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        tk.Label(footer_right, textvariable=self.current_status_var, bg=UI_THEME["panel"], fg=UI_THEME["muted"], font=("Segoe UI", 10), padx=12).pack(side=tk.LEFT)

    def _build_inspector_panel(self, parent):
        self._section_title(parent, "Job Inspector", "Selected row details")

        self.inspector_vars = {
            "job": StringVar(value="No selection"),
            "enabled": StringVar(value="-"),
            "uproject": StringVar(value="-"),
            "level": StringVar(value="-"),
            "sequence": StringVar(value="-"),
            "preset": StringVar(value="-"),
            "output": StringVar(value="-"),
            "notes": StringVar(value="-"),
            "validation": StringVar(value="Validation: -"),
        }

        fields = (
            ("Job Name", "job"),
            ("Enabled", "enabled"),
            ("Project", "uproject"),
            ("Level", "level"),
            ("Sequence", "sequence"),
            ("Preset", "preset"),
            ("Output Directory", "output"),
            ("Description", "notes"),
        )
        for title, key in fields:
            row = tk.Frame(parent, bg=UI_THEME["panel"])
            row.pack(fill=tk.X, pady=(0, 10))
            tk.Label(row, text=title, bg=UI_THEME["panel"], fg=UI_THEME["muted"], font=("Segoe UI", 9)).pack(anchor="w")
            tk.Label(
                row,
                textvariable=self.inspector_vars[key],
                bg=UI_THEME["entry"],
                fg=UI_THEME["text"],
                font=("Segoe UI", 9),
                justify=tk.LEFT,
                wraplength=self._s(300),
                padx=8,
                pady=6,
                anchor="w",
                relief=tk.FLAT,
                highlightthickness=1,
                highlightbackground=UI_THEME["border"],
            ).pack(fill=tk.X, pady=(4, 0))

        tk.Label(parent, textvariable=self.inspector_vars["validation"], bg=UI_THEME["panel"], fg=UI_THEME["text"], font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(6, 10))

        quick = tk.Frame(parent, bg=UI_THEME["panel"])
        quick.pack(fill=tk.X, pady=(0, 10))
        self._make_button(quick, "Copy Command", self.copy_command_preview).pack(fill=tk.X, pady=(0, 6))
        self._make_button(quick, "Open Output Folder", self.open_selected_output_dir).pack(fill=tk.X)

        actions = tk.Frame(parent, bg=UI_THEME["panel"])
        actions.pack(fill=tk.X, pady=(8, 0))
        self._make_button(actions, "Edit Selected", self.edit_task, variant="primary").pack(fill=tk.X, pady=(0, 6))
        self._make_button(actions, "Duplicate", self.duplicate_task).pack(fill=tk.X, pady=(0, 6))
        self._make_button(actions, "Remove", self.remove_task, variant="danger").pack(fill=tk.X)

    def _build_bottom_panel(self, parent):
        top = tk.Frame(parent, bg=UI_THEME["panel"])
        top.pack(fill=tk.X)

        controls = tk.Frame(top, bg=UI_THEME["panel"])
        controls.pack(side=tk.LEFT)
        self.btn_render_enabled = self._make_button(controls, "Render Enabled", self.run_enabled, variant="primary")
        self.btn_render_enabled.pack(side=tk.LEFT, padx=(0, 6))
        self.btn_render_selected = self._make_button(controls, "Render Selected", self.run_selected)
        self.btn_render_selected.pack(side=tk.LEFT, padx=(0, 6))
        self.btn_queue_selected = self._make_button(controls, "Queue Selected", self.enqueue_selected_or_enabled)
        self.btn_queue_selected.pack(side=tk.LEFT, padx=(0, 6))
        self.btn_render_all = self._make_button(controls, "Render All", self.run_all)
        self.btn_render_all.pack(side=tk.LEFT, padx=(0, 6))
        self._make_button(controls, "Clear Status", self.clear_status_selected).pack(side=tk.LEFT, padx=(0, 6))
        self._make_button(controls, "Stop Current Render", self.cancel_current).pack(side=tk.LEFT, padx=(0, 6))
        self._make_button(controls, "Stop All", self.cancel_all, variant="danger").pack(side=tk.LEFT)
        self.render_action_buttons = [self.btn_render_enabled, self.btn_render_selected, self.btn_render_all]

        logs_actions = tk.Frame(top, bg=UI_THEME["panel"])
        logs_actions.pack(side=tk.RIGHT)
        self._make_button(logs_actions, "Open Logs Folder", self.open_logs_folder).pack(side=tk.LEFT, padx=(0, 6))
        self._make_button(logs_actions, "Open Last Log", self.open_last_log_for_selected).pack(side=tk.LEFT)

        info_row = tk.Frame(parent, bg=UI_THEME["panel"])
        info_row.pack(fill=tk.X, pady=(12, 10))

        left_info = tk.Frame(info_row, bg=UI_THEME["panel"])
        left_info.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Label(left_info, textvariable=self.current_task_var, bg=UI_THEME["panel"], fg=UI_THEME["text"], font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        tk.Label(left_info, textvariable=self.current_status_var, bg=UI_THEME["panel"], fg=UI_THEME["muted"], font=("Segoe UI", 10), padx=12).pack(side=tk.LEFT)

        progress_shell = tk.Frame(info_row, bg=UI_THEME["panel"])
        progress_shell.pack(side=tk.RIGHT)
        self.progress_bar = ttk.Progressbar(
            progress_shell,
            variable=self.render_progress_value,
            maximum=100.0,
            length=self._s(240),
            style="Dark.Horizontal.TProgressbar",
        )
        self.progress_bar.pack(side=tk.LEFT)
        tk.Label(progress_shell, textvariable=self.current_progress_var, bg=UI_THEME["panel"], fg=UI_THEME["text"], font=("Segoe UI", 9, "bold"), padx=10).pack(side=tk.LEFT)
        tk.Label(progress_shell, textvariable=self.session_total_var, bg=UI_THEME["panel"], fg=UI_THEME["muted"], font=("Segoe UI", 10)).pack(side=tk.LEFT, padx=(6, 0))

        split = tk.PanedWindow(parent, orient=tk.HORIZONTAL, sashwidth=6, bg=UI_THEME["panel"], bd=0, relief=tk.FLAT)
        split.pack(fill=tk.BOTH, expand=True)

        cmd_panel = self._create_panel(split, padx=10, pady=10)
        split.add(cmd_panel, minsize=self._s(360))
        cmd_top = tk.Frame(cmd_panel, bg=UI_THEME["panel"])
        cmd_top.pack(fill=tk.X)
        tk.Label(cmd_top, text="Command Preview", bg=UI_THEME["panel"], fg=UI_THEME["text"], font=("Segoe UI", 11, "bold")).pack(side=tk.LEFT, anchor="w")
        self._make_button(cmd_top, "Copy", self.copy_command_preview, width=10).pack(side=tk.RIGHT)
        self.command_preview = tk.Text(
            cmd_panel,
            height=8,
            bg=UI_THEME["entry"],
            fg=UI_THEME["text"],
            insertbackground=UI_THEME["text"],
            relief=tk.FLAT,
            bd=0,
            wrap="word",
            highlightthickness=1,
            highlightbackground=UI_THEME["border"],
            padx=8,
            pady=8,
        )
        self.command_preview.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

        log_panel = self._create_panel(split, padx=10, pady=10)
        split.add(log_panel, minsize=self._s(460))
        tk.Label(log_panel, text="Log", bg=UI_THEME["panel"], fg=UI_THEME["text"], font=("Segoe UI", 11, "bold")).pack(anchor="w")
        self.log = tk.Text(
            log_panel,
            height=8,
            bg=UI_THEME["entry"],
            fg=UI_THEME["text"],
            insertbackground=UI_THEME["text"],
            relief=tk.FLAT,
            bd=0,
            wrap="word",
            highlightthickness=1,
            highlightbackground=UI_THEME["border"],
            padx=8,
            pady=8,
        )
        self.log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, pady=(8, 0))
        log_scroll = ttk.Scrollbar(log_panel, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y, pady=(8, 0))

    def _build_status_bar(self, parent):
        self.status_overall_var = StringVar(value="State: Idle")
        self.status_counts_var = StringVar(value="Queued: 0 | Running: 0 | Failed: 0 | Done: 0")
        self.status_engine_var = StringVar(value="Engine: ?")

        tk.Label(parent, textvariable=self.status_overall_var, bg=UI_THEME["panel_alt"], fg=UI_THEME["text"], font=("Segoe UI", 9, "bold"), padx=10, pady=6).pack(side=tk.LEFT)
        tk.Label(parent, textvariable=self.status_counts_var, bg=UI_THEME["panel_alt"], fg=UI_THEME["muted"], font=("Segoe UI", 9), padx=10, pady=6).pack(side=tk.LEFT)
        tk.Label(parent, textvariable=self.status_engine_var, bg=UI_THEME["panel_alt"], fg=UI_THEME["muted"], font=("Segoe UI", 9), padx=10, pady=6).pack(side=tk.RIGHT)


    def _on_tree_yview(self, *args):
        self.tree.yview(*args)
        self.after_idle(self._refresh_status_pills)

    def _on_tree_xview(self, *args):
        self.tree.xview(*args)
        self.after_idle(self._refresh_status_pills)

    def _queue_tree_refresh(self):
        self.after_idle(self._refresh_status_pills)
        if self.minimal_mode:
            self.after_idle(self._autosize_tree_columns)

    def _tree_column_text(self, idx: int, column: str) -> str:
        if not (0 <= idx < len(self.settings.tasks)):
            return ""
        task = self.settings.tasks[idx]
        state = self.state[idx] if 0 <= idx < len(self.state) else default_task_state()
        if column == "status":
            return get_status_display(state.get("status", "Ready"), task.enabled)
        if column == "level":
            return soft_name(task.level)
        if column == "sequence":
            return soft_name(task.sequence)
        if column == "preset":
            return soft_name(task.preset)
        if column == "runtime":
            return format_runtime_display(state)
        if column == "start":
            return format_state_time_display(state.get("start"))
        if column == "end":
            return format_state_time_display(state.get("end"))
        return ""

    def _get_active_tree_columns(self):
        return self.minimal_tree_columns if self.minimal_mode else self.full_tree_columns

    def _visible_tree_columns(self):
        return self._get_active_tree_columns()

    def _set_tree_display_columns(self):
        self.tree.configure(displaycolumns=self._get_active_tree_columns())

    def _apply_default_tree_columns(self):
        self._set_tree_display_columns()
        for name in self.tree_columns:
            stretch = not self.minimal_mode
            self.tree.column(name, width=self.tree_column_defaults[name], anchor="w", stretch=stretch)

    def _autosize_tree_columns(self):
        if not hasattr(self, "tree") or self.tree is None:
            return
        self._set_tree_display_columns()
        visible_items = [int(iid) for iid in self.tree.get_children()]
        font = tkfont.nametofont("TkDefaultFont")
        base_padding = self._s(28)
        for name in self._get_active_tree_columns():
            width = font.measure(self.tree_column_titles[name]) + base_padding
            if name == "status":
                width = max(width, self._s(110))
            for idx in visible_items:
                width = max(width, font.measure(self._tree_column_text(idx, name)) + base_padding)
            if name == "status":
                width = min(max(width, self._s(110)), self._s(150))
            elif name == "level":
                width = min(max(width, self._s(96)), self._s(160))
            elif name == "sequence":
                width = min(max(width, self._s(130)), self._s(260))
            elif name == "preset":
                width = min(max(width, self._s(150)), self._s(360))
            else:
                width = min(max(width, self._s(96)), self._s(132))
            self.tree.column(name, width=width, anchor="w", stretch=False)

    def _compute_minimal_width(self) -> int:
        active_columns = self._visible_tree_columns()
        total_width = sum(int(float(self.tree.column(name, "width"))) for name in active_columns)
        total_width += self._s(56)

        footer_width = self._s(520)
        current_task_len = len(self.current_task_var.get()) + len(self.current_status_var.get())
        footer_width = max(footer_width, self._s(260) + current_task_len * self._s(5))

        target_width = max(total_width, footer_width)
        screen_width = max(self.winfo_screenwidth() - self._s(80), self.minimal_mode_minsize[0])
        return max(self.minimal_mode_minsize[0], min(target_width, screen_width))

    def _compute_minimal_height(self) -> int:
        visible_rows = max(8, min(len(self.tree.get_children()), 14))
        row_height = self._s(30)
        chrome_height = self._s(220)
        screen_height = max(self.winfo_screenheight() - self._s(120), self.minimal_mode_minsize[1])
        return max(self.minimal_mode_minsize[1], min(chrome_height + visible_rows * row_height, screen_height))

    def _compute_minimal_geometry(self) -> str:
        return f"{self._compute_minimal_width()}x{self._compute_minimal_height()}"

    def _fit_minimal_width_only(self):
        if not self.minimal_mode or not self.winfo_exists():
            return
        target_width = self._compute_minimal_width()
        current_height = max(self.winfo_height(), self.minimal_mode_minsize[1])
        self.geometry(f"{target_width}x{current_height}")

    def _hide_widget(self, widget):
        if widget is None:
            return
        try:
            if widget.winfo_manager() == "pack":
                widget.pack_forget()
        except Exception:
            pass

    def _show_widget(self, widget, **pack_kwargs):
        if widget is None:
            return
        try:
            if widget.winfo_manager() != "pack":
                widget.pack(**pack_kwargs)
        except Exception:
            pass

    def _apply_minimal_layout(self):
        self.config(menu=self._empty_menu)
        self._hide_widget(self.header_panel)
        self._hide_widget(self.bottom_panel)
        self._hide_widget(self.status_bar)

        self._hide_widget(self.queue_section_header)
        self._hide_widget(self.queue_toolbar)
        self._hide_widget(self.queue_hint_frame)
        self._hide_widget(self.queue_stats_frame)
        self._hide_widget(self.queue_hscroll)

        self._show_widget(self.minimal_header, fill=tk.X, pady=(0, 10), before=self.tree_shell)
        self._show_widget(self.minimal_footer, fill=tk.X, pady=(10, 0), after=self.tree_shell)

    def _apply_full_layout(self):
        self.config(menu=self.menubar)
        self._hide_widget(self.minimal_header)
        self._hide_widget(self.minimal_footer)

        self._show_widget(self.header_panel, fill=tk.X, padx=self._s(12), pady=(self._s(12), self._s(8)), before=self.body)
        self._show_widget(self.bottom_panel, fill=tk.BOTH, expand=False, pady=(self._s(8), 0), after=self.upper_body)
        self._show_widget(self.status_bar, fill=tk.X, padx=self._s(12), pady=(0, self._s(12)), after=self.body)

        self._show_widget(self.queue_section_header, fill=tk.X, pady=(0, 10), before=self.tree_shell)
        self._show_widget(self.queue_toolbar, fill=tk.X, pady=(0, 10), before=self.tree_shell)
        self._show_widget(self.queue_hint_frame, fill=tk.X, pady=(0, 10), before=self.tree_shell)
        self._show_widget(self.queue_stats_frame, fill=tk.X, pady=(0, 10), before=self.tree_shell)
        self._show_widget(self.queue_hscroll, fill=tk.X, pady=(8, 0), after=self.tree_shell)

    def enter_minimal_mode(self):
        if self.minimal_mode:
            return
        previous_geometry = self.geometry()
        self.minimal_mode = True
        self._full_mode_geometry = previous_geometry
        try:
            self._clear_status_pills()
            self._apply_minimal_layout()
            self.minsize(*self.minimal_mode_minsize)
            self.refresh_tree()
            self.update_idletasks()
            self.geometry(self._compute_minimal_geometry())
            self._queue_tree_refresh()
        except Exception as e:
            self._log(f"[UI] Minimal Mode failed: {e}")
            self.minimal_mode = False
            try:
                self._apply_full_layout()
                self.minsize(*self.full_mode_minsize)
                self.refresh_tree()
                if previous_geometry:
                    self.geometry(previous_geometry)
                self._queue_tree_refresh()
            except Exception as restore_error:
                self._log(f"[UI] Layout restore failed: {restore_error}")

    def exit_minimal_mode(self):
        if not self.minimal_mode:
            return
        self.minimal_mode = False
        previous_geometry = self._full_mode_geometry
        try:
            self._clear_status_pills()
            self._apply_full_layout()
            self.minsize(*self.full_mode_minsize)
            self.refresh_tree()
            if previous_geometry:
                self.geometry(previous_geometry)
            self._queue_tree_refresh()
        except Exception as e:
            self._log(f"[UI] Exit Minimal Mode failed: {e}")
            self.minimal_mode = True
            try:
                self._apply_minimal_layout()
                self.minsize(*self.minimal_mode_minsize)
                self.refresh_tree()
                self.geometry(self._compute_minimal_geometry())
                self._queue_tree_refresh()
            except Exception as restore_error:
                self._log(f"[UI] Minimal layout restore failed: {restore_error}")

    def toggle_minimal_mode(self):
        if self.minimal_mode:
            self.exit_minimal_mode()
        else:
            self.enter_minimal_mode()

    def _round_rect(self, canvas: tk.Canvas, x1: int, y1: int, x2: int, y2: int, radius: int, **kwargs):
        radius = max(0, min(radius, int((x2 - x1) / 2), int((y2 - y1) / 2)))
        points = [
            x1 + radius, y1,
            x2 - radius, y1,
            x2, y1,
            x2, y1 + radius,
            x2, y2 - radius,
            x2, y2,
            x2 - radius, y2,
            x1 + radius, y2,
            x1, y2,
            x1, y2 - radius,
            x1, y1 + radius,
            x1, y1,
        ]
        return canvas.create_polygon(points, smooth=True, splinesteps=24, **kwargs)

    def _clear_status_pills(self):
        for widget in self.status_pill_widgets.values():
            try:
                widget.destroy()
            except Exception:
                pass
        self.status_pill_widgets.clear()

    def _refresh_status_pills(self):
        if not hasattr(self, "tree") or self.tree is None:
            return

        visible_ids = set()
        children = self.tree.get_children()
        if not children:
            self._clear_status_pills()
            return

        for iid in children:
            bbox = self.tree.bbox(iid, "status")
            if not bbox:
                continue

            visible_ids.add(iid)
            x, y, w, h = bbox
            idx = int(iid)
            task = self.settings.tasks[idx]
            raw_status = self.state[idx]["status"] if idx < len(self.state) else "Ready"
            status_text = get_status_display(raw_status, task.enabled)
            kind = get_status_kind(raw_status, task.enabled)
            palette = STATUS_PILL_THEME.get(kind, STATUS_PILL_THEME["ready"])

            pill_h = max(self._s(22), h - self._s(6))
            pill_w = max(self._s(92), min(w - self._s(10), self._s(22) + len(status_text) * self._s(7)))
            pill_y = y + max(1, (h - pill_h) // 2)
            pill_x = x + self._s(10)

            pill = self.status_pill_widgets.get(iid)
            if pill is None or not pill.winfo_exists():
                pill = tk.Canvas(
                    self.tree,
                    width=pill_w,
                    height=pill_h,
                    bg=UI_THEME["panel"],
                    highlightthickness=0,
                    bd=0,
                    relief=tk.FLAT,
                    takefocus=0,
                )
                pill.bind("<Button-1>", lambda e, item=iid: self._select_tree_item(item))
                pill.bind("<Double-Button-1>", lambda e, item=iid: self._toggle_tree_item_ready_disabled(item))
                self.status_pill_widgets[iid] = pill

            pill.place(x=pill_x, y=pill_y, width=pill_w, height=pill_h)
            pill.configure(bg=UI_THEME["panel"])
            pill.delete("all")
            self._round_rect(
                pill,
                1,
                1,
                pill_w - 1,
                pill_h - 1,
                radius=self._s(9),
                fill=palette["bg"],
                outline=palette["border"],
                width=1,
            )
            pill.create_text(
                pill_w // 2,
                pill_h // 2,
                text=status_text,
                fill=palette["text"],
                font=("Segoe UI", max(8, self._s(8)), "bold"),
            )

        stale_ids = [iid for iid in self.status_pill_widgets.keys() if iid not in visible_ids]
        for iid in stale_ids:
            try:
                self.status_pill_widgets[iid].destroy()
            except Exception:
                pass
            self.status_pill_widgets.pop(iid, None)

    def _select_tree_item(self, iid: str):
        try:
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            self.tree.see(iid)
            self._on_tree_selection_changed()
        except Exception:
            pass

    def _toggle_tree_item_ready_disabled(self, iid: str):
        try:
            idx = int(iid)
        except Exception:
            return
        self._select_tree_item(iid)
        self._toggle_ready_disabled([idx])

    def _detect_ue_version(self) -> str:
        ue_path = self.var_ue.get().strip() if hasattr(self, "var_ue") else self.settings.ue_cmd
        parts = ue_path.replace("\\", "/").split("/")
        for part in parts:
            if part.startswith("UE_"):
                return part.replace("_", " ")
        return "Unknown"

    def _update_engine_labels(self):
        ue_path = self.var_ue.get().strip() if hasattr(self, "var_ue") else self.settings.ue_cmd
        detected = "Detected" if ue_path and os.path.exists(ue_path) else "Missing"
        version = self._detect_ue_version()
        if hasattr(self, "status_engine_var"):
            self.status_engine_var.set(f"Engine: {version}")

    def _selected_task(self) -> Optional[RenderTask]:
        sel = self._selected_indices()
        if not sel:
            return None
        idx = sel[0]
        if 0 <= idx < len(self.settings.tasks):
            return self.settings.tasks[idx]
        return None

    def _selected_task_index(self) -> Optional[int]:
        sel = self._selected_indices()
        if not sel:
            return None
        return sel[0]

    def _build_command_preview_for_task(self, task: RenderTask) -> str:
        ue_cmd = self.var_ue.get().strip() or "<UnrealEditor-Cmd.exe>"
        cmd = [
            ue_cmd,
            task.uproject or "<uproject>",
            task.level.split(".")[0] if task.level else "<map>",
            "-game",
            f'-LevelSequence="{task.sequence or "<sequence>"}"',
            f'-MoviePipelineConfig="{task.preset or "<preset>"}"',
            "-log",
        ]
        if bool(self.var_windowed.get()):
            cmd.append("-windowed")
        else:
            cmd.append("-fullscreen")
        cmd += [f"-ResX={int(self.var_resx.get())}", f"-ResY={int(self.var_resy.get())}"]
        if bool(self.var_nts.get()):
            cmd.append("-notexturestreaming")
        extra = (self.var_extra.get() or "").strip()
        if extra:
            cmd += shlex.split(extra)
        if task.output_dir:
            cmd.append(f'-OutputDirectory="{task.output_dir}"')
        return " \\\n".join(cmd)

    def _status_tag_for_index(self, idx: int) -> str:
        if not (0 <= idx < len(self.settings.tasks)):
            return "status_ready"
        task = self.settings.tasks[idx]
        status = self.state[idx].get("status", "Ready") if 0 <= idx < len(self.state) else "Ready"
        if not task.enabled:
            return "status_disabled"
        if status.startswith("Failed") or status.startswith("Cancelled"):
            return "status_failed"
        if status.startswith("Done"):
            return "status_done"
        if status.startswith("Rendering"):
            return "status_rendering"
        if status.startswith("Skipped"):
            return "status_skipped"
        if status == "Queued":
            return "status_queued"
        return "status_ready"

    def _set_tree_item(self, idx: int):
        if self.tree.exists(str(idx)):
            self.tree.item(str(idx), values=self._row_values(idx), tags=(self._status_tag_for_index(idx),))
            if self.minimal_mode:
                self.after_idle(self._autosize_tree_columns)
            self.after_idle(self._refresh_status_pills)

    def _update_queue_stats(self):
        if not hasattr(self, "queue_stats_var"):
            return
        total = len(self.settings.tasks)
        visible = len(self.tree.get_children()) if hasattr(self, "tree") else 0
        enabled = sum(1 for t in self.settings.tasks if t.enabled)
        selected = len(self.tree.selection()) if hasattr(self, "tree") else 0
        self.queue_stats_var.set(f"Total: {total} | Visible: {visible} | Enabled: {enabled} | Selected: {selected}")

    def _on_runtime_options_changed(self, *_args):
        self.settings.auto_minimal_on_render = bool(self.var_auto_minimal.get()) if hasattr(self, "var_auto_minimal") else self.settings.auto_minimal_on_render
        self._update_engine_labels()
        if self.command_preview is not None:
            self._update_command_preview()

    def copy_command_preview(self):
        task = self._selected_task()
        if task is None and self._current_global_idx is not None and 0 <= self._current_global_idx < len(self.settings.tasks):
            task = self.settings.tasks[self._current_global_idx]
        content = "Select a task to inspect the generated command line."
        if task is not None:
            content = self._build_command_preview_for_task(task)
        self.clipboard_clear()
        self.clipboard_append(content)
        self.update_idletasks()
        self._log("[UI] Command preview copied to clipboard.")

    def open_selected_output_dir(self):
        task = self._selected_task()
        if task is None:
            self._log("[UI] Select a task first.")
            return
        path = (task.output_dir or "").strip()
        if not path:
            self._log("[UI] Selected task uses preset default output directory.")
            return
        if not os.path.isdir(path):
            self._log(f"[UI] Output directory not found: {path}")
            return
        try:
            if os.name == "nt":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            self._log(f"[UI] Failed to open output directory: {e}")

    def _update_inspector(self):
        if not self.inspector_vars:
            return
        task = self._selected_task()
        if task is None:
            self.inspector_vars["job"].set("No selection")
            self.inspector_vars["enabled"].set("-")
            self.inspector_vars["uproject"].set("-")
            self.inspector_vars["level"].set("-")
            self.inspector_vars["sequence"].set("-")
            self.inspector_vars["preset"].set("-")
            self.inspector_vars["output"].set("-")
            self.inspector_vars["notes"].set("-")
            self.inspector_vars["validation"].set("Validation: -")
            return

        job_name = soft_name(task.sequence)
        valid = all([task.uproject, task.level, task.sequence, task.preset])

        self.inspector_vars["job"].set(job_name)
        self.inspector_vars["enabled"].set("Yes" if task.enabled else "No")
        self.inspector_vars["uproject"].set(task.uproject or "-")
        self.inspector_vars["level"].set(task.level or "-")
        self.inspector_vars["sequence"].set(task.sequence or "-")
        self.inspector_vars["preset"].set(task.preset or "-")
        self.inspector_vars["output"].set(task.output_dir or "Preset default")
        self.inspector_vars["notes"].set("-")
        self.inspector_vars["validation"].set(f"Validation: {'Ready' if valid else 'Incomplete'}")

    def _update_command_preview(self):
        if self.command_preview is None:
            return
        task = self._selected_task()
        if task is None and self._current_global_idx is not None and 0 <= self._current_global_idx < len(self.settings.tasks):
            task = self.settings.tasks[self._current_global_idx]

        if task is None:
            content = "Select a task to inspect the generated command line."
        else:
            content = self._build_command_preview_for_task(task)

        self.command_preview.config(state="normal")
        self.command_preview.delete("1.0", "end")
        self.command_preview.insert("1.0", content)
        self.command_preview.config(state="disabled")

    def _is_render_active(self) -> bool:
        return bool(self.worker_running or self.process_controller.is_active())

    def _update_render_action_labels(self):
        active = self._is_render_active()
        if hasattr(self, "btn_render_enabled"):
            self.btn_render_enabled.config(text="Add Enabled to Queue" if active else "Render Enabled")
        if hasattr(self, "btn_render_selected"):
            self.btn_render_selected.config(text="Add Selected to Queue" if active else "Render Selected")
        if hasattr(self, "btn_render_all"):
            self.btn_render_all.config(text="Add All to Queue" if active else "Render All")
        if hasattr(self, "btn_queue_selected"):
            self.btn_queue_selected.config(text="Queue Selected")
        if hasattr(self, "render_mode_hint_var"):
            self.render_mode_hint_var.set(
                "Render in progress. New tasks will be appended to queue."
                if active
                else "Ready to render."
            )

    def _update_status_summary(self):
        statuses = [s.get("status", "Ready") for s in self.state]
        queued = sum(1 for s in statuses if s == "Queued")
        running = sum(1 for s in statuses if s.startswith("Rendering"))
        failed = sum(1 for s in statuses if s.startswith("Failed") or s.startswith("Cancelled"))
        done = sum(1 for s in statuses if s.startswith("Done"))

        overall = "Running" if self._is_render_active() else "Idle"
        self.status_overall_var.set(f"State: {overall}")
        self._update_render_action_labels()
        self.status_counts_var.set(f"Queued: {queued} | Running: {running} | Failed: {failed} | Done: {done}")

        task_idx = None
        if self._current_global_idx is not None and 0 <= self._current_global_idx < len(self.settings.tasks):
            task_idx = self._current_global_idx
        else:
            task_idx = self._selected_task_index()

        if task_idx is not None and 0 <= task_idx < len(self.settings.tasks):
            task = self.settings.tasks[task_idx]
            state = self.state[task_idx] if 0 <= task_idx < len(self.state) else default_task_state()
            self.current_task_var.set(f"Current task: {soft_name(task.sequence)}")
            self.current_status_var.set(f"Status: {state.get('status', 'Ready')}")
            self.current_task_time_var.set(f"Task time: {format_runtime_display(state)}")
            progress = state.get("progress")
            if progress is None:
                if state.get("status", "").startswith("Done"):
                    progress = 100
                elif state.get("status", "") == "Queued":
                    progress = 0
            if progress is None:
                progress = 0
            self.render_progress_value.set(float(progress))
            self.current_progress_var.set(f"{int(progress)}%")
        else:
            self.current_task_var.set("Current task: Idle")
            self.current_status_var.set("Status: Idle")
            self.current_task_time_var.set("Task time: —")
            self.render_progress_value.set(0.0)
            self.current_progress_var.set("0%")

        self._update_queue_stats()

    def _on_tree_selection_changed(self, _event=None):
        self.after_idle(self._refresh_status_pills)
        self._update_inspector()
        self._update_command_preview()
        self._update_status_summary()

    def _find_task_index_by_identity(self, task: RenderTask) -> Optional[int]:
        """
        Locate a task by object identity, not dataclass value equality.
        This keeps duplicate tasks (same values) addressable as distinct rows.
        """
        for i, existing in enumerate(self.settings.tasks):
            if existing is task:
                return i
        return None

    def _on_tree_right_click(self, event):
        """Select row under cursor and show the task context menu."""
        try:
            # Focus the row under the cursor
            iid = self.tree.identify_row(event.y)
            if iid:
                # If the row isn't selected, make it the only selection
                if iid not in self.tree.selection():
                    self.tree.selection_set(iid)
                    self.tree.focus(iid)
            # Popup the context menu
            self.ctx_task.tk_popup(event.x_root, event.y_root)
        finally:
            self.ctx_task.grab_release()

    # ---- Runtime state helpers ----
    def _ensure_state(self):
        while len(self.state) < len(self.settings.tasks):
            self.state.append(default_task_state())

    def _row_values(self, i: int):
        t = self.settings.tasks[i]
        st = self.state[i] if i < len(self.state) else default_task_state()
        return (
            "",
            soft_name(t.level),
            soft_name(t.sequence),
            soft_name(t.preset),
            format_runtime_display(st),
            format_state_time_display(st.get("start")),
            format_state_time_display(st.get("end")),
        )

    def _emit_runtime_event(self, event: TaskRuntimeEvent):
        self.ui_queue.put(event)

    def _set_status_async(self, idx: int, text: str):
        self._emit_runtime_event(TaskRuntimeEvent(
            event_type=self._event_type_for_status(text),
            task_index=idx,
            status=text,
        ))

    def _set_progress_async(self, idx: int, progress: int):
        self._emit_runtime_event(TaskRuntimeEvent(
            event_type=TaskRuntimeEventType.PROGRESS_UPDATED,
            task_index=idx,
            progress=progress,
        ))

    def _update_row_async(self, idx: int):
        self.ui_queue.put(("update_row", idx))

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
        if status.startswith(TaskRuntimeStatus.SKIPPED_POLICY):
            return "task_skipped"
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
            elif event.status in (
                TaskRuntimeStatus.QUEUED,
                TaskRuntimeStatus.READY,
                TaskRuntimeStatus.CANCELLED,
                TaskRuntimeStatus.CANCELLED_QUEUE,
            ):
                self.state[idx]["progress"] = 0
        if event.progress is not None:
            self.state[idx]["progress"] = event.progress

        self._set_tree_item(idx)
        return True

    # Tree helpers
    def refresh_tree(self):
        self._ensure_state()
        previous_selection = list(self.tree.selection()) if hasattr(self, "tree") else []
        self.tree.delete(*self.tree.get_children())

        query = self.var_task_filter.get().strip().lower() if hasattr(self, "var_task_filter") else ""
        for i, task in enumerate(self.settings.tasks):
            if self.minimal_mode and not task.enabled:
                continue
            haystack = " ".join([
                task.uproject,
                task.level,
                task.sequence,
                task.preset,
                task.output_dir,
                str(self.state[i].get("start", "")) if i < len(self.state) else "",
                str(self.state[i].get("end", "")) if i < len(self.state) else "",
                self.state[i].get("status", "Ready") if i < len(self.state) else "Ready",
            ]).lower()
            if query and query not in haystack:
                continue
            self.tree.insert("", "end", iid=str(i), values=self._row_values(i), tags=(self._status_tag_for_index(i),))

        visible_selection = [iid for iid in previous_selection if self.tree.exists(iid)]
        if visible_selection:
            self.tree.selection_set(visible_selection)
            self.tree.focus(visible_selection[0])

        self._set_tree_display_columns()
        if self.minimal_mode:
            self._autosize_tree_columns()
        else:
            self._apply_default_tree_columns()

        self.after_idle(self._refresh_status_pills)
        self._update_inspector()
        self._update_command_preview()
        self._update_status_summary()

    def _selected_indices(self) -> List[int]:
        return [int(iid) for iid in self.tree.selection()]

    def on_tree_dblclick(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        column = self.tree.identify_column(event.x)
        if column != "#1":
            return
        self._toggle_tree_item_ready_disabled(iid)

    def on_space_toggle(self, _):
        sel = self._selected_indices()
        if sel:
            self._toggle_ready_disabled(sel)

    # Order helpers
    def move_selected(self, delta: int):
        sel = self._selected_indices()
        if not sel:
            return
        tasks = self.settings.tasks
        state = self.state
        if delta < 0:
            sel_sorted = sorted(sel)
            for i, idx in enumerate(sel_sorted):
                if idx > 0 and (idx - 1) not in sel_sorted:
                    tasks[idx - 1], tasks[idx] = tasks[idx], tasks[idx - 1]
                    state[idx - 1], state[idx] = state[idx], state[idx - 1]
                    sel_sorted[i] = idx - 1
            new_sel = sel_sorted
        else:
            sel_sorted = sorted(sel, reverse=True)
            for i, idx in enumerate(sel_sorted):
                if idx < len(tasks) - 1 and (idx + 1) not in sel_sorted:
                    tasks[idx + 1], tasks[idx] = tasks[idx], tasks[idx + 1]
                    state[idx + 1], state[idx] = state[idx], state[idx + 1]
                    sel_sorted[i] = idx + 1
            new_sel = sel_sorted
        self.refresh_tree()
        self.tree.selection_set([str(i) for i in sorted(new_sel)])
        self.tree.see(str(sorted(new_sel)[0]))

    # Task ops
    def add_task(self):
        dlg = TaskEditor(self)
        self.wait_window(dlg)
        if dlg.result:
            self.settings.tasks.append(dlg.result)
            self.refresh_tree()

    def edit_task(self):
        sel = self._selected_indices()
        if not sel:
            return
        idx = sel[0]
        old_task = self.settings.tasks[idx]
        dlg = TaskEditor(self, self.settings.tasks[idx])
        self.wait_window(dlg)
        if dlg.result:
            dlg.result.enabled = self.settings.tasks[idx].enabled
            # If this task was already queued, remove pending old copies first.
            # Otherwise edited tasks can still run with stale parameters.
            self._remove_tasks_from_runtime_queue([old_task])
            self.settings.tasks[idx] = dlg.result
            self.refresh_tree()

    def duplicate_task(self):
        sel = self._selected_indices()
        if not sel:
            return
        # Iterate from bottom to top so index shifts do not affect
        # which original rows are duplicated.
        for idx in sorted(sel, reverse=True):
            src = self.settings.tasks[idx]
            clone_data = asdict(src)
            clone_data["added_at"] = current_task_timestamp()
            self.settings.tasks.insert(idx + 1, RenderTask(**clone_data))
            self.state.insert(idx + 1, default_task_state())
        self.refresh_tree()

    def remove_task(self):
        sel = sorted(self._selected_indices(), reverse=True)
        if not sel:
            return
        # If there are queued copies of these task objects, remove them too
        # so deleted tasks are not rendered later.
        removed_tasks = [self.settings.tasks[idx] for idx in sel]
        self._remove_tasks_from_runtime_queue(removed_tasks)
        for idx in sel:
            del self.settings.tasks[idx]
            del self.state[idx]
        self.refresh_tree()

    def remove_unchecked_tasks(self):
        """Remove all tasks that are not checked (enabled == False)."""
        removed = 0
        removed_tasks = []
        new_tasks = []
        new_state = []
        for i, t in enumerate(self.settings.tasks):
            if t.enabled:
                new_tasks.append(t)
                if i < len(self.state):
                    new_state.append(self.state[i])
            else:
                removed_tasks.append(t)
                removed += 1
        # Also purge queued runtime entries for tasks that are being removed.
        # Otherwise disabled+removed tasks can still render later from runtime queue.
        self._remove_tasks_from_runtime_queue(removed_tasks)
        self.settings.tasks = new_tasks
        self.state = new_state
        self.refresh_tree()
        self._log(f"[Tasks] Removed {removed} unchecked task(s).")

    def _toggle_ready_disabled(self, indices: List[int]):
        if not indices:
            return
        disabled_now = []
        for idx in indices:
            if not (0 <= idx < len(self.settings.tasks)):
                continue
            current_status = self.state[idx].get("status", "Ready") if 0 <= idx < len(self.state) else "Ready"
            if current_status.startswith("Rendering"):
                continue
            task = self.settings.tasks[idx]
            task.enabled = not task.enabled
            self.state[idx] = default_task_state()
            if not task.enabled:
                disabled_now.append(task)
        if disabled_now:
            self._remove_tasks_from_runtime_queue(disabled_now)
        self.refresh_tree()

    def set_enabled_all(self, val: bool):
        for idx, t in enumerate(self.settings.tasks):
            if self.state[idx].get("status", "Ready").startswith("Rendering"):
                continue
            t.enabled = val
            self.state[idx] = default_task_state()
        if not val:
            self._remove_tasks_from_runtime_queue(self.settings.tasks)
        self.refresh_tree()

    def toggle_selected(self):
        sel = self._selected_indices()
        if not sel:
            return
        self._toggle_ready_disabled(sel)

    # Save/Load JSON (queue)
    def _current_persistence_config(self) -> dict:
        return {
            "ue_cmd": self.var_ue.get().strip(),
            "retries": int(self.var_retries.get()),
            "fail_policy": self.var_policy.get(),
            "kill_timeout_s": int(self.var_kill_timeout.get()),
            "windowed": bool(self.var_windowed.get()),
            "resx": int(self.var_resx.get()),
            "resy": int(self.var_resy.get()),
            "no_texture_streaming": bool(self.var_nts.get()),
            "auto_minimal_on_render": bool(self.var_auto_minimal.get()),
            "extra_cli": self.var_extra.get().strip(),
        }

    def _apply_persistence_config(self, config: dict) -> None:
        self.var_ue.set(config["ue_cmd"])
        self.settings.retries = int(config["retries"])
        self.settings.fail_policy = config["fail_policy"]
        self.settings.kill_timeout_s = int(config["kill_timeout_s"])
        self.settings.windowed = bool(config["windowed"])
        self.settings.resx = int(config["resx"])
        self.settings.resy = int(config["resy"])
        self.settings.no_texture_streaming = bool(config["no_texture_streaming"])
        self.settings.extra_cli = config["extra_cli"]
        self.settings.auto_minimal_on_render = bool(config["auto_minimal_on_render"])

        self.var_retries.set(self.settings.retries)
        self.var_policy.set(self.settings.fail_policy)
        self.var_kill_timeout.set(self.settings.kill_timeout_s)
        self.var_windowed.set(self.settings.windowed)
        self.var_resx.set(self.settings.resx)
        self.var_resy.set(self.settings.resy)
        self.var_nts.set(self.settings.no_texture_streaming)
        self.var_auto_minimal.set(self.settings.auto_minimal_on_render)
        self.var_extra.set(self.settings.extra_cli)

    def load_from_json(self, path: str):
        try:
            config, tasks = PersistenceRepository.load_queue(path, self.settings)
        except PersistenceError as e:
            messagebox.showerror("Load Queue", str(e))
            return
        self._apply_persistence_config(config)
        self.settings.tasks = tasks
        self.state = [default_task_state() for _ in self.settings.tasks]
        self.refresh_tree()
        self._update_engine_labels()
        self._update_command_preview()

    def save_to_json(self, path: str):
        PersistenceRepository.save_queue(path, self._current_persistence_config(), self.settings.tasks)

    def load_json_dialog(self):
        p = filedialog.askopenfilename(title="Load tasks JSON", filetypes=[("JSON", "*.json")])
        if p:
            self.load_from_json(p)

    def save_json_dialog(self):
        p = filedialog.asksaveasfilename(title="Save tasks JSON", defaultextension=".json", filetypes=[("JSON", "*.json")])
        if p:
            self.save_to_json(p)

    # Task I/O (single-task files)
    def load_tasks_dialog(self):
        paths = filedialog.askopenfilenames(title="Load Task JSON(s)", filetypes=[("JSON", "*.json")])
        if not paths:
            return
        loaded = 0
        for p in paths:
            try:
                tasks = PersistenceRepository.load_task_file(p)
                for task in tasks:
                    self.settings.tasks.append(task)
                    self.state.append(default_task_state())
                    loaded += 1
            except PersistenceError as e:
                messagebox.showerror("Load Task", f"{os.path.basename(p)}: {e}")
        if loaded:
            self.refresh_tree()
            messagebox.showinfo("Load Task(s)", f"Loaded {loaded} task(s)")

    def save_selected_tasks_dialog(self):
        sel = self._selected_indices()
        if not sel:
            messagebox.showwarning("Save Task", "Select at least one task in the table.")
            return
        if len(sel) == 1:
            t = self.settings.tasks[sel[0]]
            default_name = f"{soft_name(t.level)}__{soft_name(t.sequence)}__{soft_name(t.preset)}.task.json"
            p = filedialog.asksaveasfilename(title="Save Task JSON", defaultextension=".json", initialfile=default_name,
                                             filetypes=[("JSON", "*.json")])
            if not p:
                return
            self._save_task_to_file(t, p)
            messagebox.showinfo("Save Task", f"Saved: {os.path.basename(p)}")
        else:
            folder = filedialog.askdirectory(title="Select folder to save tasks")
            if not folder:
                return
            count = 0
            for i in sel:
                t = self.settings.tasks[i]
                name = f"{soft_name(t.level)}__{soft_name(t.sequence)}__{soft_name(t.preset)}.task.json"
                p = os.path.join(folder, name)
                self._save_task_to_file(t, p)
                count += 1
            messagebox.showinfo("Save Task(s)", f"Saved {count} task file(s) to\n{folder}")

    def _save_task_to_file(self, t: RenderTask, path: str):
        PersistenceRepository.save_task(path, t)

    # ---- Progress parsing (without regex) ----
    def _extract_progress(self, line: str) -> Optional[int]:
        # Attempt to find a number before the % sign
        if "%" in line:
            i = line.find("%")
            j = i - 1
            while j >= 0 and line[j].isdigit():
                j -= 1
            digits = line[j+1:i]
            if digits.isdigit():
                v = int(digits)
                if 0 <= v <= 100:
                    return v
        # Attempt to find a token like X/Y
        tokens = line.replace("(", " ").replace(")", " ").replace("[", " ").replace("]", " ").split()
        for tok in tokens:
            if "/" in tok:
                parts = tok.split("/")
                if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                    a, b = int(parts[0]), int(parts[1])
                    if b > 0:
                        return max(0, min(100, int(a * 100 / b)))
        # progress: NN or progress=NN
        low = line.lower()
        for sep in (":", "="):
            key = "progress" + sep
            if key in low:
                tail = low.split(key, 1)[1].strip()
                num = ""
                for ch in tail:
                    if ch.isdigit():
                        num += ch
                    else:
                        break
                if num:
                    v = int(num)
                    return max(0, min(100, v))
        return None

    # Run
    def _collect(self, only_enabled=False, only_selected=False) -> List[RenderTask]:
        items = self.settings.tasks
        if only_selected:
            sel_ids = self._selected_indices()
            items = [self.settings.tasks[i] for i in sel_ids]
        if only_enabled:
            items = [t for t in items if t.enabled]
        return items

    def run_all(self):
        tasks = self._collect()
        # If already running, just enqueue
        if self.worker_running or self.process_controller.is_active():
            self._enqueue_tasks(tasks)
            return
        self._run_queue(tasks)

    def run_selected(self):
        tasks = self._collect(only_selected=True)
        if not tasks:
            messagebox.showinfo("Info", "Select at least one task in the table.")
            return
        if self.worker_running or self.process_controller.is_active():
            # Prevent spawning another render process; enqueue instead
            self._enqueue_tasks(tasks)
            return
        self._run_queue(tasks)

    def run_enabled(self):
        tasks = self._collect(only_enabled=True)
        if not tasks:
            messagebox.showinfo("Info", "No enabled tasks to run.")
            return
        if self.worker_running or self.process_controller.is_active():
            self._enqueue_tasks(tasks)
            return
        self._run_queue(tasks)

    def _task_logfile(self, task: RenderTask) -> str:
        base = f"{soft_name(task.level)}__{soft_name(task.sequence)}__{soft_name(task.preset)}"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        logs_dir = os.path.join(os.getcwd(), "mrq_logs")
        os.makedirs(logs_dir, exist_ok=True)
        return os.path.join(logs_dir, f"{ts}_{base}.log")

    def _logs_dir(self) -> str:
        return os.path.join(os.getcwd(), "mrq_logs")

    def open_logs_folder(self):
        try:
            path = self._logs_dir()
            os.makedirs(path, exist_ok=True)
            if os.name == "nt":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            self._log(f"[Logs] {e}")

    # ---- Queue summary log -------------------------------------------------
    def _queue_log_default_path(self) -> str:
        """Default path for queue summary log."""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"Queue_Log_{ts}.log"
        folder = self._logs_dir()
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, name)

    def _format_hms(self, sec: Optional[int]) -> str:
        if sec is None:
            return ""
        sec = max(0, int(sec))
        h, rem = divmod(sec, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    def _collect_queue_rows(self) -> List[str]:
        """Build rows for the queue summary file."""
        rows = []
        # Header
        header = "Level / Sequence / Preset / Start / End / Duration"
        rows.append(header)
        for i, t in enumerate(self.settings.tasks):
            st = self.state[i] if i < len(self.state) else {}
            start_ts = st.get("start")
            end_ts = st.get("end")
            start_str = datetime.fromtimestamp(start_ts).strftime("%Y-%m-%d %H:%M:%S") if start_ts else ""
            end_str = datetime.fromtimestamp(end_ts).strftime("%Y-%m-%d %H:%M:%S") if end_ts else ""
            dur = None
            if start_ts and end_ts:
                dur = int(end_ts - start_ts)
            rows.append(
                f"{soft_name(t.level)} / {soft_name(t.sequence)} / {soft_name(t.preset)} / {start_str} / {end_str} / {self._format_hms(dur)}"
            )
        return rows

    def save_queue_log(self):
        """Save a compact queue summary into mrq_logs/Queue_Log_*.log"""
        try:
            path = self._queue_log_default_path()
            rows = self._collect_queue_rows()
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(rows) + "\n")
            self._log(f"[Logs] Queue summary saved: {os.path.basename(path)}")
        except Exception as e:
            self._log(f"[Logs] Failed to save queue log: {e}")

    def open_last_log_for_selected(self):
        sel = self._selected_indices()
        if not sel:
            self._log("[Logs] Select a task first")
            return
        # find latest log for the first selected task by basename pattern
        t = self.settings.tasks[sel[0]]
        base = f"{soft_name(t.level)}__{soft_name(t.sequence)}__{soft_name(t.preset)}"
        folder = self._logs_dir()
        try:
            files = [f for f in os.listdir(folder) if f.endswith(".log") and f.endswith(f"{base}.log")]
            if not files:
                self._log("[Logs] No logs found for selected task")
                return
            files.sort(reverse=True)
            full = os.path.join(folder, files[0])
            if os.name == "nt":
                os.startfile(full)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", full])
            else:
                subprocess.Popen(["xdg-open", full])
        except Exception as e:
            self._log(f"[Logs] {e}")

    def _run_queue(self, tasks: List[RenderTask]):
        ue_cmd = self.var_ue.get().strip()
        self.settings.auto_minimal_on_render = bool(self.var_auto_minimal.get())
        if not ue_cmd or not os.path.exists(ue_cmd):
            messagebox.showerror("Error", "Specify a valid path to UnrealEditor-Cmd.exe")
            return
        if not tasks and self.runtime_queue.empty():
            if not self.worker_running:
                messagebox.showinfo("Info", "No tasks to run")
            return

        self.stop_all = False
        self.cancel_current_requested = False
        # Preload tasks into runtime queue via helper (sets statuses too)
        if tasks:
            self._enqueue_tasks(tasks, log_prefix="== Enqueued ")
        if self.settings.auto_minimal_on_render and not self.minimal_mode:
            self.after(0, self.enter_minimal_mode)
        retries = int(self.var_retries.get())
        policy = self.var_policy.get()
        kill_timeout = int(self.var_kill_timeout.get())

        # (handled inside _enqueue_tasks)

        def _fmt_hhmmss(sec: int) -> str:
            h, rem = divmod(max(0, int(sec)), 3600)
            m, s = divmod(rem, 60)
            return f"{h:02d}:{m:02d}:{s:02d}"

        def worker():
            self.worker_running = True
            idx = 0
            skip_next_pending = 0
            while True:
                if self.stop_all and self.runtime_queue.empty():
                    break
                try:
                    t = self.runtime_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                idx += 1
                if self.stop_all:
                    break
                if not all([t.uproject, t.level, t.sequence, t.preset]):
                    self._log(f"[{idx}] Skipped: task is incomplete")
                    continue
                gi = self._find_task_index_by_identity(t)

                if skip_next_pending > 0:
                    skip_next_pending -= 1
                    if gi is not None:
                        self._set_status_async(gi, "Skipped (policy)")
                    self._log(f"[{idx}] Skipped by fail policy (skip_next)")
                    continue

                attempt = 0
                logfile = self._task_logfile(t)

                cancelled_current = False
                while attempt <= retries and not self.stop_all:
                    attempt += 1
                    # Build UE command with render options from UI
                    cmd = [
                        ue_cmd,
                        t.uproject,
                        t.level.split(".")[0],
                        "-game",
                        f"-LevelSequence=\"{t.sequence}\"",
                        f"-MoviePipelineConfig=\"{t.preset}\"",
                        "-log",
                    ]
                    # Windowed / Fullscreen and resolution
                    if bool(self.var_windowed.get()):
                        cmd.append("-windowed")
                    else:
                        cmd.append("-fullscreen")
                    try:
                        rx = int(self.var_resx.get())
                        ry = int(self.var_resy.get())
                        cmd += [f"-ResX={rx}", f"-ResY={ry}"]
                    except Exception:
                        pass
                    # No Texture Streaming
                    if bool(self.var_nts.get()):
                        cmd.append("-notexturestreaming")
                    # Extra CLI (split respecting quotes)
                    extra = (self.var_extra.get() or "").strip()
                    if extra:
                        cmd += shlex.split(extra)
                    # Per-task output directory override:
                    # If set, it will override the destination defined in the MRQ Preset.
                    if t.output_dir:
                        cmd.append(f'-OutputDirectory="{t.output_dir}"')
                    self._log(f"[{idx}] Start (try {attempt}/{retries+1}): {' '.join(cmd)}")

                    start_dt = datetime.now()
                    # status
                    if gi is not None:
                        self.state[gi]["start"] = time.time()
                        self._set_status_async(gi, "Rendering 00:00:00")
                        self._current_global_idx = gi

                    try:
                        log_fp = open(logfile, "a", encoding="utf-8")
                        log_fp.write(f"CMD: {' '.join(cmd)}\n")
                        log_fp.write(f"START: {start_dt.strftime('%Y-%m-%d %H:%M:%S')}\n")
                        active_process = self.process_controller.launch(cmd)
                    except Exception as e:
                        self._log(f"[{idx}] Failed to start: {e}")
                        break

                    # --- Update status with MM:SS timer every second
                    # IMPORTANT: bind the ticker to THIS process, not self.current_process,
                    # so it cannot continue when the next task starts.
                    def tick_elapsed(gidx: Optional[int], proc: subprocess.Popen):
                        try:
                            while proc and proc.poll() is None and not self.stop_all:
                                if gidx is not None and self.state[gidx]["start"]:
                                    elapsed = int(time.time() - self.state[gidx]["start"])
                                    h, rem = divmod(elapsed, 3600)
                                    m, s = divmod(rem, 60)
                                    self._set_status_async(gidx, f"Rendering {h:02d}:{m:02d}:{s:02d}")
                                time.sleep(1.0)
                        except Exception:
                            pass

                    # --- Forward stdout to log (without % progress attempts)
                    def pump(proc: subprocess.Popen, gidx: Optional[int]):
                        try:
                            if proc.stdout:
                                for line in proc.stdout:
                                    if self.stop_all:
                                        break
                                    self._log(line.rstrip())
                                    log_fp.write(line)
                                    progress = self._extract_progress(line)
                                    if progress is not None and gidx is not None:
                                        self._set_progress_async(gidx, progress)
                        except Exception as ex:
                            self._log(f"[pump] {ex}")
                        finally:
                            try:
                                log_fp.flush()
                            except Exception:
                                pass

                    th_pump = threading.Thread(target=pump, args=(active_process, gi), daemon=True)
                    th_pump.start()
                    # Pass the concrete process handle to the ticker
                    th_tick = threading.Thread(target=tick_elapsed, args=(gi, active_process), daemon=True)
                    th_tick.start()
                    rc = active_process.wait()
                    self._log(f"[{idx}] Exit code: {rc}")
                    # Stop ticker ASAP for this process
                    try:
                        th_tick.join(timeout=0.2)
                    except Exception:
                        pass
                    self.process_controller.clear_if_current(active_process)
                    try:
                        end_dt = datetime.now()
                        log_fp.write(f"END: {end_dt.strftime('%Y-%m-%d %H:%M:%S')}\n")
                        log_fp.write(f"EXIT: {rc}\n")
                        log_fp.close()
                    except Exception:
                        pass

                    if gi is not None:
                        self.state[gi]["end"] = time.time()
                        dur = None
                        if self.state[gi]["start"]:
                            dur = int(self.state[gi]["end"] - self.state[gi]["start"])  # seconds

                    if self.cancel_current_requested:
                        self.cancel_current_requested = False
                        cancelled_current = True
                        if gi is not None:
                            self._set_status_async(gi, "Cancelled")
                            if self.state[gi].get("start") and self.state[gi].get("end"):
                                dur_txt = _fmt_hhmmss(int(self.state[gi]["end"] - self.state[gi]["start"]))
                                self._log(f"[{idx}] Current task cancelled by user | Duration: {dur_txt}")
                            else:
                                self._log(f"[{idx}] Current task cancelled by user")
                        break
                    if rc == 0:
                        if gi is not None:
                            if dur is not None:
                                self._set_status_async(gi, f"Done ({_fmt_hhmmss(dur)})")
                            else:
                                self._set_status_async(gi, "Done")
                            # Log timing summary (also to UI log)
                            if self.state[gi].get("start") and self.state[gi].get("end"):
                                dur_txt = _fmt_hhmmss(int(self.state[gi]["end"] - self.state[gi]["start"]))
                                start_txt = datetime.fromtimestamp(self.state[gi]["start"]).strftime("%Y-%m-%d %H:%M:%S")
                                end_txt = datetime.fromtimestamp(self.state[gi]["end"]).strftime("%Y-%m-%d %H:%M:%S")
                                self._log(f"[{idx}] Start: {start_txt} | End: {end_txt} | Duration: {dur_txt}")
                        break
                    else:
                        if policy == "stop_queue":
                            if gi is not None:
                                self._set_status_async(gi, f"Failed (rc={rc})")
                            self._log(f"[{idx}] Fail → stop queue by policy")
                            self.stop_all = True
                            break
                        if attempt <= retries:
                            self._log(f"[{idx}] Will retry…")
                        else:
                            if gi is not None:
                                self._set_status_async(gi, f"Failed (rc={rc})")
                            self._log(f"[{idx}] Failed after {retries+1} attempt(s)")
                            if policy == "skip_next":
                                skip_next_pending = 1
                                self._log(f"[{idx}] Policy skip_next: next task will be skipped")
                        if attempt > retries:
                            break

                if cancelled_current and not self.stop_all:
                    continue

                if self.stop_all:
                    self._log("[Cancel] Stop-all while processing queue")
                    break

            if self.stop_all:
                self._clear_pending_runtime_queue(TaskRuntimeStatus.CANCELLED_QUEUE)
            self._emit_runtime_event(TaskRuntimeEvent(event_type=TaskRuntimeEventType.QUEUE_COMPLETED))
            self._log("== Queue complete ==")
            self._current_global_idx = None
            self.worker_running = False

        # Start worker if not already running
        if not self.worker_running:
            threading.Thread(target=worker, daemon=True).start()

    def cancel_current(self):
        if self.process_controller.is_active():
            self.cancel_current_requested = True
            try:
                self.process_controller.stop_current(int(self.var_kill_timeout.get()))
            except Exception as e:
                self.cancel_current_requested = False
                self._log(f"[Cancel] Error: {e}")
        else:
            self._log("[Cancel] No running process")
    def cancel_all(self):
        self.stop_all = True
        self.cancel_current()
        self._clear_pending_runtime_queue(TaskRuntimeStatus.CANCELLED_QUEUE)
        self._log("[Cancel] Stop-all requested.")

    def _current_running_task_for_queue(self) -> Optional[RenderTask]:
        if self._current_global_idx is not None and 0 <= self._current_global_idx < len(self.settings.tasks):
            return self.settings.tasks[self._current_global_idx]
        return None

    def _task_identity_set_from_runtime_queue(self) -> set:
        return self.runtime_queue.task_identity_set()

    def _enqueue_tasks(self, tasks: List[RenderTask], mark_queued: bool = True, log_prefix: str = "[+] Added "):
        """Enqueue tasks through the runtime queue coordinator."""
        changed = self.runtime_queue.enqueue_tasks(tasks, mark_queued=mark_queued, log_prefix=log_prefix)
        if changed:
            self.refresh_tree()

    def _clear_pending_runtime_queue(self, status_text: str = TaskRuntimeStatus.CANCELLED_QUEUE):
        """Remove waiting tasks through the runtime queue coordinator."""
        removed = self.runtime_queue.clear_pending(status_text)
        if removed:
            self._emit_runtime_event(TaskRuntimeEvent(
                event_type=TaskRuntimeEventType.QUEUE_CLEARED,
                payload={"removed": removed},
            ))

    def _remove_tasks_from_runtime_queue(self, tasks_to_remove: List[RenderTask]):
        """Remove specific pending task objects through the runtime queue coordinator."""
        self.runtime_queue.remove_tasks(tasks_to_remove)

    def enqueue_selected_or_enabled(self):
        """
        Add Task(s) to Queue:
        - If selection exists: enqueue selected tasks.
        - Else: enqueue all enabled tasks.
        If no worker is running, start the queue worker.
        """
        sel = self._collect(only_selected=True)
        tasks = sel if sel else self._collect(only_enabled=True)
        if not tasks:
            messagebox.showinfo("Info", "Nothing to enqueue: select tasks or enable some tasks.")
            return
        self._enqueue_tasks(tasks)
        if not self.worker_running:
            # Start worker without adding new items (they're already in the runtime queue)
            self._run_queue([])

    # Logging & UI queues (thread-safe)
    def _log(self, msg: str):
        self.log_queue.put(msg)

    def _drain_queues(self):
        status_changed = False
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log.insert("end", msg + "\n")
                self.log.see("end")
        except queue.Empty:
            pass

        try:
            while True:
                item = self.ui_queue.get_nowait()
                if not item:
                    break
                if isinstance(item, TaskRuntimeEvent):
                    status_changed = self._apply_runtime_event(item) or status_changed
                    continue

                kind = item[0]
                if kind == "update_row":
                    _, idx = item
                    self._set_tree_item(idx)
                    status_changed = True
        except queue.Empty:
            pass

        if status_changed:
            self._update_inspector()
            self._update_command_preview()
            self._update_status_summary()

        self.after(50, self._drain_queues)

    def browse_ue(self):
        p = filedialog.askopenfilename(title="Select UnrealEditor-Cmd.exe",
                                       filetypes=[("UnrealEditor-Cmd", "UnrealEditor-Cmd.exe"), ("Exe", "*.exe"), ("All", "*.*")])
        if p:
            self.var_ue.set(p)
            self._update_engine_labels()
            self._update_command_preview()

    # ---- Session total time helpers ----
    def _compute_session_total_seconds(self) -> int:
        """Sum of finished task durations plus the currently running elapsed."""
        total = 0
        for i, st in enumerate(self.state):
            start = st.get("start")
            end = st.get("end")
            if start and end:
                total += max(0, int(end - start))
        # If there is a running task, include its current elapsed
        gi = self._current_global_idx
        if gi is not None and 0 <= gi < len(self.state):
            st = self.state[gi]
            if st.get("start") and self.process_controller.is_active():
                total += max(0, int(time.time() - st["start"]))
        return total

    def _tick_session_total(self):
        """Update the fixed label with HH:MM:SS."""
        try:
            sec = self._compute_session_total_seconds()
            h, rem = divmod(sec, 3600)
            m, s = divmod(rem, 60)
            self.session_total_var.set(f"Session total: {h:02d}:{m:02d}:{s:02d}")
            if self.minimal_mode and hasattr(self, "tree"):
                self.after_idle(self._autosize_tree_columns)
                self.after_idle(self._fit_minimal_width_only)
        finally:
            # Re-schedule periodic update
            self.after(500, self._tick_session_total)

    # ---- Status maintenance helpers ----
    def clear_status_selected(self):
        """
        Clear Status: reset status/progress/timestamps for selected tasks.
        Useful before re-rendering.
        """
        sel = self._selected_indices()
        if not sel:
            messagebox.showinfo("Clear Status", "Select at least one task to clear its status.")
            return
        self._ensure_state()
        for idx in sel:
            if 0 <= idx < len(self.state):
                self.state[idx] = default_task_state()
                self._update_row_async(idx)
        self._log(f"[Status] Cleared status for {len(sel)} task(s).")



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
        from PySide6.QtCore import QEvent, Qt, QTimer
        from PySide6.QtGui import QColor, QBrush, QPalette, QFont, QPainter, QPen
        from PySide6.QtWidgets import (
            QApplication, QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
            QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
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
            self.level_edit = QLineEdit(task.level if task else "")
            self.sequence_edit = QLineEdit(task.sequence if task else "")
            self.preset_edit = QLineEdit(task.preset if task else "")
            self.output_edit = QLineEdit(task.output_dir if task else "")

            rows = (
                ("Project (.uproject)", self.project_edit, self._browse_project),
                ("Map (SoftObjectPath)", self.level_edit, self._browse_level),
                ("Level Sequence", self.sequence_edit, self._browse_sequence),
                ("MRQ Preset", self.preset_edit, self._browse_preset),
                ("Output Directory", self.output_edit, self._browse_output),
            )
            for row, (label_text, edit, browse_cb) in enumerate(rows):
                form.addWidget(QLabel(label_text), row, 0)
                form.addWidget(edit, row, 1)
                button = QPushButton("Browse")
                button.clicked.connect(browse_cb)
                form.addWidget(button, row, 2)

            buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            buttons.accepted.connect(self._accept)
            buttons.rejected.connect(self.reject)
            layout.addWidget(buttons)
            self.resize(760, 220)

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
                edit.setText(fs_to_soft_object(path))
            except Exception as exc:
                QMessageBox.critical(self, title, str(exc))

        def _browse_output(self) -> None:
            path = QFileDialog.getExistingDirectory(self, "Select Output Directory")
            if path:
                self.output_edit.setText(path.replace("\\", "/"))

        def _accept(self) -> None:
            task = RenderTask(
                uproject=self.project_edit.text().strip(),
                level=self.level_edit.text().strip(),
                sequence=self.sequence_edit.text().strip(),
                preset=self.preset_edit.text().strip(),
                output_dir=self.output_edit.text().strip(),
                notes=(self.source_task.notes if self.source_task else ""),
                added_at=(self.source_task.added_at if self.source_task else current_task_timestamp()),
                enabled=(self.source_task.enabled if self.source_task else True),
            )
            if not all([task.uproject, task.level, task.sequence, task.preset]):
                QMessageBox.critical(self, "Validation", "Fill in all required fields.")
                return
            self.result = task
            self.accept()

    class QtMRQShell(QMainWindow):
        """Qt queue workspace backed by shared task/settings/core models."""

        COLUMNS = ("Order", "Status", "Level", "Sequence", "Preset", "Running Time", "Start", "End")

        def __init__(self):
            super().__init__()
            self.settings = AppSettings()
            self.state: List[dict] = []
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
            self.inspector_labels = {}
            self.setWindowTitle(f"MRQ Launcher (Qt Shell) ver {APP_VERSION}")
            self.resize(1420, 860)
            self.full_minimum_size = (1120, 680)
            self.minimal_minimum_size = (560, 360)
            self.setMinimumSize(*self.full_minimum_size)
            self._build_ui()
            self.refresh_queue_view()
            self.event_timer = QTimer(self)
            self.event_timer.timeout.connect(self._drain_runtime_events)
            self.event_timer.start(100)

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

            top_row = QHBoxLayout()
            title_block = QVBoxLayout()
            title = QLabel("MRQ Launcher CLI")
            title.setObjectName("TitleLabel")
            subtitle = self._muted_label("Qt runtime workspace")
            title_block.addWidget(title)
            title_block.addWidget(subtitle)
            top_row.addLayout(title_block, 1)
            load_button = self._mark_button(QPushButton("Load Queue"))
            load_button.clicked.connect(self.load_queue_dialog)
            save_button = self._mark_button(QPushButton("Save Queue"))
            save_button.clicked.connect(self.save_queue_dialog)
            minimal_button = self._mark_button(QPushButton("Minimal Mode"), "primary")
            minimal_button.clicked.connect(self.enter_minimal_mode)
            top_row.addWidget(load_button)
            top_row.addWidget(save_button)
            top_row.addWidget(minimal_button)
            layout.addLayout(top_row)

            self._build_render_options_panel(layout)
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
            self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
            self.table.setShowGrid(False)
            self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
            self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
            self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            self.table.setItemDelegateForColumn(0, QtOrderBadgeDelegate(self.table))
            self.table.setItemDelegateForColumn(1, QtStatusPillDelegate(self.table))
            self.table.setColumnWidth(0, 68)
            self.table.setColumnWidth(1, 116)
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
            self._add_context_action(menu, "Rebuild Order From List", self.toggle_all_ready_disabled, has_tasks)
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
                ("output", "Output Directory: -"), ("validation", "Validation: -"),
            ):
                label = QLabel(text)
                label.setObjectName("InspectorField")
                label.setWordWrap(True)
                label.setMinimumWidth(0)
                label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
                self.inspector_labels[key] = label
                layout.addWidget(label)
            layout.addStretch(1)
            copy_button = self._mark_button(QPushButton("Copy Command"), "primary")
            copy_button.clicked.connect(self.copy_command_preview)
            layout.addWidget(copy_button)
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
                ("Append Selected to Render Queue", self.append_selected_to_render_queue, "secondary"),
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
                ("Open Logs Folder", self.open_logs_folder),
                ("Open Last Log", self.open_last_log_for_selected),
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
            save_log_button = self._mark_button(QPushButton("Save Queue Log"))
            save_log_button.clicked.connect(self.save_queue_log)
            log_title_row.addWidget(save_log_button)
            load_task_button = self._mark_button(QPushButton("Load Task(s)"))
            load_task_button.clicked.connect(self.load_task_dialog)
            log_title_row.addWidget(load_task_button)
            save_task_button = self._mark_button(QPushButton("Save Selected Task(s)"))
            save_task_button.clicked.connect(self.save_selected_tasks_dialog)
            log_title_row.addWidget(save_task_button)
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
            for column in (6, 7):
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
                values = (
                    str(order) if order is not None else "",
                    get_status_display(state.get("status", "Ready"), task.enabled),
                    soft_name(task.level), soft_name(task.sequence), soft_name(task.preset),
                    format_runtime_display(state), format_state_time_display(state.get("start")),
                    format_state_time_display(state.get("end")),
                )
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setData(Qt.UserRole, task_index)
                    self._apply_status_item_style(item, task, state, column)
                    self.table.setItem(row, column, item)
            self.table.resizeColumnsToContents()
            self.table.setColumnWidth(0, 68)
            self.table.setColumnWidth(1, 116)
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
                tasks = [self.settings.tasks[idx] for idx in self._ordered_task_indices()]
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

        def _update_inspector(self) -> None:
            task = self._selected_task()
            if task is None:
                values = {
                    "job": "Job Name: No selection", "enabled": "Enabled: -", "project": "Project: -",
                    "level": "Level: -", "sequence": "Sequence: -", "preset": "Preset: -",
                    "output": "Output Directory: -", "validation": "Validation: -",
                }
            else:
                valid = all([task.uproject, task.level, task.sequence, task.preset])
                values = {
                    "job": f"Job Name: {soft_name(task.sequence)}", "enabled": f"Enabled: {'Yes' if task.enabled else 'No'}",
                    "project": f"Project: {task.uproject or '-'}", "level": f"Level: {task.level or '-'}",
                    "sequence": f"Sequence: {task.sequence or '-'}", "preset": f"Preset: {task.preset or '-'}",
                    "output": f"Output Directory: {task.output_dir or 'Preset default'}",
                    "validation": f"Validation: {'Ready' if valid else 'Incomplete'}",
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

        def load_queue_dialog(self) -> None:
            path, _ = QFileDialog.getOpenFileName(self, "Load Queue", "", "JSON (*.json);;All Files (*.*)")
            if not path:
                return
            try:
                config, tasks = PersistenceRepository.load_queue(path, self.settings)
            except PersistenceError as exc:
                QMessageBox.critical(self, "Load Queue", str(exc))
                return
            for key, value in config.items():
                if hasattr(self.settings, key):
                    setattr(self.settings, key, value)
            self._apply_settings_to_option_controls()
            self.settings.tasks = tasks
            self.state = [default_task_state() for _ in self.settings.tasks]
            self.runtime_queue.clear_pending(TaskRuntimeStatus.CANCELLED_QUEUE)
            self._rebuild_order_for_enabled_tasks()
            self.refresh_queue_view()
            self._append_log(f"[Qt] Loaded queue: {path}")

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
            self._append_log(f"[Qt] Saved queue: {path}")

        def _default_task_filename(self, task: RenderTask) -> str:
            return f"{soft_name(task.level)}__{soft_name(task.sequence)}__{soft_name(task.preset)}.task.json"

        def add_task_dialog(self) -> None:
            dialog = QtTaskEditor(self)
            if dialog.exec() != QDialog.Accepted or dialog.result is None:
                return
            self.settings.tasks.append(dialog.result)
            self.state.append(default_task_state())
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
            """Rebuild the session render order from the current table order."""
            if not self.settings.tasks:
                return
            if self.worker_running or self.process_controller.is_active():
                QMessageBox.information(self, "Toggle All", "Stop the active render before rebuilding the queue order.")
                return

            self._ensure_state()
            self.runtime_queue.clear_pending(TaskRuntimeStatus.CANCELLED_QUEUE)
            self.queue_order_by_task_id.clear()
            order = 1
            for idx, task in enumerate(self.settings.tasks):
                state = self.state[idx] if idx < len(self.state) else default_task_state()
                if state.get("status", "Ready").startswith(TaskRuntimeStatus.RENDERING):
                    continue
                task.enabled = True
                self.state[idx] = default_task_state()
                self.queue_order_by_task_id[id(task)] = order
                order += 1
            self.refresh_queue_view()
            self._append_log(f"[Qt] Rebuilt session queue order for {order - 1} task(s).")

        def render_selected(self) -> None:
            tasks = self._collect(only_selected=True)
            if not tasks:
                QMessageBox.information(self, "Render Selected", "Select at least one task in the table.")
                return
            self._run_queue(tasks)

        def render_enabled(self) -> None:
            tasks = self._collect(only_enabled=True)
            if not tasks:
                QMessageBox.information(self, "Render Enabled", "No ordered ready tasks to run. Double-click tasks or use Toggle All first.")
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
            self.append_selected_to_render_queue()

        def _enqueue_tasks(self, tasks: List[RenderTask]) -> bool:
            changed = self.runtime_queue.enqueue_tasks(tasks, mark_queued=True, log_prefix="[Qt] Queued ")
            if changed:
                self.refresh_queue_view()
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
                QMessageBox.information(self, "Render", "No tasks to run")
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
            rows = ["Level / Sequence / Preset / Start / End / Duration"]
            for idx, task in enumerate(self.settings.tasks):
                state = self.state[idx] if idx < len(self.state) else {}
                start_ts = state.get("start")
                end_ts = state.get("end")
                start_text = datetime.fromtimestamp(start_ts).strftime("%Y-%m-%d %H:%M:%S") if start_ts else ""
                end_text = datetime.fromtimestamp(end_ts).strftime("%Y-%m-%d %H:%M:%S") if end_ts else ""
                duration = int(end_ts - start_ts) if start_ts and end_ts else None
                rows.append(
                    f"{soft_name(task.level)} / {soft_name(task.sequence)} / {soft_name(task.preset)} / "
                    f"{start_text} / {end_text} / {self._format_hms(duration)}"
                )
            return rows

        def save_queue_log(self) -> None:
            try:
                path = self._queue_log_default_path()
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("\n".join(self._collect_queue_log_rows()) + "\n")
                self._append_log(f"[Qt Logs] Queue summary saved: {os.path.basename(path)}")
            except Exception as exc:
                QMessageBox.critical(self, "Save Queue Log", str(exc))

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
    apply_qt_dark_theme(app)
    window = QtMRQShell()
    window.show()
    return app.exec()

# -------------------------------------------------
# Entrypoint
# -------------------------------------------------

if __name__ == "__main__":
    if "--qt" in sys.argv:
        raise SystemExit(run_qt_shell())
    app = MRQLauncher()
    app.mainloop()
