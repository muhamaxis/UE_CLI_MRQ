"""Capture deterministic real-Qt UI states for visual review.

This tool never starts Unreal. It injects representative queue state after the
production window is built, then saves the native Qt window surface as PNG.
"""

import argparse
import importlib.util
import pathlib
import time
from collections import deque

from PySide6.QtWidgets import QApplication


ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "code" / "mrq_launcher.py"


def load_launcher():
    spec = importlib.util.spec_from_file_location("mrq_launcher_capture", SOURCE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def make_tasks(mrq, count):
    project = "F:/Production/Devoted Island/Builds/September Review/DevotedIsland_Production.uproject"
    return [
        mrq.RenderTask(
            uproject=project,
            level="/Game/NorthernIsle/Maps/Map_Assignment_03.Map_Assignment_03",
            sequence=f"/Game/Cinematics/Episode_04/Sequences/CS_Camera_{index + 1:03d}.CS_Camera_{index + 1:03d}",
            preset="/Game/Cinematics/OCIO/Render_Configuration_Production.Render_Configuration_Production",
            enabled=True,
        )
        for index in range(count)
    ]


def configure_window(mrq, window, scenario, requested_count=None):
    window.user_settings["auto_load_last_queue"] = False
    count = requested_count if requested_count is not None else (25 if scenario != "empty" else 0)
    window.settings.tasks = make_tasks(mrq, count)
    window.state = [mrq.default_task_state() for _ in range(count)]
    window.validation_results = [
        mrq.TaskValidationResult("Ready", "All local project and asset files were found.")
        for _ in range(count)
    ]
    window.queue_order_by_task_id = {
        id(task): index for index, task in enumerate(window.settings.tasks, start=1)
    }

    now = time.time()
    if scenario in ("running", "minimal"):
        for index, state in enumerate(window.state):
            if index < 5:
                state.update(status="Done (00:01:48)", start=now - 800 + index * 110, end=now - 690 + index * 110)
            elif index == 5:
                state.update(status="Rendering (62%)", progress=62, start=now - 74, end=None)
            else:
                state.update(status="Queued")
        active = window.settings.tasks[5]
        window.runtime_queue._active_task = active
        window.runtime_queue._pending = deque(window.settings.tasks[6:])
        window.process_controller.is_active = lambda: True
    elif scenario == "completed":
        for index, state in enumerate(window.state):
            state.update(status="Done (00:01:52)", start=now - 4000 + index * 120, end=now - 3888 + index * 120)
    elif scenario == "failed":
        for index, state in enumerate(window.state):
            status = "Done (00:01:52)" if index < 7 else ("Failed (exit 1)" if index == 7 else "Cancelled (queue)")
            state.update(status=status, start=now - 900 + index * 80, end=now - 820 + index * 80)
        window.validation_results[7] = mrq.TaskValidationResult(
            "Invalid",
            "Local files are missing.",
            [
                "Project file not found: F:/Archive/Long Missing Project Name/DevotedIsland_Production.uproject",
                "Level asset not found: F:/Archive/Content/NorthernIsle/Maps/Map_Assignment_03.umap",
                "Sequence asset not found: F:/Archive/Content/Cinematics/Episode_04/Sequences/CS_Camera_008.uasset",
                "Preset asset not found: F:/Archive/Content/Cinematics/OCIO/Render_Configuration_Production.uasset",
            ],
        )

    window.refresh_queue_view()
    if count:
        target = 5 if scenario in ("running", "minimal") else min(7, count - 1)
        window._select_task_index(target)
    if scenario == "minimal":
        window.enter_minimal_mode()
        window.resize(980, 560)
    else:
        window.resize(1420, 860)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=("empty", "idle", "running", "completed", "failed", "minimal"), required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--width", type=int, default=1420)
    parser.add_argument("--height", type=int, default=860)
    parser.add_argument("--count", type=int)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    mrq = load_launcher()

    original_exec = QApplication.exec

    def capture_exec(app):
        window = next(widget for widget in app.topLevelWidgets() if hasattr(widget, "runtime_queue"))
        configure_window(mrq, window, args.scenario, args.count)
        if args.scenario != "minimal":
            window.resize(args.width, args.height)
        window.show()
        app.processEvents()
        app.processEvents()
        if not window.grab().save(str(args.output), "PNG"):
            raise RuntimeError(f"Could not save screenshot: {args.output}")
        screen = window.screen()
        print(
            f"captured={args.output} size={window.width()}x{window.height()} "
            f"logical_dpi={screen.logicalDotsPerInch():.1f} dpr={screen.devicePixelRatio():.2f}"
        )
        window.hide()
        return 0

    QApplication.exec = capture_exec
    try:
        return mrq.run_qt_shell()
    finally:
        QApplication.exec = original_exec


if __name__ == "__main__":
    raise SystemExit(main())
