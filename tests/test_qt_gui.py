import os
import sys
import unittest

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


if __name__ == "__main__":
    unittest.main()
