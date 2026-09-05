"""Structural visual smoke test for the persistent Qt frontend."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from renweave.qt_gui import QtRenWeaveWindow


def verify_layout() -> None:
    application = QApplication.instance() or QApplication([])
    window = QtRenWeaveWindow()
    try:
        pages = tuple(page.scroll for page in window.pages)
        for width, height in ((900, 640), (1240, 840), (1440, 900)):
            window.resize(width, height)
            application.processEvents()
            for index in range(len(pages)):
                window.step = index
                window._refresh_shell()
                application.processEvents()
                if window.stack.currentWidget() is not pages[index]:
                    raise RuntimeError(f"Page {index} was recreated during a layout change")
        window.step = 3
        window._refresh_shell()
        review_page = window.pages[3].scroll
        window._toggle_pending_details()
        window._toggle_review_details()
        if window.pages[3].scroll is not review_page:
            raise RuntimeError("Review interactions replaced the page widget")
        window.step = 4
        window._refresh_shell()
        progress_page = window.pages[4].scroll
        window._toggle_log()
        if window.pages[4].scroll is not progress_page:
            raise RuntimeError("Progress interactions replaced the page widget")
    finally:
        window.close()
    print("Verified 5 persistent Qt pages across 3 window sizes and local module toggles")


if __name__ == "__main__":
    verify_layout()
