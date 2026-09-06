"""Capture reproducible real-Qt screenshots for the SVG icon system."""

import argparse
import importlib.util
import os
import pathlib
import sys


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--scale", default="1")
    return parser.parse_args()


ARGS = parse_args()
os.environ["QT_SCALE_FACTOR"] = ARGS.scale

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QMainWindow


ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "code" / "mrq_launcher.py"
SPEC = importlib.util.spec_from_file_location("mrq_launcher_capture", SOURCE)
mrq = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(mrq)
mrq.UserSettingsRepository.load = classmethod(lambda cls: dict(cls.DEFAULTS))


def render_icon(painter, path, color, rect):
    svg = path.read_text(encoding="utf-8").replace("currentColor", color)
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    if not renderer.isValid():
        raise RuntimeError(f"Invalid SVG: {path}")
    renderer.render(painter, QRectF(*rect))


def capture_icon_sheet(output_path):
    names = sorted(mrq.ACTION_ICON_NAMES)
    columns = 5
    cell_width, cell_height = 190, 68
    rows = (len(names) + columns - 1) // columns
    pixmap = QPixmap(columns * cell_width, rows * cell_height)
    pixmap.fill(QColor("#0B0D12"))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(QColor("#D7DEE8"))
    font = QFont("Segoe UI", 10)
    painter.setFont(font)
    for index, name in enumerate(names):
        row, column = divmod(index, columns)
        x, y = column * cell_width, row * cell_height
        painter.fillRect(x + 5, y + 5, cell_width - 10, cell_height - 10, QColor("#171B22"))
        render_icon(painter, pathlib.Path(mrq.action_icon_path(name)), "#D7DEE8", (x + 18, y + 18, 24, 24))
        painter.drawText(x + 54, y + 10, cell_width - 64, cell_height - 20, Qt.AlignVCenter, name)
    painter.end()
    if not pixmap.save(str(output_path)):
        raise RuntimeError(f"Could not save {output_path}")


def main():
    ARGS.output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    original_exec = QApplication.exec
    scale_slug = ARGS.scale.replace(".", "_")

    def capture_and_quit():
        windows = [widget for widget in app.topLevelWidgets() if isinstance(widget, QMainWindow)]
        if not windows:
            raise RuntimeError("MRQ main window was not created")
        window = windows[0]
        for index in range(1, 5):
            window.settings.tasks.append(mrq.RenderTask(
                uproject=r"D:\Projects\Example Project\Example.uproject",
                level=f"/Game/Maps/Demo_{index}.Demo_{index}",
                sequence=f"/Game/Cinematics/Shot_{index:02d}.Shot_{index:02d}",
                preset="/Game/Render/High.High",
            ))
        window.refresh_queue_view()
        window.table.selectRow(0)
        window.resize(1420, 860)
        app.processEvents()
        full_path = ARGS.output / f"mrq_launcher_icons_full_{scale_slug}x.png"
        if not window.grab().save(str(full_path)):
            raise RuntimeError(f"Could not save {full_path}")
        window.enter_minimal_mode()
        window.resize(900, 550)
        app.processEvents()
        minimal_path = ARGS.output / f"mrq_launcher_icons_minimal_{scale_slug}x.png"
        if not window.grab().save(str(minimal_path)):
            raise RuntimeError(f"Could not save {minimal_path}")
        capture_icon_sheet(ARGS.output / f"mrq_launcher_icon_sheet_{scale_slug}x.png")
        app.quit()

    def capture_exec(*_args):
        QTimer.singleShot(250, capture_and_quit)
        return original_exec()

    QApplication.exec = capture_exec
    return mrq.run_qt_shell()


if __name__ == "__main__":
    raise SystemExit(main())
