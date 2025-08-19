import os
import subprocess
import json
import queue
import threading
import time
from dataclasses import dataclass, asdict, field
from typing import List, Optional
from datetime import datetime

import tkinter as tk
from tkinter import filedialog, messagebox, StringVar
from tkinter import ttk

# -------------------------------------------------
# Helpers
# -------------------------------------------------

def detect_default_unreal_cmd() -> str:
    candidates = [
        # Используем прямые слэши, чтобы не было проблем с экранированием
        "C:/Program Files/Epic Games/UE_5.6/Engine/Binaries/Win64/UnrealEditor-Cmd.exe",
        "C:/Program Files/Epic Games/UE_5.5/Engine/Binaries/Win64/UnrealEditor-Cmd.exe",
        "C:/Program Files/Epic Games/UE_5.4/Engine/Binaries/Win64/UnrealEditor-Cmd.exe",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return ""


def fs_to_soft_object(uasset_path: str) -> str:
    """Путь к .uasset/.umap из папки Content → SoftObjectPath.
    Ожидается: .../<Project>/Content/<Rel>/<Asset>.uasset
    Результат: /Game/<Rel>/<Asset>.<Asset>
    """
    norm = os.path.normpath(uasset_path)
    if not norm.lower().endswith((".uasset", ".umap")):
        raise ValueError("Выбери .uasset/.umap")
    parts = norm.split(os.sep)
    if "Content" not in parts:
        raise ValueError("Путь должен содержать папку 'Content' проекта.")
    idx = len(parts) - 1 - parts[::-1].index("Content")
    rel_parts = parts[idx + 1:]
    asset_name = os.path.splitext(rel_parts[-1])[0]
    rel_dir = rel_parts[:-1]
    game_path = "/Game"
    if rel_dir:
        # избегаем обратных слешей
        game_path += "/" + "/".join(rel_dir).replace("\", "/")
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
    retries: int = 0  # автоповторы при ненулевом коде
    fail_policy: str = "retry_then_next"  # retry_then_next | skip_next | stop_queue
    kill_timeout_s: int = 10  # таймаут на мягкую отмену перед kill

# -------------------------------------------------
# Task Editor (единое окно выбора путей)
# -------------------------------------------------

class TaskEditor(tk.Toplevel):
    def __init__(self, master, task: Optional[RenderTask] = None):
        super().__init__(master)
        self.title("Task Editor")
        self.resizable(False, False)
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
        row("Map (SoftObjectPath)", self.var_level, pick_level, "Напр.: /Game/Maps/MyMap.MyMap")
        row("Level Sequence", self.var_seq, pick_seq, "Напр.: /Game/Cinematics/Shot.Shot")
        row("MRQ Preset", self.var_preset, pick_preset, "Напр.: /Game/Cinematics/MoviePipeline/Presets/High.High")

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
            messagebox.showerror("Validation", "Заполни все поля.")
            return
        self.result = t
        self.destroy()

# -------------------------------------------------
# Main App (потокобезопасный лог + статус задач)
# -------------------------------------------------

class MRQLauncher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MRQ Launcher (CLI)")
        self.geometry("1320x840")
        self.settings = AppSettings()
        self.current_process: Optional[subprocess.Popen] = None
        self._current_global_idx: Optional[int] = None
        self.stop_all = False
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.ui_queue: "queue.Queue[tuple]" = queue.Queue()
        self.state: List[dict] = []  # per-task session state: {status, progress, start, end}
        self._build_ui()
        self.after(50, self._drain_queues)

    # UI
    def _build_ui(self):
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

        mid = tk.Frame(self, padx=10, pady=8)
        mid.pack(fill=tk.BOTH, expand=True)

        cols = ("enabled", "level", "sequence", "preset", "status", "notes")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="extended")
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

        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.LEFT, fill=tk.Y)

        right = tk.Frame(mid, padx=10)
        right.pack(side=tk.LEFT, fill=tk.Y)

        # ---- Group: Tasks ----
        grp_tasks = ttk.LabelFrame(right, text="Tasks")
        grp_tasks.pack(fill=tk.X, pady=4)
        for text, cmd in (
            ("Add…", self.add_task),
            ("Edit…", self.edit_task),
            ("Duplicate", self.duplicate_task),
            ("Remove", self.remove_task),
        ):
            tk.Button(grp_tasks, text=text, width=18, command=cmd).pack(pady=2)

        # ---- Group: Order ----
        grp_order = ttk.LabelFrame(right, text="Order")
        grp_order.pack(fill=tk.X, pady=6)
        tk.Button(grp_order, text="Move Up", width=18, command=lambda: self.move_selected(-1)).pack(pady=2)
        tk.Button(grp_order, text="Move Down", width=18, command=lambda: self.move_selected(1)).pack(pady=2)

        # ---- Group: Selection ----
        grp_sel = ttk.LabelFrame(right, text="Selection")
        grp_sel.pack(fill=tk.X, pady=6)
        tk.Button(grp_sel, text="Enable All", width=18, command=lambda: self.set_enabled_all(True)).pack(pady=2)
        tk.Button(grp_sel, text="Disable All", width=18, command=lambda: self.set_enabled_all(False)).pack(pady=2)
        tk.Button(grp_sel, text="Toggle", width=18, command=self.toggle_selected).pack(pady=2)

        # ---- Group: Run ----
        grp_run = ttk.LabelFrame(right, text="Run")
        grp_run.pack(fill=tk.X, pady=6)
        tk.Button(grp_run, text="Run Selected", width=18, command=self.run_selected).pack(pady=2)
        tk.Button(grp_run, text="Run Enabled", width=18, command=self.run_enabled).pack(pady=2)
        tk.Button(grp_run, text="Run All", width=18, command=self.run_all).pack(pady=2)

        # ---- Group: Stop / Cancel ----
        grp_stop = ttk.LabelFrame(right, text="Stop / Cancel")
        grp_stop.pack(fill=tk.X, pady=6)
        tk.Button(grp_stop, text="Cancel Current", width=18, command=self.cancel_current).pack(pady=2)
        tk.Button(grp_stop, text="Cancel All", width=18, command=self.cancel_all).pack(pady=2)

        # ---- Group: Task I/O ----
        grp_taskio = ttk.LabelFrame(right, text="Task I/O")
        grp_taskio.pack(fill=tk.X, pady=6)
        tk.Button(grp_taskio, text="Load Task(s)…", width=18, command=self.load_tasks_dialog).pack(pady=2)
        tk.Button(grp_taskio, text="Save Selected Task(s)…", width=18, command=self.save_selected_tasks_dialog).pack(pady=2)

        # ---- Group: Queue I/O ----
        grp_qio = ttk.LabelFrame(right, text="Queue I/O")
        grp_qio.pack(fill=tk.X, pady=6)
        tk.Button(grp_qio, text="Load JSON…", width=18, command=self.load_json_dialog).pack(pady=2)
        tk.Button(grp_qio, text="Save JSON…", width=18, command=self.save_json_dialog).pack(pady=2)

        bottom = tk.Frame(self, padx=10, pady=6)
        bottom.pack(fill=tk.BOTH)
        self.log = tk.Text(bottom, height=12)
        self.log.pack(fill=tk.BOTH, expand=True)

        self.refresh_tree()

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
        self.var_retries.set(self.settings.retries)
        self.var_policy.set(self.settings.fail_policy)
        self.var_kill_timeout.set(self.settings.kill_timeout_s)
        self.settings.tasks = [RenderTask(**{**it, **({"enabled": True} if "enabled" not in it else {})}) for it in data.get("tasks", [])]
        self.state = [{"status": "—", "progress": None, "start": None, "end": None} for _ in self.settings.tasks]
        self.refresh_tree()

    def save_to_json(self, path: str):
        data = {
            "ue_cmd": self.var_ue.get().strip(),
            "retries": int(self.var_retries.get()),
            "fail_policy": self.var_policy.get(),
            "kill_timeout_s": int(self.var_kill_timeout.get()),
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
            messagebox.showwarning("Save Task", "Выбери хотя бы одну задачу в таблице.")
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
            messagebox.showinfo("Save Task(s)", f"Saved {count} task file(s) to
{folder}")

    def _save_task_to_file(self, t: RenderTask, path: str):
        data = asdict(t)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ---- Progress parsing (без regex) ----
    def _extract_progress(self, line: str) -> Optional[int]:
        # Пытаемся найти число перед знаком %
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
        # Пытаемся найти токен вида X/Y
        tokens = line.replace("(", " ").replace(")", " ").replace("[", " ").replace("]", " ").split()
        for tok in tokens:
            if "/" in tok:
                parts = tok.split("/")
                if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                    a, b = int(parts[0]), int(parts[1])
                    if b > 0:
                        return max(0, min(100, int(a * 100 / b)))
        # progress: NN или progress=NN
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

    def _run_queue(self, tasks: List[RenderTask]):
        ue_cmd = self.var_ue.get().strip()
        if not ue_cmd or not os.path.exists(ue_cmd):
            messagebox.showerror("Error", "Укажи корректный путь к UnrealEditor-Cmd.exe")
            return
        if not tasks:
            messagebox.showinfo("Info", "Нет задач для запуска")
            return

        self.stop_all = False
        self._log(f"== Launch {len(tasks)} task(s) ==")
        retries = int(self.var_retries.get())
        policy = self.var_policy.get()
        kill_timeout = int(self.var_kill_timeout.get())

        # пометить задачи как Queued
        for t in tasks:
            try:
                gi = self.settings.tasks.index(t)
                self._set_status_async(gi, "Queued")
            except ValueError:
                pass

        def worker(queue_tasks: List[RenderTask]):
            for idx, t in enumerate(queue_tasks, 1):
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
                    cmd = [ue_cmd, t.uproject, t.level.split(".")[0], "-game",
                           f"-LevelSequence=\"{t.sequence}\"", f"-MoviePipelineConfig=\"{t.preset}\"", "-log", "-notexturestreaming"]
                    self._log(f"[{idx}] Start (try {attempt}/{retries+1}): {' '.join(cmd)}")

                    # статус
                    if gi is not None:
                        self.state[gi]["start"] = time.time()
                        self._set_status_async(gi, "Running 0%")
                        self._current_global_idx = gi

                    try:
                        log_fp = open(logfile, "a", encoding="utf-8")
                        log_fp.write(f"CMD: {' '.join(cmd)}
")
                        self.current_process = subprocess.Popen(
                            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
                        )
                    except Exception as e:
                        self._log(f"[{idx}] Failed to start: {e}")
                        break

                    def pump(proc: subprocess.Popen, gidx: Optional[int]):
                        try:
                            if proc.stdout:
                                for line in proc.stdout:
                                    if self.stop_all:
                                        break
                                    self._log(line.rstrip())
                                    log_fp.write(line)
                                    # попытка вытащить прогресс
                                    p = self._extract_progress(line)
                                    if p is not None and gidx is not None:
                                        self.state[gidx]["progress"] = p
                                        self._set_status_async(gidx, f"Running {p}%")
                        except Exception as ex:
                            self._log(f"[pump] {ex}")
                        finally:
                            try:
                                log_fp.flush()
                            except Exception:
                                pass

                    th = threading.Thread(target=pump, args=(self.current_process, gi), daemon=True)
                    th.start()
                    rc = self.current_process.wait()
                    self._log(f"[{idx}] Exit code: {rc}")
                    self.current_process = None
                    try:
                        log_fp.write(f"
EXIT: {rc}
")
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
                                m, s = divmod(dur, 60)
                                self._set_status_async(gi, f"Done ({m:02d}:{s:02d})")
                            else:
                                self._set_status_async(gi, "Done")
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

        threading.Thread(target=worker, args=(tasks[:],), daemon=True).start()

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

    # Logging & UI queues (потокобезопасно)
    def _log(self, msg: str):
        self.log_queue.put(msg)

    def _drain_queues(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log.insert("end", msg + "
")
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

# -------------------------------------------------
# Entrypoint
# -------------------------------------------------

if __name__ == "__main__":
    app = MRQLauncher()
    app.mainloop()
