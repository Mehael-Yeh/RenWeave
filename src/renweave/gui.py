from __future__ import annotations

import json
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .pipeline import PipelineState, RenWeavePipeline
from .provider import ModelCatalog, ModelProfile, ModelVerification, OpenAICompatibleCatalog


@dataclass(slots=True)
class TranslationRequest:
    project: str
    workspace: str
    provider: str
    source_language: str
    target_language: str
    api_key: str = ""
    install: bool = False
    overwrite_existing: bool = False
    renpy_sdk: str = ""
    require_engine_validation: bool = False

    def validate(self) -> None:
        project = Path(self.project).expanduser()
        provider = Path(self.provider).expanduser()
        if not project.exists():
            raise ValueError("Select an existing Ren'Py game directory or executable")
        if not self.workspace.strip():
            raise ValueError("Select a workspace directory")
        if not provider.is_file():
            raise ValueError("Select a valid provider configuration JSON file")
        if not self.target_language.strip():
            raise ValueError("Enter a target language name or code")
        if self.renpy_sdk and not Path(self.renpy_sdk).expanduser().exists():
            raise ValueError("The selected Ren'Py SDK path does not exist")


def execute_translation(
    request: TranslationRequest,
    *,
    pipeline_factory: Callable[[str], RenWeavePipeline] = RenWeavePipeline,
) -> PipelineState:
    """Execute the same pipeline as the CLI; the API key remains memory-only."""
    request.validate()
    profile = ModelProfile.load(request.provider)
    if request.api_key:
        profile.api_key = request.api_key
    profile.validate()
    pipeline = pipeline_factory(request.workspace)
    state = pipeline.translate(
        request.project,
        request.source_language.strip() or "auto",
        request.target_language.strip(),
        profile,
        install=request.install,
        overwrite_existing=request.overwrite_existing,
        renpy_sdk_path=request.renpy_sdk or None,
        require_engine_validation=request.require_engine_validation,
    )
    if state.stage != "complete" or state.failed_scene_ids:
        detail = state.error or f"{len(state.failed_scene_ids)} scenes were not completed"
        raise RuntimeError(detail)
    return state


STAGE_LABELS = {
    "created": "Preparing task",
    "discovered": "Finding the Ren'Py project",
    "acquired": "Collecting and unpacking scripts safely",
    "decompiled": "Decompiling scripts",
    "indexed": "Building the scene and story index",
    "knowledge_ready": "Building deterministic game knowledge",
    "synthesizing": "Understanding world, characters, and terminology",
    "narrative_ready": "Narrative knowledge is ready",
    "translating": "Translating scenes with context",
    "validated": "Validating translated scenes",
    "refining": "Refining consistency across scenes",
    "refined": "Global refinement is complete",
    "building": "Building Ren'Py language scripts",
    "validating_build": "Validating generated scripts in isolation",
    "complete": "Translation package is ready",
    "failed": "Translation failed",
}

STAGE_LABELS_ZH = {
    "created": "准备任务",
    "discovered": "识别 Ren'Py 项目",
    "acquired": "安全采集与解包脚本",
    "decompiled": "反编译脚本",
    "indexed": "建立场景与剧情索引",
    "knowledge_ready": "建立确定性游戏知识",
    "synthesizing": "理解世界观、角色与术语",
    "narrative_ready": "剧情知识已就绪",
    "translating": "结合上下文翻译场景",
    "validated": "校验场景译文",
    "refining": "精修跨场景一致性",
    "refined": "全局精修完成",
    "building": "生成 Ren'Py 语言脚本",
    "validating_build": "隔离验证生成脚本",
    "complete": "翻译包已就绪",
    "failed": "翻译失败",
}


