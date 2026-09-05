import os
import sys
import unittest
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

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


if __name__ == "__main__":
    unittest.main()
