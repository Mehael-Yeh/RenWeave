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

from . import __version__
from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, Signal
from PySide6.QtGui import QCloseEvent, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QFileDialog,
    QProgressBar,
    QPushButton,
    QDialog,
    QRadioButton,
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
from .existing_translations import LANGUAGE_DISPLAY_NAMES, discover_existing_languages
from .desktop_core import (
    TranslationRequest,
    _user_home_fallback,
    default_desktop_settings_path,
    execute_blank_translation,
    execute_translation,
)
from .io import atomic_write_json, read_json
from .pipeline import PipelineStage, RenWeavePipeline
from .provider import ModelProfile, OpenAICompatibleCatalog
from .provider_presets import PROVIDER_PRESETS, PROVIDER_PRESETS_BY_ID
from .runtime import CancellationToken
from .update_check import check_for_updates


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


class SettingsDialog(QDialog):
    """Qt replacement for the legacy privacy and maintenance settings dialog."""

    def __init__(self, app: "QtRenWeaveWindow") -> None:
        super().__init__(app)
        self.app = app
        self.setWindowTitle(app._t("settings.title"))
        self.setModal(True)
        self.resize(760, 430)
        self.setMinimumSize(680, 390)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)

        title = QLabel(app._t("settings.title"), objectName="PageTitle")
        root.addWidget(title)
        body = QLabel(app._t("settings.body"), objectName="PageBody")
        body.setWordWrap(True)
        root.addWidget(body)

        cards = QHBoxLayout()
        cards.setSpacing(12)
        root.addLayout(cards, 1)

        credentials = QFrame(objectName="Card")
        credentials_layout = QVBoxLayout(credentials)
        credentials_layout.setContentsMargins(16, 16, 16, 16)
        credentials_layout.setSpacing(8)
        credentials_title = QLabel(app._t("settings.credentials"), objectName="SectionTitle")
        credentials_layout.addWidget(credentials_title)
        credentials_hint = QLabel(app._t("settings.credentials_hint"), objectName="Hint")
        credentials_hint.setWordWrap(True)
        credentials_layout.addWidget(credentials_hint)
        self.secure_radio = QRadioButton(app._t("settings.secure"))
        self.memory_radio = QRadioButton(app._t("settings.memory"))
        self.secure_radio.setChecked(app._key_storage == "secure")
        self.memory_radio.setChecked(app._key_storage == "memory")
        self.secure_radio.toggled.connect(self._storage_changed)
        credentials_layout.addWidget(self.secure_radio)
        credentials_layout.addWidget(self.memory_radio)
        self.forget_button = QPushButton(app._t("settings.forget_key"), objectName="Secondary")
        self.forget_button.clicked.connect(app._forget_api_key)
        credentials_layout.addWidget(self.forget_button, 0, Qt.AlignmentFlag.AlignLeft)
        credentials_layout.addStretch()
        cards.addWidget(credentials, 1)

        updates = QFrame(objectName="Card")
        updates_layout = QVBoxLayout(updates)
        updates_layout.setContentsMargins(16, 16, 16, 16)
        updates_layout.setSpacing(8)
        updates_title = QLabel(app._t("settings.updates"), objectName="SectionTitle")
        updates_layout.addWidget(updates_title)
        updates_hint = QLabel(app._t("settings.updates_hint"), objectName="Hint")
        updates_hint.setWordWrap(True)
        updates_layout.addWidget(updates_hint)
        self.update_toggle = QCheckBox(app._t("settings.update_toggle"))
        self.update_toggle.setChecked(app._update_checks_enabled)
        self.update_toggle.toggled.connect(app._update_checks_toggled)
        updates_layout.addWidget(self.update_toggle)
        self.check_button = QPushButton(app._t("settings.check_now"), objectName="Secondary")
        self.check_button.clicked.connect(lambda: app._check_updates(manual=True, dialog=self))
        updates_layout.addWidget(self.check_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.update_status = QLabel(app._t("settings.current_version", version=__version__), objectName="Hint")
        self.update_status.setWordWrap(True)
        updates_layout.addWidget(self.update_status)
        updates_layout.addStretch()
        cards.addWidget(updates, 1)

        footer = QHBoxLayout()
        footer.addStretch()
        close_button = QPushButton(app._t("settings.close"), objectName="Primary")
        close_button.clicked.connect(self.accept)
        footer.addWidget(close_button)
        root.addLayout(footer)

    def _storage_changed(self, checked: bool) -> None:
        if checked:
            self.app._set_key_storage("secure")
        elif self.memory_radio.isChecked():
            self.app._set_key_storage("memory")

    def set_update_status(self, text: str) -> None:
        self.update_status.setText(text)

    def set_update_busy(self, busy: bool) -> None:
        self.check_button.setEnabled(not busy)
        if busy:
            self.set_update_status(self.app._t("settings.update_checking"))


UI_COPY = {
    "en": {
        "nav.game": "Game",
        "nav.languages": "Languages",
        "nav.model": "Model",
        "nav.review": "Review",
        "nav.progress": "Translation",
        "page.game.title": "Game",
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
        "game.copy_path": "Copy path",
        "game.open_path": "Open",
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
        "model.show_key": "Show",
        "model.hide_key": "Hide",
        "model.model": "Model",
        "model.endpoint": "Endpoint",
        "model.reasoning": "Thinking level",
        "model.reasoning_hint": "Automatic uses the provider default.",
        "model.reasoning_unavailable": "This provider uses the model default.",
        "reasoning.auto": "Automatic (provider default)",
        "reasoning.low": "Low",
        "reasoning.high": "High",
        "reasoning.maximum": "Maximum",
        "model.use": "Use model translation",
        "model.use_hint": "When disabled, only blank translation files are generated for manual editing.",
        "model.load": "Load models",
        "model.browse": "Browse {count} models",
        "model_picker.title": "Select a model",
        "model_picker.search": "Filter by model ID",
        "model_picker.count": "{count} matching models",
        "model_picker.select": "Use selected model",
        "model_picker.empty": "No models match this filter.",
        "model.verify": "Verify model",
        "model.not_connected": "Not connected",
        "review.model_translation": "Model translation",
        "review.blank_translation": "Blank translation extraction",
        "review.fact_model": "Model",
        "review.fact_game": "Game",
        "review.fact_languages": "Languages",
        "review.fact_options": "Options",
        "review.waiting_scope": "Waiting for scope preview",
        "review.estimate_unavailable": "Token usage estimate unavailable",
        "review.resume_found": "Recoverable translation found",
        "review.resume_body": "{completed} completed scene checkpoint(s) are available. Continuing will reuse them.",
        "review.rpa": "Generate RPA package",
        "review.install": "Install after validation",
        "review.details": "Show technical details",
        "review.hide_details": "Hide technical details",
        "review.no_pending": "No pending units",
        "review.show_details": "Show details",
        "review.hide_pending": "Hide details",
        "review.detail_project": "Project: {value}",
        "review.detail_workspace": "Workspace: {value}",
        "review.detail_sdk": "SDK: {value}",
        "review.not_selected": "not selected",
        "progress.ready": "Ready",
        "progress.idle": "Idle",
        "progress.open": "Open output folder",
        "progress.open_rpy": "Open RPY output",
        "progress.open_rpa": "Open RPA package",
        "progress.open_install": "Open installed files",
        "progress.show_error": "Show error details",
        "progress.current": "Current operation",
        "progress.files": "File progress",
        "progress.eta": "Estimated remaining",
        "progress.usage": "Model usage",
        "progress.phase.prepare": "Prepare",
        "progress.phase.analyze": "Analyze",
        "progress.phase.translate": "Translate",
        "progress.phase.validate": "Validate",
        "progress.phase.build": "Build",
        "progress.show_log": "Show log",
        "progress.hide_log": "Hide log",
        "progress.complete": "Translation package is ready",
        "progress.completed": "Completed",
        "shell.back": "Back",
        "shell.continue": "Continue",
        "shell.start": "Start",
        "shell.start_translation": "Start translation",
        "shell.continue_translation": "Continue translation",
        "shell.pause": "Pause",
        "shell.extract_blank": "Extract blank translation",
        "shell.breadcrumb": "Step {current} / {total}",
        "footer.default": "Changes are saved locally",
        "footer.game": "Next: choose languages. Game files stay unchanged.",
        "footer.languages": "Next: review scope and the Token budget.",
        "footer.model": "Next: review the scope and Token budget. Translation starts explicitly on step 05.",
        "footer.review": "Starts billable model work; checkpoints go to the workspace.",
        "footer.blank_review": "Creates blank translation files without model calls.",
        "footer.progress": "Pause saves checkpoints; closing keeps completed outputs.",
        "shell.select_project": "Select Ren'Py project",
        "shell.select_workspace": "Select workspace",
        "shell.select_sdk": "Select Ren'Py SDK",
        "dialog.project": "Project",
        "dialog.inspecting": "Project inspection is still running.",
        "dialog.invalid_project": "Select a valid Ren'Py project first.",
        "dialog.cannot_start": "Cannot start",
        "dialog.missing_fields": "Project, workspace and target language are required.",
        "dialog.target_required": "Choose a target language before continuing.",
        "dialog.model_required": "Configure and select a model before continuing.",
        "dialog.scope_required": "Wait for the translation scope calculation to finish before continuing.",
        "dialog.validation": "Please complete the current page before continuing.",
        "dialog.open_output": "Open output",
        "model.loading": "Loading models…",
        "model.loaded": "Loaded {count} model(s) · {latency} ms",
        "model.verifying": "Verifying model…",
        "model.verified": "Verified {model} · {latency} ms",
        "model.load_failed": "Model loading failed: {error}",
        "model.verify_failed": "Verification failed: {error}",
        "model.error_details": "Show connection details",
        "error.api_key": "The API key was rejected. Check the key and provider endpoint.",
        "error.timeout": "The provider request timed out. Retry or check the network.",
        "error.connection": "The provider could not be reached. Check the endpoint and network.",
        "error.generic": "The provider operation failed. Open details for the full error.",
        "scope.failed": "Scope preview failed: {error}",
        "translation.failed": "Translation failed: {error}",
        "translation.ready": "Translation package is ready",
        "translation.completed": "Completed",
        "progress.pausing": "Pausing safely…",
        "progress.paused": "Translation paused safely",
        "progress.paused_body": "Completed checkpoints are preserved. Resume with the same project and workspace.",
        "error.title": "Operation failed",
        "error.copy": "Copy details",
        "error.close": "Close",
        "progress.resume": "Resume translation",
        "progress.retry": "Retry translation",
        "progress.output": "Open output",
        "progress.rpy_output": "RPY output: {path}",
        "progress.rpa_output": "RPA output: {path}",
        "dialog.translation_running": "Translation is still running. Pause it before leaving this page.",
        "top.settings": "Settings",
        "settings.title": "Settings",
        "settings.body": "Control API key storage and optional version checks for this user account.",
        "settings.credentials": "API key storage",
        "settings.credentials_hint": "Secure storage uses the operating system credential service. API keys never enter RenWeave settings, project files, logs, or translation packages.",
        "settings.secure": "Encrypted system storage (recommended)",
        "settings.memory": "Memory only — cleared when RenWeave closes",
        "settings.forget_key": "Forget current key",
        "settings.updates": "Version updates",
        "settings.updates_hint": "Check GitHub Releases after startup. Checks are disabled by default and never install anything automatically.",
        "settings.update_toggle": "Check for updates after startup",
        "settings.check_now": "Check now",
        "settings.current_version": "Installed version: {version}",
        "settings.update_checking": "Checking for updates…",
        "settings.update_available": "Version {latest} is available. You currently have {current}.",
        "settings.up_to_date": "RenWeave {current} is up to date. Latest release: {latest}.",
        "settings.update_failed": "Update check failed: {error}",
        "settings.key_forgotten": "The current API key was forgotten.",
        "settings.close": "Close",
        "tip.project": "Ren'Py project folder or executable to inspect.",
        "tip.workspace": "Separate workspace for caches, checkpoints, logs and outputs.",
        "tip.sdk": "Optional Ren'Py SDK used for engine validation.",
        "tip.provider": "Provider presets isolate API keys and model selections.",
        "tip.endpoint": "Editable API base URL. Use a preset or enter a documented custom endpoint.",
        "tip.api_key": "Stored securely or only in memory according to Settings.",
        "tip.model": "Select a model returned by the provider, or enter a compatible model ID.",
        "tip.reasoning": "Provider-specific thinking or reasoning control.",
        "tip.load_models": "Load available models from the provider /models endpoint.",
        "tip.browse_models": "Open a searchable list of loaded models.",
        "tip.use_model": "When disabled, create blank translation files without model calls.",
        "tip.rpa": "Generate an archive package after the RPY output is validated.",
        "tip.install": "Install validated output into the selected game directory.",
        "tip.log": "Show the workspace log, including events from earlier runs.",
    },
    "zh": {
        "nav.game": "游戏",
        "nav.languages": "语言",
        "nav.model": "模型",
        "nav.review": "确认",
        "nav.progress": "翻译",
        "page.game.title": "游戏",
        "page.game.body": "选择翻译流程使用的 Ren'Py 项目和工作区。",
        "page.languages.title": "语言",
        "page.languages.body": "选择生成翻译文件使用的源语言和目标语言。",
        "page.model.title": "模型",
        "page.model.body": "配置翻译使用的 API 提供商和模型。",
        "page.review.title": "确认",
        "page.review.body": "开始翻译前确认翻译范围和输出选项。",
        "page.progress.title": "翻译",
        "page.progress.body": "在当前页面查看翻译任务进度。",
        "game.project": "游戏项目",
        "game.workspace": "工作区",
        "game.sdk": "Ren'Py SDK（可选）",
        "game.browse_project": "选择项目",
        "game.browse_workspace": "选择工作区",
        "game.browse_sdk": "选择 SDK",
        "game.copy_path": "复制路径",
        "game.open_path": "打开",
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
        "model.show_key": "显示",
        "model.hide_key": "隐藏",
        "model.model": "模型",
        "model.endpoint": "接口地址",
        "model.reasoning": "思考设置",
        "model.reasoning_hint": "自动使用提供商或模型的默认思考设置。",
        "model.reasoning_unavailable": "此提供商使用模型默认思考设置。",
        "reasoning.auto": "自动（提供商默认）",
        "reasoning.low": "低",
        "reasoning.high": "高",
        "reasoning.maximum": "最高",
        "model.use": "使用模型翻译",
        "model.use_hint": "不勾选时只生成空白翻译文件，之后可以手动填写译文。",
        "model.load": "获取模型",
        "model.browse": "浏览 {count} 个模型",
        "model_picker.title": "选择模型",
        "model_picker.search": "按模型 ID 筛选",
        "model_picker.count": "匹配 {count} 个模型",
        "model_picker.select": "使用所选模型",
        "model_picker.empty": "没有匹配的模型。",
        "model.verify": "验证模型",
        "model.not_connected": "尚未连接",
        "review.model_translation": "模型翻译",
        "review.blank_translation": "生成空白翻译文件",
        "review.fact_model": "模型",
        "review.fact_game": "游戏",
        "review.fact_languages": "语言",
        "review.fact_options": "选项",
        "review.waiting_scope": "等待翻译范围预览",
        "review.estimate_unavailable": "暂时无法预估 Token 用量",
        "review.resume_found": "发现可恢复的翻译任务",
        "review.resume_body": "已有 {completed} 个场景检查点。继续时会复用这些检查点。",
        "review.rpa": "生成 RPA 语言包",
        "review.install": "校验后安装",
        "review.details": "显示技术详情",
        "review.hide_details": "隐藏技术详情",
        "review.no_pending": "没有待处理单元",
        "review.show_details": "显示详情",
        "review.hide_pending": "隐藏详情",
        "review.detail_project": "项目：{value}",
        "review.detail_workspace": "工作区：{value}",
        "review.detail_sdk": "SDK：{value}",
        "review.not_selected": "未选择",
        "progress.ready": "准备就绪",
        "progress.idle": "空闲",
        "progress.open": "打开输出目录",
        "progress.open_rpy": "打开 RPY 输出",
        "progress.open_rpa": "打开 RPA 语言包",
        "progress.open_install": "打开已安装文件",
        "progress.show_error": "显示错误详情",
        "progress.current": "当前操作",
        "progress.files": "文件进度",
        "progress.eta": "预计剩余",
        "progress.usage": "模型用量",
        "progress.phase.prepare": "准备",
        "progress.phase.analyze": "分析",
        "progress.phase.translate": "翻译",
        "progress.phase.validate": "校验",
        "progress.phase.build": "构建",
        "progress.show_log": "显示日志",
        "progress.hide_log": "隐藏日志",
        "progress.complete": "翻译包已准备完成",
        "progress.completed": "已完成",
        "shell.back": "返回",
        "shell.continue": "继续",
        "shell.start": "开始",
        "shell.start_translation": "开始翻译",
        "shell.continue_translation": "继续翻译",
        "shell.pause": "暂停",
        "shell.extract_blank": "提取空白翻译",
        "shell.breadcrumb": "第 {current} / {total} 步",
        "footer.default": "修改会保存到本地",
        "footer.game": "下一步：选择语言。游戏文件保持不变。",
        "footer.languages": "下一步：确认翻译范围和 Token 预算。",
        "footer.model": "下一步：确认范围和 Token 预算；翻译必须在第 05 页明确开始。",
        "footer.review": "将开始可能计费的模型调用；检查点写入工作区。",
        "footer.blank_review": "生成空白翻译文件，不调用模型。",
        "footer.progress": "暂停会保存检查点；关闭后仍保留已完成输出。",
        "shell.select_project": "选择 Ren'Py 项目",
        "shell.select_workspace": "选择工作区",
        "shell.select_sdk": "选择 Ren'Py SDK",
        "dialog.project": "项目",
        "dialog.inspecting": "项目检查仍在进行中。",
        "dialog.invalid_project": "请先选择有效的 Ren'Py 项目。",
        "dialog.cannot_start": "无法开始",
        "dialog.missing_fields": "项目、工作区和目标语言均为必填项。",
        "dialog.target_required": "请选择目标语言后再继续。",
        "dialog.model_required": "请先配置并选择模型后再继续。",
        "dialog.scope_required": "请等待翻译范围计算完成后再继续。",
        "dialog.validation": "请先完成当前页面的配置后再继续。",
        "dialog.open_output": "打开输出目录",
        "model.loading": "正在获取模型……",
        "model.loaded": "已加载 {count} 个模型 · {latency} ms",
        "model.verifying": "正在验证模型……",
        "model.verified": "模型已验证：{model} · {latency} ms",
        "model.load_failed": "获取模型失败：{error}",
        "model.verify_failed": "模型验证失败：{error}",
        "model.error_details": "显示连接详情",
        "error.api_key": "API 密钥被拒绝，请检查密钥和供应商接口地址。",
        "error.timeout": "供应商请求超时，可以重试或检查网络连接。",
        "error.connection": "无法连接供应商，请检查接口地址和网络。",
        "error.generic": "供应商操作失败，请打开详情查看完整错误。",
        "scope.failed": "翻译范围预览失败：{error}",
        "translation.failed": "翻译失败：{error}",
        "translation.ready": "翻译包已准备完成",
        "translation.completed": "已完成",
        "progress.pausing": "正在安全暂停……",
        "progress.paused": "翻译已安全暂停",
        "progress.paused_body": "已完成的检查点已经保留，可以使用相同项目和工作区继续。",
        "error.title": "操作失败",
        "error.copy": "复制详情",
        "error.close": "关闭",
        "progress.resume": "继续翻译",
        "progress.retry": "重试翻译",
        "progress.output": "打开输出",
        "progress.rpy_output": "RPY 输出：{path}",
        "progress.rpa_output": "RPA 输出：{path}",
        "dialog.translation_running": "翻译仍在运行，请先暂停后再离开当前页面。",
        "top.settings": "设置",
        "settings.title": "设置",
        "settings.body": "管理当前用户的 API 密钥存储方式和可选的版本检查。",
        "settings.credentials": "API 密钥存储",
        "settings.credentials_hint": "安全存储使用操作系统凭据服务。密钥不会写入织译设置、项目文件、日志或翻译包。",
        "settings.secure": "系统加密存储（推荐）",
        "settings.memory": "仅保留在内存——关闭织译后忘记密钥",
        "settings.forget_key": "删除当前密钥",
        "settings.updates": "版本更新",
        "settings.updates_hint": "启动后检查 GitHub Releases；默认关闭，且绝不会自动安装更新。",
        "settings.update_toggle": "启动后检查新版本",
        "settings.check_now": "立即检查",
        "settings.current_version": "当前版本：{version}",
        "settings.update_checking": "正在检查更新……",
        "settings.update_available": "已有新版本 {latest}；当前版本为 {current}。",
        "settings.up_to_date": "织译 {current} 已是最新版本；最新发布版为 {latest}。",
        "settings.update_failed": "检查更新失败：{error}",
        "settings.key_forgotten": "当前 API 密钥已删除。",
        "settings.close": "关闭",
        "tip.project": "用于检查的 Ren'Py 项目目录或可执行文件。",
        "tip.workspace": "独立保存缓存、检查点、日志和输出文件的工作区。",
        "tip.sdk": "可选的 Ren'Py SDK，用于引擎校验。",
        "tip.provider": "供应商预设会隔离 API 密钥和模型选择。",
        "tip.endpoint": "可编辑的 API 基础地址；可使用预设或输入供应商文档指定的地址。",
        "tip.api_key": "根据设置选择系统加密存储或仅保存在内存中。",
        "tip.model": "选择供应商返回的模型，也可以输入兼容的模型 ID。",
        "tip.reasoning": "供应商对应的思考或推理控制。",
        "tip.load_models": "从供应商的 /models 接口获取可用模型。",
        "tip.browse_models": "打开已加载模型的可搜索列表。",
        "tip.use_model": "关闭后只生成空白翻译文件，不调用模型。",
        "tip.rpa": "在 RPY 输出校验后生成归档语言包。",
        "tip.install": "将校验后的输出安装到所选游戏目录。",
        "tip.log": "显示工作区日志，也包括之前运行留下的事件。",
    },
}

class ModelPickerDialog(QDialog):
    """Searchable model chooser restored from the legacy desktop UI."""

    def __init__(self, app: "QtRenWeaveWindow", models: tuple[str, ...]) -> None:
        super().__init__(app)
        self.app = app
        self.models = tuple(models)
        self.filtered_models = self.models
        self.setWindowTitle(app._t("model_picker.title"))
        self.setModal(True)
        self.resize(640, 520)
        self.setMinimumSize(520, 420)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(10)
        title = QLabel(app._t("model_picker.title"), objectName="PageTitle")
        root.addWidget(title)
        search_label = QLabel(app._t("model_picker.search"), objectName="Hint")
        root.addWidget(search_label)
        self.search_edit = QLineEdit()
        self.search_edit.textChanged.connect(self._filter)
        root.addWidget(self.search_edit)
        self.count_label = QLabel(objectName="Hint")
        root.addWidget(self.count_label)
        self.list_widget = QListWidget()
        self.list_widget.itemDoubleClicked.connect(lambda _item: self._select())
        root.addWidget(self.list_widget, 1)
        actions = QHBoxLayout()
        actions.addStretch()
        cancel = QPushButton(app._t("settings.close"), objectName="Secondary")
        cancel.clicked.connect(self.reject)
        self.select_button = QPushButton(app._t("model_picker.select"), objectName="Primary")
        self.select_button.clicked.connect(self._select)
        actions.addWidget(cancel)
        actions.addWidget(self.select_button)
        root.addLayout(actions)
        self._populate()

    def _filter(self, query: str) -> None:
        needle = query.strip().casefold()
        self.filtered_models = tuple(model for model in self.models if needle in model.casefold())
        self._populate()

    def _populate(self) -> None:
        self.list_widget.clear()
        self.list_widget.addItems(self.filtered_models)
        self.count_label.setText(self.app._t("model_picker.count", count=len(self.filtered_models)))
        has_models = bool(self.filtered_models)
        self.select_button.setEnabled(has_models)
        if has_models:
            current = self.app.model_edit.currentText().strip()
            index = self.filtered_models.index(current) if current in self.filtered_models else 0
            self.list_widget.setCurrentRow(index)

    def _select(self) -> None:
        item = self.list_widget.currentItem()
        if item is None:
            return
        self.selected_model = item.text()
        self.accept()


class ErrorDetailsDialog(QDialog):
    """Non-destructive failure details dialog with a copy action."""

    def __init__(self, app: "QtRenWeaveWindow", error: object) -> None:
        super().__init__(app)
        self.setWindowTitle(app._t("error.title"))
        self.setModal(True)
        self.resize(760, 460)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(10)
        root.addWidget(QLabel(app._t("error.title"), objectName="PageTitle"))
        details = QTextEdit()
        details.setReadOnly(True)
        details.setPlainText(str(error))
        root.addWidget(details, 1)
        actions = QHBoxLayout()
        actions.addStretch()
        copy_button = QPushButton(app._t("error.copy"), objectName="Secondary")
        copy_button.clicked.connect(lambda: QApplication.clipboard().setText(details.toPlainText()))
        close_button = QPushButton(app._t("error.close"), objectName="Primary")
        close_button.clicked.connect(self.accept)
        actions.addWidget(copy_button)
        actions.addWidget(close_button)
        root.addLayout(actions)


def _application_icon() -> QIcon:
    """Recreate the original Tk geometric RenWeave mark."""
    pixmap = QPixmap(32, 32)
    pixmap.fill(QColor("#0B1020"))
    painter = QPainter(pixmap)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.fillRect(5, 6, 5, 18, QColor("#20B8CD"))
    painter.fillRect(11, 10, 5, 18, QColor("#5B5CE2"))
    painter.fillRect(17, 6, 5, 18, QColor("#8B8CF6"))
    painter.fillRect(23, 10, 5, 18, QColor("#20B8CD"))
    painter.end()
    return QIcon(pixmap)


def _provider_icon(accent: str) -> QIcon:
    """Recreate the original compact geometric provider mark."""
    pixmap = QPixmap(20, 20)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setPen(Qt.PenStyle.NoPen)
    for y in range(1, 19):
        inset = 4 if y in {1, 18} else 2 if y in {2, 17} else 1
        painter.fillRect(inset, y, 20 - inset * 2, 1, QColor(accent))
    painter.fillRect(5, 6, 2, 8, QColor("#FFFFFF"))
    painter.fillRect(9, 4, 2, 12, QColor("#FFFFFF"))
    painter.fillRect(13, 7, 2, 6, QColor("#FFFFFF"))
    painter.end()
    return QIcon(pixmap)


class QtRenWeaveWindow(QMainWindow):
    """Persistent Qt shell used while the business layer is migrated."""

    STEPS = ("game", "languages", "model", "review", "progress")

    def __init__(self, *, initial_project: str = "", initial_workspace: str = "") -> None:
        super().__init__()
        self.setWindowTitle("RenWeave")
        self.setWindowIcon(_application_icon())
        self.resize(1240, 840)
        self.setMinimumSize(900, 640)
        self.locale = "en"
        self.step = 0
        self.initial_project = initial_project
        self.initial_workspace = initial_workspace
        self._workspace_auto_generated = not bool(initial_workspace.strip())
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
        self._selected_existing_language: str | None = None
        self._existing_selection_source = ""
        self._changing_existing_selection = False
        self.existing_language_controls: dict[str, QPushButton] = {}
        self._project_validation_state = "idle"
        self._project_validation_error = ""
        self._discovered_project = None
        self._worker = None
        self._cancel_token = None
        self._logs: list[str] = []
        self._progress_payload: dict[str, object] = {}
        self._last_stage = ""
        self._last_error_details = ""
        self._last_model_error_key = ""
        self._resume_candidate: dict[str, object] | None = None
        self._translation_started = False
        self._blank_translation_mode = False
        self._last_logged_operation = ""
        self._settings_path = default_desktop_settings_path()
        self._settings = self._load_settings()
        self._credential_store = SecureCredentialStore()
        self._api_key_cache: dict[tuple[str, str], str] = {}
        self._model_by_identity: dict[tuple[str, str], str] = {}
        self._model_catalog_models: tuple[str, ...] = ()
        self._key_storage = str(self._settings.get("key_storage", "secure"))
        if self._key_storage not in {"secure", "memory"}:
            self._key_storage = "secure"
        self._update_checks_enabled = bool(self._settings.get("update_checks_enabled", False))
        self._update_worker = None
        self._settings_dialog: SettingsDialog | None = None
        self._api_key_visible = False

        self._configure_palette()
        self._build_shell()
        self._build_pages()
        self._restore_state()
        if self.project_edit.text().strip() and self._workspace_auto_generated:
            self._suggest_workspace(self.project_edit.text().strip())
        self._retranslate_ui()
        self._refresh_shell()
        if self.project_edit.text().strip():
            self._inspection_timer.start(0)
        if self._update_checks_enabled:
            QTimer.singleShot(800, self._check_updates)

    def _configure_palette(self) -> None:
        arrow_path = (Path(__file__).resolve().parent / "assets" / "chevron-down.svg").as_posix()
        self.setStyleSheet(
            """
            QMainWindow, QWidget#Root, QScrollArea, QScrollArea > QWidget > QWidget { background: #f3f6fb; color: #101828; font-family: "Microsoft YaHei UI"; }
            QFrame#Sidebar { background: #0b1020; }
            QFrame#Card, QFrame#Footer { background: #ffffff; border: 1px solid #e0e6ef; border-radius: 12px; }
            QFrame#Card QLabel, QFrame#TintCard QLabel, QFrame#SuccessCard QLabel { background: transparent; }
            QFrame#TintCard { background: #eef0ff; border: 1px solid #dfe2ff; border-radius: 10px; }
            QFrame#SuccessCard { background: #ecfdf3; border: 1px solid #c9ecd9; border-radius: 10px; }
            QLabel#Brand { color: #f9fafb; font-size: 20px; font-weight: 700; }
            QLabel#PageTitle { color: #101828; font-size: 26px; font-weight: 700; }
            QLabel#PageBody { color: #667085; font-size: 13px; }
            QLabel#SectionTitle { color: #101828; font-size: 16px; font-weight: 700; }
            QLabel#Hint { color: #667085; }
            QLabel#Hint[phase_state="active"] { color: #5b5ce2; font-weight: 700; }
            QLabel#Hint[phase_state="done"] { color: #087443; font-weight: 700; }
            QLabel#Status { color: #303176; font-weight: 600; }
            QPushButton { min-height: 36px; max-height: 36px; padding: 0 14px; border-radius: 7px; color: #344054; }
            QPushButton#Primary { background: #5b5ce2; color: white; font-weight: 700; }
            QPushButton#Primary:hover { background: #494ac8; }
            QPushButton#Primary:disabled { background: #d0d5dd; color: #98a2b3; border: 1px solid #cfd4dc; }
            QPushButton#Secondary:disabled { background: #f2f4f7; color: #98a2b3; border: 1px solid #e4e7ec; }
            QPushButton#Secondary { background: #ffffff; color: #344054; border: 1px solid #e0e6ef; }
            QPushButton#Secondary:hover { background: #e7e9ff; }
            QPushButton#Secondary:checked { background: #5b5ce2; color: #ffffff; border: 1px solid #494ac8; font-weight: 700; }
            QPushButton#Nav { color: #98a2b3; text-align: left; border: 0; padding: 8px 14px; }
            QPushButton#Nav:hover { background: #1b2440; }
            QPushButton#Nav[current="true"] { background: #5b5ce2; color: #ffffff; font-weight: 700; }
            QLineEdit, QComboBox { background: #ffffff; color: #344054; selection-background-color: #5b5ce2; selection-color: #ffffff; border: 1px solid #e0e6ef; border-radius: 6px; padding: 0 10px; min-height: 34px; max-height: 34px; }
            QTextEdit { background: #ffffff; color: #344054; selection-background-color: #5b5ce2; selection-color: #ffffff; border: 1px solid #e0e6ef; border-radius: 6px; padding: 7px; }
            QLineEdit:focus, QComboBox:focus, QTextEdit:focus { border: 1px solid #5b5ce2; }
            QLineEdit::placeholder { color: #98a2b3; }
            QComboBox QAbstractItemView { background: #ffffff; color: #344054; border: 1px solid #d0d5dd; selection-background-color: #e7e9ff; selection-color: #101828; padding: 4px; outline: 0; }
            QComboBox QAbstractItemView::item { min-height: 30px; padding: 5px 8px; border-radius: 4px; }
            QComboBox QAbstractItemView::item:hover { background: #f2f4f7; color: #101828; }
            QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: top right; width: 32px; border-left: 1px solid #e0e6ef; background: #f7f8fc; border-top-right-radius: 6px; border-bottom-right-radius: 6px; }
            QComboBox::drop-down:hover { background: #e7e9ff; }
            QComboBox::down-arrow { image: url("__ARROW__"); width: 12px; height: 8px; }
            QComboBox::down-arrow:hover { image: url("__ARROW__"); }
            QComboBox:disabled { background: #f2f4f7; color: #98a2b3; }
            QComboBox:disabled::drop-down { background: #eaecf0; }
            QCheckBox { min-height: 28px; color: #344054; }
            QScrollBar:vertical { background: #e9edf5; width: 10px; margin: 2px 0 2px 0; border-radius: 5px; }
            QScrollBar::handle:vertical { background: #b8c1d1; min-height: 36px; border-radius: 5px; }
            QScrollBar::handle:vertical:hover { background: #8f9bb0; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
            QScrollBar:horizontal { height: 0; }
            QProgressBar { border: 0; background: #e9edf5; border-radius: 5px; height: 10px; }
            QProgressBar::chunk { background: #5b5ce2; border-radius: 5px; }
            """.replace("__ARROW__", arrow_path)
        )

    def _build_shell(self) -> None:
        root = QWidget(objectName="Root")
        self.setCentralWidget(root)
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.sidebar = QFrame(objectName="Sidebar")
        self.sidebar.setFixedWidth(232)
        sidebar_layout = QVBoxLayout(self.sidebar)
        sidebar_layout.setContentsMargins(18, 24, 18, 18)
        brand_row = QHBoxLayout()
        brand_icon = QLabel()
        brand_icon.setPixmap(_application_icon().pixmap(28, 28))
        brand_row.addWidget(brand_icon)
        brand = QLabel("RenWeave", objectName="Brand")
        brand_row.addWidget(brand)
        brand_row.addStretch()
        sidebar_layout.addLayout(brand_row)
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
        main_layout.setContentsMargins(36, 20, 36, 18)
        main_layout.setSpacing(14)
        top = QHBoxLayout()
        self.breadcrumb = QLabel()
        self.breadcrumb.setObjectName("Hint")
        top.addWidget(self.breadcrumb)
        top.addStretch()
        self.locale_button = QPushButton("中文", objectName="Secondary")
        self.locale_button.clicked.connect(self._toggle_locale)
        self.settings_button = QPushButton("Settings", objectName="Secondary")
        self.settings_button.clicked.connect(self._open_settings)
        top.addWidget(self.settings_button)
        top.addWidget(self.locale_button)
        main_layout.addLayout(top)

        self.stack = QStackedWidget()
        main_layout.addWidget(self.stack, 1)

        self.footer = QFrame(objectName="Footer")
        footer = self.footer
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(14, 10, 14, 10)
        footer.setFixedHeight(62)
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
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # Reserve the scrollbar gutter even while the handle is hidden. This
        # prevents page content from shifting when a page becomes scrollable.
        scroll.setViewportMargins(0, 0, 10, 0)
        scroll.verticalScrollBar().setFixedWidth(10)
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
        combos = list(self.findChildren(QComboBox)) + [self.provider_combo]
        for combo in combos:
            combo.setFixedHeight(34)

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
        self.project_edit = QLineEdit(self._normalise_path_text(self.initial_project))
        self.workspace_edit = QLineEdit(self._normalise_path_text(self.initial_workspace))
        self.renpy_sdk_edit = QLineEdit()
        self.project_browse_button = QPushButton(objectName="Secondary")
        self.workspace_browse_button = QPushButton(objectName="Secondary")
        self.sdk_browse_button = QPushButton(objectName="Secondary")
        self.project_browse_button.clicked.connect(self._browse_project)
        self.workspace_browse_button.clicked.connect(self._browse_workspace)
        self.sdk_browse_button.clicked.connect(self._browse_sdk)
        self.project_edit.textChanged.connect(self._project_changed)
        self.workspace_edit.textEdited.connect(self._workspace_edited)
        self.project_edit.editingFinished.connect(lambda: self._normalise_path_edit(self.project_edit))
        self.workspace_edit.editingFinished.connect(lambda: self._normalise_path_edit(self.workspace_edit))
        self.renpy_sdk_edit.editingFinished.connect(lambda: self._normalise_path_edit(self.renpy_sdk_edit))
        rows = (
            (self.game_project_label, self.project_edit, self.project_browse_button),
            (self.game_workspace_label, self.workspace_edit, self.workspace_browse_button),
            (self.game_sdk_label, self.renpy_sdk_edit, self.sdk_browse_button),
        )
        for row, (label, edit, browse) in enumerate(rows):
            grid.addWidget(label, row, 0)
            grid.addWidget(edit, row, 1)
            grid.addWidget(browse, row, 2)
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
        for language in LANGUAGE_DISPLAY_NAMES:
            self.target_combo.addItem(language, language)
        self.source_combo.currentTextChanged.connect(self._language_changed)
        self.target_combo.currentTextChanged.connect(self._language_changed)
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
        self.model_provider_label = QLabel(objectName="SectionTitle")
        card_layout.addWidget(self.model_provider_label)
        self.provider_combo = QComboBox()
        self.provider_ids = [preset.id for preset in PROVIDER_PRESETS]
        self.provider_combo.addItems([preset.name for preset in PROVIDER_PRESETS])
        self.provider_combo.currentIndexChanged.connect(self._provider_changed)
        self.provider_combo.setVisible(False)
        self._active_provider_id = self.provider_ids[0]
        self._active_endpoint = PROVIDER_PRESETS[0].base_url
        provider_panel = QWidget()
        provider_grid = QGridLayout(provider_panel)
        provider_grid.setContentsMargins(0, 0, 0, 0)
        provider_grid.setSpacing(6)
        self.provider_buttons = []
        for index, preset in enumerate(PROVIDER_PRESETS):
            button = QPushButton(preset.name, objectName="Secondary")
            button.setIcon(_provider_icon(preset.accent))
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, selected=index: self._select_provider(selected))
            provider_grid.addWidget(button, index // 4, index % 4)
            self.provider_buttons.append(button)
        card_layout.addWidget(provider_panel)
        self.provider_description_label = QLabel(objectName="Hint")
        self.provider_description_label.setWordWrap(True)
        card_layout.addWidget(self.provider_description_label)

        fields = QGridLayout()
        fields.setHorizontalSpacing(12)
        fields.setVerticalSpacing(8)
        for column in range(2):
            fields.setColumnStretch(column, 1)
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_toggle = QPushButton(objectName="Secondary")
        self.api_key_toggle.setCheckable(True)
        self.api_key_toggle.setFixedWidth(68)
        self.api_key_toggle.toggled.connect(self._toggle_api_key_visibility)
        self.model_edit = QComboBox()
        self.model_edit.setEditable(True)
        self.endpoint_edit = QLineEdit()
        self.endpoint_edit.setText(PROVIDER_PRESETS[0].base_url)
        self.endpoint_preset_combo = QComboBox()
        self.endpoint_preset_combo.setVisible(False)
        self.endpoint_preset_combo.setToolTip("Select a provider endpoint preset")
        self.endpoint_preset_combo.currentTextChanged.connect(self._endpoint_preset_changed)
        self.reasoning_combo = QComboBox()
        self.reasoning_combo.setObjectName("ReasoningCombo")
        self.reasoning_combo.addItem(self._t("reasoning.auto"), "auto")
        self.reasoning_combo.addItem(self._t("reasoning.low"), "low")
        self.reasoning_combo.addItem(self._t("reasoning.high"), "high")
        self.reasoning_combo.addItem(self._t("reasoning.maximum"), "maximum")
        self.api_key_label = QLabel(objectName="SectionTitle")
        self.model_label = QLabel(objectName="SectionTitle")
        self.endpoint_label = QLabel(objectName="SectionTitle")
        self.reasoning_label = QLabel(objectName="SectionTitle")
        self.model_actions = QHBoxLayout()
        self.model_actions.setContentsMargins(0, 0, 0, 0)
        self.connect_model_button = QPushButton(objectName="Secondary")
        self.verify_model_button = QPushButton(objectName="Secondary")
        self.browse_model_button = QPushButton(objectName="Secondary")
        self.browse_model_button.setVisible(False)
        self.connect_model_button.clicked.connect(self._connect_models)
        self.verify_model_button.clicked.connect(self._verify_model)
        self.browse_model_button.clicked.connect(self._browse_models)
        self.model_actions.addWidget(self.connect_model_button)
        self.model_actions.addWidget(self.verify_model_button)
        self.model_actions.addWidget(self.browse_model_button)
        self.model_actions.addStretch()
        api_key_row = QHBoxLayout()
        api_key_row.setContentsMargins(0, 0, 0, 0)
        api_key_row.setSpacing(8)
        api_key_row.addWidget(self.api_key_edit, 1)
        api_key_row.addWidget(self.api_key_toggle)
        fields.addWidget(self.api_key_label, 0, 0, 1, 2)
        fields.addLayout(api_key_row, 1, 0, 1, 2)
        fields.addWidget(self.endpoint_label, 2, 0, 1, 2)
        endpoint_row = QHBoxLayout()
        endpoint_row.setContentsMargins(0, 0, 0, 0)
        endpoint_row.setSpacing(8)
        endpoint_row.addWidget(self.endpoint_edit, 1)
        fields.addLayout(endpoint_row, 3, 0)
        fields.addLayout(self.model_actions, 3, 1)
        fields.addWidget(self.model_label, 4, 0)
        fields.addWidget(self.reasoning_label, 4, 1)
        fields.addWidget(self.model_edit, 5, 0)
        fields.addWidget(self.reasoning_combo, 5, 1)
        self.model_edit.currentTextChanged.connect(self._model_changed)
        self.endpoint_edit.editingFinished.connect(self._endpoint_edited)
        self.reasoning_combo.currentIndexChanged.connect(self._reasoning_changed)
        self._set_endpoint_presets(PROVIDER_PRESETS[0])
        card_layout.addLayout(fields)
        self.reasoning_hint_label = QLabel(objectName="Hint")
        self.reasoning_hint_label.setWordWrap(True)
        self.use_model_check = QCheckBox()
        self.use_model_check.setChecked(True)
        self.use_model_check.toggled.connect(self._model_route_changed)
        self.use_model_hint = QLabel(objectName="Hint")
        self.use_model_hint.setWordWrap(True)
        self._model_route_layout = QHBoxLayout()
        self._model_route_layout.setContentsMargins(0, 0, 0, 0)
        self._model_route_layout.addWidget(self.use_model_check)
        self._model_route_layout.addWidget(self.use_model_hint, 1)
        card_layout.addWidget(self.reasoning_hint_label)
        card_layout.addLayout(self._model_route_layout)
        self.model_status = QLabel(objectName="Hint")
        self.model_status.setWordWrap(True)
        self.model_status.setMinimumHeight(24)
        self.model_status.setMaximumHeight(62)
        self.model_status.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        card_layout.addWidget(self.model_status)
        self.model_error_button = QPushButton(objectName="Secondary")
        self.model_error_button.clicked.connect(self._show_model_error_details)
        self.model_error_button.setVisible(False)
        card_layout.addWidget(self.model_error_button)
        layout.addWidget(card)
        layout.addStretch()
        return self.pages[-1], layout

    def _select_provider(self, index: int) -> None:
        if not 0 <= index < len(self.provider_ids):
            return
        self.provider_combo.setCurrentIndex(index)
        for button_index, button in enumerate(self.provider_buttons):
            button.setChecked(button_index == index)

    def _build_review_page(self):
        scroll, content, layout = self._new_page("review", "page.review.title", "page.review.body")
        task_card, task_layout = self._card("TintCard")
        self.review_game_label = QLabel(objectName="SectionTitle")
        self.review_languages_label = QLabel(objectName="Status")
        self.review_mode_label = QLabel(objectName="Status")
        self.review_remaining_label = QLabel(objectName="Status")
        self.review_preserved_label = QLabel("", objectName="Hint")
        self.review_resume_label = QLabel("", objectName="Hint")
        self.review_resume_label.setWordWrap(True)
        task_layout.addWidget(self.review_game_label)
        task_layout.addWidget(self.review_languages_label)
        task_layout.addWidget(self.review_mode_label)
        task_layout.addWidget(self.review_remaining_label)
        task_layout.addWidget(self.review_preserved_label)
        task_layout.addWidget(self.review_resume_label)
        layout.addWidget(task_card)

        self.review_fact_titles = []
        self.review_fact_values = []
        for key in ("review.fact_model", "review.fact_options"):
            title = QLabel(objectName="Hint")
            value = QLabel(objectName="Status")
            value.setWordWrap(True)
            task_layout.addWidget(title)
            task_layout.addWidget(value)
            self.review_fact_titles.append((key, title))
            self.review_fact_values.append(value)
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
        self.generate_rpa_check.toggled.connect(self._sync_rpa_option)
        self.install_check = QCheckBox()
        options_layout.addWidget(self.generate_rpa_check)
        options_layout.addWidget(self.install_check)
        self.review_details_toggle = None
        self.review_details_label = QLabel()
        self.review_details_label.setObjectName("Hint")
        self.review_details_label.setWordWrap(True)
        self.review_details_label.setVisible(True)
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
        self._sync_rpa_option(self.generate_rpa_check.isChecked())
        return self.pages[-1], layout

    def _build_progress_page(self):
        scroll, content, layout = self._new_page("progress", "page.progress.title", "page.progress.body")
        card, card_layout = self._card()
        heading = QHBoxLayout()
        self.progress_heading = QLabel(objectName="SectionTitle")
        self.progress_percent = QLabel("", objectName="Status")
        self.progress_percent.setVisible(False)
        heading.addWidget(self.progress_heading)
        heading.addStretch()
        heading.addWidget(self.progress_percent)
        card_layout.addLayout(heading)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setTextVisible(False)
        card_layout.addWidget(self.progress_bar)
        runtime, runtime_layout = self._card("SuccessCard")
        self.progress_runtime = QLabel(objectName="Status")
        runtime_layout.addWidget(self.progress_runtime)
        card_layout.addWidget(runtime)
        phase_row = QHBoxLayout()
        self.progress_phase_labels = []
        for phase in ("prepare", "analyze", "translate", "validate", "build"):
            label = QLabel(f"○  {self._t(f'progress.phase.{phase}')}", objectName="Hint")
            label.setProperty("phase_key", phase)
            phase_row.addWidget(label, 1)
            self.progress_phase_labels.append(label)
        card_layout.addLayout(phase_row)
        stats_grid = QGridLayout()
        stats_grid.setSpacing(8)
        self.progress_stat_titles = []
        self.progress_stat_values = []
        for index, key in enumerate(("progress.current", "progress.files", "progress.eta", "progress.usage")):
            tile = QFrame(objectName="TintCard")
            tile_layout = QVBoxLayout(tile)
            tile_layout.setContentsMargins(12, 10, 12, 10)
            title = QLabel(objectName="Hint")
            value = QLabel("—", objectName="Status")
            value.setWordWrap(True)
            tile_layout.addWidget(title)
            tile_layout.addWidget(value)
            stats_grid.addWidget(tile, 0, index)
            self.progress_stat_titles.append((key, title))
            self.progress_stat_values.append(value)
        card_layout.addLayout(stats_grid)
        self.progress_stats = QLabel("", objectName="Status")
        self.progress_stats.setWordWrap(True)
        self.progress_stats.setVisible(False)
        card_layout.addWidget(self.progress_stats)
        self.progress_output = QLabel("", objectName="Hint")
        self.progress_output.setWordWrap(True)
        card_layout.addWidget(self.progress_output)
        self.progress_error_button = QPushButton(objectName="Secondary")
        self.progress_error_button.clicked.connect(self._show_error_details)
        self.progress_error_button.setVisible(False)
        card_layout.addWidget(self.progress_error_button)
        self.progress_open_button = QPushButton(objectName="Secondary")
        self.progress_open_button.clicked.connect(lambda: self._open_progress_path("output_dir"))
        self.progress_open_button.setVisible(False)
        self.progress_open_rpa_button = QPushButton(objectName="Secondary")
        self.progress_open_rpa_button.clicked.connect(lambda: self._open_progress_path("package_path"))
        self.progress_open_rpa_button.setVisible(False)
        self.progress_open_install_button = QPushButton(objectName="Secondary")
        self.progress_open_install_button.clicked.connect(lambda: self._open_progress_path("installed_dir"))
        self.progress_open_install_button.setVisible(False)
        output_actions = QHBoxLayout()
        output_actions.setContentsMargins(0, 0, 0, 0)
        output_actions.setSpacing(8)
        output_actions.addWidget(self.progress_open_button)
        output_actions.addWidget(self.progress_open_rpa_button)
        output_actions.addWidget(self.progress_open_install_button)
        output_actions.addStretch()
        card_layout.addLayout(output_actions)
        self.log_toggle = None
        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setVisible(True)
        self.log_edit.setMinimumHeight(240)
        self.log_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        card_layout.addWidget(self.log_edit, 1)
        layout.addWidget(card, 1)
        return self.pages[-1], layout

    def _refresh_shell(self) -> None:
        self.stack.setCurrentIndex(self.step)
        self.review_mode_label.setText(
            self._t("review.blank_translation" if self._blank_translation_mode else "review.model_translation")
        )
        for index, button in enumerate(self.nav_buttons):
            button.setText(f"{'✓' if index < self.step else f'{index + 1:02d}'}    {self._t(f'nav.{self.STEPS[index]}')}")
            button.setProperty("current", "true" if index == self.step else "false")
            button.style().unpolish(button)
            button.style().polish(button)
            button.setEnabled(index <= self.step and not self._translation_started)
        self.back_button.setVisible(self.step > 0)
        self.back_button.setEnabled(not self._translation_started)
        self.breadcrumb.setText(self._t("shell.breadcrumb", current=self.step + 1, total=len(self.STEPS)))
        self.footer_effect.setText(self._footer_effect())
        if self.step == 4 and self._translation_started:
            self.action_button.setText(self._t("shell.pause"))
            self.action_button.setEnabled(True)
        elif self.step == 4:
            if self._last_stage == "paused":
                self.action_button.setText(self._t("progress.resume"))
            elif self._last_stage == "failed":
                self.action_button.setText(self._t("progress.retry"))
            elif self._last_stage == "complete":
                self.action_button.setText(self._t("progress.output"))
            else:
                self.action_button.setText(
                    self._t("shell.extract_blank")
                    if self._blank_translation_mode
                    else self._t("shell.start_translation")
                )
            self.action_button.setEnabled(
                self._last_stage == "complete"
                or self._scope_preview_status == "ready"
            )
        elif self.step == 3:
            self.action_button.setText(self._t("shell.continue"))
            self.action_button.setEnabled(self._can_continue())
        else:
            self.action_button.setText(self._t("shell.continue"))
            self.action_button.setEnabled(self._can_continue())

    def _footer_effect(self) -> str:
        return (
            self._t("footer.blank_review" if self._blank_translation_mode else "footer.review")
            if self.step == 3
            else self._t("footer.progress")
            if self.step == 4
            else self._t(f"footer.{self.STEPS[self.step]}")
            if self.step in {0, 1, 2}
            else self._t("footer.default")
        )

    def _go_back(self) -> None:
        if self._translation_started:
            QMessageBox.warning(self, self._t("dialog.project"), self._t("dialog.translation_running"))
            return
        if self.step > 0:
            self.step -= 1
            self._refresh_shell()

    def _go_to_step(self, selected: int) -> None:
        if self._translation_started:
            QMessageBox.warning(self, self._t("dialog.project"), self._t("dialog.translation_running"))
            return
        if 0 <= selected <= self.step:
            self.step = selected
            if self.step == 3:
                self._start_scope_preview()
            self._refresh_shell()

    def _continue(self) -> None:
        if not self._can_continue():
            message = self._current_step_validation_error()
            if self.step == 0 and self._project_validation_state == "pending":
                self.project_status.setText(self._t("game.inspecting"))
            QMessageBox.warning(self, self._t("dialog.project"), message)
            return
        if self.step < 3:
            if self.step == 2:
                self._blank_translation_mode = not self.use_model_check.isChecked()
                if self._blank_translation_mode:
                    self.generate_rpa_check.setChecked(False)
                    self.install_check.setChecked(False)
            self.step += 1
            self._refresh_shell()
            if self.step == 3:
                self._start_scope_preview()
            return
        if self.step == 3:
            if self._scope_preview_status != "ready":
                self.language_scope_label.setText(self._t("languages.scanning"))
                self._start_scope_preview()
                self._refresh_shell()
                return
            self.step = 4
            self._resume_candidate = self._load_resume_candidate()
            if self._resume_candidate:
                self._last_stage = str(self._resume_candidate.get("stage", "")).casefold()
            self._refresh_shell()
            return
        if self.step == 4:
            if self._translation_started and self._cancel_token is not None:
                self._cancel_token.cancel()
                self.progress_runtime.setText(self._t("progress.pausing"))
            elif self._last_stage == "complete":
                self._open_output_folder()
            else:
                self._start_translation()

    def _can_continue(self) -> bool:
        if self.step == 4 and self._translation_started:
            return True
        return not bool(self._current_step_validation_error())

    def _current_step_validation_error(self) -> str:
        if self.step == 0:
            if self._project_validation_state == "pending":
                return self._t("game.inspecting")
            if self._project_validation_state != "valid":
                return self._project_validation_error or self._t("dialog.invalid_project")
        elif self.step == 1:
            if not self._target_language_value():
                return self._t("dialog.target_required")
            if (
                self._selected_existing_language is not None
                and self._scope_preview_status != "ready"
            ):
                return self._t("dialog.scope_required")
        elif self.step == 2 and self.use_model_check.isChecked():
            try:
                self._profile(require_model=True)
            except (TypeError, ValueError) as exc:
                return str(exc) or self._t("dialog.model_required")
        elif self.step in {3, 4} and self._scope_preview_status != "ready":
            return self._t("dialog.scope_required")
        return ""

    def _toggle_locale(self) -> None:
        self.locale = "zh" if self.locale == "en" else "en"
        self._retranslate_ui()
        self._save_settings()
        self._refresh_shell()

    def _toggle_api_key_visibility(self, visible: bool) -> None:
        self._api_key_visible = visible
        self.api_key_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        )
        self.api_key_toggle.setText(self._t("model.hide_key" if visible else "model.show_key"))

    def _retranslate_ui(self) -> None:
        """Update mounted widgets in place without rebuilding any page."""
        self.locale_button.setText("中文" if self.locale == "en" else "English")
        for page in self.pages:
            page.title.setText(self._t(page.title_key))
            page.body.setText(self._t(page.body_key))
        self._refresh_static_texts()

    def _refresh_static_texts(self) -> None:
        self.back_button.setText(self._t("shell.back"))
        self.settings_button.setText(self._t("top.settings"))
        self.game_project_label.setText(self._t("game.project"))
        self.game_workspace_label.setText(self._t("game.workspace"))
        self.game_sdk_label.setText(self._t("game.sdk"))
        self.project_browse_button.setText(self._t("game.browse_project"))
        self.workspace_browse_button.setText(self._t("game.browse_workspace"))
        self.sdk_browse_button.setText(self._t("game.browse_sdk"))
        self.require_engine_check.setText(self._t("game.require_engine"))
        self.project_edit.setToolTip(self._t("tip.project"))
        self.workspace_edit.setToolTip(self._t("tip.workspace"))
        self.renpy_sdk_edit.setToolTip(self._t("tip.sdk"))
        self.project_edit.setToolTip(self._t("tip.project"))
        self.workspace_edit.setToolTip(self._t("tip.workspace"))
        self.renpy_sdk_edit.setToolTip(self._t("tip.sdk"))
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
        active_preset = PROVIDER_PRESETS_BY_ID.get(self._active_provider_id, PROVIDER_PRESETS[0])
        self.provider_description_label.setText(active_preset.localized_description(self.locale))
        self.api_key_label.setText(self._t("model.api_key"))
        self.api_key_toggle.setText(self._t("model.hide_key" if self._api_key_visible else "model.show_key"))
        self.model_label.setText(self._t("model.model"))
        self.endpoint_label.setText(self._t("model.endpoint"))
        self.reasoning_label.setText(self._t("model.reasoning"))
        self.provider_combo.setToolTip(self._t("tip.provider"))
        self.endpoint_edit.setToolTip(self._t("tip.endpoint"))
        self.endpoint_preset_combo.setToolTip(self._t("tip.endpoint"))
        self.api_key_edit.setToolTip(self._t("tip.api_key"))
        self.model_edit.setToolTip(self._t("tip.model"))
        self.reasoning_combo.setToolTip(self._t("tip.reasoning"))
        self.provider_combo.setToolTip(self._t("tip.provider"))
        self.endpoint_edit.setToolTip(self._t("tip.endpoint"))
        self.endpoint_preset_combo.setToolTip(self._t("tip.endpoint"))
        self.api_key_edit.setToolTip(self._t("tip.api_key"))
        self.model_edit.setToolTip(self._t("tip.model"))
        self.reasoning_combo.setToolTip(self._t("tip.reasoning"))
        self.reasoning_combo.setItemText(0, self._t("reasoning.auto"))
        self.reasoning_combo.setItemText(1, self._t("reasoning.low"))
        self.reasoning_combo.setItemText(2, self._t("reasoning.high"))
        self.reasoning_combo.setItemText(3, self._t("reasoning.maximum"))
        self._refresh_reasoning_hint()
        for preset, button in zip(PROVIDER_PRESETS, self.provider_buttons):
            button.setText(preset.display_name(self.locale))
            button.setToolTip(self._t("tip.provider"))
        self.use_model_check.setText(self._t("model.use"))
        self.use_model_hint.setText(self._t("model.use_hint"))
        self.connect_model_button.setText(self._t("model.load"))
        self.verify_model_button.setText(self._t("model.verify"))
        self.browse_model_button.setText(self._t("model.browse", count=len(self._model_catalog_models)))
        self.connect_model_button.setToolTip(self._t("tip.load_models"))
        self.browse_model_button.setToolTip(self._t("tip.browse_models"))
        self.use_model_check.setToolTip(self._t("tip.use_model"))
        self.connect_model_button.setToolTip(self._t("tip.load_models"))
        self.browse_model_button.setToolTip(self._t("tip.browse_models"))
        self.use_model_check.setToolTip(self._t("tip.use_model"))
        self.generate_rpa_check.setText(self._t("review.rpa"))
        self.install_check.setText(self._t("review.install"))
        self.generate_rpa_check.setToolTip(self._t("tip.rpa"))
        self.install_check.setToolTip(self._t("tip.install"))
        self.generate_rpa_check.setToolTip(self._t("tip.rpa"))
        self.install_check.setToolTip(self._t("tip.install"))
        self.budget_title.setText("AI usage estimate" if self.locale == "en" else "AI 用量预估")
        if self._scope_preview_inventory is None:
            self.budget_label.setText(self._t("review.estimate_unavailable"))
        self.review_mode_label.setText(
            self._t("review.blank_translation" if self._blank_translation_mode else "review.model_translation")
        )
        for key, label in self.review_fact_titles:
            label.setText(self._t(key))
        if self._scope_preview_inventory is None:
            self.review_remaining_label.setText(self._t("review.waiting_scope"))
        self.pending_title.setText(self._t("review.no_pending"))
        self.pending_toggle.setText(
            self._t("review.hide_pending" if self.pending_details.isVisible() else "review.show_details")
        )
        self._toggle_review_details()
        self.progress_open_button.setText(self._t("progress.open_rpy"))
        self.progress_open_rpa_button.setText(self._t("progress.open_rpa"))
        self.progress_open_install_button.setText(self._t("progress.open_install"))
        self.progress_error_button.setText(self._t("progress.show_error"))
        self.model_error_button.setText(self._t("model.error_details"))
        if self._last_model_error_key:
            self.model_status.setText(self._t(self._last_model_error_key))
        self.progress_error_button.setText(self._t("progress.show_error"))
        for key, label in self.progress_stat_titles:
            label.setText(self._t(key))
        for label in self.progress_phase_labels:
            phase = label.property("phase_key")
            label.setText(f"○  {self._t(f'progress.phase.{phase}')}")
        self._set_progress_phase(str(self._progress_payload.get("stage", "")))
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
        if self._scope_preview_inventory is not None:
            self._refresh_review_preview()

    def _toggle_pending_details(self) -> None:
        visible = not self.pending_details.isVisible()
        self.pending_details.setVisible(visible)
        self.pending_toggle.setText(self._t("review.hide_pending" if visible else "review.show_details"))

    def _sync_rpa_option(self, enabled: bool) -> None:
        self.install_check.setEnabled(enabled)
        self.install_check.setVisible(enabled)
        if not enabled:
            self.install_check.setChecked(False)
        if self._scope_preview_inventory is not None:
            self._refresh_review_preview()

    def _toggle_log(self) -> None:
        self.log_edit.setVisible(True)

    def _project_changed(self, _value: str = "") -> None:
        self._project_revision += 1
        self._discovered_project = None
        self._project_validation_error = ""
        has_project = bool(self.project_edit.text().strip())
        if has_project and self._workspace_auto_generated:
            self._suggest_workspace(self.project_edit.text().strip())
        self._project_validation_state = "pending" if has_project else "idle"
        self._inspection_revision = None
        self._inspection_value = ""
        self._scope_preview_signature = None
        self._scope_preview_status = "idle"
        self.project_status.setText(self._t("game.inspecting" if has_project else "game.waiting"))
        self._inspection_timer.start(150)
        self._refresh_shell()

    @staticmethod
    def _normalise_path_text(value: str) -> str:
        return str(value or "").replace("\\", "/")

    def _normalise_path_edit(self, edit: QLineEdit) -> None:
        value = self._normalise_path_text(edit.text())
        if value != edit.text():
            edit.setText(value)

    def _workspace_edited(self, _value: str = "") -> None:
        self._workspace_auto_generated = False

    def _suggest_workspace(self, project: str) -> None:
        source = Path(project).expanduser()
        if source.is_file() or source.suffix.casefold() == ".exe":
            name = source.stem
        else:
            name = source.parent.name if source.name.casefold() == "game" else source.name
        base = _user_home_fallback() / "Documents" / "RenWeaveWork"
        self.workspace_edit.setText(self._normalise_path_text(str(base / (name or "project"))))
        self._workspace_auto_generated = True

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
            self.renpy_sdk_edit.setText(self._normalise_path_text(str(sdk.root)))
            self.require_engine_check.setChecked(True)
        current_target = self._target_language_value()
        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        existing_display_names: set[str] = set()
        for item in languages:
            display_name = getattr(item, "display_name", "") or item.language
            existing_display_names.add(display_name)
            self.target_combo.addItem(display_name, item.language)
        for language in LANGUAGE_DISPLAY_NAMES:
            if language not in existing_display_names and self.target_combo.findData(language) < 0:
                self.target_combo.addItem(language, language)
        if current_target:
            self._set_target_language_value(current_target)
        self.target_combo.blockSignals(False)
        self._refresh_existing_languages()
        self.project_status.setText(self._t("game.detected", count=len(languages)))
        self._inspection_revision = None
        self._inspection_value = ""
        self._refresh_shell()

    def _refresh_existing_languages(self) -> None:
        languages = list(getattr(self, "existing_languages", []))
        self._selected_existing_language = None
        self._existing_selection_source = ""
        self.existing_language_controls.clear()
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
            display_name = getattr(item, "display_name", "") or item.language
            label = display_name if display_name == item.language else f"{display_name}（{item.language}）"
            button = QPushButton(
                f"{label} · {item.script_files + item.compiled_files} files",
                objectName="Secondary",
            )
            button.setCheckable(True)
            button.clicked.connect(
                lambda _checked=False, language=item.language: self._select_existing_language(language)
            )
            self.existing_language_controls[item.language] = button
            self.existing_language_buttons.addWidget(button)

    def _select_existing_language(self, language: str) -> None:
        if language not in self.existing_language_controls:
            return
        self._selected_existing_language = language
        self._existing_selection_source = self.source_combo.currentText().strip() or "auto"
        self._changing_existing_selection = True
        try:
            self._set_target_language_value(language)
        finally:
            self._changing_existing_selection = False
        for item_language, button in self.existing_language_controls.items():
            button.setChecked(item_language == language)
        self._invalidate_scope_preview()
        self._start_scope_preview()
        self._refresh_shell()

    def _clear_existing_language_selection(self) -> None:
        if self._selected_existing_language is None:
            return
        self._selected_existing_language = None
        self._existing_selection_source = ""
        for button in self.existing_language_controls.values():
            button.setChecked(False)

    def _language_changed(self, _value: str = "") -> None:
        if not self._changing_existing_selection and self._selected_existing_language is not None:
            source = self.source_combo.currentText().strip() or "auto"
            target = self._target_language_value()
            if source != self._existing_selection_source or target != self._selected_existing_language:
                self._clear_existing_language_selection()
                self._invalidate_scope_preview()
        if self.step >= 3:
            self._start_scope_preview()
        self._refresh_shell()

    def _target_language_value(self) -> str:
        value = self.target_combo.currentData()
        return str(value).strip() if value else self.target_combo.currentText().strip()

    def _set_target_language_value(self, language: str) -> None:
        index = self.target_combo.findData(language)
        if index >= 0:
            self.target_combo.setCurrentIndex(index)
        else:
            self.target_combo.setCurrentText(language)

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
        target = self._target_language_value()
        if not project or not target:
            return
        signature = (project, workspace, self.source_combo.currentText().strip() or "auto", target)
        if signature == self._scope_preview_signature and self._scope_preview_status in {"scanning", "ready"}:
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

    def _invalidate_scope_preview(self) -> None:
        self._scope_preview_signature = None
        self._scope_preview_status = "idle"
        self._scope_preview_inventory = None
        self._scope_preview_budget = None
        self.language_scope_label.setText(self._t("languages.scope"))

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
        self._refresh_shell()

    def _scope_preview_failed(self, error: BaseException) -> None:
        self._scope_preview_status = "error"
        self.language_scope_label.setText(self._t("scope.failed", error=str(error)))
        self._refresh_shell()

    def _refresh_reasoning_hint(self) -> None:
        provider_id = self.provider_ids[self.provider_combo.currentIndex()]
        preset = PROVIDER_PRESETS_BY_ID[provider_id]
        self.reasoning_hint_label.setText(
            self._t("model.reasoning_unavailable" if preset.reasoning_control == "none" else "model.reasoning_hint")
        )

    def _persist_api_key(self, provider_id: str, base_url: str, secret: str) -> None:
        identity = (provider_id, base_url.strip())
        self._api_key_cache[identity] = secret
        if self._key_storage != "secure":
            return
        if not secret:
            try:
                self._credential_store.delete(*identity)
            except CredentialStorageError:
                self._logs.append("Could not forget the API key in the system credential store.")
            return
        try:
            self._credential_store.set(*identity, secret)
        except CredentialStorageError:
            self._logs.append("Could not save the API key in the system credential store.")

    def _read_api_key(self, provider_id: str, base_url: str) -> str:
        identity = (provider_id, base_url.strip())
        if identity in self._api_key_cache:
            return self._api_key_cache[identity]
        if self._key_storage != "secure":
            return ""
        try:
            secret = self._credential_store.get(*identity)
        except CredentialStorageError:
            secret = ""
        self._api_key_cache[identity] = secret or ""
        return secret or ""

    def _set_endpoint_presets(self, preset) -> None:
        current = self.endpoint_edit.text().strip()
        self.endpoint_preset_combo.blockSignals(True)
        self.endpoint_preset_combo.clear()
        self.endpoint_preset_combo.addItems(preset.base_urls)
        selected = self.endpoint_preset_combo.findText(current)
        self.endpoint_preset_combo.setCurrentIndex(selected if selected >= 0 else -1)
        self.endpoint_preset_combo.blockSignals(False)

    def _endpoint_preset_changed(self, endpoint: str) -> None:
        endpoint = endpoint.strip()
        if not endpoint or endpoint == self.endpoint_edit.text().strip():
            return
        self.endpoint_edit.setText(endpoint)
        self._endpoint_edited()

    def _sync_endpoint_preset_selection(self) -> None:
        index = self.endpoint_preset_combo.findText(self.endpoint_edit.text().strip())
        self.endpoint_preset_combo.blockSignals(True)
        self.endpoint_preset_combo.setCurrentIndex(index)
        self.endpoint_preset_combo.blockSignals(False)

    def _endpoint_edited(self) -> None:
        endpoint = self.endpoint_edit.text().strip()
        if not endpoint or endpoint == self._active_endpoint:
            return
        previous_identity = (self._active_provider_id, self._active_endpoint)
        self._persist_api_key(*previous_identity, self.api_key_edit.text())
        self._model_by_identity[previous_identity] = self.model_edit.currentText().strip()
        self._active_endpoint = endpoint
        self.api_key_edit.setText(self._read_api_key(self._active_provider_id, endpoint))
        self.model_edit.clear()
        remembered_model = self._model_by_identity.get((self._active_provider_id, endpoint), "")
        if remembered_model:
            self.model_edit.setCurrentText(remembered_model)
        self._sync_endpoint_preset_selection()
        self._save_settings()

    def _set_key_storage(self, storage: str) -> None:
        if storage not in {"secure", "memory"} or storage == self._key_storage:
            return
        identity = (self._active_provider_id, self.endpoint_edit.text().strip())
        if storage == "secure":
            self._key_storage = storage
            self._persist_api_key(*identity, self.api_key_edit.text())
        else:
            self._key_storage = storage
            try:
                self._credential_store.delete(*identity)
            except CredentialStorageError:
                self._logs.append("Could not remove the API key from the system credential store.")
        self._save_settings()

    def _forget_api_key(self) -> None:
        identity = (self._active_provider_id, self.endpoint_edit.text().strip())
        self._api_key_cache.pop(identity, None)
        try:
            self._credential_store.delete(*identity)
        except CredentialStorageError as exc:
            QMessageBox.warning(self, self._t("settings.title"), str(exc))
        self.api_key_edit.clear()
        self._save_settings()
        if self._settings_dialog is not None:
            self._settings_dialog.set_update_status(self._t("settings.key_forgotten"))

    def _open_settings(self) -> None:
        if self._settings_dialog is not None:
            self._settings_dialog.raise_()
            self._settings_dialog.activateWindow()
            return
        dialog = SettingsDialog(self)
        self._settings_dialog = dialog
        try:
            dialog.exec()
        finally:
            self._settings_dialog = None

    def _update_checks_toggled(self, enabled: bool) -> None:
        self._update_checks_enabled = enabled
        self._save_settings()
        if enabled:
            self._check_updates()

    def _check_updates(self, *, manual: bool = False, dialog: SettingsDialog | None = None) -> None:
        if self._update_worker is not None:
            return
        worker = _Worker(lambda: check_for_updates(__version__))
        self._update_worker = worker
        if dialog is not None:
            dialog.set_update_busy(True)

        def finished(result) -> None:
            self._update_worker = None
            target = dialog or self._settings_dialog
            if target is not None:
                target.set_update_status(
                    self._t(
                        "settings.update_available" if result.update_available else "settings.up_to_date",
                        latest=result.latest_version,
                        current=result.current_version,
                    )
                )
                target.set_update_busy(False)
            elif manual or result.update_available:
                QMessageBox.information(
                    self,
                    self._t("settings.title"),
                    self._t(
                        "settings.update_available" if result.update_available else "settings.up_to_date",
                        latest=result.latest_version,
                        current=result.current_version,
                    ),
                )

        def failed(error: BaseException) -> None:
            self._update_worker = None
            target = dialog or self._settings_dialog
            message = self._t("settings.update_failed", error=str(error))
            if target is not None:
                target.set_update_status(message)
                target.set_update_busy(False)
            elif manual:
                QMessageBox.warning(self, self._t("settings.title"), message)

        worker.signals.finished.connect(finished)
        worker.signals.failed.connect(failed)
        self.thread_pool.start(worker)

    def _model_changed(self, value: str) -> None:
        self._model_by_identity[(self._active_provider_id, self._active_endpoint)] = value.strip()
        self._save_settings()

    def _reasoning_changed(self, _index: int) -> None:
        self._save_settings()

    def _model_route_changed(self, checked: bool) -> None:
        self._blank_translation_mode = not checked
        if not checked:
            self.generate_rpa_check.setChecked(False)
            self.install_check.setChecked(False)
        self._refresh_shell()

    def _provider_changed(self, index: int) -> None:
        if not self.provider_ids:
            return
        provider_id = self.provider_ids[index]
        preset = PROVIDER_PRESETS_BY_ID[provider_id]
        previous_identity = (self._active_provider_id, self._active_endpoint)
        if previous_identity != (provider_id, preset.base_url):
            self._persist_api_key(*previous_identity, self.api_key_edit.text())
            self._model_by_identity[previous_identity] = self.model_edit.currentText().strip()
        self._active_provider_id = provider_id
        self._active_endpoint = preset.base_url
        self.endpoint_edit.blockSignals(True)
        self.endpoint_edit.setText(preset.base_url)
        self.endpoint_edit.blockSignals(False)
        self._set_endpoint_presets(preset)
        self.api_key_edit.setText(self._read_api_key(provider_id, preset.base_url))
        self.model_edit.blockSignals(True)
        self.model_edit.clear()
        remembered_model = self._model_by_identity.get((provider_id, preset.base_url), "")
        if remembered_model:
            self.model_edit.setCurrentText(remembered_model)
        self.model_edit.blockSignals(False)
        self._model_catalog_models = ()
        self.browse_model_button.setVisible(False)
        self.reasoning_combo.setEnabled(preset.reasoning_control != "none")
        self.provider_description_label.setText(preset.localized_description(self.locale))
        self._last_model_error_key = ""
        self.model_error_button.setVisible(False)
        self._refresh_reasoning_hint()
        for button_index, button in enumerate(getattr(self, "provider_buttons", [])):
            button.setChecked(button_index == index)
        self._save_settings()

    def _connect_models(self) -> None:
        try:
            profile = self._profile()
        except (TypeError, ValueError) as error:
            self._model_operation_failed(error)
            return
        self.model_status.setText(self._t("model.loading"))
        self._last_model_error_key = ""
        self.model_error_button.setVisible(False)
        self._run_worker(
            lambda: OpenAICompatibleCatalog(profile).list_models(),
            lambda catalog: self._models_loaded(catalog),
            lambda error: self._model_operation_failed(error, loading=True),
        )

    def _models_loaded(self, catalog) -> None:
        self._last_model_error_key = ""
        self._model_catalog_models = tuple(catalog.models)
        self.model_status.setText(self._t("model.loaded", count=len(catalog.models), latency=catalog.latency_ms))
        self.model_error_button.setVisible(False)
        current = self.model_edit.currentText().strip()
        self.model_edit.clear()
        self.model_edit.addItems(catalog.models)
        if catalog.models:
            selected = current if current in catalog.models else self._model_by_identity.get(
                (self._active_provider_id, self._active_endpoint), ""
            )
            selected_index = catalog.models.index(selected) if selected in catalog.models else 0
            self.model_edit.setCurrentIndex(selected_index)
            self._model_by_identity[(self._active_provider_id, self._active_endpoint)] = catalog.models[selected_index]
            self._save_settings()
        self.browse_model_button.setVisible(bool(self._model_catalog_models))

    def _browse_models(self) -> None:
        if not self._model_catalog_models:
            return
        dialog = ModelPickerDialog(self, self._model_catalog_models)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.model_edit.setCurrentText(dialog.selected_model)

    def _verify_model(self) -> None:
        try:
            profile = self._profile(require_model=True)
        except (TypeError, ValueError) as error:
            self._model_operation_failed(error)
            return
        self.model_status.setText(self._t("model.verifying"))
        self._last_model_error_key = ""
        self.model_error_button.setVisible(False)
        self._run_worker(
            lambda: OpenAICompatibleCatalog(profile).verify_model(),
            self._model_verified,
            lambda error: self._model_operation_failed(error, verifying=True),
        )

    def _model_verified(self, result) -> None:
        self._last_model_error_key = ""
        self.model_error_button.setVisible(False)
        self.model_status.setText(
            self._t("model.verified", model=result.model, latency=result.latency_ms)
        )

    def _friendly_error_key(self, error: object) -> str:
        message = str(error)
        lowered = message.casefold()
        if any(token in lowered for token in ("401", "403", "unauthorized", "forbidden", "api key", "authentication")):
            return "error.api_key"
        if "timeout" in lowered or "timed out" in lowered:
            return "error.timeout"
        if any(token in lowered for token in ("connection", "dns", "network", "name resolution")):
            return "error.connection"
        return "error.generic"

    def _model_operation_failed(self, error: object, *, loading: bool = False, verifying: bool = False) -> None:
        self._last_error_details = str(error)
        self._last_model_error_key = self._friendly_error_key(error)
        self.model_error_button.setVisible(True)
        self.model_status.setText(self._t(self._last_model_error_key))

    def _show_model_error_details(self) -> None:
        if self._last_error_details:
            ErrorDetailsDialog(self, self._last_error_details).exec()

    def _profile(self, *, require_model: bool = False) -> ModelProfile:
        provider_id = self.provider_ids[self.provider_combo.currentIndex()]
        preset = PROVIDER_PRESETS_BY_ID[provider_id]
        profile = ModelProfile(
            name=preset.name,
            model=self.model_edit.currentText().strip(),
            base_url=self.endpoint_edit.text().strip(),
            provider_id=provider_id,
            api_key=self.api_key_edit.text(),
            api_key_env=preset.api_key_env,
            supports_json=preset.supports_json_parameter,
            reasoning_level=str(self.reasoning_combo.currentData() or "auto"),
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

    def _load_resume_candidate(self) -> dict[str, object] | None:
        """Return a compatible, incomplete workspace state for the current task."""
        project = self.project_edit.text().strip()
        workspace = self.workspace_edit.text().strip()
        target = self._target_language_value()
        if not project or not workspace or not target:
            return None
        state_path = Path(workspace).expanduser() / "state.json"
        try:
            payload = read_json(state_path)
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        stage = str(payload.get("stage", "")).strip().casefold()
        completed = payload.get("completed_scene_ids", [])
        if stage in {"", PipelineStage.COMPLETE.value} or not isinstance(completed, list) or not completed:
            return None
        try:
            current_project = str(Path(project).expanduser().resolve())
            saved_project = str(Path(str(payload.get("project_target", ""))).expanduser().resolve())
        except (OSError, RuntimeError, TypeError):
            return None
        if current_project != saved_project:
            return None
        source = self.source_combo.currentText().strip() or "auto"
        if str(payload.get("source_language", "auto")) != source:
            return None
        if str(payload.get("target_language", "")) != target:
            return None
        return payload

    def _refresh_review_preview(self) -> None:
        inventory = self._scope_preview_inventory
        budget = self._scope_preview_budget
        if inventory is None:
            return
        self._resume_candidate = self._load_resume_candidate()
        self.review_game_label.setText(Path(self.project_edit.text().strip()).name or self.project_edit.text().strip())
        self.review_languages_label.setText(
            f"{self.source_combo.currentText().strip() or 'auto'}  →  {self.target_combo.currentText().strip()}"
        )
        self.review_fact_values[0].setText(
            "—" if self._blank_translation_mode else f"{self.provider_combo.currentText()} · {self.model_edit.currentText().strip() or '—'}"
        )
        self.review_fact_values[1].setText(
            (
                (
                    "生成 RPA · 校验后安装"
                    if self.generate_rpa_check.isChecked() and self.install_check.isChecked()
                    else "生成 RPA"
                    if self.generate_rpa_check.isChecked()
                    else "不生成 RPA"
                )
                if self.locale == "zh"
                else (
                    "Generate RPA · Install after validation"
                    if self.generate_rpa_check.isChecked() and self.install_check.isChecked()
                    else "Generate RPA"
                    if self.generate_rpa_check.isChecked()
                    else "Do not generate RPA"
                )
            )
        )
        self.review_remaining_label.setText(
            (f"待翻译模型单元：{inventory.model_units}" if self.locale == "zh" else f"Remaining model units: {inventory.model_units}")
        )
        self.review_preserved_label.setText(
            (f"可复用已有单元：{inventory.reusable_units}" if self.locale == "zh" else f"Reusable existing units: {inventory.reusable_units}")
        )
        if self._resume_candidate:
            self.review_resume_label.setVisible(True)
            self.review_resume_label.setText(
                self._t(
                    "review.resume_body",
                    completed=len(self._resume_candidate.get("completed_scene_ids", [])),
                )
            )
        else:
            self.review_resume_label.clear()
            self.review_resume_label.setVisible(False)
        if self._blank_translation_mode:
            self.budget_label.setText("0 Token" if self.locale == "en" else "0 Token")
            self.budget_note.setText(
                "Blank translation generation uses no Tokens."
                if self.locale == "en"
                else "生成空白翻译不产生Token消耗"
            )
        elif budget is not None:
            self.budget_label.setText(
                (
                    f"预估用量：{budget.estimated_total_low:,}–{budget.estimated_total_high:,} Token"
                    if self.locale == "zh"
                    else f"Estimated usage: {budget.estimated_total_low:,}–{budget.estimated_total_high:,} tokens"
                )
            )
            self.budget_note.setText(
                "预估范围会在建立索引后更新。" if self.locale == "zh" else "The estimate is refined after indexing."
            )
        else:
            self.budget_label.setText(self._t("review.estimate_unavailable"))
            self.budget_note.clear()
        self.pending_title.setText(
            f"待处理单元（{len(inventory.pending_units)}）"
            if self.locale == "zh"
            else f"Pending units ({len(inventory.pending_units)})"
        )
        rows = []
        for item in inventory.pending_units[:50]:
            rows.append(f"{item.get('file', '')}:{item.get('line', 0)}  {item.get('source', '')}")
        self.pending_details.setPlainText("\n\n".join(rows))

    def _toggle_review_details(self) -> None:
        self.review_details_label.setVisible(True)
        self.review_details_label.setText(
            "\n".join(
                (
                    self._t("review.detail_project", value=self.project_edit.text()),
                    self._t("review.detail_workspace", value=self.workspace_edit.text()),
                    self._t(
                        "review.detail_sdk",
                        value=self.renpy_sdk_edit.text() or self._t("review.not_selected"),
                    ),
                )
            )
        )

    def _start_translation(self) -> None:
        self._load_workspace_log()
        self._last_error_details = ""
        self.progress_error_button.setVisible(False)
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
                target_language=self._target_language_value(),
                api_key=self.api_key_edit.text(),
                generate_rpa=self.generate_rpa_check.isChecked(),
                install=self.install_check.isChecked(),
                renpy_sdk=self.renpy_sdk_edit.text().strip(),
                require_engine_validation=self.require_engine_check.isChecked(),
            )
            request.validate()
        except (OSError, TypeError, ValueError) as exc:
            QMessageBox.warning(self, self._t("dialog.cannot_start"), str(exc))
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
        self._load_workspace_log()
        self._last_error_details = ""
        self.progress_error_button.setVisible(False)
        project = self.project_edit.text().strip()
        workspace = self.workspace_edit.text().strip()
        source = self.source_combo.currentText().strip() or "auto"
        target = self._target_language_value()
        if not project or not workspace or not target:
            QMessageBox.warning(
                self,
                self._t("dialog.cannot_start"),
                self._t("dialog.missing_fields"),
            )
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
        self._progress_payload = state.to_dict()
        self._translation_started = False
        if state.stage == PipelineStage.PAUSED:
            self._last_stage = "paused"
            self.progress_heading.setText(self._t("progress.paused"))
            self.progress_runtime.setText(self._t("progress.paused_body"))
            self.progress_output.clear()
            self.progress_error_button.setVisible(False)
            self.progress_open_button.setVisible(False)
            self.progress_open_rpa_button.setVisible(False)
            self.progress_open_install_button.setVisible(False)
            self._refresh_shell()
            return
        self._last_stage = "complete"
        self._last_error_details = ""
        self.progress_error_button.setVisible(False)
        self.progress_heading.setText(self._t("translation.ready"))
        self.progress_runtime.setText(self._t("translation.completed"))
        output_dir = str(self._progress_payload.get("output_dir", "") or "")
        package_path = str(self._progress_payload.get("package_path", "") or "")
        self.progress_output.setText(
            "\n".join(item for item in (
                self._t("progress.rpy_output", path=output_dir) if output_dir else "",
                self._t("progress.rpa_output", path=package_path) if package_path else "",
            ) if item)
        )
        self.progress_open_button.setVisible(bool(output_dir))
        self.progress_open_rpa_button.setVisible(bool(package_path))
        self.progress_open_install_button.setVisible(bool(self._progress_payload.get("installed_dir", "")))
        self._refresh_shell()

    def _translation_failed(self, error: BaseException) -> None:
        self._last_stage = "failed"
        self._translation_started = False
        self._last_error_details = str(error)
        self.progress_error_button.setVisible(True)
        self.progress_runtime.setText(self._t("translation.failed", error=error))
        self._refresh_shell()

    def _set_progress_phase(self, stage: str) -> None:
        phase_by_stage = {
            "created": 0,
            "discovered": 0,
            "acquired": 0,
            "decompiled": 0,
            "indexed": 0,
            "knowledge_ready": 1,
            "synthesizing": 1,
            "narrative_ready": 1,
            "translating": 2,
            "validated": 3,
            "refining": 3,
            "refined": 3,
            "building": 4,
            "validating_build": 4,
            "complete": 5,
        }
        current = phase_by_stage.get(stage.casefold())
        if current is None:
            return
        for index, label in enumerate(self.progress_phase_labels):
            state = "done" if index < current or current == 5 else "active" if index == current else "idle"
            label.setProperty("phase_state", state)
            prefix = "✓" if state == "done" else "●" if state == "active" else "○"
            phase = label.property("phase_key")
            label.setText(f"{prefix}  {self._t(f'progress.phase.{phase}')}")
            label.style().unpolish(label)
            label.style().polish(label)

    def _progress_stage_text(self, stage: str) -> str:
        stage = stage.casefold()
        if stage in {"created", "discovered", "acquired", "decompiled", "indexed"}:
            return self._t("progress.phase.prepare")
        if stage in {"knowledge_ready", "synthesizing", "narrative_ready"}:
            return self._t("progress.phase.analyze")
        if stage == "translating":
            return self._t("progress.phase.translate")
        if stage in {"validated", "refining", "refined"}:
            return self._t("progress.phase.validate")
        if stage in {"building", "validating_build"}:
            return self._t("progress.phase.build")
        if stage == "complete":
            return self._t("translation.completed")
        if stage == "paused":
            return self._t("progress.paused")
        if stage == "failed":
            return self._t("translation.failed", error="")
        return self._t("progress.idle")

    def _progress_operation_text(self, operation: str, stage: str) -> str:
        if self.locale == "en" or not operation:
            return operation
        exact = {
            "Upgrading the project analysis": "正在升级项目分析",
            "Discovering the Ren'Py project": "正在发现 Ren'Py 项目",
            "Collecting scripts from game files and archives": "正在收集游戏文件和归档中的脚本",
            "Decompiling compiled Ren'Py scripts": "正在反编译已编译的 Ren'Py 脚本",
            "Building the scene and control-flow index": "正在建立场景和控制流索引",
            "Deterministic game knowledge is ready": "游戏知识索引已准备完成",
            "Generating blank Ren'Py translation scripts": "正在生成空白 Ren'Py 翻译脚本",
            "Validating blank Ren'Py translation scripts": "正在校验空白 Ren'Py 翻译脚本",
            "Blank translation scripts are ready": "空白翻译脚本已准备完成",
            "Preparing or restoring project context": "正在准备或恢复项目上下文",
            "Understanding storylines, characters, and terminology": "正在理解剧情、人物和术语",
            "Narrative context is ready": "剧情上下文已准备完成",
            "Translating scenes with narrative context": "正在结合剧情上下文翻译场景",
            "Reviewing terminology and voice consistency": "正在检查术语和语气一致性",
            "Global refinement is complete": "全局润色已完成",
            "Generating Ren'Py translation scripts": "正在生成 Ren'Py 翻译脚本",
            "Validating generated Ren'Py scripts": "正在校验生成的 Ren'Py 脚本",
            "Some scenes require attention before packaging": "部分场景需要处理后才能打包",
            "Paused safely after saving the latest checkpoint": "已保存最新检查点并安全暂停",
        }
        if operation in exact:
            return exact[operation]
        if operation.startswith("Translating "):
            return "正在翻译 " + operation.removeprefix("Translating ")
        return self._progress_stage_text(stage)

    def _show_error_details(self) -> None:
        if self._last_error_details:
            ErrorDetailsDialog(self, self._last_error_details).exec()

    def _copy_path(self, value: str) -> None:
        value = value.strip()
        if value:
            QApplication.clipboard().setText(value)

    def _open_path(self, value: str, title: str) -> None:
        value = value.strip()
        if not value:
            return
        try:
            target = str(Path(value).expanduser().resolve(strict=True))
            if os.name == "nt":
                os.startfile(target)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", target])
            else:
                subprocess.Popen(["xdg-open", target])
        except OSError as exc:
            QMessageBox.warning(self, title, str(exc))

    def _open_progress_path(self, payload_key: str) -> None:
        value = str(self._progress_payload.get(payload_key, "") or "")
        self._open_path(value, self._t("dialog.open_output"))

    def _open_output_folder(self) -> None:
        self._open_progress_path("output_dir")

    def _load_workspace_log(self) -> None:
        if self.log_edit.toPlainText().strip():
            return
        workspace = Path(self.workspace_edit.text().strip()).expanduser()
        candidates = []
        log_path = str(self._progress_payload.get("log_path", "") or "").strip()
        if log_path:
            candidates.append(Path(log_path).expanduser())
        candidates.append(workspace / "logs" / "renweave.log")
        for candidate in candidates:
            try:
                if candidate.is_file():
                    self.log_edit.setPlainText(candidate.read_text(encoding="utf-8"))
                    return
            except (OSError, UnicodeError):
                continue

    def _progress_received(self, payload) -> None:
        self._progress_payload = payload.to_dict() if hasattr(payload, "to_dict") else dict(payload)
        self._set_progress_phase(str(self._progress_payload.get("stage", "")))
        percent = float(self._progress_payload.get("progress_percent", 0) or 0)
        self.progress_bar.setValue(max(0, min(100, round(percent))))
        self.progress_percent.setText(f"{percent:.0f}%")
        stage = str(self._progress_payload.get("stage", ""))
        operation = str(self._progress_payload.get("current_operation", "") or "")
        self.progress_heading.setText(self._progress_operation_text(operation, stage))
        completed = self._progress_payload.get("completed_scenes", 0)
        total = self._progress_payload.get("total_scenes", 0)
        self.progress_stats.setText(
            f"场景：{completed}/{total}" if self.locale == "zh" else f"Scenes: {completed}/{total}"
        )
        self.progress_runtime.setText(self._progress_stage_text(stage))
        eta_seconds = self._progress_payload.get("eta_seconds", -1)
        eta = "—" if not isinstance(eta_seconds, (int, float)) or eta_seconds < 0 else f"{int(eta_seconds)}s"
        calls = int(self._progress_payload.get("total_model_calls", 0) or 0)
        tokens = int(self._progress_payload.get("total_prompt_tokens", 0) or 0) + int(
            self._progress_payload.get("total_completion_tokens", 0) or 0
        )
        self.progress_stat_values[0].setText(self._progress_operation_text(operation, stage) or "—")
        self.progress_stat_values[1].setText(f"{completed}/{total}")
        self.progress_stat_values[2].setText(eta)
        self.progress_stat_values[3].setText(
            f"{calls} 次调用 · {tokens:,} Token"
            if self.locale == "zh"
            else f"{calls} calls · {tokens:,} tokens"
        )
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
        self._select_provider(self.provider_combo.currentIndex())
        saved_endpoint = str(self._settings.get("base_url", self.endpoint_edit.text())).strip()
        if saved_endpoint and saved_endpoint != self.endpoint_edit.text().strip():
            self.endpoint_edit.setText(saved_endpoint)
            self._active_endpoint = saved_endpoint
            self.api_key_edit.setText(self._read_api_key(self._active_provider_id, saved_endpoint))
            self.model_edit.setCurrentText(self._model_by_identity.get((self._active_provider_id, saved_endpoint), ""))
        self._sync_endpoint_preset_selection()
        saved_model = str(self._settings.get("model", "")).strip()
        if saved_model:
            self._model_by_identity[(self._active_provider_id, self._active_endpoint)] = saved_model
        reasoning_level = str(self._settings.get("reasoning_level", "auto"))
        reasoning_index = self.reasoning_combo.findData(reasoning_level)
        if reasoning_index >= 0:
            self.reasoning_combo.setCurrentIndex(reasoning_index)
        self.api_key_edit.setText(self._read_api_key(self._active_provider_id, self._active_endpoint))

    def _save_settings(self) -> None:
        payload = {
            "schema_version": 1,
            "locale": self.locale,
            "provider_id": self.provider_ids[self.provider_combo.currentIndex()],
            "model": self.model_edit.currentText().strip(),
            "base_url": self.endpoint_edit.text().strip(),
            "reasoning_level": str(self.reasoning_combo.currentData() or "auto"),
            "key_storage": self._key_storage,
            "update_checks_enabled": self._update_checks_enabled,
        }
        try:
            atomic_write_json(self._settings_path, payload)
        except OSError:
            pass

    def _save_api_key(self) -> None:
        self._persist_api_key(self._active_provider_id, self.endpoint_edit.text().strip(), self.api_key_edit.text())

    def _browse_project(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, self._t("shell.select_project"))
        if selected:
            self.project_edit.setText(self._normalise_path_text(selected))

    def _browse_workspace(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, self._t("shell.select_workspace"))
        if selected:
            self.workspace_edit.setText(self._normalise_path_text(selected))

    def _browse_sdk(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, self._t("shell.select_sdk"))
        if selected:
            self.renpy_sdk_edit.setText(self._normalise_path_text(selected))
            self.require_engine_check.setChecked(True)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._translation_started and self._cancel_token is not None:
            self._cancel_token.cancel()
        self._save_api_key()
        self._save_settings()
        self.thread_pool.clear()
        self.thread_pool.waitForDone(3000)
        event.accept()


def launch_qt_gui(*, initial_project: str = "", initial_workspace: str = "") -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    window = QtRenWeaveWindow(initial_project=initial_project, initial_workspace=initial_workspace)
    window.show()
    return app.exec()


def main() -> int:
    return launch_qt_gui()


__all__ = ["QtRenWeaveWindow", "launch_qt_gui", "main"]