COPY = {
    "en": {
        "app_title": "RenWeave",
        "app_subtitle": "Context-aware Ren'Py translation, from game files to a verified language package.",
        "language": "Interface language",
        "steps.model": "Model",
        "steps.game": "Game",
        "steps.languages": "Languages",
        "steps.review": "Review",
        "steps.progress": "Progress",
        "step_count": "Step {current} of {total}",
        "back": "Back",
        "continue": "Continue",
        "start": "Start translation",
        "close": "Close",
        "retry": "Try again",
        "cancel": "Cancel",
        "browse": "Browse",
        "optional": "Optional",
        "model.title": "Connect your AI model",
        "model.body": "This is the only required setup before selecting a game. RenWeave will check the API, load its models, and verify the model you choose.",
        "model.api_type": "API type",
        "model.api_type_value": "OpenAI-compatible",
        "model.provider": "Provider name",
        "model.endpoint": "Base URL",
        "model.key": "API key",
        "model.key_hint": "Kept in memory for this session and never written to the profile.",
        "model.show_key": "Show key",
        "model.import": "Import profile",
        "model.connect": "Connect and load models",
        "model.model": "Model",
        "model.model_hint": "Choose a discovered model or enter an exact model ID.",
        "model.verify": "Verify selected model",
        "model.idle": "Not connected",
        "model.idle_body": "Enter an endpoint and API key, then load the available models.",
        "model.connecting": "Connecting",
        "model.connecting_body": "Checking the models endpoint…",
        "model.connected": "API connected",
        "model.connected_body": "Loaded {count} models in {latency} ms. Verify the selected model to continue.",
        "model.verifying": "Verifying model",
        "model.verifying_body": "Making one minimal chat request…",
        "model.verified": "Model ready",
        "model.verified_body": "{model} responded successfully in {latency} ms.",
        "model.failed": "Connection failed",
        "model.changed": "Settings changed",
        "model.changed_body": "Reconnect and verify the model before continuing.",
        "model.import_title": "Import an OpenAI-compatible profile",
        "model.import_error": "Profile could not be imported",
        "model.required": "Connect to the API and verify a model before continuing.",
        "game.title": "Choose the Ren'Py game",
        "game.body": "RenWeave works from an isolated workspace and does not modify the source game during analysis.",
        "game.project": "Game directory or executable",
        "game.project_hint": "Select the game root, its game folder, or the game executable.",
        "game.workspace": "Workspace",
        "game.workspace_hint": "Analysis, caches, checkpoints, and output packages are stored here.",
        "game.advanced": "Engine validation",
        "game.sdk": "Ren'Py SDK",
        "game.sdk_hint": "Optional. Select an SDK to compile-check generated scripts.",
        "game.require_engine": "Require Ren'Py engine validation before packaging",
        "game.project_picker": "Select a Ren'Py game root or game directory",
        "game.workspace_picker": "Select or create a RenWeave workspace",
        "game.sdk_picker": "Select a Ren'Py SDK directory",
        "game.invalid": "Choose a valid game and workspace before continuing.",
        "languages.title": "Choose source and target languages",
        "languages.body": "Any language name or locale code is accepted. Source detection is automatic by default.",
        "languages.source": "Source language",
        "languages.target": "Target language",
        "languages.auto": "Auto detect",
        "languages.hint": "Examples: English, 简体中文, Deutsch, pt-BR, 日本語.",
        "languages.required": "Enter a target language before continuing.",
        "review.title": "Review and start",
        "review.body": "RenWeave will run the complete pipeline without asking for glossaries, character lists, or world-building notes.",
        "review.model": "AI model",
        "review.game": "Game and workspace",
        "review.languages": "Translation",
        "review.options": "Output options",
        "review.install": "Install the completed language package into the game",
        "review.install_hint": "Off by default. The packaged output always remains in the workspace.",
        "review.engine_yes": "Ren'Py engine validation required",
        "review.engine_no": "Built-in validation",
        "review.key_safe": "API key remains memory-only",
        "progress.title": "Translation in progress",
        "progress.body": "You can follow each stage here. Checkpoints make interrupted work resumable.",
        "progress.ready": "Preparing the one-click pipeline…",
        "progress.scenes": "{count} scenes completed",
        "progress.log": "Activity",
        "progress.started": "Task started. Analysis and AI artifacts are being written to the isolated workspace.",
        "progress.complete": "Translation complete",
        "progress.complete_body": "The verified language package and RPA archive are ready.",
        "progress.failed": "Translation stopped",
        "progress.failed_body": "The workspace, checkpoints, and diagnostics were kept so the task can be retried.",
        "dialog.cannot_continue": "Cannot continue",
        "dialog.cannot_start": "Cannot start",
        "dialog.complete": "RenWeave finished",
        "dialog.failed": "Translation failed",
    },
    "zh": {
        "app_title": "RenWeave / 织译",
        "app_subtitle": "理解上下文的 Ren'Py 翻译：从游戏文件到通过验证的语言包。",
        "language": "界面语言",
        "steps.model": "模型",
        "steps.game": "游戏",
        "steps.languages": "语言",
        "steps.review": "确认",
        "steps.progress": "进度",
        "step_count": "第 {current} 步，共 {total} 步",
        "back": "返回",
        "continue": "继续",
        "start": "开始一键翻译",
        "close": "关闭",
        "retry": "重试",
        "cancel": "取消",
        "browse": "选择",
        "optional": "可选",
        "model.title": "连接 AI 模型",
        "model.body": "这是选择游戏前唯一需要完成的设置。织译会检查 API、载入模型列表，并验证你选择的模型。",
        "model.api_type": "API 类型",
        "model.api_type_value": "OpenAI 兼容接口",
        "model.provider": "服务名称",
        "model.endpoint": "基础 URL",
        "model.key": "API 密钥",
        "model.key_hint": "仅保留在本次运行的内存中，不会写入配置文件。",
        "model.show_key": "显示密钥",
        "model.import": "导入配置",
        "model.connect": "连接并获取模型",
        "model.model": "模型",
        "model.model_hint": "选择获取到的模型，或输入准确的模型 ID。",
        "model.verify": "验证所选模型",
        "model.idle": "尚未连接",
        "model.idle_body": "填写接口地址和 API 密钥，然后获取可用模型。",
        "model.connecting": "正在连接",
        "model.connecting_body": "正在检查模型接口…",
        "model.connected": "API 已连接",
        "model.connected_body": "在 {latency} 毫秒内获取到 {count} 个模型。请验证所选模型后继续。",
        "model.verifying": "正在验证模型",
        "model.verifying_body": "正在执行一次最小化聊天请求…",
        "model.verified": "模型已就绪",
        "model.verified_body": "{model} 已在 {latency} 毫秒内成功响应。",
        "model.failed": "连接失败",
        "model.changed": "设置已更改",
        "model.changed_body": "继续前请重新连接并验证模型。",
        "model.import_title": "导入 OpenAI 兼容模型配置",
        "model.import_error": "无法导入配置",
        "model.required": "请先连接 API 并验证模型。",
        "game.title": "选择 Ren'Py 游戏",
        "game.body": "织译在独立工作区运行，分析过程中不会修改游戏源文件。",
        "game.project": "游戏目录或程序",
        "game.project_hint": "可选择游戏根目录、game 目录或游戏程序。",
        "game.workspace": "工作区",
        "game.workspace_hint": "分析结果、缓存、检查点和输出包都会保存在这里。",
        "game.advanced": "引擎验证",
        "game.sdk": "Ren'Py SDK",
        "game.sdk_hint": "可选。选择 SDK 后可用引擎编译检查生成脚本。",
        "game.require_engine": "打包前必须通过 Ren'Py 引擎验证",
        "game.project_picker": "选择 Ren'Py 游戏根目录或 game 目录",
        "game.workspace_picker": "选择或创建织译工作区",
        "game.sdk_picker": "选择 Ren'Py SDK 目录",
        "game.invalid": "请先选择有效的游戏和工作区。",
        "languages.title": "选择源语言和目标语言",
        "languages.body": "支持任意语言名称或区域代码；默认自动识别源语言。",
        "languages.source": "源语言",
        "languages.target": "目标语言",
        "languages.auto": "自动识别",
        "languages.hint": "例如：English、简体中文、Deutsch、pt-BR、日本語。",
        "languages.required": "请输入目标语言后再继续。",
        "review.title": "确认并开始",
        "review.body": "织译会自动完成全部流程，不要求手工填写术语表、角色名单或世界观资料。",
        "review.model": "AI 模型",
        "review.game": "游戏与工作区",
        "review.languages": "翻译语言",
        "review.options": "输出选项",
        "review.install": "完成后将语言包安装到游戏",
        "review.install_hint": "默认关闭；打包输出始终会保留在工作区。",
        "review.engine_yes": "必须通过 Ren'Py 引擎验证",
        "review.engine_no": "使用内置验证",
        "review.key_safe": "API 密钥仅保留在内存",
        "progress.title": "正在翻译",
        "progress.body": "可在这里查看每个阶段；检查点让中断后的任务能够恢复。",
        "progress.ready": "正在准备一键翻译流程…",
        "progress.scenes": "已完成 {count} 个场景",
        "progress.log": "活动记录",
        "progress.started": "任务已启动，分析结果与 AI 产物正在写入独立工作区。",
        "progress.complete": "翻译完成",
        "progress.complete_body": "已生成通过验证的语言包和 RPA 归档。",
        "progress.failed": "翻译已停止",
        "progress.failed_body": "工作区、检查点与诊断信息均已保留，可以重试。",
        "dialog.cannot_continue": "无法继续",
        "dialog.cannot_start": "无法开始",
        "dialog.complete": "织译已完成",
        "dialog.failed": "翻译失败",
    },
}


