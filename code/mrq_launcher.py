import os
import subprocess
import json
import queue
import threading
import time
import sys
import shlex
from dataclasses import dataclass, asdict, field
from typing import List, Optional
from datetime import datetime

import tkinter as tk
from tkinter import filedialog, messagebox, StringVar
from tkinter import ttk

# -------------------------------------------------
# App meta
# -------------------------------------------------

APP_VERSION = "1.5.2"

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


def default_task_state() -> dict:
    return {"status": "Ready", "progress": None, "start": None, "end": None}


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
        self.configure(bg=UI_THEME["panel"])

        self.var_uproj = StringVar(value=(task.uproject if task else ""))
        self.var_level = StringVar(value=(task.level if task else ""))
        self.var_seq = StringVar(value=(task.sequence if task else ""))
        self.var_preset = StringVar(value=(task.preset if task else ""))
        self.var_output_dir = StringVar(value=(task.output_dir if task else ""))
        self.var_notes = StringVar(value=(task.notes if task else ""))

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

        tk.Label(frm, text="Notes", bg=UI_THEME["panel"], fg=UI_THEME["text"]).pack(anchor="w")
        tk.Entry(frm, textvariable=self.var_notes, width=95, bg=UI_THEME["entry"], fg=UI_THEME["text"], insertbackground=UI_THEME["text"], relief=tk.FLAT, bd=0, highlightthickness=1, highlightbackground=UI_THEME["border"], highlightcolor=UI_THEME["accent"]).pack(fill="x")

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
            notes=self.var_notes.get().strip(),
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
        self.minsize(self._s(1280), self._s(760))
        self.settings = AppSettings()
        self.current_process: Optional[subprocess.Popen] = None
        self._current_global_idx: Optional[int] = None
        self.stop_all = False
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.ui_queue: "queue.Queue[tuple]" = queue.Queue()
        self.state: List[dict] = []  # {status, progress, start, end}
        # --- New: shared runtime task queue and worker flag
        self.runtime_q: "queue.Queue[RenderTask]" = queue.Queue()
        self.worker_running: bool = False
        # Session time label data
        self._session_total_label: Optional[tk.Label] = None
        self.command_preview: Optional[tk.Text] = None
        self.inspector_vars = {}
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

        shell = tk.Frame(self, bg=UI_THEME["bg"])
        shell.pack(fill=tk.BOTH, expand=True)

        self.header_panel = self._create_panel(shell, padx=14, pady=12)
        self.header_panel.pack(fill=tk.X, padx=self._s(12), pady=(self._s(12), self._s(8)))
        self._build_header(self.header_panel)

        body = tk.Frame(shell, bg=UI_THEME["bg"])
        body.pack(fill=tk.BOTH, expand=True, padx=self._s(12), pady=(0, self._s(8)))

        upper = tk.Frame(body, bg=UI_THEME["bg"])
        upper.pack(fill=tk.BOTH, expand=True)

        self.queue_panel = self._create_panel(upper, padx=12, pady=12)
        self.queue_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, self._s(8)))
        self._build_queue_workspace(self.queue_panel)

        self.inspector_panel = self._create_panel(upper, width=self._s(360), padx=12, pady=12)
        self.inspector_panel.pack(side=tk.RIGHT, fill=tk.Y)
        self.inspector_panel.pack_propagate(False)
        self._build_inspector_panel(self.inspector_panel)

        self.bottom_panel = self._create_panel(body, padx=12, pady=12)
        self.bottom_panel.pack(fill=tk.BOTH, expand=False, pady=(self._s(8), 0))
        self._build_bottom_panel(self.bottom_panel)

        self.status_bar = tk.Frame(shell, bg=UI_THEME["panel_alt"], highlightthickness=1, highlightbackground=UI_THEME["border"])
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
        m_run.add_command(label="Cancel Current", command=self.cancel_current)
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
        self._section_title(parent, "Render Queue", "Main operational surface")

        toolbar = tk.Frame(parent, bg=UI_THEME["panel"])
        toolbar.pack(fill=tk.X, pady=(0, 10))

        left = tk.Frame(toolbar, bg=UI_THEME["panel"])
        left.pack(side=tk.LEFT)
        self._make_button(left, "Add Job", self.add_task).pack(side=tk.LEFT, padx=(0, 6))
        self._make_button(left, "Edit", self.edit_task).pack(side=tk.LEFT, padx=(0, 6))
        self._make_button(left, "Duplicate", self.duplicate_task).pack(side=tk.LEFT, padx=(0, 6))
        self._make_button(left, "Remove", self.remove_task).pack(side=tk.LEFT, padx=(0, 6))
        self._make_button(left, "Move Up", lambda: self.move_selected(-1)).pack(side=tk.LEFT, padx=(0, 6))
        self._make_button(left, "Move Down", lambda: self.move_selected(1)).pack(side=tk.LEFT, padx=(0, 6))
        self._make_button(left, "Toggle", self.toggle_selected).pack(side=tk.LEFT)

        right = tk.Frame(toolbar, bg=UI_THEME["panel"])
        right.pack(side=tk.RIGHT, fill=tk.X)
        tk.Label(right, text="Filter", bg=UI_THEME["panel"], fg=UI_THEME["muted"], font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(0, 8))
        self.var_task_filter = StringVar()
        self.var_task_filter.trace_add("write", lambda *_: self.refresh_tree())
        self.filter_entry = self._make_entry(right, textvariable=self.var_task_filter, width=24)
        self.filter_entry.pack(side=tk.LEFT)

        stats = tk.Frame(parent, bg=UI_THEME["panel"])
        stats.pack(fill=tk.X, pady=(0, 10))
        self.queue_stats_var = StringVar(value="Total: 0 | Visible: 0 | Enabled: 0 | Selected: 0")
        tk.Label(stats, textvariable=self.queue_stats_var, bg=UI_THEME["panel"], fg=UI_THEME["muted"], font=("Segoe UI", 9)).pack(side=tk.LEFT)

        tree_shell = tk.Frame(parent, bg=UI_THEME["panel"])
        tree_shell.pack(fill=tk.BOTH, expand=True)

        cols = ("enabled", "job", "level", "sequence", "preset", "status", "notes")
        self.tree = ttk.Treeview(tree_shell, columns=cols, show="headings", selectmode="extended")
        for name, title, width, anchor in (
            ("enabled", "On", self._s(48), "center"),
            ("job", "Job", self._s(210), "w"),
            ("level", "Level", self._s(180), "w"),
            ("sequence", "Sequence", self._s(200), "w"),
            ("preset", "Preset", self._s(280), "w"),
            ("status", "Status", self._s(180), "w"),
            ("notes", "Notes", self._s(280), "w"),
        ):
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, anchor=anchor, stretch=(name != "enabled"))

        self.tree.tag_configure("status_ready", foreground=UI_THEME["text"])
        self.tree.tag_configure("status_queued", foreground="#FFD28A")
        self.tree.tag_configure("status_rendering", foreground="#A8D1FF")
        self.tree.tag_configure("status_done", foreground="#8BE2B5")
        self.tree.tag_configure("status_failed", foreground="#FF9AA9")
        self.tree.tag_configure("status_disabled", foreground=UI_THEME["muted"])
        self.tree.tag_configure("status_skipped", foreground="#D7C7FF")

        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.tree.bind("<Double-1>", self.on_tree_dblclick)
        self.tree.bind("<space>", self.on_space_toggle)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_selection_changed)

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

        sb = ttk.Scrollbar(tree_shell, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        hsb = ttk.Scrollbar(parent, orient="horizontal", command=self.tree.xview)
        self.tree.configure(xscrollcommand=hsb.set)
        hsb.pack(fill=tk.X, pady=(8, 0))

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
        self._make_button(controls, "Render Enabled", self.run_enabled, variant="primary").pack(side=tk.LEFT, padx=(0, 6))
        self._make_button(controls, "Render Selected", self.run_selected).pack(side=tk.LEFT, padx=(0, 6))
        self._make_button(controls, "Queue Selected", self.enqueue_selected_or_enabled).pack(side=tk.LEFT, padx=(0, 6))
        self._make_button(controls, "Render All", self.run_all).pack(side=tk.LEFT, padx=(0, 6))
        self._make_button(controls, "Clear Status", self.clear_status_selected).pack(side=tk.LEFT, padx=(0, 6))
        self._make_button(controls, "Stop All", self.cancel_all, variant="danger").pack(side=tk.LEFT)

        logs_actions = tk.Frame(top, bg=UI_THEME["panel"])
        logs_actions.pack(side=tk.RIGHT)
        self._make_button(logs_actions, "Open Logs Folder", self.open_logs_folder).pack(side=tk.LEFT, padx=(0, 6))
        self._make_button(logs_actions, "Open Last Log", self.open_last_log_for_selected).pack(side=tk.LEFT)

        info_row = tk.Frame(parent, bg=UI_THEME["panel"])
        info_row.pack(fill=tk.X, pady=(12, 10))
        self.current_task_var = StringVar(value="Current task: Idle")
        self.current_status_var = StringVar(value="Status: Idle")
        self.current_progress_var = StringVar(value="0%")
        self.render_progress_value = tk.DoubleVar(value=0.0)

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
        self._session_total_label = tk.Label(progress_shell, text="Session total: 00:00:00", bg=UI_THEME["panel"], fg=UI_THEME["muted"], font=("Segoe UI", 10))
        self._session_total_label.pack(side=tk.LEFT, padx=(6, 0))

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

    def _update_queue_stats(self):
        if not hasattr(self, "queue_stats_var"):
            return
        total = len(self.settings.tasks)
        visible = len(self.tree.get_children()) if hasattr(self, "tree") else 0
        enabled = sum(1 for t in self.settings.tasks if t.enabled)
        selected = len(self.tree.selection()) if hasattr(self, "tree") else 0
        self.queue_stats_var.set(f"Total: {total} | Visible: {visible} | Enabled: {enabled} | Selected: {selected}")

    def _on_runtime_options_changed(self, *_args):
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

        job_name = task.notes.strip() or soft_name(task.sequence)
        valid = all([task.uproject, task.level, task.sequence, task.preset])

        self.inspector_vars["job"].set(job_name)
        self.inspector_vars["enabled"].set("Yes" if task.enabled else "No")
        self.inspector_vars["uproject"].set(task.uproject or "-")
        self.inspector_vars["level"].set(task.level or "-")
        self.inspector_vars["sequence"].set(task.sequence or "-")
        self.inspector_vars["preset"].set(task.preset or "-")
        self.inspector_vars["output"].set(task.output_dir or "Preset default")
        self.inspector_vars["notes"].set(task.notes or "-")
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

    def _update_status_summary(self):
        statuses = [s.get("status", "Ready") for s in self.state]
        queued = sum(1 for s in statuses if s == "Queued")
        running = sum(1 for s in statuses if s.startswith("Rendering"))
        failed = sum(1 for s in statuses if s.startswith("Failed") or s.startswith("Cancelled"))
        done = sum(1 for s in statuses if s.startswith("Done"))

        overall = "Running" if self.worker_running or (self.current_process and self.current_process.poll() is None) else "Idle"
        self.status_overall_var.set(f"State: {overall}")
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
            self.render_progress_value.set(0.0)
            self.current_progress_var.set("0%")

        self._update_queue_stats()

    def _on_tree_selection_changed(self, _event=None):
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
        st = self.state[i]["status"] if i < len(self.state) else "Ready"
        job_name = t.notes.strip() or soft_name(t.sequence)
        return (
            "✔" if t.enabled else "",
            job_name,
            soft_name(t.level),
            soft_name(t.sequence),
            soft_name(t.preset),
            st,
            t.notes,
        )

    def _set_status_async(self, idx: int, text: str):
        self.ui_queue.put(("set_status", idx, text))

    def _update_row_async(self, idx: int):
        self.ui_queue.put(("update_row", idx))

    # Tree helpers
    def refresh_tree(self):
        self._ensure_state()
        previous_selection = list(self.tree.selection()) if hasattr(self, "tree") else []
        self.tree.delete(*self.tree.get_children())

        query = self.var_task_filter.get().strip().lower() if hasattr(self, "var_task_filter") else ""
        for i, task in enumerate(self.settings.tasks):
            haystack = " ".join([
                task.notes,
                task.uproject,
                task.level,
                task.sequence,
                task.preset,
                task.output_dir,
            ]).lower()
            if query and query not in haystack:
                continue
            self.tree.insert("", "end", iid=str(i), values=self._row_values(i), tags=(self._status_tag_for_index(i),))

        visible_selection = [iid for iid in previous_selection if self.tree.exists(iid)]
        if visible_selection:
            self.tree.selection_set(visible_selection)
            self.tree.focus(visible_selection[0])

        self._update_inspector()
        self._update_command_preview()
        self._update_status_summary()

    def _selected_indices(self) -> List[int]:
        return [int(iid) for iid in self.tree.selection()]

    def on_tree_dblclick(self, _):
        sel = self._selected_indices()
        if not sel:
            return
        for idx in sel:
            self.settings.tasks[idx].enabled = not self.settings.tasks[idx].enabled
        self.refresh_tree()

    def on_space_toggle(self, _):
        self.on_tree_dblclick(_)

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
            self.settings.tasks.insert(idx + 1, RenderTask(**asdict(src)))
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
        # Otherwise disabled+removed tasks can still render later from runtime_q.
        self._remove_tasks_from_runtime_queue(removed_tasks)
        self.settings.tasks = new_tasks
        self.state = new_state
        self.refresh_tree()
        self._log(f"[Tasks] Removed {removed} unchecked task(s).")

    def set_enabled_all(self, val: bool):
        for t in self.settings.tasks:
            t.enabled = val
        # If tasks are being disabled, also remove their pending queued copies.
        # Otherwise already-enqueued items can still run despite being unchecked.
        if not val:
            self._remove_tasks_from_runtime_queue(self.settings.tasks)
        self.refresh_tree()

    def toggle_selected(self):
        sel = self._selected_indices()
        if not sel:
            return
        disabled_now = []
        for idx in sel:
            t = self.settings.tasks[idx]
            t.enabled = not t.enabled
            if not t.enabled:
                disabled_now.append(t)
        if disabled_now:
            # Keep runtime queue aligned with the visible enabled state.
            self._remove_tasks_from_runtime_queue(disabled_now)
        self.refresh_tree()

    # Save/Load JSON (queue)
    def load_from_json(self, path: str):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.var_ue.set(data.get("ue_cmd", self.var_ue.get()))
        self.settings.retries = int(data.get("retries", self.settings.retries))
        self.settings.fail_policy = data.get("fail_policy", self.settings.fail_policy)
        self.settings.kill_timeout_s = int(data.get("kill_timeout_s", self.settings.kill_timeout_s))
        # render opts
        self.settings.windowed = bool(data.get("windowed", self.settings.windowed))
        self.settings.resx = int(data.get("resx", self.settings.resx))
        self.settings.resy = int(data.get("resy", self.settings.resy))
        self.settings.no_texture_streaming = bool(data.get("no_texture_streaming", self.settings.no_texture_streaming))
        self.settings.extra_cli = data.get("extra_cli", self.settings.extra_cli)

        self.var_retries.set(self.settings.retries)
        self.var_policy.set(self.settings.fail_policy)
        self.var_kill_timeout.set(self.settings.kill_timeout_s)
        self.var_windowed.set(self.settings.windowed)
        self.var_resx.set(self.settings.resx)
        self.var_resy.set(self.settings.resy)
        self.var_nts.set(self.settings.no_texture_streaming)
        self.var_extra.set(self.settings.extra_cli)
        self.settings.tasks = [RenderTask(**{**it, **({"enabled": True} if "enabled" not in it else {})}) for it in data.get("tasks", [])]
        self.state = [default_task_state() for _ in self.settings.tasks]
        self.refresh_tree()
        self._update_engine_labels()
        self._update_command_preview()

    def save_to_json(self, path: str):
        data = {
            "ue_cmd": self.var_ue.get().strip(),
            "retries": int(self.var_retries.get()),
            "fail_policy": self.var_policy.get(),
            "kill_timeout_s": int(self.var_kill_timeout.get()),
            "windowed": bool(self.var_windowed.get()),
            "resx": int(self.var_resx.get()),
            "resy": int(self.var_resy.get()),
            "no_texture_streaming": bool(self.var_nts.get()),
            "extra_cli": self.var_extra.get().strip(),
            "tasks": [asdict(t) for t in self.settings.tasks],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

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
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and all(k in data for k in ("uproject","level","sequence","preset")):
                    self.settings.tasks.append(RenderTask(**{**data, **({"enabled": True} if "enabled" not in data else {})}))
                    self.state.append(default_task_state())
                    loaded += 1
                elif isinstance(data, dict) and "tasks" in data:
                    for it in data.get("tasks", []):
                        if all(k in it for k in ("uproject","level","sequence","preset")):
                            self.settings.tasks.append(RenderTask(**{**it, **({"enabled": True} if "enabled" not in it else {})}))
                            self.state.append(default_task_state())
                            loaded += 1
            except Exception as e:
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
        data = asdict(t)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

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
        if self.worker_running or (self.current_process and self.current_process.poll() is None):
            self._enqueue_tasks(tasks)
            return
        self._run_queue(tasks)

    def run_selected(self):
        tasks = self._collect(only_selected=True)
        if not tasks:
            messagebox.showinfo("Info", "Select at least one task in the table.")
            return
        if self.worker_running or (self.current_process and self.current_process.poll() is None):
            # Prevent spawning another render process; enqueue instead
            self._enqueue_tasks(tasks)
            return
        self._run_queue(tasks)

    def run_enabled(self):
        tasks = self._collect(only_enabled=True)
        if not tasks:
            messagebox.showinfo("Info", "No enabled tasks to run.")
            return
        if self.worker_running or (self.current_process and self.current_process.poll() is None):
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
        if not ue_cmd or not os.path.exists(ue_cmd):
            messagebox.showerror("Error", "Specify a valid path to UnrealEditor-Cmd.exe")
            return
        if not tasks and self.runtime_q.empty():
            if not self.worker_running:
                messagebox.showinfo("Info", "No tasks to run")
            return

        self.stop_all = False
        # Preload tasks into runtime queue via helper (sets statuses too)
        if tasks:
            self._enqueue_tasks(tasks, log_prefix="== Enqueued ")
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
                if self.stop_all and self.runtime_q.empty():
                    break
                try:
                    t = self.runtime_q.get(timeout=0.5)
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
                        self.current_process = subprocess.Popen(
                            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
                        )
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
                                        self.ui_queue.put(("set_progress", gidx, progress))
                        except Exception as ex:
                            self._log(f"[pump] {ex}")
                        finally:
                            try:
                                log_fp.flush()
                            except Exception:
                                pass

                    th_pump = threading.Thread(target=pump, args=(self.current_process, gi), daemon=True)
                    th_pump.start()
                    # Pass the concrete process handle to the ticker
                    th_tick = threading.Thread(target=tick_elapsed, args=(gi, self.current_process), daemon=True)
                    th_tick.start()
                    rc = self.current_process.wait()
                    self._log(f"[{idx}] Exit code: {rc}")
                    # Stop ticker ASAP for this process
                    try:
                        th_tick.join(timeout=0.2)
                    except Exception:
                        pass
                    self.current_process = None
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

                if self.stop_all:
                    self._log("[Cancel] Stop-all while processing queue")
                    break

            if self.stop_all:
                self._clear_pending_runtime_queue("Cancelled (queue)")
            self._log("== Queue complete ==")
            self._current_global_idx = None
            self.worker_running = False

        # Start worker if not already running
        if not self.worker_running:
            threading.Thread(target=worker, daemon=True).start()

    def cancel_current(self):
        if self.current_process and self.current_process.poll() is None:
            try:
                self.current_process.terminate()
                self._log("[Cancel] Sent terminate to current task…")
                if self._current_global_idx is not None:
                    self._set_status_async(self._current_global_idx, "Cancelled")
                timeout = int(self.var_kill_timeout.get())
                if timeout > 0:
                    try:
                        self.current_process.wait(timeout=timeout)
                    except Exception:
                        self.current_process.kill()
                        self._log("[Cancel] Kill after timeout")
            except Exception as e:
                self._log(f"[Cancel] Error: {e}")
        else:
            self._log("[Cancel] No running process")

    def cancel_all(self):
        self.stop_all = True
        self.cancel_current()
        self._clear_pending_runtime_queue("Cancelled (queue)")
        self._log("[Cancel] Stop-all requested.")

    def _enqueue_tasks(self, tasks: List[RenderTask], mark_queued: bool = True, log_prefix: str = "[+] Added "):
        """Enqueue a list of tasks into the runtime queue and optionally mark them as Queued."""
        if not tasks:
            return
        count = 0
        for t in tasks:
            # Skip incomplete tasks defensively
            if not all([t.uproject, t.level, t.sequence, t.preset]):
                continue
            self.runtime_q.put(t)
            if mark_queued:
                gi = self._find_task_index_by_identity(t)
                if gi is not None:
                    self._set_status_async(gi, "Queued")
            count += 1
        if count:
            self._log(f"{log_prefix}{count} task(s) to queue")
            # Ensure table reflects status changes
            self.refresh_tree()

    def _clear_pending_runtime_queue(self, status_text: str = "Cancelled (queue)"):
        """
        Remove tasks waiting in runtime_q and optionally update their visible status.
        This prevents stale queued tasks from running during the next render session.
        """
        removed = 0
        while True:
            try:
                t = self.runtime_q.get_nowait()
            except queue.Empty:
                break
            gi = self._find_task_index_by_identity(t)
            if gi is not None:
                self._set_status_async(gi, status_text)
            removed += 1
        if removed:
            self._log(f"[Cancel] Removed {removed} queued task(s).")

    def _remove_tasks_from_runtime_queue(self, tasks_to_remove: List[RenderTask]):
        """
        Remove specific task objects from the runtime queue by identity.
        Used to keep pending runtime queue items aligned with table edits.
        """
        if not tasks_to_remove:
            return
        to_remove_ids = {id(t) for t in tasks_to_remove}
        kept = []
        removed = 0
        while True:
            try:
                t = self.runtime_q.get_nowait()
            except queue.Empty:
                break
            if id(t) in to_remove_ids:
                removed += 1
                continue
            kept.append(t)

        for t in kept:
            self.runtime_q.put(t)

        if removed:
            self._log(f"[Tasks] Removed {removed} queued item(s) from runtime queue.")

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
            # Start worker without adding new items (they're already in runtime_q)
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
                kind = item[0]
                if kind == "set_status":
                    _, idx, text = item
                    if 0 <= idx < len(self.state):
                        self.state[idx]["status"] = text
                        if text.startswith("Done"):
                            self.state[idx]["progress"] = 100
                        elif text in ("Queued", "Ready", "Cancelled", "Cancelled (queue)"):
                            self.state[idx]["progress"] = 0
                        self._set_tree_item(idx)
                        status_changed = True
                elif kind == "update_row":
                    _, idx = item
                    self._set_tree_item(idx)
                    status_changed = True
                elif kind == "set_progress":
                    _, idx, progress = item
                    if 0 <= idx < len(self.state):
                        self.state[idx]["progress"] = progress
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
            if st.get("start") and self.current_process and self.current_process.poll() is None:
                total += max(0, int(time.time() - st["start"]))
        return total

    def _tick_session_total(self):
        """Update the fixed label with HH:MM:SS."""
        try:
            if self._session_total_label is not None:
                sec = self._compute_session_total_seconds()
                h, rem = divmod(sec, 3600)
                m, s = divmod(rem, 60)
                self._session_total_label.config(text=f"Session total: {h:02d}:{m:02d}:{s:02d}")
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

# -------------------------------------------------
# Entrypoint
# -------------------------------------------------

if __name__ == "__main__":
    app = MRQLauncher()
    app.mainloop()
