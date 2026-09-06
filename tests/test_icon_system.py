import importlib.util
import os
import pathlib
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "code" / "mrq_launcher.py"
ICONS = ROOT / "resources" / "icons"
BUILD_SCRIPT = ROOT / "tools" / "build_exe.bat"
SPEC = importlib.util.spec_from_file_location("mrq_launcher_icons", SOURCE)
mrq = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(mrq)


class SvgIconCatalogTests(unittest.TestCase):
    def test_catalog_and_svg_files_match(self):
        files = {path.stem for path in ICONS.glob("*.svg")}
        self.assertEqual(files, mrq.ACTION_ICON_NAMES)

    def test_every_svg_uses_shared_geometry_language(self):
        for path in ICONS.glob("*.svg"):
            root = ET.fromstring(path.read_text(encoding="utf-8"))
            with self.subTest(icon=path.name):
                self.assertEqual(root.attrib.get("viewBox"), "0 0 24 24")
                self.assertEqual(root.attrib.get("fill"), "none")
                self.assertEqual(root.attrib.get("stroke"), "currentColor")
                self.assertEqual(root.attrib.get("stroke-width"), "1.8")
                self.assertEqual(root.attrib.get("stroke-linecap"), "round")
                self.assertEqual(root.attrib.get("stroke-linejoin"), "round")

    def test_every_referenced_icon_exists(self):
        source = SOURCE.read_text(encoding="utf-8")
        referenced = set(re.findall(r'qt_icons\.(?:icon|apply_button|apply_action)\([^\n]*?"([a-z-]+)"', source))
        self.assertTrue(referenced)
        self.assertEqual(set(), referenced - mrq.ACTION_ICON_NAMES)

    def test_source_path_is_independent_of_current_working_directory(self):
        with tempfile.TemporaryDirectory() as other_directory:
            with mock.patch.object(os, "getcwd", return_value=other_directory):
                path = pathlib.Path(mrq.action_icon_path("render"))
        self.assertEqual(path, ICONS / "render.svg")

    def test_frozen_path_uses_pyinstaller_bundle_root(self):
        with tempfile.TemporaryDirectory() as bundle_root:
            with mock.patch.object(mrq.sys, "_MEIPASS", bundle_root, create=True):
                path = pathlib.Path(mrq.action_icon_path("render"))
        self.assertEqual(path, pathlib.Path(bundle_root) / "resources" / "icons" / "render.svg")

    def test_unknown_icon_names_are_rejected(self):
        with self.assertRaises(KeyError):
            mrq.action_icon_path("../../outside")

    def test_both_pyinstaller_builds_embed_icon_directory(self):
        script = BUILD_SCRIPT.read_text(encoding="utf-8")
        add_data = '--add-data "%ICONS_ABS%;resources\\icons"'
        self.assertEqual(script.count(add_data), 2)

    def test_action_icons_do_not_fall_back_to_font_glyphs(self):
        source = SOURCE.read_text(encoding="utf-8")
        for glyph in ("▶", "▾", "▸", "⚠"):
            self.assertNotIn(glyph, source)


if __name__ == "__main__":
    unittest.main()
