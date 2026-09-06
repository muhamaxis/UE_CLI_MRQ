import importlib.util
import os
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "code" / "mrq_launcher.py"
SPEC = importlib.util.spec_from_file_location("mrq_launcher_qt_tests", SOURCE)
mrq = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(mrq)


try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSettings, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
except ImportError:
    QApplication = None


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class QtWorkspaceTests(unittest.TestCase):
    def test_queue_first_layout_and_refresh_contract(self):
        with tempfile.TemporaryDirectory() as settings_dir:
            QSettings.setDefaultFormat(QSettings.IniFormat)
            QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, settings_dir)
            observed = {}
            original_exec = QApplication.exec

            def inspect_exec(app):
                window = next(widget for widget in app.topLevelWidgets() if hasattr(widget, "runtime_queue"))
                window.resize(1120, 680)
                window.settings.tasks = [
                    mrq.RenderTask(
                        uproject="F:/A very long production path/Project/Example.uproject",
                        level="/Game/Maps/Main.Main",
                        sequence=f"/Game/Sequences/Shot_{index:03d}.Shot_{index:03d}",
                        preset="/Game/Presets/Production.Production",
                    )
                    for index in range(100)
                ]
                window.state = [mrq.default_task_state() for _ in window.settings.tasks]
                window.validation_results = [
                    mrq.TaskValidationResult("Ready", "All local files were found.")
                    for _ in window.settings.tasks
                ]
                window._rebuild_order_for_enabled_tasks()
                window.refresh_queue_view()
                window._select_task_index(70)
                app.processEvents()
                window.table.verticalScrollBar().setValue(42)
                selected_before = window.selected_indices()
                window.refresh_queue_view()
                app.processEvents()
                scroll_after_refresh = window.table.verticalScrollBar().value()
                selected_after_refresh = window.selected_indices()
                window._select_task_index(0)
                window.table.setFocus()
                QTest.keyClick(window.table, Qt.Key_Space)
                observed.update(
                    tasks=window.table.rowCount(),
                    filter_width=window.filter_edit.width(),
                    selected_before=selected_before,
                    selected_after=selected_after_refresh,
                    scroll=scroll_after_refresh,
                    validation=window.table.item(0, 2).text(),
                    inspector_tooltip=window.inspector_labels["project"].toolTip(),
                    primary_text=window.render_enabled_button.text(),
                    primary_role=window.render_enabled_button.property("role"),
                    danger_role=window.stop_all_button.property("role"),
                    space_toggled=not window.settings.tasks[0].enabled,
                    filter_tab_focus=bool(window.filter_edit.focusPolicy() & Qt.TabFocus),
                    stop_visible=window.stop_all_button.isVisibleTo(window),
                )
                window.hide()
                return 0

            QApplication.exec = inspect_exec
            try:
                self.assertEqual(mrq.run_qt_shell(), 0)
            finally:
                QApplication.exec = original_exec

        self.assertEqual(observed["tasks"], 100)
        self.assertGreaterEqual(observed["filter_width"], 260)
        self.assertEqual(observed["selected_before"], observed["selected_after"])
        self.assertGreater(observed["scroll"], 0)
        self.assertEqual(observed["validation"], "Ready")
        self.assertIn("A very long production path", observed["inspector_tooltip"])
        self.assertEqual(observed["primary_text"], "Render Enabled (99)")
        self.assertEqual(observed["primary_role"], "primary")
        self.assertEqual(observed["danger_role"], "danger")
        self.assertTrue(observed["space_toggled"])
        self.assertTrue(observed["filter_tab_focus"])
        self.assertFalse(observed["stop_visible"])

    def test_validation_details_are_not_truncated(self):
        details = [f"detail {index}" for index in range(8)]
        result = mrq.TaskValidationResult("Invalid", "Missing files.", details)
        self.assertTrue(all(detail in result.display_text for detail in details))


if __name__ == "__main__":
    unittest.main()
