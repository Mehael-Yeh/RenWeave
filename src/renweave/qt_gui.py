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
    QGridLayout,
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
    title_key: str
    body_key: str
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


UI_COPY = {
    "en": {
        "nav.game": "Game setup",
        "nav.languages": "Languages",
        "nav.model": "Model",
        "nav.review": "Review",
        "nav.progress": "Translation",
        "page.game.title": "Game setup",
        "page.game.body": "Choose the Ren'Py project and workspace used by the translation pipeline.",
        "page.languages.title": "Languages",
        "page.languages.body": "Choose the source and target language for the generated translation files.",
        "page.model.title": "Model",
        "page.model.body": "Configure the provider and model used for translation.",
        "page.review.title": "Review",
        "page.review.body": "Review the translation scope and output options before starting.",
        "page.progress.title": "Translation",
        "page.progress.body": "Follow the current translation task without leaving this page.",
        "game.project": "Game project",
        "game.workspace": "Workspace",
        "game.sdk": "Ren'Py SDK (optional)",
        "game.browse_project": "Browse project",
        "game.browse_workspace": "Browse workspace",
        "game.browse_sdk": "Browse SDK",
        "game.require_engine": "Require engine validation",
        "game.waiting": "Waiting for project",
        "game.inspecting": "Inspecting project…",
        "game.detected": "Project detected · {count} existing language(s)",
        "game.not_recognized": "Project not recognized: {error}",
        "game.safety_title": "Read-only project analysis",
        "game.safety_body": "Game files stay unchanged during analysis. Caches, checkpoints, logs and packages are written to the workspace.",
        "languages.source": "Source language",
        "languages.target": "Target language",
        "languages.scope": "Scope preview will appear here.",
        "languages.scanning": "Scanning translation scope…",
        "languages.scope_value": "{total} units · {reusable} reusable · {model} require model translation",
        "languages.hint": "Leave Auto detect unless the game intentionally mixes languages or detection is unreliable.",
        "languages.source_hint": "Auto detect is recommended for normal projects.",
        "languages.target_hint": "This determines the Ren'Py language directory.",
        "languages.existing_title": "Existing translations found",
        "languages.existing_body": "Choose one to preserve valid translations and process only missing or outdated text.",
        "model.provider": "Provider",
        "model.api_key": "API key",
        "model.model": "Model",
        "model.endpoint": "Endpoint",
        "model.use": "Use model translation",
        "model.load": "Load models",
        "model.verify": "Verify model",
        "model.not_connected": "Not connected",
        "review.model_translation": "Model translation",
        "review.waiting_scope": "Waiting for scope preview",
        "review.estimate_unavailable": "Token usage estimate unavailable",
        "review.rpa": "Generate RPA package",
        "review.install": "Install after validation",
        "review.details": "Show technical details",
        "review.hide_details": "Hide technical details",
        "review.no_pending": "No pending units",
        "review.show_details": "Show details",
        "review.hide_pending": "Hide details",
        "progress.ready": "Ready",
        "progress.idle": "Idle",
        "progress.open": "Open output folder",
        "progress.show_log": "Show log",
        "progress.hide_log": "Hide log",
        "progress.complete": "Translation package is ready",
        "progress.completed": "Completed",
        "shell.back": "Back",
        "shell.continue": "Continue",
        "shell.start": "Start",
        "shell.pause": "Pause",
        "shell.breadcrumb": "Step {current} / {total}",
        "footer.default": "Changes are saved locally",
        "footer.game": "Next: choose languages. Game files stay unchanged.",
        "footer.languages": "Next: review scope and the Token budget.",
        "footer.model": "Next: review the scope and Token budget. Translation starts explicitly on step 05.",
        "footer.review": "Starts billable model work; checkpoints go to the workspace.",
        "footer.progress": "Pause saves checkpoints; closing keeps completed outputs.",
        "shell.select_project": "Select Ren'Py project",
        "shell.select_workspace": "Select workspace",
        "shell.select_sdk": "Select Ren'Py SDK",
        "dialog.project": "Project",
        "dialog.inspecting": "Project inspection is still running.",
        "dialog.invalid_project": "Select a valid Ren'Py project first.",
    },
    "zh": {
        "nav.game": "游戏设置",
        "nav.languages": "语言设置",
        "nav.model": "模型设置",
        "nav.review": "确认任务",
        "nav.progress": "翻译进度",
        "page.game.title": "游戏设置",
        "page.game.body": "选择翻译流程使用的 Ren'Py 项目和工作区。",
        "page.languages.title": "语言设置",
        "page.languages.body": "选择生成翻译文件使用的源语言和目标语言。",
        "page.model.title": "模型设置",
        "page.model.body": "配置翻译使用的 API 提供商和模型。",
        "page.review.title": "确认任务",
        "page.review.body": "开始翻译前确认翻译范围和输出选项。",
        "page.progress.title": "翻译进度",
        "page.progress.body": "在当前页面查看翻译任务进度。",
        "game.project": "游戏项目",
        "game.workspace": "工作区",
        "game.sdk": "Ren'Py SDK（可选）",
        "game.browse_project": "选择项目",
        "game.browse_workspace": "选择工作区",
        "game.browse_sdk": "选择 SDK",
        "game.require_engine": "要求进行引擎校验",
        "game.waiting": "等待选择项目",
        "game.inspecting": "正在检查项目……",
        "game.detected": "已识别项目 · 已有 {count} 种语言",
        "game.not_recognized": "无法识别项目：{error}",
        "game.safety_title": "只读分析项目",
        "game.safety_body": "分析期间不会修改游戏文件；缓存、检查点、日志和语言包会写入工作区。",
        "languages.source": "源语言",
        "languages.target": "目标语言",
        "languages.scope": "翻译范围预览将在这里显示。",
        "languages.scanning": "正在扫描翻译范围……",
        "languages.scope_value": "共 {total} 个单元 · 可复用 {reusable} 个 · 需要模型翻译 {model} 个",
        "languages.hint": "除非项目混合多种语言或自动识别不可靠，否则建议保留自动检测。",
        "languages.source_hint": "普通项目建议使用自动检测。",
        "languages.target_hint": "该选项决定 Ren'Py 语言目录名称。",
        "languages.existing_title": "发现已有翻译",
        "languages.existing_body": "选择已有语言可以保留有效译文，只处理缺失或已经过时的文本。",
        "model.provider": "提供商",
        "model.api_key": "API 密钥",
        "model.model": "模型",
        "model.endpoint": "接口地址",
        "model.use": "使用模型翻译",
        "model.load": "获取模型",
        "model.verify": "验证模型",
        "model.not_connected": "尚未连接",
        "review.model_translation": "模型翻译",
        "review.waiting_scope": "等待翻译范围预览",
        "review.estimate_unavailable": "暂时无法预估 Token 用量",
        "review.rpa": "生成 RPA 语言包",
        "review.install": "校验后安装",
        "review.details": "显示技术详情",
        "review.hide_details": "隐藏技术详情",
        "review.no_pending": "没有待处理单元",
        "review.show_details": "显示详情",
        "review.hide_pending": "隐藏详情",
        "progress.ready": "准备就绪",
        "progress.idle": "空闲",
        "progress.open": "打开输出目录",
        "progress.show_log": "显示日志",
        "progress.hide_log": "隐藏日志",
        "progress.complete": "翻译包已准备完成",
        "progress.completed": "已完成",
        "shell.back": "返回",
        "shell.continue": "继续",
        "shell.start": "开始",
        "shell.pause": "暂停",
        "shell.breadcrumb": "第 {current} / {total} 步",
        "footer.default": "修改会保存到本地",
        "footer.game": "下一步：选择语言。游戏文件保持不变。",
        "footer.languages": "下一步：确认翻译范围和 Token 预算。",
        "footer.model": "下一步：确认范围和 Token 预算；翻译必须在第 05 页明确开始。",
        "footer.review": "将开始可能计费的模型调用；检查点写入工作区。",
        "footer.progress": "暂停会保存检查点；关闭后仍保留已完成输出。",
        "shell.select_project": "选择 Ren'Py 项目",
        "shell.select_workspace": "选择工作区",
        "shell.select_sdk": "选择 Ren'Py SDK",
        "dialog.project": "项目",
        "dialog.inspecting": "项目检查仍在进行中。",
        "dialog.invalid_project": "请先选择有效的 Ren'Py 项目。",
    },
}


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
        self._project_revision = 0
        self._inspection_revision = None
        self._inspection_value = ""
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
        self._retranslate_ui()
        self._refresh_shell()
        if self.project_edit.text().strip():
            self._inspection_timer.start(0)

    def _configure_palette(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget#Root, QScrollArea, QScrollArea > QWidget > QWidget { background: #f7f8fc; color: #20213a; }
            QFrame#Sidebar { background: #20213a; }
            QFrame#Card { background: #ffffff; border: 1px solid #e3e5f0; border-radius: 12px; }
            QFrame#Card QLabel, QFrame#TintCard QLabel, QFrame#SuccessCard QLabel { background: transparent; }
            QFrame#TintCard { background: #f4f5ff; border: 1px solid #e2e3fb; border-radius: 10px; }
            QFrame#SuccessCard { background: #eefbf3; border: 1px solid #ccebd7; border-radius: 10px; }
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
        root = QWidget(objectName="Root")
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

    def _t(self, key: str, **values: object) -> str:
        text = UI_COPY.get(self.locale, UI_COPY["en"]).get(key, UI_COPY["en"].get(key, key))
        return text.format(**values) if values else text

    def _new_page(self, key: str, title_key: str, body_key: str) -> tuple[QScrollArea, QWidget, QVBoxLayout]:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(2, 2, 10, 18)
        layout.setSpacing(12)
        title_label = QLabel(self._t(title_key), objectName="PageTitle")
        body_label = QLabel(self._t(body_key), objectName="PageBody")
        body_label.setWordWrap(True)
        layout.addWidget(title_label)
        layout.addWidget(body_label)
        scroll.setWidget(content)
        self.stack.addWidget(scroll)
        page = QtPage(key, title_key, body_key, title_label, body_label, scroll, content)
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
    def _card(object_name: str = "Card") -> tuple[QFrame, QVBoxLayout]:
        card = QFrame(objectName=object_name)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(9)
        return card, layout

    def _build_game_page(self):
        scroll, content, layout = self._new_page("game", "page.game.title", "page.game.body")
        card = QFrame(objectName="Card")
        grid = QGridLayout(card)
        grid.setContentsMargins(20, 18, 20, 18)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)
        self.game_project_label = QLabel(objectName="SectionTitle")
        self.game_workspace_label = QLabel(objectName="SectionTitle")
        self.game_sdk_label = QLabel(objectName="SectionTitle")
        self.project_edit = QLineEdit(self.initial_project)
        self.workspace_edit = QLineEdit(self.initial_workspace)
        self.renpy_sdk_edit = QLineEdit()
        self.project_browse_button = QPushButton(objectName="Secondary")
        self.workspace_browse_button = QPushButton(objectName="Secondary")
        self.sdk_browse_button = QPushButton(objectName="Secondary")
        self.project_browse_button.clicked.connect(self._browse_project)
        self.workspace_browse_button.clicked.connect(self._browse_workspace)
        self.sdk_browse_button.clicked.connect(self._browse_sdk)
        self.project_edit.textChanged.connect(self._project_changed)
        rows = (
            (self.game_project_label, self.project_edit, self.project_browse_button),
            (self.game_workspace_label, self.workspace_edit, self.workspace_browse_button),
            (self.game_sdk_label, self.renpy_sdk_edit, self.sdk_browse_button),
        )
        for row, (label, edit, button) in enumerate(rows):
            grid.addWidget(label, row, 0)
            grid.addWidget(edit, row, 1)
            grid.addWidget(button, row, 2)
        self.project_status = QLabel(objectName="Hint")
        self.project_status.setWordWrap(True)
        grid.addWidget(self.project_status, 3, 0, 1, 3)
        self.require_engine_check = QCheckBox()
        grid.addWidget(self.require_engine_check, 4, 0, 1, 3)
        layout.addWidget(card)
        safety, safety_layout = self._card("SuccessCard")
        self.game_safety_title = QLabel(objectName="Status")
        self.game_safety_body = QLabel(objectName="Hint")
        self.game_safety_body.setWordWrap(True)
        safety_layout.addWidget(self.game_safety_title)
        safety_layout.addWidget(self.game_safety_body)
        layout.addWidget(safety)
        layout.addStretch()
        return self.pages[-1], layout

    def _build_language_page(self):
        scroll, content, layout = self._new_page("languages", "page.languages.title", "page.languages.body")
        existing, existing_layout = self._card("SuccessCard")
        self.existing_languages_card = existing
        self.existing_languages_title = QLabel(objectName="Status")
        self.existing_languages_body = QLabel(objectName="Hint")
        self.existing_languages_body.setWordWrap(True)
        self.existing_language_buttons = QVBoxLayout()
        existing_layout.addWidget(self.existing_languages_title)
        existing_layout.addWidget(self.existing_languages_body)
        existing_layout.addLayout(self.existing_language_buttons)
        existing.setVisible(False)
        layout.addWidget(existing)

        card, card_layout = self._card()
        columns = QGridLayout()
        columns.setHorizontalSpacing(16)
        columns.setColumnStretch(0, 1)
        columns.setColumnStretch(1, 1)
        self.source_label = QLabel(objectName="SectionTitle")
        self.target_label = QLabel(objectName="SectionTitle")
        columns.addWidget(self.source_label, 0, 0)
        columns.addWidget(self.target_label, 0, 1)
        self.source_combo = QComboBox()
        self.source_combo.setEditable(True)
        self.source_combo.addItems(["auto", "English", "简体中文", "繁體中文", "日本語"])
        self.target_combo = QComboBox()
        self.target_combo.setEditable(True)
        self.target_combo.addItems(["简体中文", "繁體中文", "English", "日本語", "Français"])
        self.target_combo.currentTextChanged.connect(self._start_scope_preview)
        columns.addWidget(self.source_combo, 1, 0)
        columns.addWidget(self.target_combo, 1, 1)
        self.source_hint = QLabel(objectName="Hint")
        self.source_hint.setWordWrap(True)
        self.target_hint = QLabel(objectName="Hint")
        self.target_hint.setWordWrap(True)
        columns.addWidget(self.source_hint, 2, 0)
        columns.addWidget(self.target_hint, 2, 1)
        card_layout.addLayout(columns)
        self.language_hint = QLabel(objectName="Hint")
        self.language_hint.setWordWrap(True)
        card_layout.addWidget(self.language_hint)
        scope_card, scope_layout = self._card("TintCard")
        self.language_scope_label = QLabel("Scope preview will appear here.", objectName="Hint")
        self.language_scope_label.setWordWrap(True)
        scope_layout.addWidget(self.language_scope_label)
        layout.addWidget(card)
        layout.addWidget(scope_card)
        layout.addStretch()
        return self.pages[-1], layout

    def _build_model_page(self):
        scroll, content, layout = self._new_page("model", "page.model.title", "page.model.body")
        card, card_layout = self._card()
        fields = QGridLayout()
        fields.setHorizontalSpacing(12)
        fields.setVerticalSpacing(8)
        for column in range(4):
            fields.setColumnStretch(column, 1)
        self.model_provider_label = QLabel(objectName="SectionTitle")
        self.api_key_label = QLabel(objectName="SectionTitle")
        self.model_label = QLabel(objectName="SectionTitle")
        self.endpoint_label = QLabel(objectName="SectionTitle")
        fields.addWidget(self.model_provider_label, 0, 0)
        fields.addWidget(self.api_key_label, 0, 1)
        fields.addWidget(self.model_label, 0, 2)
        fields.addWidget(self.endpoint_label, 0, 3)
        self.provider_combo = QComboBox()
        self.provider_ids = [preset.id for preset in PROVIDER_PRESETS]
        self.provider_combo.addItems([preset.name for preset in PROVIDER_PRESETS])
        self.provider_combo.currentIndexChanged.connect(self._provider_changed)
        fields.addWidget(self.provider_combo, 1, 0)
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.model_edit = QLineEdit()
        self.endpoint_edit = QLineEdit()
        fields.addWidget(self.api_key_edit, 1, 1)
        fields.addWidget(self.model_edit, 1, 2)
        fields.addWidget(self.endpoint_edit, 1, 3)
        card_layout.addLayout(fields)
        self.use_model_check = QCheckBox()
        self.use_model_check.setChecked(True)
        card_layout.addWidget(self.use_model_check)
        buttons = QHBoxLayout()
        self.connect_model_button = QPushButton(objectName="Secondary")
        self.verify_model_button = QPushButton(objectName="Secondary")
        self.connect_model_button.clicked.connect(self._connect_models)
        self.verify_model_button.clicked.connect(self._verify_model)
        buttons.addWidget(self.connect_model_button)
        buttons.addWidget(self.verify_model_button)
        buttons.addStretch()
        card_layout.addLayout(buttons)
        self.model_status = QLabel(objectName="Hint")
        card_layout.addWidget(self.model_status)
        layout.addWidget(card)
        layout.addStretch()
        return self.pages[-1], layout

    def _build_review_page(self):
        scroll, content, layout = self._new_page("review", "page.review.title", "page.review.body")
        task_card, task_layout = self._card("TintCard")
        self.review_game_label = QLabel(objectName="SectionTitle")
        self.review_languages_label = QLabel(objectName="Status")
        self.review_mode_label = QLabel(objectName="Status")
        self.review_remaining_label = QLabel(objectName="Status")
        self.review_preserved_label = QLabel("", objectName="Hint")
        task_layout.addWidget(self.review_game_label)
        task_layout.addWidget(self.review_languages_label)
        task_layout.addWidget(self.review_mode_label)
        task_layout.addWidget(self.review_remaining_label)
        task_layout.addWidget(self.review_preserved_label)
        layout.addWidget(task_card)
        budget_card, budget_layout = self._card("TintCard")
        self.budget_title = QLabel(objectName="Hint")
        self.budget_label = QLabel(objectName="SectionTitle")
        self.budget_note = QLabel(objectName="Hint")
        self.budget_note.setWordWrap(True)
        budget_layout.addWidget(self.budget_title)
        budget_layout.addWidget(self.budget_label)
        budget_layout.addWidget(self.budget_note)
        layout.addWidget(budget_card)
        options, options_layout = self._card()
        self.generate_rpa_check = QCheckBox()
        self.generate_rpa_check.setChecked(True)
        self.install_check = QCheckBox()
        options_layout.addWidget(self.generate_rpa_check)
        options_layout.addWidget(self.install_check)
        self.review_details_toggle = QPushButton(objectName="Secondary")
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
        self.pending_title = QLabel(objectName="SectionTitle")
        self.pending_toggle = QPushButton(objectName="Secondary")
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
        scroll, content, layout = self._new_page("progress", "page.progress.title", "page.progress.body")
        card, card_layout = self._card()
        heading = QHBoxLayout()
        self.progress_heading = QLabel(objectName="SectionTitle")
        self.progress_percent = QLabel("", objectName="Status")
        heading.addWidget(self.progress_heading)
        heading.addStretch()
        heading.addWidget(self.progress_percent)
        card_layout.addLayout(heading)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        card_layout.addWidget(self.progress_bar)
        runtime, runtime_layout = self._card("SuccessCard")
        self.progress_runtime = QLabel(objectName="Status")
        runtime_layout.addWidget(self.progress_runtime)
        card_layout.addWidget(runtime)
        phase_row = QHBoxLayout()
        self.progress_phase_labels = []
        for phase in ("Prepare", "Analyze", "Translate", "Validate", "Build"):
            label = QLabel(f"○  {phase}", objectName="Hint")
            phase_row.addWidget(label, 1)
            self.progress_phase_labels.append(label)
        card_layout.addLayout(phase_row)
        self.progress_stats = QLabel("", objectName="Status")
        self.progress_stats.setWordWrap(True)
        card_layout.addWidget(self.progress_stats)
        self.progress_output = QLabel("", objectName="Hint")
        self.progress_output.setWordWrap(True)
        card_layout.addWidget(self.progress_output)
        self.progress_open_button = QPushButton(objectName="Secondary")
        self.progress_open_button.clicked.connect(self._open_output_folder)
        self.progress_open_button.setVisible(False)
        card_layout.addWidget(self.progress_open_button)
        self.log_toggle = QPushButton(objectName="Secondary")
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
            button.setText(f"{'✓' if index < self.step else f'{index + 1:02d}'}    {self._t(f'nav.{self.STEPS[index]}')}")
            button.setProperty("current", "true" if index == self.step else "false")
            button.style().unpolish(button)
            button.style().polish(button)
            button.setEnabled(index <= self.step)
        self.back_button.setVisible(self.step > 0)
        self.breadcrumb.setText(self._t("shell.breadcrumb", current=self.step + 1, total=len(self.STEPS)))
        self.footer_effect.setText(self._footer_effect())
        if self.step == 4 and self._translation_started:
            self.action_button.setText(self._t("shell.pause"))
            self.action_button.setEnabled(True)
        elif self.step == 3 and self._blank_translation_mode:
            self.action_button.setText("提取空白翻译" if self.locale == "zh" else "Extract blank translation")
            self.action_button.setEnabled(True)
        else:
            self.action_button.setText(self._t("shell.start" if self.step == 3 else "shell.continue"))
            self.action_button.setEnabled(not (self.step == 0 and self._project_validation_state == "pending"))

    def _footer_effect(self) -> str:
        return (
            self._t("footer.review")
            if self.step == 3
            else self._t("footer.progress")
            if self.step == 4
            else self._t(f"footer.{self.STEPS[self.step]}")
            if self.step in {0, 1, 2}
            else self._t("footer.default")
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
            if self._project_validation_state == "pending":
                self.project_status.setText(self._t("game.inspecting"))
                return
            if self._project_validation_state != "valid":
                QMessageBox.warning(
                    self,
                    self._t("dialog.project"),
                    self._project_validation_error or self._t("dialog.invalid_project"),
                )
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
        self._retranslate_ui()
        self._save_settings()
        self._refresh_shell()

    def _retranslate_ui(self) -> None:
        """Update mounted widgets in place without rebuilding any page."""
        self.locale_button.setText("中文" if self.locale == "en" else "English")
        for page in self.pages:
            page.title.setText(self._t(page.title_key))
            page.body.setText(self._t(page.body_key))
        self._refresh_static_texts()

    def _refresh_static_texts(self) -> None:
        self.game_project_label.setText(self._t("game.project"))
        self.game_workspace_label.setText(self._t("game.workspace"))
        self.game_sdk_label.setText(self._t("game.sdk"))
        self.project_browse_button.setText(self._t("game.browse_project"))
        self.workspace_browse_button.setText(self._t("game.browse_workspace"))
        self.sdk_browse_button.setText(self._t("game.browse_sdk"))
        self.require_engine_check.setText(self._t("game.require_engine"))
        self.game_safety_title.setText(self._t("game.safety_title"))
        self.game_safety_body.setText(self._t("game.safety_body"))
        self.source_label.setText(self._t("languages.source"))
        self.target_label.setText(self._t("languages.target"))
        self.language_hint.setText(self._t("languages.hint"))
        self.source_hint.setText(self._t("languages.source_hint"))
        self.target_hint.setText(self._t("languages.target_hint"))
        self.existing_languages_title.setText(self._t("languages.existing_title"))
        self.existing_languages_body.setText(self._t("languages.existing_body"))
        self.model_provider_label.setText(self._t("model.provider"))
        self.api_key_label.setText(self._t("model.api_key"))
        self.model_label.setText(self._t("model.model"))
        self.endpoint_label.setText(self._t("model.endpoint"))
        self.use_model_check.setText(self._t("model.use"))
        self.connect_model_button.setText(self._t("model.load"))
        self.verify_model_button.setText(self._t("model.verify"))
        self.generate_rpa_check.setText(self._t("review.rpa"))
        self.install_check.setText(self._t("review.install"))
        self.budget_title.setText("AI usage estimate" if self.locale == "en" else "AI 用量预估")
        if self._scope_preview_inventory is None:
            self.budget_label.setText(self._t("review.estimate_unavailable"))
        self.review_mode_label.setText(self._t("review.model_translation"))
        if self._scope_preview_inventory is None:
            self.review_remaining_label.setText(self._t("review.waiting_scope"))
        self.pending_title.setText(self._t("review.no_pending"))
        self.review_details_toggle.setText(
            self._t("review.hide_details" if self.review_details_label.isVisible() else "review.details")
        )
        self.pending_toggle.setText(
            self._t("review.hide_pending" if self.pending_details.isVisible() else "review.show_details")
        )
        self.progress_open_button.setText(self._t("progress.open"))
        self.log_toggle.setText(self._t("progress.hide_log" if self.log_edit.isVisible() else "progress.show_log"))
        if self._project_validation_state == "idle":
            self.project_status.setText(self._t("game.waiting"))
        elif self._project_validation_state == "pending":
            self.project_status.setText(self._t("game.inspecting"))
        elif self._project_validation_state == "valid":
            self.project_status.setText(self._t("game.detected", count=len(getattr(self, "existing_languages", []))))
        elif self._project_validation_state == "invalid":
            self.project_status.setText(self._t("game.not_recognized", error=self._project_validation_error))
        if self._scope_preview_status == "scanning":
            self.language_scope_label.setText(self._t("languages.scanning"))
        if not self.model_status.text() or self.model_status.text() in {"Not connected", "尚未连接"}:
            self.model_status.setText(self._t("model.not_connected"))
        if not self.progress_heading.text() or self.progress_heading.text() in {"Ready", "准备就绪"}:
            self.progress_heading.setText(self._t("progress.ready"))
        if self.progress_runtime.text() in {"Idle", "空闲"}:
            self.progress_runtime.setText(self._t("progress.idle"))

    def _toggle_pending_details(self) -> None:
        visible = not self.pending_details.isVisible()
        self.pending_details.setVisible(visible)
        self.pending_toggle.setText("Hide details" if visible else "Show details")

    def _toggle_log(self) -> None:
        visible = not self.log_edit.isVisible()
        self.log_edit.setVisible(visible)
        self.log_toggle.setText("Hide log" if visible else "Show log")

    def _project_changed(self, _value: str = "") -> None:
        self._project_revision += 1
        self._discovered_project = None
        self._project_validation_error = ""
        self._project_validation_state = "idle"
        self._inspection_revision = None
        self._inspection_value = ""
        self._scope_preview_signature = None
        self._scope_preview_status = "idle"
        self._inspection_timer.start(150)
        self._refresh_shell()

    def _inspect_project(self) -> None:
        value = self.project_edit.text().strip()
        if not value:
            self._project_validation_state = "idle"
            self._project_validation_error = ""
            self.project_status.setText(self._t("game.waiting"))
            self._refresh_shell()
            return
        revision = self._project_revision
        if (
            self._project_validation_state == "pending"
            and self._inspection_revision == revision
            and self._inspection_value == value
        ):
            return
        self._project_validation_state = "pending"
        self._inspection_revision = revision
        self._inspection_value = value
        self.project_status.setText(self._t("game.inspecting"))
        self._refresh_shell()

        def inspect():
            project = ProjectDiscovery().discover(value)
            languages = discover_existing_languages(value)
            sdk = RenpySdkLocator().resolve(project_root=project.project_root)
            return project, languages, sdk

        self._run_worker(
            inspect,
            lambda result, revision=revision, value=value: self._project_inspected(result, revision, value),
            lambda error, revision=revision, value=value: self._project_inspection_failed(error, revision, value),
        )

    def _project_inspected(self, result, revision: int | None = None, value: str | None = None) -> None:
        if revision is not None and (revision != self._project_revision or value != self.project_edit.text().strip()):
            return
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
        self._refresh_existing_languages()
        self.project_status.setText(self._t("game.detected", count=len(languages)))
        self._inspection_revision = None
        self._inspection_value = ""
        self._start_scope_preview()
        self._refresh_shell()

    def _refresh_existing_languages(self) -> None:
        languages = list(getattr(self, "existing_languages", []))
        while self.existing_language_buttons.count():
            item = self.existing_language_buttons.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.existing_languages_card.setVisible(bool(languages))
        if not languages:
            return
        self.existing_languages_title.setText(self._t("languages.existing_title"))
        self.existing_languages_body.setText(self._t("languages.existing_body"))
        for item in languages:
            button = QPushButton(
                f"{item.language} · {item.script_files + item.compiled_files} files",
                objectName="Secondary",
            )
            button.clicked.connect(lambda _checked=False, language=item.language: self.target_combo.setCurrentText(language))
            self.existing_language_buttons.addWidget(button)

    def _project_inspection_failed(
        self,
        error: BaseException,
        revision: int | None = None,
        value: str | None = None,
    ) -> None:
        if revision is not None and (revision != self._project_revision or value != self.project_edit.text().strip()):
            return
        self._project_validation_state = "invalid"
        self._project_validation_error = str(error)
        self.project_status.setText(self._t("game.not_recognized", error=error))
        self._inspection_revision = None
        self._inspection_value = ""
        self._refresh_shell()

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
        self.language_scope_label.setText(self._t("languages.scanning"))

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
            self._t(
                "languages.scope_value",
                total=inventory.total_units,
                reusable=inventory.reusable_units,
                model=inventory.model_units,
            )
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
        self.review_game_label.setText(Path(self.project_edit.text().strip()).name or self.project_edit.text().strip())
        self.review_languages_label.setText(
            f"{self.source_combo.currentText().strip() or 'auto'}  →  {self.target_combo.currentText().strip()}"
        )
        self.review_remaining_label.setText(
            (f"待翻译模型单元：{inventory.model_units}" if self.locale == "zh" else f"Remaining model units: {inventory.model_units}")
        )
        self.review_preserved_label.setText(
            (f"可复用已有单元：{inventory.reusable_units}" if self.locale == "zh" else f"Reusable existing units: {inventory.reusable_units}")
        )
        if budget is not None:
            self.budget_label.setText(
                f"Estimated usage: {budget.estimated_total_low:,}–{budget.estimated_total_high:,} tokens"
            )
            self.budget_note.setText(
                "预估范围会在建立索引后更新。" if self.locale == "zh" else "The estimate is refined after indexing."
            )
        else:
            self.budget_label.setText(self._t("review.estimate_unavailable"))
            self.budget_note.clear()
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
        selected = QFileDialog.getExistingDirectory(self, self._t("shell.select_project"))
        if selected:
            self.project_edit.setText(selected)

    def _browse_workspace(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, self._t("shell.select_workspace"))
        if selected:
            self.workspace_edit.setText(selected)

    def _browse_sdk(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, self._t("shell.select_sdk"))
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
