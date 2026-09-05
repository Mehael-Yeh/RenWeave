"""Qt Widgets frontend for RenWeave.

The Qt frontend keeps the application shell and page widgets mounted for the
life of the window.  Page controllers update their own modules in place; they
do not rebuild a canvas tree when a worker publishes new state.
"""

from __future__ import annotations

import sys
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QFileDialog,
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

from .build_validation import RenpySdkLocator
from .credentials import CredentialStorageError, SecureCredentialStore
from .discovery import ProjectDiscovery
from .existing_translations import discover_existing_languages
from .desktop_core import (
    TranslationRequest,
    _user_home_fallback,
    default_desktop_settings_path,
    execute_blank_translation,
    execute_translation,
)
from .io import atomic_write_json, read_json
from .pipeline import RenWeavePipeline
from .provider import ModelProfile, OpenAICompatibleCatalog
from .provider_presets import PROVIDER_PRESETS, PROVIDER_PRESETS_BY_ID
from .runtime import CancellationToken


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
    progress = Signal(object)


class _Worker(QRunnable):
    def __init__(self, callback, *, progress_callback=None) -> None:
        super().__init__()
        self.callback = callback
        self.progress_callback = progress_callback
        self.signals = _WorkerSignals()

    def run(self) -> None:
        try:
            if self.progress_callback is None:
                result = self.callback()
            else:
                result = self.callback(self.signals.progress.emit)
            self.signals.finished.emit(result)
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
        self._scope_preview_signature = None
        self._scope_preview_status = "idle"
        self._scope_preview_inventory = None
        self._scope_preview_budget = None
        self._scope_preview_worker = None
        self._project_validation_state = "idle"
        self._project_validation_error = ""
        self._discovered_project = None
        self._worker = None
        self._cancel_token = None
        self._logs: list[str] = []
        self._progress_payload: dict[str, object] = {}
        self._last_stage = ""
        self._translation_started = False
        self._blank_translation_mode = False
        self._last_logged_operation = ""
        self._settings_path = default_desktop_settings_path()
        self._settings = self._load_settings()
        self._credential_store = SecureCredentialStore()

        self._configure_palette()
        self._build_shell()
        self._build_pages()
        self._restore_state()
        self._refresh_shell()
        self._inspect_project()

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
        project_browse = QPushButton("Browse project", objectName="Secondary")
        project_browse.clicked.connect(self._browse_project)
        card_layout.addWidget(project_browse)
        self.project_status = QLabel("Waiting for project", objectName="Hint")
        card_layout.addWidget(self.project_status)
        self.workspace_edit = self._field(card_layout, "Workspace", self.initial_workspace)
        workspace_browse = QPushButton("Browse workspace", objectName="Secondary")
        workspace_browse.clicked.connect(self._browse_workspace)
        card_layout.addWidget(workspace_browse)
        self.renpy_sdk_edit = self._field(card_layout, "Ren'Py SDK (optional)")
        sdk_browse = QPushButton("Browse SDK", objectName="Secondary")
        sdk_browse.clicked.connect(self._browse_sdk)
        card_layout.addWidget(sdk_browse)
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
        self.target_combo.currentTextChanged.connect(self._start_scope_preview)
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
        self.provider_ids = [preset.id for preset in PROVIDER_PRESETS]
        self.provider_combo.addItems([preset.name for preset in PROVIDER_PRESETS])
        self.provider_combo.currentIndexChanged.connect(self._provider_changed)
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
        self.connect_model_button.clicked.connect(self._connect_models)
        self.verify_model_button.clicked.connect(self._verify_model)
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
        self.review_details_toggle = QPushButton("Show technical details", objectName="Secondary")
        self.review_details_toggle.clicked.connect(self._toggle_review_details)
        options_layout.addWidget(self.review_details_toggle)
        self.review_details_label = QLabel()
        self.review_details_label.setObjectName("Hint")
        self.review_details_label.setWordWrap(True)
        self.review_details_label.setVisible(False)
        options_layout.addWidget(self.review_details_label)
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
        self.progress_output = QLabel("", objectName="Hint")
        self.progress_output.setWordWrap(True)
        card_layout.addWidget(self.progress_output)
        self.progress_open_button = QPushButton("Open output folder", objectName="Secondary")
        self.progress_open_button.clicked.connect(self._open_output_folder)
        self.progress_open_button.setVisible(False)
        card_layout.addWidget(self.progress_open_button)
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
        if self.step == 4 and self._translation_started:
            self.action_button.setText("Pause")
            self.action_button.setEnabled(True)
        elif self.step == 3 and self._blank_translation_mode:
            self.action_button.setText("Extract blank translation")
            self.action_button.setEnabled(True)
        else:
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
        if self.step == 0:
            self._inspect_project()
            if self._project_validation_state == "pending":
                QMessageBox.information(self, "Project", "Project inspection is still running.")
                return
            if self._project_validation_state != "valid":
                QMessageBox.warning(self, "Project", self._project_validation_error or "Select a valid Ren'Py project first.")
                return
        if self.step < 3:
            if self.step == 2:
                self._blank_translation_mode = not self.use_model_check.isChecked()
            self.step += 1
            self._refresh_shell()
            if self.step == 3:
                self._start_scope_preview()
            return
        if self.step == 3:
            self._start_translation()
            return
        if self.step == 4 and self._cancel_token is not None:
            self._cancel_token.cancel()
            self.progress_runtime.setText("Pausing safely…")

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
        if not value:
            self._project_validation_state = "idle"
            self.project_status.setText("Waiting for project")
            return
        self._project_validation_state = "pending"
        self.project_status.setText("Inspecting project…")

        def inspect():
            project = ProjectDiscovery().discover(value)
            languages = discover_existing_languages(value)
            sdk = RenpySdkLocator().resolve(project_root=project.project_root)
            return project, languages, sdk

        self._run_worker(inspect, self._project_inspected, self._project_inspection_failed)

    def _project_inspected(self, result) -> None:
        project, languages, sdk = result
        self._discovered_project = project
        self.existing_languages = languages
        self._project_validation_state = "valid"
        self._project_validation_error = ""
        if sdk is not None and not self.renpy_sdk_edit.text().strip():
            self.renpy_sdk_edit.setText(str(sdk.root))
            self.require_engine_check.setChecked(True)
        current_target = self.target_combo.currentText().strip()
        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        self.target_combo.addItems(
            [item.language for item in languages]
            + ["简体中文", "繁體中文", "English", "日本語", "Français"]
        )
        if current_target:
            self.target_combo.setCurrentText(current_target)
        self.target_combo.blockSignals(False)
        self.project_status.setText(f"Project detected · {len(languages)} existing language(s)")
        self._start_scope_preview()

    def _project_inspection_failed(self, error: BaseException) -> None:
        self._project_validation_state = "invalid"
        self._project_validation_error = str(error)
        self.project_status.setText(f"Project not recognized: {error}")

    def _start_scope_preview(self, *_args) -> None:
        project = self.project_edit.text().strip()
        workspace = self.workspace_edit.text().strip() or str(_user_home_fallback() / "Documents" / "RenWeaveWork")
        target = self.target_combo.currentText().strip()
        if not project or not target:
            return
        signature = (project, workspace, self.source_combo.currentText().strip() or "auto", target)
        if signature == self._scope_preview_signature and self._scope_preview_status == "scanning":
            return
        self._scope_preview_signature = signature
        self._scope_preview_status = "scanning"
        self._scope_preview_inventory = None
        self._scope_preview_budget = None
        self.language_scope_label.setText("Scanning translation scope…")

        def preview():
            return RenWeavePipeline(workspace).preview_translation_scope(
                project,
                source_language=signature[2],
                target_language=target,
            )

        self._run_worker(preview, self._scope_preview_ready, self._scope_preview_failed)

    def _scope_preview_ready(self, result) -> None:
        inventory, budget = result
        self._scope_preview_status = "ready"
        self._scope_preview_inventory = inventory
        self._scope_preview_budget = budget
        self.language_scope_label.setText(
            f"{inventory.total_units} units · {inventory.reusable_units} reusable · {inventory.model_units} require model translation"
        )
        self._refresh_review_preview()

    def _scope_preview_failed(self, error: BaseException) -> None:
        self._scope_preview_status = "error"
        self.language_scope_label.setText(f"Scope preview failed: {error}")

    def _provider_changed(self, index: int) -> None:
        if not self.provider_ids:
            return
        preset = PROVIDER_PRESETS_BY_ID[self.provider_ids[index]]
        self.endpoint_edit.setText(preset.base_url)
        self._save_settings()

    def _connect_models(self) -> None:
        profile = self._profile()
        self.model_status.setText("Loading models…")
        self._run_worker(
            lambda: OpenAICompatibleCatalog(profile).list_models(),
            lambda catalog: self._models_loaded(catalog),
            lambda error: self.model_status.setText(f"Model loading failed: {error}"),
        )

    def _models_loaded(self, catalog) -> None:
        self.model_status.setText(f"Loaded {len(catalog.models)} model(s) · {catalog.latency_ms} ms")
        if catalog.models and not self.model_edit.text().strip():
            self.model_edit.setText(catalog.models[0])

    def _verify_model(self) -> None:
        profile = self._profile(require_model=True)
        self.model_status.setText("Verifying model…")
        self._run_worker(
            lambda: OpenAICompatibleCatalog(profile).verify_model(),
            lambda result: self.model_status.setText(f"Verified {result.model} · {result.latency_ms} ms"),
            lambda error: self.model_status.setText(f"Verification failed: {error}"),
        )

    def _profile(self, *, require_model: bool = False) -> ModelProfile:
        provider_id = self.provider_ids[self.provider_combo.currentIndex()]
        preset = PROVIDER_PRESETS_BY_ID[provider_id]
        profile = ModelProfile(
            name=preset.name,
            model=self.model_edit.text().strip(),
            base_url=self.endpoint_edit.text().strip(),
            provider_id=provider_id,
            api_key=self.api_key_edit.text(),
            api_key_env=preset.api_key_env,
            supports_json=preset.supports_json_parameter,
            reasoning_level="auto",
            max_retries=2,
            retry_base_seconds=0.5,
        )
        profile.validate() if require_model else profile.validate_connection()
        return profile

    def _run_worker(self, callback, finished, failed, *, progress: bool = False) -> None:
        worker = _Worker(callback, progress_callback=(lambda: None) if progress else None)
        worker.signals.finished.connect(finished)
        worker.signals.failed.connect(failed)
        if progress:
            worker.signals.progress.connect(self._progress_received)
        self._worker = worker
        self.thread_pool.start(worker)

    def _refresh_review_preview(self) -> None:
        inventory = self._scope_preview_inventory
        budget = self._scope_preview_budget
        if inventory is None:
            return
        self.review_remaining_label.setText(f"Remaining model units: {inventory.model_units}")
        self.review_preserved_label.setText(f"Reusable existing units: {inventory.reusable_units}")
        if budget is not None:
            self.budget_label.setText(
                f"Estimated usage: {budget.estimated_total_low:,}–{budget.estimated_total_high:,} tokens"
            )
        self.pending_title.setText(f"Pending units ({len(inventory.pending_units)})")
        rows = []
        for item in inventory.pending_units[:50]:
            rows.append(f"{item.get('file', '')}:{item.get('line', 0)}  {item.get('source', '')}")
        self.pending_details.setPlainText("\n\n".join(rows))

    def _toggle_review_details(self) -> None:
        visible = not self.review_details_label.isVisible()
        self.review_details_label.setVisible(visible)
        self.review_details_toggle.setText("Hide technical details" if visible else "Show technical details")
        if visible:
            self.review_details_label.setText(
                f"Project: {self.project_edit.text()}\n"
                f"Workspace: {self.workspace_edit.text()}\n"
                f"SDK: {self.renpy_sdk_edit.text() or 'not selected'}"
            )

    def _start_translation(self) -> None:
        if self._blank_translation_mode:
            self._start_blank_translation()
            return
        try:
            profile = self._profile(require_model=not self._blank_translation_mode)
            workspace = Path(self.workspace_edit.text().strip()).expanduser()
            workspace.mkdir(parents=True, exist_ok=True)
            provider_path = workspace / ".renweave" / "provider.json"
            profile.save(provider_path)
            request = TranslationRequest(
                project=self.project_edit.text().strip(),
                workspace=str(workspace),
                provider=str(provider_path),
                source_language=self.source_combo.currentText().strip() or "auto",
                target_language=self.target_combo.currentText().strip(),
                api_key=self.api_key_edit.text(),
                generate_rpa=self.generate_rpa_check.isChecked(),
                install=self.install_check.isChecked(),
                renpy_sdk=self.renpy_sdk_edit.text().strip(),
                require_engine_validation=self.require_engine_check.isChecked(),
            )
            request.validate()
        except (OSError, TypeError, ValueError) as exc:
            QMessageBox.warning(self, "Cannot start", str(exc))
            return
        self._translation_started = True
        self._cancel_token = CancellationToken()
        self.step = 4
        self._refresh_shell()
        self._run_worker(
            lambda emit: execute_translation(
                request,
                cancel_token=self._cancel_token,
                progress_callback=emit,
            ),
            self._translation_finished,
            self._translation_failed,
            progress=True,
        )

    def _start_blank_translation(self) -> None:
        project = self.project_edit.text().strip()
        workspace = self.workspace_edit.text().strip()
        source = self.source_combo.currentText().strip() or "auto"
        target = self.target_combo.currentText().strip()
        if not project or not workspace or not target:
            QMessageBox.warning(self, "Cannot start", "Project, workspace and target language are required.")
            return
        self._translation_started = True
        self._cancel_token = CancellationToken()
        self.step = 4
        self._refresh_shell()
        self._run_worker(
            lambda emit: execute_blank_translation(
                project,
                workspace,
                source,
                target,
                cancel_token=self._cancel_token,
                progress_callback=emit,
            ),
            self._translation_finished,
            self._translation_failed,
            progress=True,
        )

    def _translation_finished(self, state) -> None:
        self._last_stage = "complete"
        self._progress_payload = state.to_dict()
        self._translation_started = False
        self.progress_heading.setText("Translation package is ready")
        self.progress_runtime.setText("Completed")
        output_dir = str(self._progress_payload.get("output_dir", "") or "")
        package_path = str(self._progress_payload.get("package_path", "") or "")
        self.progress_output.setText(
            "\n".join(item for item in (
                f"RPY output: {output_dir}" if output_dir else "",
                f"RPA output: {package_path}" if package_path else "",
            ) if item)
        )
        self.progress_open_button.setVisible(bool(output_dir))
        self._refresh_shell()

    def _translation_failed(self, error: BaseException) -> None:
        self._last_stage = "failed"
        self._translation_started = False
        self.progress_runtime.setText(f"Translation failed: {error}")
        self._refresh_shell()

    def _open_output_folder(self) -> None:
        output_dir = str(self._progress_payload.get("output_dir", "") or "")
        if not output_dir:
            return
        try:
            target = str(Path(output_dir).expanduser().resolve(strict=True))
            if os.name == "nt":
                os.startfile(target)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", target])
            else:
                subprocess.Popen(["xdg-open", target])
        except OSError as exc:
            QMessageBox.warning(self, "Open output", str(exc))

    def _progress_received(self, payload) -> None:
        self._progress_payload = payload.to_dict() if hasattr(payload, "to_dict") else dict(payload)
        percent = float(self._progress_payload.get("progress_percent", 0) or 0)
        self.progress_bar.setValue(max(0, min(100, round(percent))))
        self.progress_percent.setText(f"{percent:.0f}%")
        self.progress_heading.setText(str(self._progress_payload.get("current_operation", "Translating")))
        completed = self._progress_payload.get("completed_scenes", 0)
        total = self._progress_payload.get("total_scenes", 0)
        self.progress_stats.setText(f"Scenes: {completed}/{total}")
        self.progress_runtime.setText(str(self._progress_payload.get("stage", "running")))
        operation = str(self._progress_payload.get("current_operation", "") or "")
        if operation and operation != self._last_logged_operation:
            self._last_logged_operation = operation
            self.log_edit.append(operation)

    def _load_settings(self) -> dict[str, object]:
        try:
            payload = read_json(self._settings_path)
        except (OSError, ValueError, TypeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _restore_state(self) -> None:
        saved_locale = str(self._settings.get("locale", "en"))
        if saved_locale in {"en", "zh"}:
            self.locale = saved_locale
            self.locale_button.setText("中文" if self.locale == "en" else "English")
        provider_id = str(self._settings.get("provider_id", "openai"))
        if provider_id in self.provider_ids:
            self.provider_combo.setCurrentIndex(self.provider_ids.index(provider_id))
        self.model_edit.setText(str(self._settings.get("model", "")))
        self.endpoint_edit.setText(str(self._settings.get("base_url", self.endpoint_edit.text())))
        identity = (provider_id, self.endpoint_edit.text().strip())
        try:
            secret = self._credential_store.get(*identity)
        except CredentialStorageError:
            secret = ""
        self.api_key_edit.setText(secret or "")

    def _save_settings(self) -> None:
        payload = {
            "schema_version": 1,
            "locale": self.locale,
            "provider_id": self.provider_ids[self.provider_combo.currentIndex()],
            "model": self.model_edit.text().strip(),
            "base_url": self.endpoint_edit.text().strip(),
        }
        try:
            atomic_write_json(self._settings_path, payload)
        except OSError:
            pass

    def _save_api_key(self) -> None:
        provider_id = self.provider_ids[self.provider_combo.currentIndex()]
        identity = (provider_id, self.endpoint_edit.text().strip())
        secret = self.api_key_edit.text()
        if not secret:
            return
        try:
            self._credential_store.set(*identity, secret)
        except CredentialStorageError:
            self._logs.append("Could not save the API key in the system credential store.")

    def _browse_project(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Select Ren'Py project")
        if selected:
            self.project_edit.setText(selected)

    def _browse_workspace(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Select workspace")
        if selected:
            self.workspace_edit.setText(selected)

    def _browse_sdk(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Select Ren'Py SDK")
        if selected:
            self.renpy_sdk_edit.setText(selected)
            self.require_engine_check.setChecked(True)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._save_api_key()
        self._save_settings()
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
