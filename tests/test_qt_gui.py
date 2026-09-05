import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from renweave.decompiler import run_unrpyc_in_process
from renweave.qt_gui import QtRenWeaveWindow


class QtFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_shell_keeps_five_page_instances_and_switches_in_place(self):
        window = QtRenWeaveWindow()
        try:
            self.assertEqual(len(window.pages), 5)
            page_widgets = [page.scroll for page in window.pages]
            window.step = 3
            window._refresh_shell()
            window.step = 4
            window._refresh_shell()
            self.assertEqual([page.scroll for page in window.pages], page_widgets)
            self.assertIs(window.stack.currentWidget(), page_widgets[4])
        finally:
            window.close()

    def test_frontend_does_not_load_heavy_qt_modules(self):
        self.assertNotIn("PySide6.QtWebEngineCore", sys.modules)
        self.assertNotIn("PySide6.QtMultimedia", sys.modules)
        self.assertNotIn("PySide6.QtQuick", sys.modules)

    def test_unrpyc_isolation_survives_pyside_import_hook(self):
        entrypoint = (
            Path(__file__).parents[1]
            / "src"
            / "renweave"
            / "_vendor"
            / "unrpyc"
            / "unrpyc.py"
        )
        returncode, stdout, stderr = run_unrpyc_in_process(entrypoint, ["--version"])
        self.assertEqual(returncode, 0, stderr)
        self.assertEqual(stdout.strip(), "Unrpyc v2.0.3")

    def test_review_and_progress_modules_toggle_in_place(self):
        window = QtRenWeaveWindow()
        try:
            window.show()
            window._scope_preview_inventory = SimpleNamespace(
                model_units=2,
                reusable_units=8,
                pending_units=[{"file": "script.rpy", "line": 12, "source": "Hello"}],
            )
            window._scope_preview_budget = SimpleNamespace(
                estimated_total_low=100,
                estimated_total_high=200,
            )
            window.step = 3
            window._refresh_shell()
            window._refresh_review_preview()
            pending_details = window.pending_details
            review_details = window.review_details_label
            window._toggle_pending_details()
            window._toggle_review_details()
            self.assertIs(window.pending_details, pending_details)
            self.assertIs(window.review_details_label, review_details)
            self.assertTrue(window.pending_details.isVisible())
            self.assertTrue(window.review_details_label.isVisible())

            window._progress_received({"current_operation": "translated script.rpy"})
            window.step = 4
            window._refresh_shell()
            log_edit = window.log_edit
            window._toggle_log()
            self.assertIs(window.log_edit, log_edit)
            self.assertTrue(window.log_edit.isVisible())
            self.assertIn("translated script.rpy", window.log_edit.toPlainText())
        finally:
            window.close()

    def test_locale_switch_retranslates_mounted_widgets(self):
        window = QtRenWeaveWindow()
        try:
            page_widgets = [page.scroll for page in window.pages]
            window._toggle_locale()
            self.assertEqual(window.locale, "zh")
            self.assertEqual(window.pages[0].title.text(), "游戏设置")
            self.assertEqual(window.action_button.text(), "继续")
            self.assertEqual([page.scroll for page in window.pages], page_widgets)
            window._toggle_locale()
            self.assertEqual(window.pages[0].title.text(), "Game setup")
            self.assertEqual(window.action_button.text(), "Continue")
        finally:
            window.close()

    def test_project_revision_invalidates_pending_inspection(self):
        window = QtRenWeaveWindow()
        try:
            window.project_edit.setText("first")
            first_revision = window._project_revision
            window._project_validation_state = "pending"
            window._inspection_revision = first_revision
            window._inspection_value = "first"
            window.project_edit.setText("second")
            self.assertGreater(window._project_revision, first_revision)
            self.assertEqual(window._project_validation_state, "pending")
            self.assertFalse(window.action_button.isEnabled())
            window._project_inspected((object(), [], None), first_revision, "first")
            self.assertIsNone(window._discovered_project)
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
