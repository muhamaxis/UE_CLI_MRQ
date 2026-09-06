"""Launch a copied PyInstaller build from another cwd and capture its Qt window."""

import argparse
import ctypes
import ctypes.wintypes
import pathlib
import shutil
import subprocess
import tempfile
import time

from PySide6.QtWidgets import QApplication


ROOT = pathlib.Path(__file__).resolve().parents[1]


def launcher_windows():
    user32 = ctypes.windll.user32
    found = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @callback_type
    def callback(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            title = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title, length + 1)
            if "MRQ Launcher" in title.value:
                found.append(hwnd)
        return True

    user32.EnumWindows(callback, 0)
    return found


def find_new_launcher_window(existing, process, timeout=45.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        windows = [hwnd for hwnd in launcher_windows() if hwnd not in existing]
        if windows:
            return windows[0]
        if process.poll() is not None:
            raise RuntimeError(f"Build exited before showing a window (code {process.returncode})")
        time.sleep(0.2)
    raise TimeoutError(f"No new MRQ Launcher window appeared for process {process.pid}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", choices=("onefile", "onedir"), required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    app = QApplication.instance() or QApplication([])

    smoke_root = ROOT / "build" / "relocated-smoke"
    smoke_root.mkdir(parents=True, exist_ok=True)
    run_root = pathlib.Path(tempfile.mkdtemp(prefix=f"Кириллица {args.package} ", dir=smoke_root))
    working_dir = run_root / "different working directory"
    working_dir.mkdir()

    if args.package == "onefile":
        source = ROOT / "dist" / "MRQLauncherCLI.exe"
        target = run_root / "MRQ Launcher Portable.exe"
        shutil.copy2(source, target)
    else:
        source = ROOT / "dist" / "MRQLauncherCLI"
        package_dir = run_root / "MRQ Launcher OneDir"
        shutil.copytree(source, package_dir)
        target = package_dir / "MRQLauncherCLI.exe"

    existing_windows = set(launcher_windows())
    process = subprocess.Popen([str(target)], cwd=working_dir)
    try:
        hwnd = find_new_launcher_window(existing_windows, process)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        time.sleep(1.0)
        app.processEvents()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        pixmap = app.primaryScreen().grabWindow(int(hwnd))
        if pixmap.isNull() or not pixmap.save(str(args.output), "PNG"):
            raise RuntimeError("Qt could not capture the relocated build window")
        print(f"package={args.package} exe={target} cwd={working_dir} screenshot={args.output} size={pixmap.width()}x{pixmap.height()}")
        ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)
        process.wait(timeout=8)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=8)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
