"""Qt Widgets frontend for RenWeave.

The Qt frontend keeps the application shell and page widgets mounted for the
life of the window.  Page controllers update their own modules in place; they
do not rebuild a canvas tree when a worker publishes new state.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QRunnable, QThreadPool, QTimer, Qt, Signal
from PySide6.QtGui import QCloseEvent, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpacerItem,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .gui import COPY


@dataclass(slots=True)
class QtPage:
    key: str
    title: QLabel
    body: QLabel
    scroll: QScrollArea
    content: QWidget


class _WorkerSignals(QObject):
    finished = Signal(object)
    failed = Signal(object)


class _Worker(QRunnable):
    def __init__(self, callback) -> None:
        super().__init__()
        self.callback = callback
        self.signals = _WorkerSignals()

    def run(self) -> None:
        try:
            self.signals.finished.emit(self.callback())
        except BaseException as exc:  # pragma: no cover - exercised by UI
            self.signals.failed.emit(exc)


class QtRenWeaveWindow(QMainWindow):
    """Persistent Qt shell used while the business layer is migrated."""

    STEPS = ("game", "languages", "model", "review", "progress")

    def __init__(self, *, initial_project: str = "", initial_workspace: str = "") -> None:
        super().__init__()
        self.setWindowTitle("RenWeave")
        self.resize(1240, 840)
        self.setMinimumSize(900, 640)
        self.locale = "en"
        self.step = 0
        self.initial_project = initial_project
        self.initial_workspace = initial_workspace
        self.thread_pool = QThreadPool.globalInstance()
        self._inspection_timer = QTimer(self)
        self._inspection_timer.setSingleShot(True)
        self._inspection_timer.timeout.connect(self._inspect_project)

        self._configure_palette()
        self._build_shell()
        self._build_pages()
        self._refresh_shell()

    def _configure_palette(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #f7f8fc; color: #20213a; }
            QFrame#Sidebar { background: #20213a; }
            QFrame#Card { background: #ffffff; border: 1px solid #e3e5f0; border-radius: 12px; }
            QLabel#Brand { color: #ffffff; font-size: 20px; font-weight: 700; }
            QLabel#PageTitle { color: #20213a; font-size: 26px; font-weight: 700; }
            QLabel#PageBody { color: #676a82; font-size: 13px; }
            QLabel#SectionTitle { color: #20213a; font-size: 16px; font-weight: 700; }
            QLabel#Hint { color: #74778d; }
            QLabel#Status { color: #454963; font-weight: 600; }
            QPushButton { min-height: 32px; padding: 0 14px; border-radius: 7px; }
            QPushButton#Primary { background: #5b5ce2; color: white; font-weight: 700; }
            QPushButton#Primary:hover { background: #4f50cb; }
            QPushButton#Secondary { background: #ffffff; color: #42455f; border: 1px solid #d5d8e6; }
            QPushButton#Nav { color: #c9cbe0; text-align: left; border: 0; padding: 8px 14px; }
            QPushButton#Nav:hover { background: #303252; }
            QPushButton#Nav[current="true"] { background: #5b5ce2; color: #ffffff; font-weight: 700; }
            QLineEdit, QComboBox, QTextEdit { background: #ffffff; border: 1px solid #d5d8e6; border-radius: 6px; padding: 7px; }
            QProgressBar { border: 0; background: #e5e7f3; border-radius: 5px; height: 10px; }
            QProgressBar::chunk { background: #5b5ce2; border-radius: 5px; }
            """
        )

    def _build_shell(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.sidebar = QFrame(objectName="Sidebar")
        self.sidebar.setFixedWidth(224)
        sidebar_layout = QVBoxLayout(self.sidebar)
        sidebar_layout.setContentsMargins(18, 24, 18, 18)
        brand = QLabel("RenWeave", objectName="Brand")
        sidebar_layout.addWidget(brand)
        sidebar_layout.addSpacing(26)
        self.nav_buttons: list[QPushButton] = []
        for index, key in enumerate(self.STEPS):
            button = QPushButton(objectName="Nav")
            button.clicked.connect(lambda _checked=False, selected=index: self._go_to_step(selected))
            self.nav_buttons.append(button)
            sidebar_layout.addWidget(button)
        sidebar_layout.addItem(QSpacerItem(1, 1, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding))
        root_layout.addWidget(self.sidebar)

        main = QWidget()
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(28, 20, 28, 18)
        main_layout.setSpacing(14)
        top = QHBoxLayout()
        self.breadcrumb = QLabel()
        self.breadcrumb.setObjectName("Hint")
        top.addWidget(self.breadcrumb)
        top.addStretch()
        self.locale_button = QPushButton("中文", objectName="Secondary")
        self.locale_button.clicked.connect(self._toggle_locale)
        top.addWidget(self.locale_button)
        main_layout.addLayout(top)

        self.stack = QStackedWidget()
        main_layout.addWidget(self.stack, 1)

        footer = QFrame(objectName="Card")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(14, 10, 14, 10)
        self.back_button = QPushButton("Back", objectName="Secondary")
        self.back_button.clicked.connect(self._go_back)
        footer_layout.addWidget(self.back_button)
        self.footer_effect = QLabel()
        self.footer_effect.setAlignment(Qt.AlignmentFlag.AlignCenter)
        footer_layout.addWidget(self.footer_effect, 1)
        self.action_button = QPushButton("Continue", objectName="Primary")
        self.action_button.clicked.connect(self._continue)
        footer_layout.addWidget(self.action_button)
        main_layout.addWidget(footer)
        root_layout.addWidget(main, 1)

    def _new_page(self, key: str, title: str, body: str) -> tuple[QScrollArea, QWidget, QVBoxLayout]:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(2, 2, 10, 18)
        layout.setSpacing(12)
        title_label = QLabel(title, objectName="PageTitle")
        body_label = QLabel(body, objectName="PageBody")
        body_label.setWordWrap(True)
        layout.addWidget(title_label)
        layout.addWidget(body_label)
        scroll.setWidget(content)
        self.stack.addWidget(scroll)
        page = QtPage(key, title_label, body_label, scroll, content)
        self.pages.append(page)
        return scroll, content, layout

    def _build_pages(self) -> None:
        self.pages: list[QtPage] = []
        self.game_page, self.game_layout = self._build_game_page()
        self.language_page, self.language_layout = self._build_language_page()
        self.model_page, self.model_layout = self._build_model_page()
        self.review_page, self.review_layout = self._build_review_page()
        self.progress_page, self.progress_layout = self._build_progress_page()

    @staticmethod
    def _card() -> tuple[QFrame, QVBoxLayout]:
        card = QFrame(objectName="Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(9)
        return card, layout

    @staticmethod
    def _field(layout: QVBoxLayout, label: str, value: str = "") -> QLineEdit:
        layout.addWidget(QLabel(label, objectName="SectionTitle"))
        edit = QLineEdit(value)
        layout.addWidget(edit)
        return edit

    def _build_game_page(self):
        scroll, content, layout = self._new_page("game", "Game setup", "Choose the Ren'Py project and workspace used by the translation pipeline.")
        card, card_layout = self._card()
        self.project_edit = self._field(card_layout, "Game project", self.initial_project)
        self.project_edit.textChanged.connect(lambda: self._inspection_timer.start(150))
        self.project_status = QLabel("Waiting for project", objectName="Hint")
        card_layout.addWidget(self.project_status)
        self.workspace_edit = self._field(card_layout, "Workspace", self.initial_workspace)
        self.renpy_sdk_edit = self._field(card_layout, "Ren'Py SDK (optional)")
        self.require_engine_check = QCheckBox("Require engine validation")
        card_layout.addWidget(self.require_engine_check)
        layout.addWidget(card)
        layout.addStretch()
        return self.pages[-1], layout

    def _build_language_page(self):
        scroll, content, layout = self._new_page("languages", "Languages", "Choose the source and target language for the generated translation files.")
        card, card_layout = self._card()
        card_layout.addWidget(QLabel("Source language", objectName="SectionTitle"))
        self.source_combo = QComboBox()
        self.source_combo.setEditable(True)
        self.source_combo.addItems(["auto", "English", "简体中文", "繁體中文", "日本語"])
        card_layout.addWidget(self.source_combo)
        card_layout.addWidget(QLabel("Target language", objectName="SectionTitle"))
        self.target_combo = QComboBox()
        self.target_combo.setEditable(True)
        self.target_combo.addItems(["简体中文", "繁體中文", "English", "日本語", "Français"])
        card_layout.addWidget(self.target_combo)
        self.language_scope_label = QLabel("Scope preview will appear here.", objectName="Hint")
        self.language_scope_label.setWordWrap(True)
        card_layout.addWidget(self.language_scope_label)
        layout.addWidget(card)
        layout.addStretch()
        return self.pages[-1], layout

    def _build_model_page(self):
        scroll, content, layout = self._new_page("model", "Model", "Configure the provider and model used for translation.")
        card, card_layout = self._card()
        card_layout.addWidget(QLabel("Provider", objectName="SectionTitle"))
        self.provider_combo = QComboBox()
        self.provider_combo.addItems(["OpenAI", "MiniMax", "Alibaba Cloud", "Custom API"])
        card_layout.addWidget(self.provider_combo)
        self.api_key_edit = self._field(card_layout, "API key")
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.model_edit = self._field(card_layout, "Model")
        self.endpoint_edit = self._field(card_layout, "Endpoint")
        self.use_model_check = QCheckBox("Use model translation")
        self.use_model_check.setChecked(True)
        card_layout.addWidget(self.use_model_check)
        buttons = QHBoxLayout()
        self.connect_model_button = QPushButton("Load models", objectName="Secondary")
        self.verify_model_button = QPushButton("Verify model", objectName="Secondary")
        buttons.addWidget(self.connect_model_button)
        buttons.addWidget(self.verify_model_button)
        buttons.addStretch()
        card_layout.addLayout(buttons)
        self.model_status = QLabel("Not connected", objectName="Hint")
        card_layout.addWidget(self.model_status)
        layout.addWidget(card)
        layout.addStretch()
        return self.pages[-1], layout

    def _build_review_page(self):
        scroll, content, layout = self._new_page("review", "Review", "Review the translation scope and output options before starting.")
        task_card, task_layout = self._card()
        self.review_mode_label = QLabel("Model translation", objectName="Status")
        self.review_remaining_label = QLabel("Waiting for scope preview", objectName="Status")
        self.review_preserved_label = QLabel("", objectName="Hint")
        task_layout.addWidget(self.review_mode_label)
        task_layout.addWidget(self.review_remaining_label)
        task_layout.addWidget(self.review_preserved_label)
        layout.addWidget(task_card)
        budget_card, budget_layout = self._card()
        self.budget_label = QLabel("Token usage estimate unavailable", objectName="SectionTitle")
        budget_layout.addWidget(self.budget_label)
        layout.addWidget(budget_card)
        options, options_layout = self._card()
        self.generate_rpa_check = QCheckBox("Generate RPA package")
        self.generate_rpa_check.setChecked(True)
        self.install_check = QCheckBox("Install after validation")
        options_layout.addWidget(self.generate_rpa_check)
        options_layout.addWidget(self.install_check)
        layout.addWidget(options)
        pending, pending_layout = self._card()
        pending_header = QHBoxLayout()
        self.pending_title = QLabel("No pending units", objectName="SectionTitle")
        self.pending_toggle = QPushButton("Show details", objectName="Secondary")
        self.pending_toggle.clicked.connect(self._toggle_pending_details)
        pending_header.addWidget(self.pending_title)
        pending_header.addStretch()
        pending_header.addWidget(self.pending_toggle)
        pending_layout.addLayout(pending_header)
        self.pending_details = QTextEdit()
        self.pending_details.setReadOnly(True)
        self.pending_details.setVisible(False)
        self.pending_details.setMinimumHeight(150)
        pending_layout.addWidget(self.pending_details)
        layout.addWidget(pending)
        layout.addStretch()
        return self.pages[-1], layout

    def _build_progress_page(self):
        scroll, content, layout = self._new_page("progress", "Translation", "Follow the current translation task without leaving this page.")
        card, card_layout = self._card()
        self.progress_heading = QLabel("Ready", objectName="SectionTitle")
        self.progress_percent = QLabel("", objectName="Status")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        card_layout.addWidget(self.progress_heading)
        card_layout.addWidget(self.progress_percent)
        card_layout.addWidget(self.progress_bar)
        self.progress_runtime = QLabel("Idle", objectName="Hint")
        card_layout.addWidget(self.progress_runtime)
        self.progress_stats = QLabel("", objectName="Status")
        self.progress_stats.setWordWrap(True)
        card_layout.addWidget(self.progress_stats)
        self.log_toggle = QPushButton("Show log", objectName="Secondary")
        self.log_toggle.clicked.connect(self._toggle_log)
        card_layout.addWidget(self.log_toggle)
        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setVisible(False)
        self.log_edit.setMinimumHeight(180)
        card_layout.addWidget(self.log_edit)
        layout.addWidget(card)
        layout.addStretch()
        return self.pages[-1], layout

    def _refresh_shell(self) -> None:
        self.stack.setCurrentIndex(self.step)
        for index, button in enumerate(self.nav_buttons):
            button.setText(f"{'✓' if index < self.step else f'{index + 1:02d}'}    {self.STEPS[index].title()}")
            button.setProperty("current", "true" if index == self.step else "false")
            button.style().unpolish(button)
            button.style().polish(button)
            button.setEnabled(index <= self.step)
        self.back_button.setVisible(self.step > 0)
        self.breadcrumb.setText(f"Step {self.step + 1} / {len(self.STEPS)}")
        self.footer_effect.setText(self._footer_effect())
        self.action_button.setText("Start" if self.step == 3 else "Continue")

    def _footer_effect(self) -> str:
        return (
            "Review and start translation"
            if self.step == 3
            else "Translation is running"
            if self.step == 4
            else "Changes are saved locally"
        )

    def _go_back(self) -> None:
        if self.step > 0:
            self.step -= 1
            self._refresh_shell()

    def _go_to_step(self, selected: int) -> None:
        if 0 <= selected <= self.step:
            self.step = selected
            self._refresh_shell()

    def _continue(self) -> None:
        if self.step < 3:
            self.step += 1
            self._refresh_shell()
            return
        if self.step == 3:
            self.step = 4
            self._refresh_shell()

    def _toggle_locale(self) -> None:
        self.locale = "zh" if self.locale == "en" else "en"
        self.locale_button.setText("中文" if self.locale == "en" else "English")

    def _toggle_pending_details(self) -> None:
        visible = not self.pending_details.isVisible()
        self.pending_details.setVisible(visible)
        self.pending_toggle.setText("Hide details" if visible else "Show details")

    def _toggle_log(self) -> None:
        visible = not self.log_edit.isVisible()
        self.log_edit.setVisible(visible)
        self.log_toggle.setText("Hide log" if visible else "Show log")

    def _inspect_project(self) -> None:
        value = self.project_edit.text().strip()
        self.project_status.setText("Project selected" if value else "Waiting for project")

    def closeEvent(self, event: QCloseEvent) -> None:
        self.thread_pool.clear()
        event.accept()


def launch_qt_gui(*, initial_project: str = "", initial_workspace: str = "") -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    window = QtRenWeaveWindow(initial_project=initial_project, initial_workspace=initial_workspace)
    window.show()
    return app.exec()


def main() -> int:
    return launch_qt_gui()


__all__ = ["QtRenWeaveWindow", "launch_qt_gui", "main"]
