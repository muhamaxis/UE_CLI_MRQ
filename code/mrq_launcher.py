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
APP_VERSION = "1.2.8"
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

# -------------------------------------------------
# Data
# -------------------------------------------------

@dataclass
class RenderTask:
    uproject: str = ""
    level: str = ""
    sequence: str = ""
    preset: str = ""
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

        self.var_uproj = StringVar(value=(task.uproject if task else ""))
        self.var_level = StringVar(value=(task.level if task else ""))
        self.var_seq = StringVar(value=(task.sequence if task else ""))
        self.var_preset = StringVar(value=(task.preset if task else ""))
        self.var_notes = StringVar(value=(task.notes if task else ""))

        frm = tk.Frame(self, padx=10, pady=10)
        frm.pack(fill=tk.BOTH, expand=True)

        def row(lbl, var, browse_cb=None, hint: str = ""):
            r = tk.Frame(frm)
            r.pack(fill="x", pady=3)
            tk.Label(r, text=lbl, width=20, anchor="w").pack(side=tk.LEFT)
            tk.Entry(r, textvariable=var, width=70).pack(side=tk.LEFT, padx=5)
            if browse_cb:
                tk.Button(r, text="Browse", command=browse_cb).pack(side=tk.LEFT)
            if hint:
                tk.Label(frm, text=hint, fg="gray").pack(anchor="w")

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

        row("Project (.uproject)", self.var_uproj, pick_uproj)
        row("Map (SoftObjectPath)", self.var_level, pick_level, "e.g.: /Game/Maps/MyMap.MyMap")
        row("Level Sequence", self.var_seq, pick_seq, "e.g.: /Game/Cinematics/Shot.Shot")
        row("MRQ Preset", self.var_preset, pick_preset, "e.g.: /Game/Cinematics/MoviePipeline/Presets/High.High")

        tk.Label(frm, text="Notes").pack(anchor="w")
        tk.Entry(frm, textvariable=self.var_notes, width=95).pack(fill="x")

        btn = tk.Frame(frm)
        btn.pack(fill="x", pady=10)
        tk.Button(btn, text="OK", command=self.on_ok).pack(side=tk.LEFT, padx=4)
        tk.Button(btn, text="Cancel", command=self.destroy).pack(side=tk.LEFT)

    def on_ok(self):
        t = RenderTask(
            uproject=self.var_uproj.get().strip(),
            level=self.var_level.get().strip(),
            sequence=self.var_seq.get().strip(),
            preset=self.var_preset.get().strip(),
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
        super().__init__()
        # Window title with version
        self.title(f"MRQ Launcher (CLI) ver {APP_VERSION}")
        self.geometry("1320x840")
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
        self._build_ui()
        self.after(50, self._drain_queues)
        # Periodic update for the session total time label
        self.after(500, self._tick_session_total)

    # UI
    def _build_ui(self):
        # ---- Menu Bar ----
        menubar = tk.Menu(self)
        # Task (only add/edit/duplicate here)
        m_task = tk.Menu(menubar, tearoff=0)
        m_task.add_command(label="Add Task", command=self.add_task)
        m_task.add_command(label="Edit Task", command=self.edit_task)
        m_task.add_command(label="Duplicate Task", command=self.duplicate_task)
        menubar.add_cascade(label="Task", menu=m_task)
        # Selections (moved Move Up/Down here + Remove Task(s))
        m_sel = tk.Menu(menubar, tearoff=0)
        m_sel.add_command(label="Enable All Tasks", command=lambda: self.set_enabled_all(True))
        m_sel.add_command(label="Disable All Tasks", command=lambda: self.set_enabled_all(False))
        m_sel.add_command(label="Remove Task(s)", command=self.remove_task)
        m_sel.add_command(label="Toggle Selection", command=self.toggle_selected)
        m_sel.add_separator()
        m_sel.add_command(label="Move Up", command=lambda: self.move_selected(-1))
        m_sel.add_command(label="Move Down", command=lambda: self.move_selected(1))
        menubar.add_cascade(label="Selections", menu=m_sel)
        # Render
        m_run = tk.Menu(menubar, tearoff=0)
        m_run.add_command(label="Render Selected", command=self.run_selected)
        m_run.add_command(label="Render Checked", command=self.run_enabled)
        m_run.add_command(label="Add Task(s) to Queue", command=self.add_task_and_enqueue)
        m_run.add_separator()
        m_run.add_command(label="Cancel Current", command=self.cancel_current)
        m_run.add_command(label="Cancel All", command=self.cancel_all)
        menubar.add_cascade(label="Render", menu=m_run)
        # Save
        m_save = tk.Menu(menubar, tearoff=0)
        m_save.add_command(label="Load Task(s)", command=self.load_tasks_dialog)
        m_save.add_command(label="Save Selected Task(s)", command=self.save_selected_tasks_dialog)
        m_save.add_separator()
        m_save.add_command(label="Load Queue", command=self.load_json_dialog)
        m_save.add_command(label="Save Queue", command=self.save_json_dialog)
        menubar.add_cascade(label="Save", menu=m_save)
        self.config(menu=menubar)

        top = tk.Frame(self, padx=10, pady=8)
        top.pack(fill=tk.X)
        tk.Label(top, text="UnrealEditor-Cmd.exe:").pack(side=tk.LEFT)
        self.var_ue = StringVar(value=self.settings.ue_cmd)
        tk.Entry(top, textvariable=self.var_ue, width=100).pack(side=tk.LEFT, padx=6)
        tk.Button(top, text="Browse", command=self.browse_ue).pack(side=tk.LEFT)
        tk.Label(top, text="Retries:").pack(side=tk.LEFT, padx=(12,4))
        self.var_retries = tk.IntVar(value=self.settings.retries)
        tk.Spinbox(top, from_=0, to=3, width=3, textvariable=self.var_retries).pack(side=tk.LEFT)
        tk.Label(top, text="On fail:").pack(side=tk.LEFT, padx=(12,4))
        self.var_policy = StringVar(value=self.settings.fail_policy)
        ttk.Combobox(top, textvariable=self.var_policy, width=16, state="readonly",
                     values=("retry_then_next","skip_next","stop_queue")).pack(side=tk.LEFT)
        tk.Label(top, text="Kill timeout s:").pack(side=tk.LEFT, padx=(12,4))
        self.var_kill_timeout = tk.IntVar(value=self.settings.kill_timeout_s)
        tk.Spinbox(top, from_=0, to=120, width=4, textvariable=self.var_kill_timeout).pack(side=tk.LEFT)

        # ---- Row: Render opts (windowed / res / NTS / extra) ----
        opts = tk.Frame(self, padx=10, pady=4)
        opts.pack(fill=tk.X)
        self.var_windowed = tk.BooleanVar(value=self.settings.windowed)
        tk.Checkbutton(opts, text="Windowed", variable=self.var_windowed).pack(side=tk.LEFT)
        tk.Label(opts, text="ResX:").pack(side=tk.LEFT, padx=(12,4))
        self.var_resx = tk.IntVar(value=self.settings.resx)
        tk.Spinbox(opts, from_=320, to=16384, width=6, textvariable=self.var_resx).pack(side=tk.LEFT)
        tk.Label(opts, text="ResY:").pack(side=tk.LEFT, padx=(12,4))
        self.var_resy = tk.IntVar(value=self.settings.resy)
        tk.Spinbox(opts, from_=240, to=16384, width=6, textvariable=self.var_resy).pack(side=tk.LEFT)
        self.var_nts = tk.BooleanVar(value=self.settings.no_texture_streaming)
        tk.Checkbutton(opts, text="No Texture Streaming (-notexturestreaming)", variable=self.var_nts).pack(side=tk.LEFT, padx=(12,0))
        tk.Label(opts, text="Extra CLI:").pack(side=tk.LEFT, padx=(12,4))
        self.var_extra = StringVar(value=self.settings.extra_cli)
        tk.Entry(opts, textvariable=self.var_extra, width=60).pack(side=tk.LEFT, padx=(0,6), fill=tk.X, expand=True)

        # ---- Split area: table (left) + scrollable right sidebar with buttons ----
        mid = ttk.Panedwindow(self, orient="horizontal")
        mid.pack(fill=tk.BOTH, expand=True, padx=10, pady=8)
        left_pane = tk.Frame(mid)
        right_shell = tk.Frame(mid)  # will contain a Canvas with vertical scrollbar
        mid.add(left_pane, weight=4)
        mid.add(right_shell, weight=1)

        cols = ("enabled", "level", "sequence", "preset", "status", "notes")
        self.tree = ttk.Treeview(left_pane, columns=cols, show="headings", selectmode="extended")
        for name, title, width in (
            ("enabled", "✔", 40),
            ("level", "Level", 220),
            ("sequence", "Sequence", 220),
            ("preset", "Preset", 300),
            ("status", "Status", 220),
            ("notes", "Notes", 260),
        ):
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, anchor="center" if name=="enabled" else "w")
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.tree.bind("<Double-1>", self.on_tree_dblclick)
        self.tree.bind("<space>", self.on_space_toggle)
        # Right-click context menu for task operations
        self.ctx_task = tk.Menu(self, tearoff=0)
        self.ctx_task.add_command(label="Add Task", command=self.add_task)
        self.ctx_task.add_command(label="Edit Task", command=self.edit_task)
        self.ctx_task.add_command(label="Duplicate Task", command=self.duplicate_task)
        self.ctx_task.add_command(label="Remove Task(s)", command=self.remove_task)
        self.ctx_task.add_separator()
        self.ctx_task.add_command(label="Move Up", command=lambda: self.move_selected(-1))
        self.ctx_task.add_command(label="Move Down", command=lambda: self.move_selected(1))
        # Duplicate Save/Load under context menu for quick access
        self.ctx_task.add_separator()
        self.ctx_task.add_command(label="Load Task(s)…", command=self.load_tasks_dialog)
        self.ctx_task.add_command(label="Save Selected Task(s)…", command=self.save_selected_tasks_dialog)
        self.ctx_task.add_separator()
        self.ctx_task.add_command(label="Load Queue…", command=self.load_json_dialog)
        self.ctx_task.add_command(label="Save Queue…", command=self.save_json_dialog)
        # Bind right-click (Button-3 on Windows/Linux; macOS users often use Ctrl-Click)
        self.tree.bind("<Button-3>", self._on_tree_right_click)
        self.tree.bind("<Control-Button-1>", self._on_tree_right_click)

        # Vertical and horizontal scrollbars for the task table
        sb = ttk.Scrollbar(left_pane, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.LEFT, fill=tk.Y)
        hsb = ttk.Scrollbar(left_pane, orient="horizontal", command=self.tree.xview)
        self.tree.configure(xscrollcommand=hsb.set)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)

        # ---- Scrollable right sidebar (vertical layout; no clipping) ----
        right_canvas = tk.Canvas(right_shell, highlightthickness=0)
        right_scroll = ttk.Scrollbar(right_shell, orient="vertical", command=right_canvas.yview)
        right_frame = tk.Frame(right_canvas)
        right_frame.bind("<Configure>", lambda e: right_canvas.configure(scrollregion=right_canvas.bbox("all")))
        right_canvas.create_window((0, 0), window=right_frame, anchor="nw")
        right_canvas.configure(yscrollcommand=right_scroll.set)
        right_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        right_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # Selections group (vertical buttons)
        grp_sel = ttk.LabelFrame(right_frame, text="Selections")
        grp_sel.pack(fill=tk.X, pady=6)
        tk.Button(grp_sel, text="Enable All Tasks", width=20, command=lambda: self.set_enabled_all(True)).pack(pady=2)
        tk.Button(grp_sel, text="Disable All Tasks", width=20, command=lambda: self.set_enabled_all(False)).pack(pady=2)
        tk.Button(grp_sel, text="Remove Task(s)", width=20, command=self.remove_task).pack(pady=2)
        tk.Button(grp_sel, text="Toggle Selection", width=20, command=self.toggle_selected).pack(pady=2)
        tk.Button(grp_sel, text="Move Up", width=20, command=lambda: self.move_selected(-1)).pack(pady=2)
        tk.Button(grp_sel, text="Move Down", width=20, command=lambda: self.move_selected(1)).pack(pady=2)

        # Render group (vertical buttons)
        grp_run = ttk.LabelFrame(right_frame, text="Render")
        grp_run.pack(fill=tk.X, pady=6)
        tk.Button(grp_run, text="Render Selected", width=20, command=self.run_selected).pack(pady=2)
        tk.Button(grp_run, text="Render Checked", width=20, command=self.run_enabled).pack(pady=2)
        tk.Button(grp_run, text="Add Task(s) to Queue", width=20, command=self.add_task_and_enqueue).pack(pady=2)
        tk.Button(grp_run, text="Cancel Current", width=20, command=self.cancel_current).pack(pady=2)
        tk.Button(grp_run, text="Cancel All", width=20, command=self.cancel_all).pack(pady=2)

        # ---- Group: Logs & Queue I/O (bottom bar under the table)
        logs_bar = tk.Frame(self, padx=10)
        logs_bar.pack(fill=tk.X, pady=(0, 4))
        # left side — log helpers
        tk.Button(logs_bar, text="Open Logs Folder", command=self.open_logs_folder).pack(side=tk.LEFT, padx=(0,6))
        tk.Button(logs_bar, text="Open Last Log (Selected)", command=self.open_last_log_for_selected).pack(side=tk.LEFT)
        # right side — queue I/O
        tk.Button(logs_bar, text="Save Queue", command=self.save_json_dialog).pack(side=tk.RIGHT, padx=(6,0))
        tk.Button(logs_bar, text="Load Queue", command=self.load_json_dialog).pack(side=tk.RIGHT, padx=(6,0))

        # ---- Fixed session total time row (just under the table area) ----
        # Sits above the log box.
        bar = tk.Frame(self, padx=12)
        bar.pack(fill=tk.X, padx=10, pady=(0, 0))
        self._session_total_label = tk.Label(bar, text="Session total: 00:00:00", anchor="w")
        self._session_total_label.pack(side=tk.LEFT)

        bottom = tk.Frame(self, padx=10, pady=6)
        bottom.pack(fill=tk.BOTH)
        self.log = tk.Text(bottom, height=12)
        self.log.pack(fill=tk.BOTH, expand=True)

        self.refresh_tree()

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
            self.state.append({"status": "—", "progress": None, "start": None, "end": None})

    def _row_values(self, i: int):
        t = self.settings.tasks[i]
        st = self.state[i]["status"] if i < len(self.state) else "—"
        return ("✔" if t.enabled else " ", soft_name(t.level), soft_name(t.sequence), soft_name(t.preset), st, t.notes)

    def _set_status_async(self, idx: int, text: str):
        self.ui_queue.put(("set_status", idx, text))

    def _update_row_async(self, idx: int):
        self.ui_queue.put(("update_row", idx))

    # Tree helpers
    def refresh_tree(self):
        self._ensure_state()
        self.tree.delete(*self.tree.get_children())
        for i, _ in enumerate(self.settings.tasks):
            self.tree.insert("", "end", iid=str(i), values=self._row_values(i))

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
        dlg = TaskEditor(self, self.settings.tasks[idx])
        self.wait_window(dlg)
        if dlg.result:
            dlg.result.enabled = self.settings.tasks[idx].enabled
            self.settings.tasks[idx] = dlg.result
            self.refresh_tree()

    def duplicate_task(self):
        sel = self._selected_indices()
        if not sel:
            return
        for idx in sel:
            src = self.settings.tasks[idx]
            self.settings.tasks.insert(idx + 1, RenderTask(**asdict(src)))
            self.state.insert(idx + 1, {"status": "—", "progress": None, "start": None, "end": None})
        self.refresh_tree()

    def remove_task(self):
        sel = sorted(self._selected_indices(), reverse=True)
        for idx in sel:
            del self.settings.tasks[idx]
            del self.state[idx]
        self.refresh_tree()

    def set_enabled_all(self, val: bool):
        for t in self.settings.tasks:
            t.enabled = val
        self.refresh_tree()

    def toggle_selected(self):
        sel = self._selected_indices()
        if not sel:
            return
        for idx in sel:
            self.settings.tasks[idx].enabled = not self.settings.tasks[idx].enabled
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
        self.state = [{"status": "—", "progress": None, "start": None, "end": None} for _ in self.settings.tasks]
        self.refresh_tree()

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
                    self.state.append({"status": "—", "progress": None, "start": None, "end": None})
                    loaded += 1
                elif isinstance(data, dict) and "tasks" in data:
                    for it in data.get("tasks", []):
                        if all(k in it for k in ("uproject","level","sequence","preset")):
                            self.settings.tasks.append(RenderTask(**{**it, **({"enabled": True} if "enabled" not in it else {})}))
                            self.state.append({"status": "—", "progress": None, "start": None, "end": None})
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
        self._run_queue(self._collect())

    def run_selected(self):
        self._run_queue(self._collect(only_selected=True))

    def run_enabled(self):
        self._run_queue(self._collect(only_enabled=True))

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
        # Preload tasks into runtime queue
        for t in tasks:
            self.runtime_q.put(t)
        if tasks:
            self._log(f"== Enqueued {len(tasks)} task(s) ==")
        retries = int(self.var_retries.get())
        policy = self.var_policy.get()
        kill_timeout = int(self.var_kill_timeout.get())

        # Mark tasks as Queued (only newly added ones)
        for t in tasks:
            try:
                gi = self.settings.tasks.index(t)
                self._set_status_async(gi, "Queued")
            except ValueError:
                pass

        def _fmt_hhmmss(sec: int) -> str:
            h, rem = divmod(max(0, int(sec)), 3600)
            m, s = divmod(rem, 60)
            return f"{h:02d}:{m:02d}:{s:02d}"

        def worker():
            self.worker_running = True
            idx = 0
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

                attempt = 0
                logfile = self._task_logfile(t)
                gi = None
                try:
                    gi = self.settings.tasks.index(t)
                except ValueError:
                    gi = None

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
                    self._log(f"[{idx}] Start (try {attempt}/{retries+1}): {' '.join(cmd)}")

                    start_dt = datetime.now()
                    # status
                    if gi is not None:
                        self.state[gi]["start"] = time.time()
                        self._set_status_async(gi, "Rendering 00:00")
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
                                    m, s = divmod(elapsed, 60)
                                    self._set_status_async(gidx, f"Rendering {m:02d}:{s:02d}")
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
                                self._set_status_async(gi, f"Done ({_fmt_hhmmss(dur)[3:]})")
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
                            break

                if self.stop_all:
                    self._log("[Cancel] Stop-all while processing queue")
                    break

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
        self._log("[Cancel] Stop-all requested.")

    def add_task_and_enqueue(self):
        """Create a new task and enqueue it into the running queue without stopping the worker."""
        dlg = TaskEditor(self)
        self.wait_window(dlg)
        if not dlg.result:
            return
        t = dlg.result
        # Add to app list and UI
        self.settings.tasks.append(t)
        self._ensure_state()
        try:
            gi = self.settings.tasks.index(t)
            self._set_status_async(gi, "Queued")
        except ValueError:
            pass
        self.refresh_tree()
        self.runtime_q.put(t)
        # If the queue hasn't been started, launch the worker for a single item
        if not self.worker_running:
            self._run_queue([])  # starts the worker without an initial list
        else:
            self._log("[+] Task added to running queue")

    # Logging & UI queues (thread-safe)
    def _log(self, msg: str):
        self.log_queue.put(msg)

    def _drain_queues(self):
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
                        if self.tree.exists(str(idx)):
                            self.tree.item(str(idx), values=self._row_values(idx))
                elif kind == "update_row":
                    _, idx = item
                    if self.tree.exists(str(idx)):
                        self.tree.item(str(idx), values=self._row_values(idx))
        except queue.Empty:
            pass

        self.after(50, self._drain_queues)

    def browse_ue(self):
        p = filedialog.askopenfilename(title="Select UnrealEditor-Cmd.exe",
                                       filetypes=[("UnrealEditor-Cmd", "UnrealEditor-Cmd.exe"), ("Exe", "*.exe"), ("All", "*.*")])
        if p:
            self.var_ue.set(p)

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

# -------------------------------------------------
# Entrypoint
# -------------------------------------------------

if __name__ == "__main__":
    app = MRQLauncher()
    app.mainloop()