class Colors:
    PRIMARY = "#0B57D0"
    ON_PRIMARY = "#FFFFFF"
    PRIMARY_CONTAINER = "#D3E3FD"
    ON_PRIMARY_CONTAINER = "#041E49"
    SURFACE = "#F8FAFD"
    SURFACE_CONTAINER = "#F0F4F9"
    SURFACE_HIGH = "#E9EEF6"
    CARD = "#FFFFFF"
    ON_SURFACE = "#1F1F1F"
    ON_SURFACE_VARIANT = "#444746"
    OUTLINE = "#747775"
    OUTLINE_VARIANT = "#C4C7C5"
    SUCCESS = "#146C2E"
    SUCCESS_CONTAINER = "#C4EED0"
    ERROR = "#B3261E"
    ERROR_CONTAINER = "#F9DEDC"


class MaterialDialog:
    """A small, consistent modal used instead of platform-mixed message boxes."""

    def __init__(self, app: "RenWeaveDesktopApp", title: str, body: str, *, error: bool = False) -> None:
        tk, ttk = app.tk, app.ttk
        self.window = tk.Toplevel(app.root)
        self.window.title(title)
        self.window.configure(background=Colors.SURFACE)
        self.window.resizable(False, False)
        self.window.transient(app.root)
        self.window.grab_set()
        card = ttk.Frame(self.window, style="Dialog.TFrame", padding=24)
        card.grid(sticky="nsew")
        ttk.Label(card, text=title, style="DialogTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            card,
            text=body,
            style="ErrorBody.TLabel" if error else "Body.TLabel",
            wraplength=440,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(12, 24))
        close = ttk.Button(card, text=app.t("close"), style="Primary.TButton", command=self.window.destroy)
        close.grid(row=2, column=0, sticky="e")
        self.window.bind("<Escape>", lambda _event: self.window.destroy())
        self.window.bind("<Return>", lambda _event: self.window.destroy())
        self.window.protocol("WM_DELETE_WINDOW", self.window.destroy)
        self.window.update_idletasks()
        x = app.root.winfo_rootx() + max(0, (app.root.winfo_width() - self.window.winfo_width()) // 2)
        y = app.root.winfo_rooty() + max(0, (app.root.winfo_height() - self.window.winfo_height()) // 2)
        self.window.geometry(f"+{x}+{y}")
        close.focus_set()


class RenWeaveDesktopApp:
    STEPS = ("model", "game", "languages", "review", "progress")
    LANGUAGE_CHOICES = ("English", "简体中文", "繁體中文", "日本語", "한국어", "Deutsch", "Français", "Español", "Português", "Русский")

    def __init__(self, root, *, initial_project: str = "", initial_workspace: str = "") -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.root.title("RenWeave")
        self.root.geometry("1080x760")
        self.root.minsize(920, 680)
        self.root.configure(background=Colors.SURFACE)

        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.step = 0
        self.last_stage = ""
        self.logs: list[str] = []
        self.connection_state = "idle"
        self.connection_detail: dict[str, object] = {}
        self._suspend_provider_trace = False

        self.locale = tk.StringVar(value="en")
        self.locale_display = tk.StringVar(value="English")
        self.project = tk.StringVar(value=initial_project)
        self.workspace = tk.StringVar(value=initial_workspace)
        self.provider = tk.StringVar()
        self.provider_name = tk.StringVar(value="OpenAI-compatible")
        self.base_url = tk.StringVar(value="https://api.openai.com/v1")
        self.model = tk.StringVar()
        self.model_choices: tuple[str, ...] = ()
        self.api_key = tk.StringVar()
        self.show_key = tk.BooleanVar(value=False)
        self.source_language = tk.StringVar(value="auto")
        self.target_language = tk.StringVar()
        self.renpy_sdk = tk.StringVar()
        self.install = tk.BooleanVar(value=False)
        self.require_engine = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value=self.t("model.idle_body"))

        self.start_button = None
        self.next_button = None
        self.back_button = None
        self.progress = None
        self.log = None
        self.content = None
        self.nav = None
        self._configure_styles()
        self._build_shell()
        self._bind_provider_changes()
        if initial_project and not initial_workspace:
            self._suggest_workspace(initial_project)
        self._render()
        self.root.after(150, self._poll_events)

    def t(self, key: str, **values: object) -> str:
        text = COPY[self.locale.get()].get(key, COPY["en"].get(key, key))
        return text.format(**values) if values else text

    def _configure_styles(self) -> None:
        style = self.ttk.Style(self.root)
        # Clam consistently honors semantic color tokens on Windows. Vista may
        # retain a native white button surface while applying our white label.
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("App.TFrame", background=Colors.SURFACE)
        style.configure("TopBar.TFrame", background=Colors.SURFACE, padding=(28, 18))
        style.configure("Nav.TFrame", background=Colors.SURFACE_CONTAINER, padding=(16, 20))
        style.configure("Content.TFrame", background=Colors.SURFACE, padding=(32, 24))
        style.configure("Card.TFrame", background=Colors.CARD, relief="solid", borderwidth=1)
        style.configure("TintCard.TFrame", background=Colors.SURFACE_CONTAINER, relief="flat")
        style.configure("Dialog.TFrame", background=Colors.CARD)
        style.configure("Title.TLabel", background=Colors.SURFACE, foreground=Colors.ON_SURFACE, font=("Segoe UI", 22, "bold"))
        style.configure("Subtitle.TLabel", background=Colors.SURFACE, foreground=Colors.ON_SURFACE_VARIANT, font=("Segoe UI", 10))
        style.configure("Headline.TLabel", background=Colors.SURFACE, foreground=Colors.ON_SURFACE, font=("Segoe UI", 20, "bold"))
        style.configure("Section.TLabel", background=Colors.CARD, foreground=Colors.ON_SURFACE, font=("Segoe UI", 12, "bold"))
        style.configure("Body.TLabel", background=Colors.CARD, foreground=Colors.ON_SURFACE_VARIANT, font=("Segoe UI", 10))
        style.configure("SurfaceBody.TLabel", background=Colors.SURFACE, foreground=Colors.ON_SURFACE_VARIANT, font=("Segoe UI", 10))
        style.configure("Hint.TLabel", background=Colors.CARD, foreground=Colors.ON_SURFACE_VARIANT, font=("Segoe UI", 9))
        style.configure("Field.TLabel", background=Colors.CARD, foreground=Colors.ON_SURFACE, font=("Segoe UI", 10, "bold"))
        style.configure("DialogTitle.TLabel", background=Colors.CARD, foreground=Colors.ON_SURFACE, font=("Segoe UI", 16, "bold"))
        style.configure("ErrorBody.TLabel", background=Colors.CARD, foreground=Colors.ERROR, font=("Segoe UI", 10))
        style.configure("Status.TLabel", background=Colors.SURFACE_CONTAINER, foreground=Colors.ON_SURFACE, font=("Segoe UI", 10, "bold"))
        style.configure("StatusBody.TLabel", background=Colors.SURFACE_CONTAINER, foreground=Colors.ON_SURFACE_VARIANT, font=("Segoe UI", 9))
        style.configure("Nav.TButton", anchor="w", padding=(14, 12), font=("Segoe UI", 10), borderwidth=0)
        style.configure("NavCurrent.TButton", anchor="w", padding=(14, 12), font=("Segoe UI", 10, "bold"), background=Colors.PRIMARY_CONTAINER, foreground=Colors.ON_PRIMARY_CONTAINER, borderwidth=0)
        style.map("NavCurrent.TButton", background=[("active", Colors.PRIMARY_CONTAINER)])
        style.configure("Primary.TButton", padding=(18, 10), font=("Segoe UI", 10, "bold"), foreground=Colors.ON_PRIMARY, background=Colors.PRIMARY)
        style.map("Primary.TButton", background=[("active", "#0842A0"), ("disabled", Colors.OUTLINE_VARIANT)])
        style.configure("Secondary.TButton", padding=(16, 9), font=("Segoe UI", 10), foreground=Colors.PRIMARY)
        style.configure("Quiet.TButton", padding=(12, 8), font=("Segoe UI", 9))
        style.configure("Material.TEntry", padding=(10, 9), font=("Segoe UI", 10))
        style.configure("Material.TCombobox", padding=(10, 8), font=("Segoe UI", 10))
        style.configure("Material.TCheckbutton", background=Colors.CARD, foreground=Colors.ON_SURFACE, font=("Segoe UI", 10))
        style.configure("Horizontal.TProgressbar", background=Colors.PRIMARY, troughcolor=Colors.SURFACE_HIGH)

    def _build_shell(self) -> None:
        self.shell = self.ttk.Frame(self.root, style="App.TFrame")
        self.shell.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        self.shell.columnconfigure(1, weight=1)
        self.shell.rowconfigure(1, weight=1)

        top = self.ttk.Frame(self.shell, style="TopBar.TFrame")
        top.grid(row=0, column=0, columnspan=2, sticky="ew")
        top.columnconfigure(1, weight=1)
        self.brand_title = self.ttk.Label(top, style="Title.TLabel")
        self.brand_title.grid(row=0, column=0, sticky="w")
        self.brand_subtitle = self.ttk.Label(top, style="Subtitle.TLabel")
        self.brand_subtitle.grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 0))
        language_box = self.ttk.Frame(top, style="App.TFrame")
        language_box.grid(row=0, column=2, rowspan=2, sticky="e")
        self.language_label = self.ttk.Label(language_box, style="SurfaceBody.TLabel")
        self.language_label.grid(row=0, column=0, sticky="e", padx=(0, 10))
        chooser = self.ttk.Combobox(
            language_box,
            textvariable=self.locale_display,
            values=("English", "简体中文"),
            state="readonly",
            width=12,
            style="Material.TCombobox",
        )
        chooser.grid(row=0, column=1)
        chooser.bind("<<ComboboxSelected>>", self._change_locale)

        self.nav = self.ttk.Frame(self.shell, style="Nav.TFrame", width=220)
        self.nav.grid(row=1, column=0, sticky="nsew")
        self.nav.grid_propagate(False)
        self.content = self.ttk.Frame(self.shell, style="Content.TFrame")
        self.content.grid(row=1, column=1, sticky="nsew")
        self.content.columnconfigure(0, weight=1)
        self.content.rowconfigure(1, weight=1)

        self.footer = self.ttk.Frame(self.shell, style="TopBar.TFrame")
        self.footer.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.footer.columnconfigure(1, weight=1)

    def _change_locale(self, _event=None) -> None:
        self.locale.set("zh" if self.locale_display.get() == "简体中文" else "en")
        self._render()

    def _render(self) -> None:
        self.brand_title.configure(text=self.t("app_title"))
        self.brand_subtitle.configure(text=self.t("app_subtitle"))
        self.language_label.configure(text=self.t("language"))
        for parent in (self.nav, self.content, self.footer):
            for child in parent.winfo_children():
                child.destroy()
        self._render_nav()
        self._render_header()
        getattr(self, f"_render_{self.STEPS[self.step]}")()
        self._render_footer()

    def _render_nav(self) -> None:
        for index, step in enumerate(self.STEPS):
            style = "NavCurrent.TButton" if index == self.step else "Nav.TButton"
            prefix = f"{index + 1:02d}"
            button = self.ttk.Button(
                self.nav,
                text=f"{prefix}   {self.t(f'steps.{step}')}",
                style=style,
                command=lambda selected=index: self._go_to_step(selected),
                state="normal" if index <= self.step and self.step < 4 else "disabled",
            )
            button.grid(row=index, column=0, sticky="ew", pady=3)
        self.nav.columnconfigure(0, weight=1)

    def _render_header(self) -> None:
        step_name = self.STEPS[self.step]
        self.ttk.Label(
            self.content,
            text=self.t("step_count", current=self.step + 1, total=len(self.STEPS)),
            style="SurfaceBody.TLabel",
        ).grid(row=0, column=0, sticky="w")
        body = self.ttk.Frame(self.content, style="App.TFrame")
        body.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)
        self.page = body
        self.ttk.Label(body, text=self.t(f"{step_name}.title"), style="Headline.TLabel").grid(row=0, column=0, sticky="w")
        self.ttk.Label(
            body,
            text=self.t(f"{step_name}.body"),
            style="SurfaceBody.TLabel",
            wraplength=720,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(6, 18))

    def _card(self, parent=None, *, row: int = 2, padding: int = 20):
        card = self.ttk.Frame(parent or self.page, style="Card.TFrame", padding=padding)
        card.grid(row=row, column=0, sticky="nsew")
        card.columnconfigure(0, weight=1)
        return card

    def _field(self, parent, row: int, label: str, variable, *, hint: str = "", secret: bool = False):
        self.ttk.Label(parent, text=label, style="Field.TLabel").grid(row=row, column=0, sticky="w", pady=(0 if row == 0 else 14, 5))
        entry = self.ttk.Entry(parent, textvariable=variable, style="Material.TEntry", show="•" if secret and not self.show_key.get() else "")
        entry.grid(row=row + 1, column=0, sticky="ew")
        if hint:
            self.ttk.Label(parent, text=hint, style="Hint.TLabel", wraplength=680, justify="left").grid(row=row + 2, column=0, sticky="w", pady=(4, 0))
        return entry

    def _render_model(self) -> None:
        card = self._card()
        card.columnconfigure(0, weight=1)
        card.columnconfigure(1, weight=1)

        self.ttk.Label(card, text=self.t("model.api_type"), style="Field.TLabel").grid(row=0, column=0, sticky="w")
        self.ttk.Label(card, text=self.t("model.api_type_value"), style="Body.TLabel").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.ttk.Button(card, text=self.t("model.import"), style="Secondary.TButton", command=self._browse_provider).grid(row=0, column=1, rowspan=2, sticky="e")

        left = self.ttk.Frame(card, style="Card.TFrame")
        left.grid(row=2, column=0, sticky="nsew", padx=(0, 12), pady=(18, 0))
        left.columnconfigure(0, weight=1)
        self._field(left, 0, self.t("model.provider"), self.provider_name)
        self._field(left, 3, self.t("model.endpoint"), self.base_url)
        self.api_key_entry = self._field(left, 6, self.t("model.key"), self.api_key, hint=self.t("model.key_hint"), secret=True)
        self.ttk.Checkbutton(left, text=self.t("model.show_key"), variable=self.show_key, style="Material.TCheckbutton", command=self._toggle_key).grid(row=9, column=0, sticky="w", pady=(8, 0))
        self.connect_button = self.ttk.Button(left, text=self.t("model.connect"), style="Primary.TButton", command=self._connect_models)
        self.connect_button.grid(row=10, column=0, sticky="w", pady=(18, 0))

        right = self.ttk.Frame(card, style="Card.TFrame")
        right.grid(row=2, column=1, sticky="nsew", padx=(12, 0), pady=(18, 0))
        right.columnconfigure(0, weight=1)
        self.ttk.Label(right, text=self.t("model.model"), style="Field.TLabel").grid(row=0, column=0, sticky="w")
        self.model_box = self.ttk.Combobox(right, textvariable=self.model, values=self.model_choices, style="Material.TCombobox")
        self.model_box.grid(row=1, column=0, sticky="ew", pady=(5, 0))
        self.ttk.Label(right, text=self.t("model.model_hint"), style="Hint.TLabel", wraplength=320, justify="left").grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.verify_button = self.ttk.Button(right, text=self.t("model.verify"), style="Secondary.TButton", command=self._verify_model)
        self.verify_button.grid(row=3, column=0, sticky="w", pady=(14, 0))
        status_card = self.ttk.Frame(right, style="TintCard.TFrame", padding=16)
        status_card.grid(row=4, column=0, sticky="ew", pady=(18, 0))
        state_key = self.connection_state if self.connection_state in {"idle", "connecting", "connected", "verifying", "verified", "failed", "changed"} else "idle"
        title = self.t(f"model.{state_key}")
        detail_key = f"model.{state_key}_body"
        details = dict(self.connection_detail)
        body = self.t(detail_key, **details) if detail_key in COPY[self.locale.get()] else str(details.get("message", ""))
        if state_key == "failed":
            body = str(details.get("message", ""))
        self.model_status_title = self.ttk.Label(status_card, text=title, style="Status.TLabel")
        self.model_status_title.grid(row=0, column=0, sticky="w")
        self.model_status_body = self.ttk.Label(status_card, text=body, style="StatusBody.TLabel", wraplength=320, justify="left")
        self.model_status_body.grid(row=1, column=0, sticky="w", pady=(5, 0))
        busy = self.connection_state in {"connecting", "verifying"}
        if busy:
            self.connect_button.configure(state="disabled")
            self.verify_button.configure(state="disabled")
        elif self.connection_state not in {"connected", "verified", "changed"}:
            self.verify_button.configure(state="disabled")

    def _toggle_key(self) -> None:
        if hasattr(self, "api_key_entry"):
            self.api_key_entry.configure(show="" if self.show_key.get() else "•")

    def _render_game(self) -> None:
        card = self._card()
        self._path_field(card, 0, self.t("game.project"), self.project, self.t("game.project_hint"), self._browse_project)
        self._path_field(card, 3, self.t("game.workspace"), self.workspace, self.t("game.workspace_hint"), self._browse_workspace)
        self.ttk.Label(card, text=self.t("game.advanced"), style="Section.TLabel").grid(row=6, column=0, sticky="w", pady=(20, 8))
        self._path_field(card, 7, self.t("game.sdk"), self.renpy_sdk, self.t("game.sdk_hint"), self._browse_sdk)
        self.ttk.Checkbutton(card, text=self.t("game.require_engine"), variable=self.require_engine, style="Material.TCheckbutton").grid(row=10, column=0, sticky="w", pady=(12, 0))

    def _path_field(self, parent, row: int, label: str, variable, hint: str, command: Callable[[], None]) -> None:
        self.ttk.Label(parent, text=label, style="Field.TLabel").grid(row=row, column=0, sticky="w")
        line = self.ttk.Frame(parent, style="Card.TFrame")
        line.grid(row=row + 1, column=0, sticky="ew", pady=(5, 0))
        line.columnconfigure(0, weight=1)
        self.ttk.Entry(line, textvariable=variable, style="Material.TEntry").grid(row=0, column=0, sticky="ew")
        self.ttk.Button(line, text=self.t("browse"), style="Secondary.TButton", command=command).grid(row=0, column=1, padx=(10, 0))
        self.ttk.Label(parent, text=hint, style="Hint.TLabel", wraplength=680, justify="left").grid(row=row + 2, column=0, sticky="w", pady=(4, 0))

    def _render_languages(self) -> None:
        card = self._card()
        card.columnconfigure(0, weight=1)
        card.columnconfigure(1, weight=1)
        self.ttk.Label(card, text=self.t("languages.source"), style="Field.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 12))
        source_values = ("auto",) + self.LANGUAGE_CHOICES
        self.ttk.Combobox(card, textvariable=self.source_language, values=source_values, style="Material.TCombobox").grid(row=1, column=0, sticky="ew", padx=(0, 12), pady=(5, 0))
        self.ttk.Label(card, text=self.t("languages.target"), style="Field.TLabel").grid(row=0, column=1, sticky="w", padx=(12, 0))
        target = self.ttk.Combobox(card, textvariable=self.target_language, values=self.LANGUAGE_CHOICES, style="Material.TCombobox")
        target.grid(row=1, column=1, sticky="ew", padx=(12, 0), pady=(5, 0))
        self.ttk.Label(card, text=self.t("languages.hint"), style="Hint.TLabel", wraplength=680, justify="left").grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        if not self.target_language.get():
            target.focus_set()

    def _render_review(self) -> None:
        card = self._card()
        summaries = (
            (self.t("review.model"), f"{self.provider_name.get()}  ·  {self.model.get()}\n{self.base_url.get()}"),
            (self.t("review.game"), f"{self.project.get()}\n{self.workspace.get()}"),
            (self.t("review.languages"), f"{self.source_language.get() or 'auto'}  →  {self.target_language.get()}"),
            (self.t("review.options"), self.t("review.engine_yes") if self.require_engine.get() else self.t("review.engine_no")),
        )
        for row, (title, body) in enumerate(summaries):
            summary = self.ttk.Frame(card, style="TintCard.TFrame", padding=14)
            summary.grid(row=row, column=0, sticky="ew", pady=(0 if row == 0 else 8, 0))
            summary.columnconfigure(0, weight=1)
            self.ttk.Label(summary, text=title, style="Status.TLabel").grid(row=0, column=0, sticky="w")
            self.ttk.Label(summary, text=body, style="StatusBody.TLabel", wraplength=650, justify="left").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.ttk.Checkbutton(card, text=self.t("review.install"), variable=self.install, style="Material.TCheckbutton").grid(row=4, column=0, sticky="w", pady=(18, 0))
        self.ttk.Label(card, text=self.t("review.install_hint"), style="Hint.TLabel").grid(row=5, column=0, sticky="w", pady=(4, 0))
        self.ttk.Label(card, text=self.t("review.key_safe"), style="Hint.TLabel").grid(row=6, column=0, sticky="w", pady=(14, 0))

    def _render_progress(self) -> None:
        card = self._card()
        self.ttk.Label(card, textvariable=self.status, style="Section.TLabel", wraplength=690).grid(row=0, column=0, sticky="w")
        self.progress = self.ttk.Progressbar(card, mode="determinate", maximum=max(1, len(STAGE_LABELS) - 1))
        self.progress.grid(row=1, column=0, sticky="ew", pady=(12, 18))
        if self.last_stage in STAGE_LABELS:
            self.progress["value"] = list(STAGE_LABELS).index(self.last_stage)
        self.ttk.Label(card, text=self.t("progress.log"), style="Field.TLabel").grid(row=2, column=0, sticky="w")
        log_frame = self.ttk.Frame(card, style="Card.TFrame")
        log_frame.grid(row=3, column=0, sticky="nsew", pady=(6, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        card.rowconfigure(3, weight=1)
        self.log = self.tk.Text(
            log_frame,
            height=13,
            wrap="word",
            state="normal",
            borderwidth=1,
            relief="solid",
            background=Colors.SURFACE_CONTAINER,
            foreground=Colors.ON_SURFACE,
            font=("Cascadia Mono", 9),
            padx=12,
            pady=10,
        )
        self.log.grid(row=0, column=0, sticky="nsew")
        scrollbar = self.ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scrollbar.set)
        for line in self.logs:
            self.log.insert("end", line.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _render_footer(self) -> None:
        if self.step > 0 and self.step < 4:
            self.back_button = self.ttk.Button(self.footer, text=self.t("back"), style="Secondary.TButton", command=lambda: self._go_to_step(self.step - 1))
            self.back_button.grid(row=0, column=0, sticky="w")
        action_text = self.t("start") if self.step == 3 else self.t("continue")
        if self.step < 4:
            command = self._start if self.step == 3 else self._continue
            self.next_button = self.ttk.Button(self.footer, text=action_text, style="Primary.TButton", command=command)
            self.next_button.grid(row=0, column=2, sticky="e")
            if self.step == 3:
                self.start_button = self.next_button
            if self.step == 0 and self.connection_state != "verified":
                self.next_button.configure(state="disabled")

    def _bind_provider_changes(self) -> None:
        for variable in (self.provider_name, self.base_url, self.api_key, self.model):
            variable.trace_add("write", self._provider_changed)

    def _provider_changed(self, *_args) -> None:
        if self._suspend_provider_trace or self.connection_state in {"idle", "connecting", "verifying"}:
            return
        self.connection_state = "changed"
        self.connection_detail = {}
        if self.step == 0:
            # Update state in place so typing never destroys the focused field.
            if self.next_button is not None:
                self.next_button.configure(state="disabled")
            if hasattr(self, "verify_button"):
                self.verify_button.configure(state="normal")
            if hasattr(self, "model_status_title"):
                self.model_status_title.configure(text=self.t("model.changed"))
                self.model_status_body.configure(text=self.t("model.changed_body"))

    def _profile(self, *, require_model: bool = False) -> ModelProfile:
        profile = ModelProfile(
            name=self.provider_name.get().strip(),
            model=self.model.get().strip(),
            base_url=self.base_url.get().strip(),
            api_key=self.api_key.get(),
            max_retries=2,
            retry_base_seconds=0.5,
        )
        profile.validate() if require_model else profile.validate_connection()
        return profile

    def _connect_models(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            profile = self._profile()
        except (TypeError, ValueError) as exc:
            self._dialog(self.t("model.failed"), str(exc), error=True)
            return
        self.connection_state = "connecting"
        self.connection_detail = {}
        self._render()

        def run() -> None:
            try:
                self.events.put(("catalog", OpenAICompatibleCatalog(profile).list_models()))
            except BaseException as exc:
                self.events.put(("connection_error", exc))

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    def _verify_model(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            profile = self._profile(require_model=True)
        except (TypeError, ValueError) as exc:
            self._dialog(self.t("model.failed"), str(exc), error=True)
            return
        self.connection_state = "verifying"
        self.connection_detail = {}
        self._render()

        def run() -> None:
            try:
                self.events.put(("verified", OpenAICompatibleCatalog(profile).verify_model()))
            except BaseException as exc:
                self.events.put(("connection_error", exc))

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    def _browse_provider(self) -> None:
        from tkinter import filedialog

        selected = filedialog.askopenfilename(
            title=self.t("model.import_title"),
            filetypes=(("JSON", "*.json"), ("All files", "*.*")),
        )
        if not selected:
            return
        try:
            profile = ModelProfile.load(selected)
            profile.validate_connection()
        except (OSError, ValueError, TypeError) as exc:
            self._dialog(self.t("model.import_error"), str(exc), error=True)
            return
        self._suspend_provider_trace = True
        try:
            self.provider.set(selected)
            self.provider_name.set(profile.name)
            self.base_url.set(profile.base_url)
            self.model.set(profile.model)
            if profile.api_key:
                self.api_key.set(profile.api_key)
        finally:
            self._suspend_provider_trace = False
        self.connection_state = "changed"
        self.connection_detail = {}
        self._render()

    def _browse_project(self) -> None:
        from tkinter import filedialog

        selected = filedialog.askdirectory(title=self.t("game.project_picker"))
        if selected:
            self.project.set(selected)
            if not self.workspace.get().strip():
                self._suggest_workspace(selected)

    def _suggest_workspace(self, project: str) -> None:
        source = Path(project).expanduser()
        name = source.parent.name if source.name.casefold() == "game" else source.name
        base = Path.home() / "Documents" / "RenWeaveWork"
        self.workspace.set(str(base / (name or "project")))

    def _browse_workspace(self) -> None:
        from tkinter import filedialog

        selected = filedialog.askdirectory(title=self.t("game.workspace_picker"), mustexist=False)
        if selected:
            self.workspace.set(selected)

    def _browse_sdk(self) -> None:
        from tkinter import filedialog

        selected = filedialog.askdirectory(title=self.t("game.sdk_picker"))
        if selected:
            self.renpy_sdk.set(selected)

    def _go_to_step(self, selected: int) -> None:
        if 0 <= selected <= self.step and self.step < 4:
            self.step = selected
            self._render()

    def _continue(self) -> None:
        if self.step == 0:
            if self.connection_state != "verified":
                self._dialog(self.t("dialog.cannot_continue"), self.t("model.required"), error=True)
                return
        elif self.step == 1:
            if not Path(self.project.get().strip()).expanduser().exists() or not self.workspace.get().strip():
                self._dialog(self.t("dialog.cannot_continue"), self.t("game.invalid"), error=True)
                return
            if self.renpy_sdk.get().strip() and not Path(self.renpy_sdk.get().strip()).expanduser().exists():
                self._dialog(self.t("dialog.cannot_continue"), self.t("game.invalid"), error=True)
                return
        elif self.step == 2 and not self.target_language.get().strip():
            self._dialog(self.t("dialog.cannot_continue"), self.t("languages.required"), error=True)
            return
        self.step = min(3, self.step + 1)
        self._render()

    def _persist_profile(self) -> Path:
        workspace = Path(self.workspace.get().strip()).expanduser()
        target = workspace / ".renweave" / "provider.json"
        self._profile(require_model=True).save(target)
        self.provider.set(str(target))
        return target

    def _request(self) -> TranslationRequest:
        provider = self._persist_profile()
        return TranslationRequest(
            project=self.project.get().strip(),
            workspace=self.workspace.get().strip(),
            provider=str(provider),
            source_language=self.source_language.get().strip() or "auto",
            target_language=self.target_language.get().strip(),
            api_key=self.api_key.get(),
            install=self.install.get(),
            renpy_sdk=self.renpy_sdk.get().strip(),
            require_engine_validation=self.require_engine.get(),
        )

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            if self.connection_state != "verified":
                raise ValueError(self.t("model.required"))
            request = self._request()
            request.validate()
        except (OSError, ValueError, TypeError) as exc:
            self._dialog(self.t("dialog.cannot_start"), str(exc), error=True)
            return
        self.step = 4
        self.status.set(self.t("progress.ready"))
        self.last_stage = ""
        self.logs = []
        self._append_log(self.t("progress.started"))
        self._render()
        self.worker = threading.Thread(target=self._run_worker, args=(request,), daemon=True)
        self.worker.start()

    def _run_worker(self, request: TranslationRequest) -> None:
        try:
            self.events.put(("complete", execute_translation(request)))
        except BaseException as exc:
            self.events.put(("translation_error", exc))

    def _poll_events(self) -> None:
        if self.step == 4 and self.worker and self.worker.is_alive():
            self._read_pipeline_state()
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "catalog":
                catalog = value
                assert isinstance(catalog, ModelCatalog)
                self.model_choices = catalog.models
                self._suspend_provider_trace = True
                try:
                    if self.model.get().strip() not in catalog.models:
                        self.model.set(catalog.models[0])
                finally:
                    self._suspend_provider_trace = False
                self.connection_state = "connected"
                self.connection_detail = {"count": len(catalog.models), "latency": catalog.latency_ms}
                self._render()
            elif kind == "verified":
                verified = value
                assert isinstance(verified, ModelVerification)
                self.connection_state = "verified"
                self.connection_detail = {"model": verified.model, "latency": verified.latency_ms}
                self._render()
            elif kind == "connection_error":
                self.connection_state = "failed"
                self.connection_detail = {"message": str(value)}
                self._render()
            elif kind == "complete":
                state = value
                self.last_stage = "complete"
                self.status.set(self.t("progress.complete"))
                self._append_log(f"Package: {state.package_path}")
                if state.installed_dir:
                    self._append_log(f"Installed: {state.installed_dir}")
                self._render()
                self._dialog(self.t("dialog.complete"), f"{self.t('progress.complete_body')}\n\n{state.package_path}")
            elif kind == "translation_error":
                self.last_stage = "failed"
                self.status.set(self.t("progress.failed"))
                self._append_log(f"Error: {value}")
                self._render()
                self._dialog(self.t("dialog.failed"), f"{self.t('progress.failed_body')}\n\n{value}", error=True)
        try:
            if self.root.winfo_exists():
                self.root.after(150, self._poll_events)
        except self.tk.TclError:
            pass

    def _read_pipeline_state(self) -> None:
        state_path = Path(self.workspace.get().strip()) / "state.json"
        if not state_path.is_file():
            return
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8-sig"))
            stage = str(payload.get("stage", ""))
            if not stage or stage == self.last_stage:
                return
            self.last_stage = stage
            labels = STAGE_LABELS_ZH if self.locale.get() == "zh" else STAGE_LABELS
            label = labels.get(stage, stage)
            completed = len(payload.get("completed_scene_ids", []))
            self.status.set(f"{label} · {self.t('progress.scenes', count=completed)}")
            self._append_log(label)
            if self.progress is not None and stage in STAGE_LABELS:
                self.progress["value"] = list(STAGE_LABELS).index(stage)
        except (OSError, ValueError, TypeError):
            pass

    def _append_log(self, text: str) -> None:
        self.logs.append(text.rstrip())
        if self.log is not None and self.log.winfo_exists():
            self.log.configure(state="normal")
            self.log.insert("end", text.rstrip() + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")

    def _dialog(self, title: str, body: str, *, error: bool = False) -> MaterialDialog:
        return MaterialDialog(self, title, body, error=error)


def launch_gui(*, initial_project: str = "", initial_workspace: str = "") -> int:
    try:
        import tkinter as tk
    except ImportError as exc:
        raise RuntimeError("Tk is not installed for this Python environment") from exc
    try:
        root = tk.Tk()
    except Exception as exc:
        raise RuntimeError(f"Unable to start the desktop interface: {exc}") from exc
    RenWeaveDesktopApp(root, initial_project=initial_project, initial_workspace=initial_workspace)
    root.mainloop()
    return 0


def main() -> int:
    return launch_gui()
