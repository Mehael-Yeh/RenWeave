from __future__ import annotations

import json
import math
import os
import queue
import subprocess
import sys
import tempfile
import threading
import traceback
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from . import __version__
from .build_validation import RenpySdkLocator
from .credentials import CredentialStorageError, SecureCredentialStore
from .discovery import ProjectDiscovery
from .existing_translations import (
    ExistingLanguageSummary,
    ExistingTranslationInventory,
    discover_existing_languages,
)
from .io import atomic_write_json, read_json
from .pipeline import PipelineStage, PipelineState, RenWeavePipeline
from .provider import ModelCatalog, ModelProfile, ModelVerification, OpenAICompatibleCatalog
from .provider_presets import PROVIDER_PRESETS, PROVIDER_PRESETS_BY_ID, get_provider_preset
from .runtime import CancellationToken
from .usage import TokenBudget, estimate_project_tokens
from .update_check import UpdateResult, check_for_updates


PROVIDER_SHORT_NAMES_EN = {
    "alibaba": "Alibaba Cloud",
    "custom": "Custom API",
    "custom2": "Custom API 2",
}


def _user_home_fallback() -> Path:
    """Resolve a writable user location even in stripped frozen environments."""
    candidates = [
        os.environ.get("USERPROFILE"),
        (
            f"{os.environ.get('HOMEDRIVE', '')}{os.environ.get('HOMEPATH', '')}"
            if os.environ.get("HOMEDRIVE") and os.environ.get("HOMEPATH")
            else None
        ),
    ]
    for candidate in candidates:
        if candidate:
            return Path(candidate)
    try:
        return Path.home()
    except (OSError, RuntimeError):
        return Path(tempfile.gettempdir())


def default_desktop_settings_path() -> Path:
    """Return the per-user, non-secret desktop settings path."""
    if os.name == "nt":
        configured = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        base = Path(configured) if configured else _user_home_fallback() / "AppData" / "Roaming"
    else:
        configured = os.environ.get("XDG_CONFIG_HOME")
        base = Path(configured) if configured else _user_home_fallback() / ".config"
    return base / "RenWeave" / "settings.json"


@dataclass(slots=True)
class TranslationRequest:
    project: str
    workspace: str
    provider: str
    source_language: str
    target_language: str
    api_key: str = ""
    generate_rpa: bool = True
    install: bool = False
    overwrite_existing: bool = False
    renpy_sdk: str = ""
    require_engine_validation: bool = False
    knowledge_consent: str = "auto"

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
    cancel_token: CancellationToken | None = None,
    progress_callback: Callable[[PipelineState], None] | None = None,
) -> PipelineState:
    """Execute the same pipeline as the CLI; the API key remains memory-only."""
    request.validate()
    profile = ModelProfile.load(request.provider)
    if request.api_key:
        profile.api_key = request.api_key
    profile.validate()
    pipeline = pipeline_factory(request.workspace)
    try:
        state = pipeline.translate(
            request.project,
            request.source_language.strip() or "auto",
            request.target_language.strip(),
            profile,
            install=request.install,
            overwrite_existing=request.overwrite_existing,
            generate_rpa=request.generate_rpa,
            renpy_sdk_path=request.renpy_sdk or None,
            require_engine_validation=request.require_engine_validation,
            knowledge_consent=request.knowledge_consent,
            cancel_token=cancel_token,
            progress_callback=progress_callback,
        )
    except BaseException as exc:
        pipeline.logger.exception("run_failed", exc)
        raise
    if state.stage == PipelineStage.PAUSED:
        return state
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
    "paused": "Translation paused safely",
    "failed": "Translation failed",
}

PROGRESS_STAGE_ORDER = tuple(
    str(stage)
    for stage in PipelineStage
    if stage not in {PipelineStage.PAUSED, PipelineStage.FAILED}
)

PROGRESS_STAGE_PHASE = {
    "created": "prepare",
    "discovered": "prepare",
    "acquired": "prepare",
    "decompiled": "prepare",
    "indexed": "prepare",
    "knowledge_ready": "understand",
    "synthesizing": "understand",
    "narrative_ready": "understand",
    "translating": "translate",
    "validated": "translate",
    "refining": "refine",
    "refined": "refine",
    "building": "build",
    "validating_build": "build",
    "complete": "done",
}


class TaskState(str, Enum):
    """Single UI-facing task state used by every progress-page component."""

    IDLE = "idle"
    PREPARING = "preparing"
    ANALYZING = "analyzing"
    TRANSLATING = "translating"
    VALIDATING = "validating"
    BUILDING = "building"
    COMPLETED = "completed"
    PAUSING = "pausing"
    PAUSED = "paused"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class TaskPresentation:
    state: TaskState
    pipeline_stage: str
    phase: str
    percent: float | None
    completed: int
    total: int


TASK_STATE_BY_STAGE = {
    "created": TaskState.PREPARING,
    "discovered": TaskState.PREPARING,
    "acquired": TaskState.PREPARING,
    "decompiled": TaskState.PREPARING,
    "indexed": TaskState.ANALYZING,
    "knowledge_ready": TaskState.ANALYZING,
    "synthesizing": TaskState.ANALYZING,
    "narrative_ready": TaskState.ANALYZING,
    "translating": TaskState.TRANSLATING,
    "validated": TaskState.VALIDATING,
    "refining": TaskState.VALIDATING,
    "refined": TaskState.VALIDATING,
    "building": TaskState.BUILDING,
    "validating_build": TaskState.BUILDING,
    "complete": TaskState.COMPLETED,
    "paused": TaskState.PAUSED,
    "failed": TaskState.FAILED,
}

TASK_PHASE = {
    TaskState.IDLE: "prepare",
    TaskState.PREPARING: "prepare",
    TaskState.ANALYZING: "analyze",
    TaskState.TRANSLATING: "translate",
    TaskState.VALIDATING: "validate",
    TaskState.BUILDING: "build",
    TaskState.COMPLETED: "done",
    TaskState.PAUSING: "translate",
    TaskState.PAUSED: "translate",
    TaskState.FAILED: "translate",
    TaskState.CANCELLED: "translate",
}

COPY: dict[str, dict[str, str]] = {"en": {}, "zh": {}}

COPY["en"].update({
    "workspace_label": "TRANSLATION WORKSPACE",
    "nav_privacy": "Private by default\nKeys use encrypted system storage. Game files remain local.",
    "settings.title": "Settings",
    "settings.body": "Control credential persistence and optional online update checks. These choices apply to this user account.",
    "settings.credentials": "API key storage",
    "settings.credentials_hint": "Secure storage uses the operating system credential service. API keys never enter RenWeave settings, project files, logs, or translation packages.",
    "settings.secure": "Encrypted system storage (recommended)",
    "settings.memory": "Memory only — cleared when RenWeave closes",
    "settings.forget_key": "Forget current key",
    "settings.key_error": "Credential storage error",
    "settings.key_save_failed": "The model is verified, but the operating system credential service could not save this key. It remains available only for the current session.",
    "settings.updates": "Version updates",
    "settings.updates_hint": "Automatic checks contact GitHub Releases after startup. They are disabled by default and never install anything automatically.",
    "settings.update_toggle": "Check for updates after startup",
    "settings.check_now": "Check now",
    "settings.current_version": "Installed version: {version}",
    "settings.update_title": "RenWeave update",
    "settings.update_available": "Version {latest} is available. You currently have {current}.",
    "settings.up_to_date": "RenWeave {current} is up to date. Latest release: {latest}.",
    "model.reasoning": "Thinking level",
    "model.reasoning_hint": "Auto follows the model default. Other levels are translated to the selected provider's supported reasoning controls.",
    "model.reasoning_unavailable": "This provider does not expose a supported reasoning-level control; the selected model decides automatically.",
    "reasoning.auto": "Automatic (provider default)",
    "reasoning.low": "Low",
    "reasoning.high": "High",
    "reasoning.maximum": "Maximum",
    "provider.choose": "Choose a provider",
    "provider.choose_hint": "Start with an official or aggregator preset. Every endpoint remains editable.",
    "provider.official": "Official",
    "provider.aggregator": "Aggregator",
    "provider.custom": "Custom",
    "provider.selected": "Selected provider",
    "provider.more": "Show {count} more providers",
    "provider.less": "Show fewer providers",
    "model.load": "Load available models",
    "model.browse": "Browse {count} models",
    "model.discovery_failed": "Model list unavailable",
    "model.discovery_failed_body": "The provider did not return a model list. Enter an exact model ID and verify it directly.\n{message}",
    "model_picker.title": "Choose a model",
    "model_picker.search": "Filter by model ID",
    "model_picker.count": "{count} matching models",
    "model_picker.select": "Use selected model",
    "model_picker.empty": "No models match this filter.",
    "model.config": "Connection details",
    "model.validation": "Model validation",
    "progress.overall": "Overall progress",
    "progress.current": "Current operation",
    "progress.scene_count": "Scene checkpoints",
    "progress.file_count": "File progress",
    "progress.files_remaining": "{completed}/{total} complete · {remaining} remaining",
    "progress.phase_units": "Work units {completed}/{total}",
    "progress.eta": "Estimated remaining",
    "progress.estimating": "Estimating after the first completed scene",
    "progress.model_usage": "Model usage",
    "progress.calls_tokens": "{calls} calls · {tokens} tokens",
    "progress.pause": "Pause safely",
    "progress.pausing": "Finishing the current safe unit and saving its checkpoint…",
    "progress.paused": "Translation paused",
    "progress.paused_body": "All completed checkpoints are preserved. Resume with the same project and workspace at any time.",
    "progress.resume": "Resume translation",
    "progress.enter": "Open translation",
    "progress.retry": "Resume / retry",
    "progress.log_path": "Persistent diagnostic log: {path}",
    "progress.copy_log": "Copy log path",
    "progress.phase.prepare": "Prepare",
    "progress.phase.understand": "Analyze",
    "progress.phase.translate": "Translate",
    "progress.phase.refine": "Validate",
    "progress.phase.build": "Build",
    "progress.phase.done": "Done",
    "review.resume_found": "Recoverable work found",
    "review.resume_body": "{completed} of {total} scene checkpoints are available. RenWeave will verify them before continuing.",
    "budget.title": "AI usage estimate",
    "budget.range": "Main translation text: about {low}–{high} Tokens",
    "budget.breakdown": "Estimated input {input_low}–{input_high} · output {output_low}–{output_high}",
    "budget.note": "Planning range based on {scripts} script sources ({confidence} confidence). Provider retries and currency pricing are not included; the estimate is refined after indexing.",
    "budget.unavailable": "Select a valid game to calculate a Token budget before starting.",
    "budget.confidence.medium": "medium",
    "budget.confidence.low": "low",
    "budget.actual": "Provider-reported {total} · input {input} · output {output}",
    "budget.projected": "Initial text estimate {low}–{high} · excludes context and validation calls",
    "budget.reporting.reported": "Usage reported by provider",
    "budget.reporting.unavailable": "Provider did not report Token counts; actual usage may be higher",
    "budget.reporting.pending": "Waiting for provider usage data",
    "budget.ledger": "Detailed ledger: {path}",
    "model.sequence": "1  Provider     2  Model ID     3  Minimal verification",
    "model.load_effect": "Loads the catalog; normally no model Tokens are used.",
    "model.verify_effect": "Sends a minimal request that may bill a few Tokens.",
    "game.safety_note": "The selected game is read-only during analysis. All caches, checkpoints, logs, and packages go to the workspace.",
    "languages.source_hint": "Leave Auto detect unless the game intentionally mixes languages or detection is unreliable.",
    "languages.target_hint": "Enter any language name or locale supported by the selected model. This determines the Ren'Py language directory.",
    "languages.existing_title": "Existing translations found",
    "languages.existing_body": "Choose one to preserve its valid translations and process only missing or outdated text.",
    "languages.existing_action": "Continue {language} incrementally · {files} files",
    "languages.incremental_selected": "Incremental translation selected: existing valid text will be preserved.",
    "dialog.copy_details": "Copy details",
    "dialog.error_log_hint": "Translation errors also include a full traceback in the workspace diagnostic log.",
    "tip.provider": "Switch provider presets. Keys are isolated by provider and endpoint.",
    "tip.endpoint": "Editable API base URL. Keep the preset unless your provider documents a different endpoint.",
    "tip.api_key": "Saved in encrypted system storage by default, or kept only in memory if you choose that in Settings. Never written to project files.",
    "tip.load_models": "Contact the /models endpoint and populate the model picker; normally no model Tokens are used.",
    "tip.model_id": "Enter the exact API model identifier, including any provider prefix required by an aggregator.",
    "tip.browse_models": "Open a searchable list of models returned by the provider.",
    "tip.verify_model": "Send one minimal chat request to confirm the selected model works before continuing.",
    "tip.game": "Choose the game root, game folder, or launcher executable. RenWeave will discover the actual game directory.",
    "tip.workspace": "Choose an isolated writable folder for analysis, checkpoints, logs, and output. Reuse it to resume.",
    "tip.sdk": "Optional official Ren'Py SDK used to compile-check generated scripts in isolation.",
    "tip.back": "Return to the previous screen without discarding entered settings.",
    "tip.continue": "Validate this screen and move to the next setup step. Translation does not start yet.",
    "tip.start": "Save the secret-free profile and run the complete automated translation pipeline.",
    "tip.pause": "Finish the current network request or atomic unit, save a checkpoint, then stop before the next unit.",
    "tip.resume": "Revalidate existing checkpoints and continue only the unfinished or damaged work.",
    "tip.copy_log": "Copy the persistent diagnostic log path to the clipboard.",
    "tip.show_key": "Reveal the API key only while this switch is on. Storage follows your Settings choice.",
    "tip.settings": "Choose encrypted or memory-only key storage and control optional update checks.",
    "tip.reasoning": "Choose how much reasoning the model may use. Unsupported levels safely fall back to the provider's documented on/off control.",
    "tip.model_picker": "Choose the highlighted model ID and return it to the connection form.",
    "footer.effect.model": "Next: choose the game. Translation has not started.",
    "footer.effect.game": "Next: choose languages. Game files stay unchanged.",
    "footer.effect.languages": "Next: review scope and the Token budget.",
    "footer.effect.review": "Starts billable model work; checkpoints go to the workspace.",
    "footer.effect.progress": "Pause saves checkpoints; closing keeps completed outputs.",
})

COPY["zh"].update({
    "workspace_label": "游戏翻译工作台",
    "nav_privacy": "默认保护隐私\n密钥使用系统加密存储，游戏文件保留在本机。",
    "settings.title": "设置",
    "settings.body": "管理密钥保存方式与可选的在线版本检查；这些选项仅应用于当前系统用户。",
    "settings.credentials": "API 密钥存储",
    "settings.credentials_hint": "安全存储使用操作系统凭据服务。密钥不会写入织译设置、项目文件、日志或翻译包。",
    "settings.secure": "系统加密存储（推荐）",
    "settings.memory": "仅保留在内存——关闭织译后忘记密钥",
    "settings.forget_key": "删除当前密钥",
    "settings.key_error": "密钥存储错误",
    "settings.key_save_failed": "模型已验证，但操作系统凭据服务无法保存该密钥；它将只在当前会话中可用。",
    "settings.updates": "版本更新",
    "settings.updates_hint": "自动检查会在启动后访问 GitHub Releases；默认关闭，且绝不会自动安装更新。",
    "settings.update_toggle": "启动后检查新版本",
    "settings.check_now": "立即检查",
    "settings.current_version": "当前版本：{version}",
    "settings.update_title": "织译版本更新",
    "settings.update_available": "已有新版本 {latest}；当前版本为 {current}。",
    "settings.up_to_date": "织译 {current} 已是最新版本；最新发布版为 {latest}。",
    "model.reasoning": "思考等级",
    "model.reasoning_hint": "“auto”遵循模型默认值；其他等级会转换成所选提供商正式支持的推理参数。",
    "model.reasoning_unavailable": "该提供商未公开受支持的思考等级参数；将由所选模型自动决定。",
    "reasoning.auto": "自动（提供商默认）",
    "reasoning.low": "低",
    "reasoning.high": "高",
    "reasoning.maximum": "最高",
    "provider.choose": "选择 API 提供商",
    "provider.choose_hint": "优先使用官方或聚合平台预设；所有接口地址仍可编辑。",
    "provider.official": "官方",
    "provider.aggregator": "聚合平台",
    "provider.custom": "自定义",
    "provider.selected": "当前提供商",
    "provider.more": "显示其他 {count} 个提供商",
    "provider.less": "收起其他提供商",
    "model.load": "获取可用模型",
    "model.browse": "浏览 {count} 个模型",
    "model.discovery_failed": "无法获取模型列表",
    "model.discovery_failed_body": "该提供商未返回模型列表。你仍可输入准确的模型 ID 并直接验证。\n{message}",
    "model_picker.title": "选择模型",
    "model_picker.search": "按模型 ID 筛选",
    "model_picker.count": "找到 {count} 个模型",
    "model_picker.select": "使用所选模型",
    "model_picker.empty": "没有符合筛选条件的模型。",
    "model.config": "连接信息",
    "model.validation": "模型验证",
    "progress.overall": "总体进度",
    "progress.current": "当前操作",
    "progress.scene_count": "场景检查点",
    "progress.file_count": "文件进度",
    "progress.files_remaining": "已完成 {completed}/{total} · 剩余 {remaining} 个文件",
    "progress.phase_units": "工作单元 {completed}/{total}",
    "progress.eta": "预计剩余时间",
    "progress.estimating": "完成首个场景后开始估算",
    "progress.model_usage": "模型用量",
    "progress.calls_tokens": "{calls} 次调用 · {tokens} Token",
    "progress.pause": "安全暂停",
    "progress.pausing": "正在完成当前安全单元并保存检查点……",
    "progress.paused": "翻译已暂停",
    "progress.paused_body": "所有已完成检查点均已保留；以后使用相同项目和工作区即可继续。",
        "progress.resume": "继续翻译",
        "progress.enter": "进入翻译",
    "progress.retry": "继续 / 重试",
    "progress.log_path": "持久诊断日志：{path}",
    "progress.copy_log": "复制日志路径",
    "progress.phase.prepare": "准备",
    "progress.phase.understand": "分析",
    "progress.phase.translate": "翻译",
    "progress.phase.refine": "校验",
    "progress.phase.build": "构建",
    "progress.phase.done": "完成",
    "review.resume_found": "发现可恢复任务",
    "review.resume_body": "已有 {completed}/{total} 个场景检查点；继续前会自动核验其完整性。",
    "budget.title": "AI 用量预估",
    "budget.range": "主要翻译文本约需 {low}–{high} Token",
    "budget.breakdown": "预计输入 {input_low}–{input_high} · 输出 {output_low}–{output_high}",
    "budget.note": "依据 {scripts} 个脚本来源估算（{confidence}置信度）。不含提供商重试和货币价格；建立索引后会更新估算。",
    "budget.unavailable": "请选择有效游戏，以便在开始前计算 Token 预算。",
    "budget.confidence.medium": "中等",
    "budget.confidence.low": "较低",
    "budget.actual": "提供商已报告 {total} · 输入 {input} · 输出 {output}",
    "budget.projected": "初步文本估算 {low}–{high} · 不含上下文和校验调用",
    "budget.reporting.reported": "提供商已返回 Token 用量",
    "budget.reporting.unavailable": "提供商未返回 Token 数；实际用量可能更高",
    "budget.reporting.pending": "正在等待提供商用量数据",
    "budget.ledger": "详细账本：{path}",
    "model.sequence": "1  提供商     2  模型 ID     3  最小请求验证",
    "model.load_effect": "获取模型目录，通常不会消耗模型 Token。",
    "model.verify_effect": "发送最小请求，提供商可能计费少量 Token。",
    "game.safety_note": "分析时游戏目录保持只读；缓存、检查点、日志和语言包全部写入工作区。",
    "languages.source_hint": "除非游戏混合多种语言或自动识别不可靠，否则建议保留“自动检测”。",
    "languages.target_hint": "可填写模型支持的任意语言名称或区域代码；它会决定 Ren'Py 语言目录名称。",
    "languages.existing_title": "发现已有翻译",
    "languages.existing_body": "直接选择已有语言，织译会保留有效译文，只处理缺失或因原文变化而失效的部分。",
    "languages.existing_action": "继续增量翻译 {language} · 已有 {files} 个文件",
    "languages.incremental_selected": "已选择增量翻译：现有有效译文会原样保留。",
    "dialog.copy_details": "复制详情",
    "dialog.error_log_hint": "翻译流程错误还会在工作区诊断日志中保存完整堆栈。",
    "tip.provider": "切换提供商预设；密钥按提供商和接口地址相互隔离。",
    "tip.endpoint": "可编辑的 API 基础地址；除非提供商另有说明，建议保留预设值。",
    "tip.api_key": "默认保存到系统加密凭据库，也可在设置中改为仅驻留内存；绝不会写入项目文件。",
    "tip.load_models": "访问 /models 接口并填充模型选择器；通常不会消耗模型 Token。",
    "tip.model_id": "填写准确的 API 模型标识；聚合平台可能要求包含提供商前缀。",
    "tip.browse_models": "打开提供商返回模型的可搜索列表。",
    "tip.verify_model": "发送一次最小聊天请求，确认所选模型可用后才能继续。",
    "tip.game": "选择游戏根目录、game 文件夹或启动程序；织译会自动识别实际游戏目录。",
    "tip.workspace": "选择独立可写目录保存分析、检查点、日志与输出；以后复用它即可继续任务。",
    "tip.sdk": "通常会从游戏目录自动找到。它只在独立工作区检查语言包能否运行，不会修改游戏。",
    "tip.back": "返回上一页，不会丢弃已经填写的设置。",
    "tip.continue": "校验当前页面并进入下一设置步骤，此时不会开始翻译。",
    "tip.start": "保存不含密钥的配置并运行完整自动翻译流程。",
    "tip.pause": "完成当前网络请求或原子操作，保存检查点，并在下一单元前停止。",
    "tip.resume": "重新核验现有检查点，只继续尚未完成或已经损坏的部分。",
    "tip.copy_log": "把持久诊断日志路径复制到剪贴板。",
    "tip.show_key": "仅在开关启用时显示 API 密钥；保存方式遵循设置中的选择。",
    "tip.settings": "选择系统加密或仅内存密钥存储，并管理可选的版本检查。",
    "tip.reasoning": "选择模型可使用的推理强度；不支持细分等级的接口会安全退化为官方开关参数。",
    "tip.model_picker": "采用当前高亮的模型 ID，并返回连接页面。",
    "footer.effect.model": "下一步选择游戏；翻译尚未开始。",
    "footer.effect.game": "下一步选择语言；游戏文件保持不变。",
    "footer.effect.languages": "下一步确认范围与 Token 预算。",
    "footer.effect.review": "开始可能计费的模型调用；检查点写入工作区。",
    "footer.effect.progress": "暂停会保存检查点；关闭后仍保留已完成输出。",
})

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
    "paused": "翻译已安全暂停",
    "failed": "翻译失败",
}


_BASE_COPY = {
    "en": {
        "app_title": "RenWeave",
        "app_subtitle": "Context-aware Ren'Py translation, from game files to a verified language package.",
        "language": "Interface language",
        "steps.model": "Model",
        "steps.game": "Game",
        "steps.languages": "Languages",
        "steps.review": "Review",
        "steps.progress": "Translation",
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
        "model.key_hint": "Encrypted system storage is the default. Change this anytime in Settings.",
        "model.show_key": "Show key",
        "model.settings_saved": "Saved automatically",
        "model.connect": "Connect and load models",
        "model.model": "Model",
        "model.model_hint": "Choose a discovered model or enter an exact model ID.",
        "model.verify": "Verify selected model",
        "model.idle": "Not connected",
        "model.idle_body": "Enter an API key, then load available models.",
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
        "model.required": "Connect to the API and verify a model before continuing.",
        "game.title": "Choose the Ren'Py game",
        "game.body": "RenWeave works from an isolated workspace and does not modify the source game during analysis.",
        "game.project": "Game directory or executable",
        "game.project_hint": "Select the game root, its game folder, or the game executable.",
        "game.workspace": "Workspace",
        "game.workspace_hint": "Analysis, caches, checkpoints, and output packages are stored here.",
        "game.advanced": "Automatic compatibility check",
        "game.sdk": "Tool used to test the language pack",
        "game.sdk_hint": "Filled automatically from the selected game. Normally you do not need to change it.",
        "game.require_engine": "Test that the completed language pack can be loaded by this game",
        "game.runtime_found": "Game runtime found automatically: {path}",
        "game.runtime_missing": "No bundled test tool was found. RenWeave will still run its built-in checks.",
        "game.project_picker": "Select a Ren'Py game root or game directory",
        "game.workspace_picker": "Select or create a RenWeave workspace",
        "game.sdk_picker": "Select a Ren'Py SDK directory",
        "game.invalid": "Choose a valid game and workspace before continuing.",
        "languages.title": "Choose what to translate",
        "languages.body": "RenWeave checks the selected game for existing languages first, then continues only unfinished work.",
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
        "review.rpa": "Create a ready-to-use language package",
        "review.rpa_hint": "On by default. Ren'Py compatibility is checked automatically when translation finishes.",
        "review.install": "Install the completed language package into the game",
        "review.install_hint": "Off by default. Generated RPY files always remain in the workspace.",
        "review.engine_yes": "Ren'Py engine validation required",
        "review.engine_no": "Built-in validation",
        "review.key_safe": "API key stays in encrypted system storage or session memory",
        "review.incremental_scope": "Incremental scope: {units} text units in {scenes} scenes / {files} files",
        "review.incremental_scope_unknown": "Incremental scope will be scanned before model work",
        "review.incremental_scanning": "Scanning the complete existing language pack now…",
        "review.incremental_scan_failed": "Preflight scan failed: {error}",
        "review.incremental_scope_exact": "Current source: {total} · safely reused: {reused} · needs translation: {units} · preserved files: {files}",
        "review.pending_title": "Texts that still need translation ({count})",
        "review.pending_row": "{file}:{line}\n{source}\nReason: {detail}",
        "progress.title": "Translation in progress",
        "progress.body": "You can follow each stage here. Checkpoints make interrupted work resumable.",
        "progress.ready": "Preparing the one-click pipeline…",
        "progress.scenes": "{count} scenes completed",
        "progress.log": "Activity",
        "progress.started": "Task started. Analysis and AI artifacts are being written to the isolated workspace.",
        "progress.complete": "Translation complete",
        "progress.complete_body_rpa": "The verified RPY files and RPA archive are ready. Package metadata records compiled runtime readiness.",
        "progress.complete_body_rpy": "The verified RPY translation files are ready. RPA creation was disabled.",
        "progress.rpy_output": "RPY files",
        "progress.rpa_output": "RPA archive",
        "progress.outputs": "Translation package",
        "progress.outputs_hint": "Open the generated files directly. The RPA button opens the folder containing the archive.",
        "progress.open_rpy": "Open RPY folder",
        "progress.open_rpa": "Open RPA folder",
        "dialog.open_failed": "Could not open folder",
        "progress.failed": "Translation stopped",
        "progress.failed_body": "The workspace, checkpoints, and diagnostics were kept so the task can be retried.",
        "progress.runtime": "Run status",
        "progress.running": "Running normally",
        "progress.starting": "Starting the translation worker",
        "progress.runtime_paused": "Translation is paused",
        "progress.runtime_failed": "Translation has stopped",
        "progress.runtime_complete": "Translation is complete",
        "progress.stage_counter": "Pipeline stage {current} of {total}",
        "progress.stage_unknown": "Initializing pipeline stages",
        "progress.back_warning_title": "Review before resuming",
        "progress.back_warning_body": "The translation has stopped, so you can return to setup. Changes to the game, workspace, provider, endpoint, model, languages, or engine validation can affect the entire translation chain.",
        "progress.config_changed_title": "Critical translation settings changed",
        "progress.config_changed_body": "These settings changed since this run started:\n{fields}\n\nContinuing may combine saved checkpoints with a different translation setup. Continue only if this is intentional.",
        "progress.continue_changed": "Continue anyway",
        "dialog.cannot_continue": "Cannot continue",
        "dialog.cannot_start": "Cannot start",
        "dialog.complete": "RenWeave finished",
        "dialog.failed": "Translation failed",
    },
    "zh": {
        "app_title": "织译",
        "app_subtitle": "理解上下文的 Ren'Py 翻译：从游戏文件到通过验证的语言包。",
        "language": "界面语言",
        "steps.model": "模型",
        "steps.game": "游戏",
        "steps.languages": "语言",
        "steps.review": "确认",
        "steps.progress": "翻译",
        "step_count": "第 {current} 步，共 {total} 步",
        "back": "返回",
        "continue": "继续",
        "start": "开始翻译",
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
        "model.key_hint": "默认使用系统加密存储；可随时在“设置”中改为仅内存。",
        "model.show_key": "显示密钥",
        "model.settings_saved": "设置自动保存",
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
        "model.required": "请先连接 API 并验证模型。",
        "game.title": "选择 Ren'Py 游戏",
        "game.body": "织译在独立工作区运行，分析过程中不会修改游戏源文件。",
        "game.project": "游戏目录或程序",
        "game.project_hint": "可选择游戏根目录、game 目录或游戏程序。",
        "game.workspace": "工作区",
        "game.workspace_hint": "分析结果、缓存、检查点和输出包都会保存在这里。",
        "game.advanced": "自动兼容性检查",
        "game.sdk": "用于测试语言包的游戏工具",
        "game.sdk_hint": "选择游戏后自动填写，通常不需要修改。它只会在独立工作区测试生成结果。",
        "game.require_engine": "完成前检查语言包能否被这个游戏正常读取",
        "game.runtime_found": "已自动找到游戏运行环境：{path}",
        "game.runtime_missing": "没有找到游戏自带的测试工具；仍会执行织译内置检查。",
        "game.project_picker": "选择 Ren'Py 游戏根目录或 game 目录",
        "game.workspace_picker": "选择或创建织译工作区",
        "game.sdk_picker": "选择 Ren'Py SDK 目录",
        "game.invalid": "请先选择有效的游戏和工作区。",
        "languages.title": "选择需要继续处理的语言",
        "languages.body": "织译会先检查所选游戏已有的语言包，再只处理尚未完成的部分。",
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
        "review.rpa": "生成可直接使用的语言包",
        "review.rpa_hint": "默认开启。翻译完成后自动进行 Ren'Py 兼容性检查。",
        "review.install": "完成后将语言包安装到游戏",
        "review.install_hint": "默认关闭；生成的 RPY 文件始终保留在工作区。",
        "review.engine_yes": "必须通过 Ren'Py 引擎验证",
        "review.engine_no": "使用内置验证",
        "review.key_safe": "API 密钥仅保存在系统加密凭据库或会话内存",
        "review.incremental_scope": "增量范围：{units} 个文本单元，{scenes} 个场景，{files} 个文件",
        "review.incremental_scope_unknown": "将在调用模型前扫描并确认实际增量范围",
        "review.incremental_scanning": "正在完整扫描现有语言包，不会调用模型……",
        "review.incremental_scan_failed": "预检扫描失败：{error}",
        "review.incremental_scope_exact": "当前原文 {total} 条 · 安全复用 {reused} 条 · 待翻译 {units} 条 · 原样保留 {files} 个文件",
        "review.pending_title": "仍需翻译的文本（{count} 条）",
        "review.pending_row": "{file}:{line}\n{source}\n原因：{detail}",
        "progress.title": "正在翻译",
        "progress.body": "可在这里查看每个阶段；检查点让中断后的任务能够恢复。",
        "progress.ready": "正在准备翻译流程…",
        "progress.scenes": "已完成 {count} 个场景",
        "progress.log": "活动记录",
        "progress.started": "任务已启动，分析结果与 AI 产物正在写入独立工作区。",
        "progress.complete": "翻译完成",
        "progress.complete_body_rpa": "已生成通过验证的 RPY 与 RPA；包元数据会记录 RPYC 编译及运行时就绪状态。",
        "progress.complete_body_rpy": "已生成通过验证的 RPY 翻译文件；本次未生成 RPA。",
        "progress.rpy_output": "RPY 文件",
        "progress.rpa_output": "RPA 归档",
        "progress.outputs": "翻译包",
        "progress.outputs_hint": "可直接打开生成结果；RPA 按钮会打开归档文件所在目录。",
        "progress.open_rpy": "打开 RPY 目录",
        "progress.open_rpa": "打开 RPA 目录",
        "dialog.open_failed": "无法打开目录",
        "progress.failed": "翻译已停止",
        "progress.failed_body": "工作区、检查点与诊断信息均已保留，可以重试。",
        "progress.runtime": "运行状态",
        "progress.running": "程序正在正常运行",
        "progress.starting": "正在启动翻译工作线程",
        "progress.runtime_paused": "翻译已暂停",
        "progress.runtime_failed": "翻译已停止",
        "progress.runtime_complete": "翻译已完成",
        "progress.stage_counter": "翻译链路第 {current}/{total} 阶段",
        "progress.stage_unknown": "正在初始化翻译链路",
        "progress.back_warning_title": "恢复翻译前请检查",
        "progress.back_warning_body": "翻译已经停止，现在可以返回修改设置。游戏、工作区、提供商、接口地址、模型、语言或引擎验证一旦改变，都可能影响整个翻译链路。",
        "progress.config_changed_title": "翻译关键设置已改变",
        "progress.config_changed_body": "以下设置与本次任务开始时不同：\n{fields}\n\n继续可能会把已有检查点与不同的翻译设置混合。请确认这是有意修改后再继续。",
        "progress.continue_changed": "仍然继续",
        "dialog.cannot_continue": "无法继续",
        "dialog.cannot_start": "无法开始",
        "dialog.complete": "织译已完成",
        "dialog.failed": "翻译失败",
    },
}

for _locale, _strings in _BASE_COPY.items():
    COPY[_locale].update(_strings)

COPY["en"].update({
    "languages.scan_summary": "Translation summary",
    "languages.scan_counts": "Existing translations  {reused}\nStill to translate  {pending}\nOriginal language files  {files}",
    "languages.incremental_choice": "Continue incremental translation",
    "review.title": "Ready to translate",
    "review.body": "Confirm this task. Starting it will call the selected AI model.",
    "review.task_mode": "Incremental translation",
    "review.remaining": "{count} texts remaining",
    "review.preserved": "{count} valid existing translations will be preserved",
    "review.game_unchanged": "Original game files will not be changed",
    "review.output_plain": "A Ren'Py-validated language package",
    "review.details": "View task details",
    "review.hide_details": "Hide task details",
    "review.pending_show": "View content",
    "review.pending_hide": "Hide content",
    "review.start_remaining": "Translate remaining {count}",
    "review.rpa": "Create a ready-to-use language package",
    "review.rpa_hint": "On by default. RenWeave checks Ren'Py compatibility when translation finishes.",
    "review.rpa_technical": "Technical details: create RPA and compile RPYC when a compatible runtime is available.",
    "review.technical_show": "Technical details",
    "review.technical_hide": "Hide technical details",
    "review.install_hint": "Off by default. When enabled, successful translation and validation writes language-package files into the game directory.",
    "review.install_warning": "This option modifies language-package files in the selected game directory after validation succeeds.",
    "review.resume_body_unknown": "{completed} checkpoints have been saved. RenWeave will verify them before continuing.",
    "budget.title": "AI usage estimate",
    "budget.range": "Main translation text: about {low}–{high} Tokens",
    "budget.note": "Initial estimate from {scripts} script sources ({confidence} confidence). Context, validation, retries, and other model calls can use additional Tokens.",
    "budget.projected": "Initial text estimate {low}–{high} · excludes context and validation calls",
    "budget.unavailable": "No translatable text is available for an estimate yet. RenWeave will update this after scanning.",
    "progress.task.idle": "Ready to start",
    "progress.task.preparing": "Preparing translation",
    "progress.task.analyzing": "Analyzing the game",
    "progress.task.translating": "Translating",
    "progress.task.validating": "Validating translation",
    "progress.task.building": "Building the language package",
    "progress.task.completed": "Translation complete",
    "progress.task.pausing": "Saving progress before pausing",
    "progress.pausing_action": "Pausing…",
    "progress.task.paused": "Translation paused",
    "progress.task.failed": "Translation stopped",
    "progress.task.cancelled": "Translation cancelled",
    "progress.scene_ratio": "{completed} / {total} scenes",
    "progress.scene_total_unknown": "{completed} scenes completed",
    "progress.current_file": "Current file: {path}",
    "progress.phase.analyze": "Analyze",
    "progress.phase.validate": "Validate",
    "progress.log": "Run details",
    "progress.log_show": "View log",
    "progress.log_hide": "Hide log",
    "progress.activity_recent": "Recent activity",
    "progress.usage_actual": "AI usage {total} Tokens",
    "progress.usage_breakdown": "Input {input} · output {output}",
    "progress.safe_to_close": "Progress is safely saved. You can close RenWeave and resume later.",
    "progress.package_ready": "Language package generated",
    "progress.installed_output": "Installed in game",
    "error.api_key": "The API key was rejected. Check it and try again.",
    "error.timeout": "The request timed out. Check the network or service address and try again.",
    "error.connection": "Could not reach the AI service. Check the network and service address.",
    "error.generic": "The task could not continue. Your workspace and saved progress were kept.",
    "model.advanced_show": "Advanced settings",
    "model.advanced_hide": "Hide advanced settings",
    "game.change": "Change",
    "game.copy_path": "Copy path",
    "game.open_folder": "Open folder",
    "game.manual_path": "Edit path manually",
    "game.hide_manual_path": "Hide manual path",
    "game.not_selected": "Not selected",
    "top.settings": "⚙  Settings",
    "game.safety_title": "Original game files will not be changed",
    "game.safety_body": "Analysis, caches, translations, and language packages stay in the separate workspace.",
})

COPY["zh"].update({
    "languages.scan_summary": "已有翻译摘要",
    "languages.scan_counts": "已有翻译  {reused} 条\n仍需翻译  {pending} 条\n原有语言文件  {files} 个",
    "languages.incremental_choice": "继续增量翻译",
    "review.title": "准备开始翻译",
    "review.body": "请确认本次任务。开始后会调用所选 AI 模型。",
    "review.task_mode": "增量翻译",
    "review.remaining": "剩余 {count} 条文本",
    "review.preserved": "已有 {count} 条有效翻译会被保留",
    "review.game_unchanged": "不会修改游戏原文件",
    "review.output_plain": "经过 Ren'Py 验证的语言包",
    "review.details": "查看任务详情",
    "review.hide_details": "收起任务详情",
    "review.pending_show": "查看内容",
    "review.pending_hide": "收起内容",
    "review.start_remaining": "翻译剩余 {count} 条",
    "review.rpa": "生成可直接使用的语言包",
    "review.rpa_hint": "默认开启。翻译完成后自动进行 Ren'Py 兼容性检查。",
    "review.rpa_technical": "技术详情：生成 RPA，并在运行环境可用时编译 RPYC。",
    "review.technical_show": "技术详情",
    "review.technical_hide": "收起技术详情",
    "review.install_hint": "默认关闭。开启后会在翻译和验证成功后向游戏目录写入语言包文件。",
    "review.install_warning": "此选项会在验证成功后修改所选游戏目录中的语言包文件。",
    "review.resume_body_unknown": "已保存 {completed} 个检查点；继续前会自动核验其完整性。",
    "budget.title": "AI 用量预估",
    "budget.range": "待翻译主要文本约需 {low}–{high} Token",
    "budget.note": "依据 {scripts} 个脚本来源进行初步估算（准确度：{confidence}）。上下文、校验、重试和其他模型调用可能产生额外 Token。",
    "budget.projected": "初始文本估算 {low}–{high} · 不含上下文和校验调用",
    "budget.unavailable": "暂未发现可供估算的待翻译文本；扫描完成后会自动更新。",
    "progress.task.idle": "准备开始",
    "progress.task.preparing": "正在准备",
    "progress.task.analyzing": "正在分析游戏",
    "progress.task.translating": "正在翻译",
    "progress.task.validating": "正在校验译文",
    "progress.task.building": "正在生成语言包",
    "progress.task.completed": "翻译完成",
    "progress.task.pausing": "正在保存进度并暂停",
    "progress.pausing_action": "正在暂停…",
    "progress.task.paused": "翻译已暂停",
    "progress.task.failed": "翻译已停止",
    "progress.task.cancelled": "翻译已取消",
    "progress.scene_ratio": "已完成 {completed} / {total} 个场景",
    "progress.scene_total_unknown": "已完成 {completed} 个场景",
    "progress.current_file": "当前文件：{path}",
    "progress.phase.analyze": "分析",
    "progress.phase.validate": "校验",
    "progress.log": "运行详情",
    "progress.log_show": "查看日志",
    "progress.log_hide": "收起日志",
    "progress.activity_recent": "最近活动",
    "progress.usage_actual": "AI 用量 {total} Token",
    "progress.usage_breakdown": "输入 {input} · 输出 {output}",
    "progress.safe_to_close": "进度已安全保存。可以关闭程序，下次从此处继续。",
    "progress.package_ready": "语言包已生成",
    "progress.installed_output": "已安装到游戏",
    "error.api_key": "API Key 无效，请检查后重试。",
    "error.timeout": "请求超时，请检查网络或服务地址后重试。",
    "error.connection": "无法连接 AI 服务，请检查网络和服务地址。",
    "error.generic": "任务无法继续；工作区与已保存进度均已保留。",
    "model.advanced_show": "高级设置",
    "model.advanced_hide": "收起高级设置",
    "game.change": "更改",
    "game.copy_path": "复制路径",
    "game.open_folder": "打开文件夹",
    "game.manual_path": "手动输入路径",
    "game.hide_manual_path": "收起路径输入",
    "game.not_selected": "尚未选择",
    "top.settings": "⚙  设置",
    "game.safety_title": "原游戏文件不会被修改",
    "game.safety_body": "分析、缓存、翻译结果和语言包都会保存在独立工作区。",
})


class Colors:
    PRIMARY = "#5B5CE2"
    PRIMARY_HOVER = "#494AC8"
    ON_PRIMARY = "#FFFFFF"
    PRIMARY_CONTAINER = "#EEF0FF"
    ON_PRIMARY_CONTAINER = "#303176"
    ACCENT = "#20B8CD"
    SURFACE = "#F3F6FB"
    SURFACE_CONTAINER = "#F7F9FC"
    SURFACE_HIGH = "#E9EDF5"
    CARD = "#FFFFFF"
    ON_SURFACE = "#101828"
    ON_SURFACE_VARIANT = "#667085"
    OUTLINE = "#98A2B3"
    OUTLINE_VARIANT = "#E0E6EF"
    SUCCESS = "#067647"
    SUCCESS_CONTAINER = "#ECFDF3"
    ERROR = "#B42318"
    ERROR_CONTAINER = "#FEF3F2"
    NAV = "#0B1020"
    NAV_ACTIVE = "#1B2440"
    NAV_MUTED = "#98A2B3"
    NAV_TEXT = "#F9FAFB"
    WARNING = "#B45309"
    WARNING_CONTAINER = "#FFF7ED"
    CONTROL_HOVER = "#E7E9FF"
    CONTROL_PRESSED = "#D9DDFF"
    SCROLL_THUMB = "#B2BBC9"
    SCROLL_THUMB_HOVER = "#818B9B"


class Metrics:
    """Single source of truth for desktop component sizing and alignment."""

    SPACE_1 = 4
    SPACE_2 = 8
    SPACE_3 = 12
    SPACE_4 = 16
    SPACE_5 = 20
    SPACE_6 = 24
    PAGE_INSET = 36
    COMPACT_PAGE_INSET = 24
    SCROLLBAR_GUTTER = 13
    CARD_PADDING = 20
    FIELD_GAP = 6
    CONTROL_PADDING_Y = 9
    ANIMATION_FRAME_MS = 16
    ANIMATION_DURATION_MS = 160
    RESIZE_LAYOUT_FRAME_MS = 40
    ACTIVITY_FRAME_MS = 45
    SCROLL_INCREMENT = 44
    BUTTON_WIDTH = 18
    FIELD_ACTION_WIDTH = 16
    DIALOG_ACTION_WIDTH = 15
    FOOTER_BACK_WIDTH = 17
    FOOTER_ACTION_WIDTH = 17
    FOOTER_SLOT_WIDTH = 180
    COMPACT_FOOTER_SLOT_WIDTH = 150
    LIST_ROW_HEIGHT = 38
    COMPACT_BREAKPOINT = 1100
    NARROW_BREAKPOINT = 1020
    SIDEBAR_WIDTH = 232
    NARROW_SIDEBAR_WIDTH = 76


class Typography:
    """Cross-language font roles with a deliberate Windows CJK face."""

    UI = "Microsoft YaHei UI"
    MONO = "Cascadia Mono"
    SMALL = 9
    BODY = 10
    TITLE = 18
    DISPLAY = 24


class GuidedTooltip:
    """Consistent delayed help for controls whose consequences need explanation."""

    def __init__(self, app: "RenWeaveDesktopApp", widget, translation_key: str) -> None:
        self.app = app
        self.widget = widget
        self.translation_key = translation_key
        self.after_id = None
        self.window = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._hide()
        try:
            self.after_id = self.widget.after(450, self._show)
        except self.app.tk.TclError:
            pass

    def _show(self) -> None:
        self.after_id = None
        try:
            if not self.widget.winfo_exists():
                return
            x = self.widget.winfo_rootx() + 8
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 7
        except self.app.tk.TclError:
            return
        self.window = self.app.tk.Toplevel(self.widget)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        shell = self.app.tk.Frame(
            self.window,
            background=Colors.NAV,
            highlightbackground=Colors.NAV_ACTIVE,
            highlightthickness=1,
            padx=12,
            pady=9,
        )
        shell.pack()
        self.app.tk.Label(
            shell,
            text=self.app.t(self.translation_key),
            background=Colors.NAV,
            foreground=Colors.NAV_TEXT,
            font=(Typography.UI, Typography.SMALL),
            justify="left",
            wraplength=330,
        ).pack()
        self.window.geometry(f"+{x}+{y}")

    def _hide(self, _event=None) -> None:
        if self.after_id is not None:
            try:
                self.widget.after_cancel(self.after_id)
            except self.app.tk.TclError:
                pass
            self.after_id = None
        if self.window is not None:
            try:
                self.window.destroy()
            except self.app.tk.TclError:
                pass
            self.window = None


class SettingsDialog:
    """Privacy and maintenance settings, kept separate from the translation flow."""

    def __init__(self, app: "RenWeaveDesktopApp") -> None:
        tk, ttk = app.tk, app.ttk
        self.app = app
        self.window = tk.Toplevel(app.root)
        self.window.title(app.t("settings.title"))
        self.window.configure(background=Colors.CARD)
        self.window.geometry("820x460")
        self.window.minsize(760, 440)
        self.window.transient(app.root)
        self.window.grab_set()
        self.window.columnconfigure(0, weight=1)
        self.window.rowconfigure(1, weight=1)

        header = ttk.Frame(self.window, style="Dialog.TFrame", padding=(28, 24, 28, 12))
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text=app.t("settings.title"), style="DialogTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            header, text=app.t("settings.body"), style="DialogHint.TLabel",
            wraplength=750, justify="left"
        ).grid(row=1, column=0, sticky="w", pady=(7, 0))

        body = ttk.Frame(self.window, style="Dialog.TFrame", padding=(28, 8, 28, 16))
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)

        credentials = ttk.Frame(body, style="Card.TFrame", padding=18)
        credentials.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        credentials.columnconfigure(0, weight=1)
        ttk.Label(credentials, text=app.t("settings.credentials"), style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            credentials, text=app.t("settings.credentials_hint"), style="Hint.TLabel",
            wraplength=330, justify="left"
        ).grid(row=1, column=0, sticky="w", pady=(4, 8))
        secure = ttk.Radiobutton(
            credentials, text=app.t("settings.secure"), value="secure",
            variable=app.key_storage, style="Material.TRadiobutton", command=self._storage_changed
        )
        secure.grid(row=2, column=0, sticky="w")
        memory = ttk.Radiobutton(
            credentials, text=app.t("settings.memory"), value="memory",
            variable=app.key_storage, style="Material.TRadiobutton", command=self._storage_changed
        )
        memory.grid(row=3, column=0, sticky="w", pady=(3, 0))
        forget = app._button(
            credentials, app.t("settings.forget_key"), app._forget_api_key,
            kind="secondary", width=16
        )
        forget.grid(row=4, column=0, sticky="w", pady=(10, 0))

        updates = ttk.Frame(body, style="Card.TFrame", padding=18)
        updates.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        updates.columnconfigure(0, weight=1)
        ttk.Label(updates, text=app.t("settings.updates"), style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            updates, text=app.t("settings.updates_hint"), style="Hint.TLabel",
            wraplength=330, justify="left"
        ).grid(row=1, column=0, sticky="w", pady=(4, 8))
        update_toggle = ttk.Checkbutton(
            updates, text=app.t("settings.update_toggle"), variable=app.update_checks_enabled,
            style="Material.TCheckbutton", command=app._schedule_settings_save
        )
        update_toggle.grid(row=2, column=0, sticky="w")
        check = app._button(
            updates, app.t("settings.check_now"), lambda: app._check_updates(manual=True),
            kind="secondary", width=16
        )
        check.grid(row=3, column=0, sticky="w", pady=(10, 0))
        ttk.Label(
            updates, text=app.t("settings.current_version", version=__version__),
            style="Hint.TLabel"
        ).grid(row=4, column=0, sticky="w", pady=(5, 0))

        footer = ttk.Frame(self.window, style="Dialog.TFrame", padding=(28, 8, 28, 24))
        footer.grid(row=2, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        close = app._button(footer, app.t("close"), self._close, width=Metrics.DIALOG_ACTION_WIDTH)
        close.grid(row=0, column=1, sticky="e")
        self.window.bind("<Escape>", lambda _event: self._close())
        self.window.protocol("WM_DELETE_WINDOW", self._close)
        self.window.update_idletasks()
        x = app.root.winfo_rootx() + max(0, (app.root.winfo_width() - self.window.winfo_width()) // 2)
        y = app.root.winfo_rooty() + max(0, (app.root.winfo_height() - self.window.winfo_height()) // 2)
        self.window.geometry(f"+{x}+{y}")
        app._style_native_window(self.window, dark=False)
        close.focus_set()

    def _storage_changed(self) -> None:
        if self.app.key_storage.get() == "secure":
            self.app._save_api_key()
        self.app._schedule_settings_save()

    def _close(self) -> None:
        self.app._save_api_key()
        self.app._save_desktop_settings()
        self.window.destroy()


class MaterialDialog:
    """A consistent workspace modal used instead of platform-mixed message boxes."""

    def __init__(
        self,
        app: "RenWeaveDesktopApp",
        title: str,
        body: str,
        *,
        error: bool = False,
        details: str = "",
        warning: bool = False,
        confirm_text: str = "",
        on_confirm: Callable[[], None] | None = None,
        extra_actions: tuple[tuple[str, Callable[[], None]], ...] = (),
    ) -> None:
        tk, ttk = app.tk, app.ttk
        self.window = tk.Toplevel(app.root)
        self.window.title(title)
        self.window.configure(background=Colors.SURFACE)
        self.window.resizable(False, False)
        self.window.transient(app.root)
        self.window.grab_set()
        card = ttk.Frame(self.window, style="Dialog.TFrame", padding=24)
        card.grid(sticky="nsew")
        card.columnconfigure(1, weight=1)
        accent_color = Colors.ERROR if error else (Colors.WARNING if warning else Colors.SUCCESS)
        accent = tk.Frame(card, background=accent_color, width=4)
        accent.grid(row=0, column=0, rowspan=3, sticky="ns", padx=(0, 16))
        accent.grid_propagate(False)
        ttk.Label(card, text=title, style="DialogTitle.TLabel").grid(row=0, column=1, sticky="w")
        ttk.Label(
            card,
            text=body,
            style="ErrorBody.TLabel" if error else "Body.TLabel",
            wraplength=440,
            justify="left",
        ).grid(row=1, column=1, sticky="w", pady=(10, 8))
        if error:
            ttk.Label(
                card,
                text=app.t("dialog.error_log_hint"),
                style="DialogHint.TLabel",
                wraplength=440,
                justify="left",
            ).grid(row=2, column=1, sticky="w", pady=(0, 20))
        actions = ttk.Frame(card, style="Dialog.TFrame")
        actions.grid(row=3, column=0, columnspan=2, sticky="e")
        if error:
            copy = app._button(
                actions,
                app.t("dialog.copy_details"),
                lambda: app._copy_text(details or body),
                kind="secondary",
                width=Metrics.DIALOG_ACTION_WIDTH,
            )
            copy.pack(side="left", padx=(0, 8))
        for action_text, action_command in extra_actions:
            action = app._button(
                actions,
                action_text,
                action_command,
                kind="secondary",
                width=Metrics.DIALOG_ACTION_WIDTH,
            )
            action.pack(side="left", padx=(0, 8))
        def confirm() -> None:
            self.window.destroy()
            if on_confirm is not None:
                on_confirm()

        if on_confirm is not None:
            close = app._button(
                actions,
                app.t("cancel"),
                self.window.destroy,
                kind="secondary",
                width=Metrics.DIALOG_ACTION_WIDTH,
            )
            close.pack(side="left", padx=(0, 8))
            primary = app._button(
                actions,
                confirm_text or app.t("continue"),
                confirm,
                width=Metrics.DIALOG_ACTION_WIDTH,
            )
            primary.pack(side="left")
        else:
            close = app._button(actions, app.t("close"), self.window.destroy, width=Metrics.DIALOG_ACTION_WIDTH)
            close.pack(side="left")
            primary = close
        self.window.bind("<Escape>", lambda _event: self.window.destroy())
        self.window.bind("<Return>", lambda _event: confirm() if on_confirm is not None else self.window.destroy())
        self.window.protocol("WM_DELETE_WINDOW", self.window.destroy)
        self.window.update_idletasks()
        x = app.root.winfo_rootx() + max(0, (app.root.winfo_width() - self.window.winfo_width()) // 2)
        y = app.root.winfo_rooty() + max(0, (app.root.winfo_height() - self.window.winfo_height()) // 2)
        self.window.geometry(f"+{x}+{y}")
        app._style_native_window(self.window, dark=False)
        primary.focus_set()


class ModelPickerDialog:
    def __init__(self, app: "RenWeaveDesktopApp") -> None:
        tk, ttk = app.tk, app.ttk
        self.app = app
        self.all_models = tuple(app.model_choices)
        self.filtered_models = self.all_models
        self.window = tk.Toplevel(app.root)
        self.window.title(app.t("model_picker.title"))
        self.window.configure(background=Colors.CARD)
        self.window.geometry("640x520")
        self.window.minsize(520, 420)
        self.window.transient(app.root)
        self.window.grab_set()
        self.window.columnconfigure(0, weight=1)
        self.window.rowconfigure(1, weight=1)

        header = ttk.Frame(self.window, style="Dialog.TFrame", padding=(24, 22, 24, 12))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=app.t("model_picker.title"), style="DialogTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text=app.t("model_picker.search"), style="DialogHint.TLabel").grid(
            row=1, column=0, sticky="w", pady=(14, 5)
        )
        self.query = tk.StringVar()
        search = app._entry(header, self.query)
        search.grid(row=2, column=0, sticky="ew")
        search.insert(0, "")
        self.query.trace_add("write", self._filter)

        body = ttk.Frame(self.window, style="Dialog.TFrame", padding=(24, 0, 24, 12))
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)
        self.count = ttk.Label(body, style="DialogHint.TLabel")
        self.count.grid(row=0, column=0, sticky="w", pady=(0, 8))
        list_frame = tk.Frame(body, background=Colors.OUTLINE_VARIANT, padx=1, pady=1)
        list_frame.grid(row=1, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self.listbox = ttk.Treeview(
            list_frame,
            show="tree",
            selectmode="browse",
            style="ModelList.Treeview",
            takefocus=True,
        )
        self.listbox.grid(row=0, column=0, sticky="nsew")
        scrollbar = app._scrollbar(list_frame, command=self.listbox.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.listbox.configure(yscrollcommand=scrollbar.set)
        self.listbox.bind("<Double-Button-1>", lambda _event: self._select())
        self.listbox.bind("<Return>", lambda _event: self._select())

        footer = ttk.Frame(self.window, style="Dialog.TFrame", padding=(24, 8, 24, 22))
        footer.grid(row=2, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        cancel_button = app._button(footer, app.t("cancel"), self.window.destroy, kind="secondary", width=Metrics.DIALOG_ACTION_WIDTH)
        cancel_button.grid(row=0, column=1, padx=(0, 8))
        self.select_button = app._button(footer, app.t("model_picker.select"), self._select, width=20)
        self.select_button.grid(row=0, column=2)
        app._guide(self.select_button, "tip.model_picker")
        self.window.bind("<Escape>", lambda _event: self.window.destroy())
        self._populate()
        self.window.update_idletasks()
        x = app.root.winfo_rootx() + max(0, (app.root.winfo_width() - self.window.winfo_width()) // 2)
        y = app.root.winfo_rooty() + max(0, (app.root.winfo_height() - self.window.winfo_height()) // 2)
        self.window.geometry(f"+{x}+{y}")
        app._style_native_window(self.window, dark=False)
        search.focus_set()

    def _filter(self, *_args) -> None:
        query = self.query.get().strip().casefold()
        self.filtered_models = tuple(model for model in self.all_models if query in model.casefold())
        self._populate()

    def _populate(self) -> None:
        for item in self.listbox.get_children(""):
            self.listbox.delete(item)
        for index, model in enumerate(self.filtered_models):
            self.listbox.insert("", "end", iid=str(index), text=model)
        self.count.configure(text=self.app.t("model_picker.count", count=len(self.filtered_models)))
        self.select_button.configure(state="normal" if self.filtered_models else "disabled")
        if self.filtered_models:
            current = self.app.model.get()
            try:
                index = self.filtered_models.index(current)
            except ValueError:
                index = 0
            self.listbox.selection_set(str(index))
            self.listbox.focus(str(index))
            self.listbox.see(str(index))

    def _select(self) -> None:
        selection = self.listbox.selection()
        if not selection:
            return
        self.app.model.set(self.filtered_models[int(selection[0])])
        self.window.destroy()


class RenWeaveDesktopApp:
    STEPS = ("model", "game", "languages", "review", "progress")
    LANGUAGE_CHOICES = ("English", "简体中文", "繁體中文", "日本語", "한국어", "Deutsch", "Français", "Español", "Português", "Русский")

    def __init__(
        self,
        root,
        *,
        initial_project: str = "",
        initial_workspace: str = "",
        settings_path: str | Path | None = None,
        credential_store: SecureCredentialStore | None = None,
    ) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.root.title("RenWeave")
        self.root.geometry("1240x840")
        self.root.minsize(900, 640)
        self.root.configure(background=Colors.SURFACE)
        self._configure_font_defaults()
        self._install_app_icon()

        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel_token: CancellationToken | None = None
        self.step = 0
        self.last_stage = ""
        self.last_active_stage = ""
        self.last_state_updated_at = ""
        self.progress_payload: dict[str, object] = {}
        self.resume_candidate: dict[str, object] | None = None
        self.token_budget: TokenBudget | None = None
        self.token_budget_target = ""
        self.logs: list[str] = []
        self.connection_state = "idle"
        self.connection_detail: dict[str, object] = {}
        self._suspend_provider_trace = False
        self.compact_layout = False
        self.narrow_layout = False
        self._responsive_render_id = None
        self._content_layout_id = None
        self._progress_animation_id = None
        self._restore_redraw_id = None
        self._restore_hidden = False
        self._displayed_progress_percent = 0.0
        self.progress_activity = None
        self._progress_activity_running = False
        self.translation_baseline: dict[str, str] | None = None
        # Entering the translation page is intentionally side-effect free.  This
        # flag distinguishes that preparation state from a real run so users can
        # freely inspect earlier pages before starting model work.
        self.translation_started = False
        self._settings_save_id = None
        self._project_inspection_id = None
        self._auto_sdk_path = ""
        self.existing_languages: list[ExistingLanguageSummary] = []
        self.scope_preview_status = "idle"
        self.scope_preview_signature: tuple[str, str, str, str] | None = None
        self.scope_preview_inventory: ExistingTranslationInventory | None = None
        self.scope_preview_budget: TokenBudget | None = None
        self.scope_preview_error = ""
        self.scope_preview_worker: threading.Thread | None = None
        self._session_keys: dict[tuple[str, str], str] = {}
        self._tooltips: list[GuidedTooltip] = []
        self.credential_store = credential_store or SecureCredentialStore()
        self.settings_path = Path(settings_path).expanduser() if settings_path else default_desktop_settings_path()
        saved_settings = self._load_desktop_settings()

        saved_locale = str(saved_settings.get("locale", "en"))
        initial_locale = saved_locale if saved_locale in {"en", "zh"} else "en"
        self.locale = tk.StringVar(value=initial_locale)
        self.locale_display = tk.StringVar(value="简体中文" if initial_locale == "zh" else "English")
        self.project = tk.StringVar(value=initial_project)
        self.workspace = tk.StringVar(value=initial_workspace)
        saved_provider_id = str(saved_settings.get("provider_id", "openai"))
        initial_preset = get_provider_preset(saved_provider_id if saved_provider_id in PROVIDER_PRESETS_BY_ID else "openai")
        self.provider = tk.StringVar()
        self.selected_provider_id = tk.StringVar(value=initial_preset.id)
        self.provider_name = tk.StringVar(value=str(saved_settings.get("provider_name", initial_preset.name)))
        self.base_url = tk.StringVar(value=str(saved_settings.get("base_url", initial_preset.base_url)))
        self.api_key_env = tk.StringVar(value=str(saved_settings.get("api_key_env", initial_preset.api_key_env)))
        self.supports_json = tk.BooleanVar(value=bool(saved_settings.get("supports_json", initial_preset.supports_json_parameter)))
        self.model = tk.StringVar(value=str(saved_settings.get("model", "")))
        self.model_choices: tuple[str, ...] = ()
        self.api_key = tk.StringVar()
        self.key_storage = tk.StringVar(value=str(saved_settings.get("key_storage", "secure")))
        if self.key_storage.get() not in {"secure", "memory"}:
            self.key_storage.set("secure")
        self.reasoning_level = tk.StringVar(value=str(saved_settings.get("reasoning_level", "auto")))
        if self.reasoning_level.get() not in {"auto", "low", "high", "maximum"}:
            self.reasoning_level.set("auto")
        self.reasoning_display = tk.StringVar(value=self.t(f"reasoning.{self.reasoning_level.get()}"))
        self.reasoning_display.trace_add("write", self._reasoning_selection_changed)
        self.update_checks_enabled = tk.BooleanVar(value=bool(saved_settings.get("update_checks_enabled", False)))
        self.update_result: UpdateResult | None = None
        self.update_check_state = "idle"
        self._update_worker: threading.Thread | None = None
        self.show_key = tk.BooleanVar(value=False)
        self.source_language = tk.StringVar(value="auto")
        self.target_language = tk.StringVar()
        self.source_language_display = tk.StringVar()
        self.target_language_display = tk.StringVar()
        self._language_display_snapshot = {"source": "", "target": ""}
        self.renpy_sdk = tk.StringVar()
        self.generate_rpa = tk.BooleanVar(value=True)
        self.install = tk.BooleanVar(value=False)
        self.require_engine = tk.BooleanVar(value=False)
        self.show_review_details = tk.BooleanVar(value=False)
        self.show_pending_details = tk.BooleanVar(value=False)
        self.show_package_technical = tk.BooleanVar(value=False)
        self.show_log_details = tk.BooleanVar(value=False)
        self.show_model_advanced = tk.BooleanVar(value=False)
        self.show_manual_paths = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value=self.t("model.idle_body"))

        self.start_button = None
        self.next_button = None
        self.back_button = None
        self.page_card = None
        self.review_options = None
        self.review_details = None
        self.review_pending_title = None
        self.review_detail_host = None
        self.review_detail_text = None
        self.progress = None
        self.log = None
        self.content = None
        self.content_canvas = None
        self.content_window = None
        self.content_scrollbar = None
        self.nav = None
        self._configure_styles()
        self._build_shell()
        self.root.report_callback_exception = self._report_callback_exception
        self.root.after(250, lambda: self._style_native_window(self.root, dark=True))
        self._restore_api_key()
        self._bind_provider_changes()
        self.project.trace_add("write", self._schedule_project_inspection)
        if initial_project and not initial_workspace:
            self._suggest_workspace(initial_project)
        if initial_project:
            self._inspect_project_selection()
        self._render()
        self.root.protocol("WM_DELETE_WINDOW", self._close_window)
        self.root.bind("<Configure>", self._on_root_configure, add="+")
        self.root.bind("<Map>", self._on_window_restored, add="+")
        self.root.bind("<Visibility>", self._on_window_restored, add="+")
        self.root.bind("<Unmap>", self._on_window_unmapped, add="+")
        self.root.bind_all("<MouseWheel>", self._on_content_mousewheel, add="+")
        self.root.after(150, self._poll_events)
        if self.update_checks_enabled.get():
            self.root.after(800, self._check_updates)

    def t(self, key: str, **values: object) -> str:
        text = COPY[self.locale.get()].get(key, COPY["en"].get(key, key))
        return text.format(**values) if values else text

    def _configure_font_defaults(self) -> None:
        from tkinter import font as tkfont

        roles = {
            "TkDefaultFont": 10,
            "TkTextFont": 10,
            "TkMenuFont": 10,
            "TkHeadingFont": 10,
            "TkCaptionFont": 10,
            "TkSmallCaptionFont": 9,
            "TkIconFont": 10,
            "TkTooltipFont": 9,
        }
        for name, size in roles.items():
            try:
                tkfont.nametofont(name).configure(family=Typography.UI, size=size)
            except self.tk.TclError:
                continue
        try:
            tkfont.nametofont("TkFixedFont").configure(family=Typography.MONO, size=9)
        except self.tk.TclError:
            pass

    def _report_callback_exception(self, exception_type, exception, exception_traceback) -> None:
        """Keep Tk callback failures inside the RenWeave visual system."""
        details = "".join(traceback.format_exception(exception_type, exception, exception_traceback))
        self._append_log(f"Error: {exception}")
        self._dialog(
            self.t("dialog.failed"),
            f"{self.t('progress.failed_body')}\n\n{exception}",
            error=True,
            details=details,
        )

    def _install_app_icon(self) -> None:
        icon = self.tk.PhotoImage(width=32, height=32)
        icon.put(Colors.NAV, to=(0, 0, 32, 32))
        icon.put(Colors.ACCENT, to=(5, 6, 10, 24))
        icon.put(Colors.PRIMARY, to=(11, 10, 16, 28))
        icon.put("#8B8CF6", to=(17, 6, 22, 24))
        icon.put(Colors.ACCENT, to=(23, 10, 28, 28))
        self.root.iconphoto(True, icon)
        self._app_icon = icon

    @staticmethod
    def _style_native_window(window, *, dark: bool) -> None:
        if os.name != "nt":
            return
        try:
            import ctypes
            from tkinter import TclError

            window.update_idletasks()
            widget_hwnd = window.winfo_id()
            wrapper_hwnd = ctypes.windll.user32.GetParent(widget_hwnd)

            def colorref(value: str) -> int:
                red, green, blue = (
                    int(value[index:index + 2], 16) for index in (1, 3, 5)
                )
                return red | (green << 8) | (blue << 16)

            for hwnd in {widget_hwnd, wrapper_hwnd}:
                if not hwnd:
                    continue
                dark_mode = ctypes.c_int(1 if dark else 0)
                border = ctypes.c_int(colorref(Colors.NAV if dark else Colors.OUTLINE_VARIANT))
                caption = ctypes.c_int(colorref(Colors.NAV if dark else Colors.CARD))
                text = ctypes.c_int(colorref(Colors.NAV_TEXT if dark else Colors.ON_SURFACE))
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, 20, ctypes.byref(dark_mode), ctypes.sizeof(dark_mode)
                )
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, 34, ctypes.byref(border), ctypes.sizeof(border)
                )
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, 35, ctypes.byref(caption), ctypes.sizeof(caption)
                )
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, 36, ctypes.byref(text), ctypes.sizeof(text)
                )
        except (AttributeError, OSError, TclError):
            return

    def _load_desktop_settings(self) -> dict[str, object]:
        try:
            payload = read_json(self.settings_path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _schedule_settings_save(self) -> None:
        if self._settings_save_id is not None:
            try:
                self.root.after_cancel(self._settings_save_id)
            except self.tk.TclError:
                pass
        self._settings_save_id = self.root.after(500, self._save_desktop_settings)

    def _save_desktop_settings(self) -> None:
        self._settings_save_id = None
        payload = {
            "schema_version": 1,
            "locale": self.locale.get(),
            "provider_id": self.selected_provider_id.get().strip() or "custom",
            "provider_name": self.provider_name.get().strip(),
            "base_url": self.base_url.get().strip(),
            "model": self.model.get().strip(),
            "api_key_env": self.api_key_env.get().strip(),
            "supports_json": bool(self.supports_json.get()),
            "key_storage": self.key_storage.get(),
            "reasoning_level": self.reasoning_level.get(),
            "update_checks_enabled": bool(self.update_checks_enabled.get()),
        }
        try:
            atomic_write_json(self.settings_path, payload)
        except OSError as exc:
            self.logs.append(f"Settings could not be saved: {exc}")

    def _credential_identity(self) -> tuple[str, str]:
        return (self.selected_provider_id.get().strip() or "custom", self.base_url.get().strip())

    def _save_api_key(self) -> bool:
        identity = self._credential_identity()
        secret = self.api_key.get()
        if secret:
            self._session_keys[identity] = secret
        if secret and self.key_storage.get() == "secure":
            try:
                self.credential_store.set(*identity, secret)
            except CredentialStorageError as exc:
                self.logs.append(str(exc))
                return False
        return True

    def _restore_api_key(self) -> None:
        identity = self._credential_identity()
        secret = self._session_keys.get(identity, "")
        if not secret and self.key_storage.get() == "secure":
            try:
                secret = self.credential_store.get(*identity)
            except CredentialStorageError as exc:
                self.logs.append(str(exc))
        self.api_key.set(secret)

    def _guide(self, widget, key: str):
        self._tooltips = [
            tooltip
            for tooltip in self._tooltips
            if tooltip.widget.winfo_exists()
        ]
        self._tooltips.append(GuidedTooltip(self, widget, key))
        return widget

    def _button(
        self,
        parent,
        text: str,
        command: Callable[[], None],
        *,
        kind: str = "primary",
        width: int | None = None,
    ):
        styles = {
            "primary": "Primary.TButton",
            "secondary": "Secondary.TButton",
            "ghost": "Ghost.TButton",
            "field": "FieldAction.TButton",
            "top": "TopAux.TButton",
        }
        default_width = Metrics.FIELD_ACTION_WIDTH if kind == "field" else Metrics.BUTTON_WIDTH
        return self.ttk.Button(
            parent,
            text=text,
            command=command,
            style=styles[kind],
            width=width or default_width,
            cursor="hand2",
            takefocus=True,
        )

    def _entry(self, parent, variable, *, secret: bool = False):
        return self.ttk.Entry(
            parent,
            textvariable=variable,
            style="Workspace.TEntry",
            show="•" if secret else "",
            takefocus=True,
        )

    def _combobox(self, parent, variable, values, *, readonly: bool = False, width: int | None = None):
        options = {
            "textvariable": variable,
            "values": values,
            "style": "Workspace.TCombobox",
            "state": "readonly" if readonly else "normal",
            "takefocus": True,
        }
        if width is not None:
            options["width"] = width
        return self.ttk.Combobox(parent, **options)

    def _scrollbar(self, parent, *, command):
        return self.ttk.Scrollbar(
            parent,
            orient="vertical",
            command=command,
            style="Workspace.Vertical.TScrollbar",
        )

    def _provider_display_name(self, preset) -> str:
        if self.locale.get() == "zh":
            return preset.name_zh
        return PROVIDER_SHORT_NAMES_EN.get(preset.id, preset.name)

    def _provider_icon(self, preset):
        """Build a compact geometric provider mark without font-dependent initials."""
        icon = self.tk.PhotoImage(width=20, height=20)
        for y in range(1, 19):
            inset = 4 if y in {1, 18} else 2 if y in {2, 17} else 1
            icon.put(preset.accent, to=(inset, y, 20 - inset, y + 1))
        icon.put("#FFFFFF", to=(5, 6, 7, 14))
        icon.put("#FFFFFF", to=(9, 4, 11, 16))
        icon.put("#FFFFFF", to=(13, 7, 15, 13))
        return icon

    @staticmethod
    def _display_path(value: str, *, max_chars: int = 52) -> str:
        rendered = value.strip()
        if len(rendered) <= max_chars:
            return rendered
        normalized = rendered.replace("\\", "/")
        parts = [part for part in normalized.split("/") if part]
        tail = "/".join(parts[-2:]) if len(parts) >= 2 else normalized[-(max_chars - 2):]
        if len(tail) > max_chars - 2:
            tail = tail[-(max_chars - 2):]
        return f"…/{tail}"

    @staticmethod
    def _format_token_count(value: int) -> str:
        value = max(0, int(value))
        if value >= 1_000_000:
            rendered = f"{value / 1_000_000:.1f}".rstrip("0").rstrip(".")
            return f"{rendered}M"
        if value >= 1_000:
            rendered = f"{value / 1_000:.1f}".rstrip("0").rstrip(".")
            return f"{rendered}K"
        return f"{value:,}"

    def _get_token_budget(self) -> TokenBudget | None:
        if (
            self.scope_preview_status == "ready"
            and self.scope_preview_signature == self._scope_signature()
            and self.scope_preview_budget is not None
        ):
            return self.scope_preview_budget
        target = self.project.get().strip()
        if not target:
            return None
        try:
            resolved = str(Path(target).expanduser().resolve())
        except OSError:
            return None
        if self.token_budget is not None and self.token_budget_target == resolved:
            return self.token_budget
        try:
            self.token_budget = estimate_project_tokens(resolved)
            self.token_budget_target = resolved
        except (OSError, ValueError, RuntimeError):
            self.token_budget = None
            self.token_budget_target = ""
        return self.token_budget

    def _configure_styles(self) -> None:
        style = self.ttk.Style(self.root)
        try:
            import sv_ttk

            sv_ttk.set_theme("light")
        except (ImportError, self.tk.TclError):
            if "clam" in style.theme_names():
                style.theme_use("clam")
        style.configure("App.TFrame", background=Colors.SURFACE)
        style.configure("TopBar.TFrame", background=Colors.CARD)
        style.configure("Footer.TFrame", background=Colors.SURFACE, relief="flat", borderwidth=0)
        style.configure("FooterRule.TFrame", background=Colors.OUTLINE_VARIANT, relief="flat", borderwidth=0)
        style.configure("FooterSlot.TFrame", background=Colors.SURFACE)
        style.configure("ActionBar.TFrame", background=Colors.SURFACE, relief="flat", borderwidth=0)
        style.configure("LanguageGroup.TFrame", background=Colors.SURFACE_HIGH, relief="flat", borderwidth=0)
        style.configure("Content.TFrame", background=Colors.SURFACE, padding=(36, 24))
        style.configure("Card.TFrame", background=Colors.CARD, relief="solid", borderwidth=1, bordercolor=Colors.OUTLINE_VARIANT)
        style.configure("CardBody.TFrame", background=Colors.CARD, relief="flat", borderwidth=0)
        style.configure("TintCard.TFrame", background=Colors.SURFACE_CONTAINER, relief="solid", borderwidth=1, bordercolor=Colors.OUTLINE_VARIANT)
        style.configure("Dialog.TFrame", background=Colors.CARD)
        style.configure("SuccessActions.TFrame", background=Colors.SUCCESS_CONTAINER)
        style.configure("TopLabel.TLabel", background=Colors.CARD, foreground=Colors.ON_SURFACE_VARIANT, font=(Typography.UI, Typography.SMALL, "bold"))
        style.configure("Headline.TLabel", background=Colors.SURFACE, foreground=Colors.ON_SURFACE, font=(Typography.UI, Typography.DISPLAY, "bold"))
        style.configure("Section.TLabel", background=Colors.CARD, foreground=Colors.ON_SURFACE, font=(Typography.UI, Typography.BODY, "bold"))
        style.configure("Body.TLabel", background=Colors.CARD, foreground=Colors.ON_SURFACE_VARIANT, font=(Typography.UI, Typography.BODY))
        style.configure("SurfaceBody.TLabel", background=Colors.SURFACE, foreground=Colors.ON_SURFACE_VARIANT, font=(Typography.UI, Typography.BODY))
        style.configure("Hint.TLabel", background=Colors.CARD, foreground=Colors.ON_SURFACE_VARIANT, font=(Typography.UI, Typography.SMALL))
        style.configure("ActionHint.TLabel", background=Colors.SURFACE, foreground=Colors.ON_SURFACE_VARIANT, font=(Typography.UI, Typography.SMALL))
        style.configure("ActionWarning.TLabel", background=Colors.SURFACE, foreground=Colors.WARNING, font=(Typography.UI, Typography.SMALL, "bold"))
        style.configure("AutoSave.TLabel", background=Colors.CARD, foreground=Colors.SUCCESS, font=(Typography.UI, Typography.SMALL, "bold"))
        style.configure("Field.TLabel", background=Colors.CARD, foreground=Colors.ON_SURFACE, font=(Typography.UI, Typography.BODY, "bold"))
        style.configure("DialogTitle.TLabel", background=Colors.CARD, foreground=Colors.ON_SURFACE, font=(Typography.UI, Typography.TITLE, "bold"))
        style.configure("DialogHint.TLabel", background=Colors.CARD, foreground=Colors.ON_SURFACE_VARIANT, font=(Typography.UI, Typography.SMALL))
        style.configure("ErrorBody.TLabel", background=Colors.CARD, foreground=Colors.ERROR, font=(Typography.UI, Typography.BODY))
        style.configure("Status.TLabel", background=Colors.SURFACE_CONTAINER, foreground=Colors.ON_SURFACE, font=(Typography.UI, Typography.BODY, "bold"))
        style.configure("StatusBody.TLabel", background=Colors.SURFACE_CONTAINER, foreground=Colors.ON_SURFACE_VARIANT, font=(Typography.UI, Typography.SMALL))
        style.configure("TintHint.TLabel", background=Colors.SURFACE_CONTAINER, foreground=Colors.ON_SURFACE_VARIANT, font=(Typography.UI, Typography.SMALL))
        style.configure("Primary.TButton", anchor="center", padding=(16, Metrics.CONTROL_PADDING_Y), font=(Typography.UI, Typography.BODY, "bold"), foreground=Colors.ON_PRIMARY, background=Colors.PRIMARY, borderwidth=0, relief="flat")
        style.map("Primary.TButton", background=[("pressed", Colors.PRIMARY_HOVER), ("active", Colors.PRIMARY_HOVER), ("disabled", Colors.OUTLINE_VARIANT)], foreground=[("disabled", Colors.CARD)], bordercolor=[("focus", Colors.ON_PRIMARY_CONTAINER)])
        style.configure("Secondary.TButton", anchor="center", padding=(14, Metrics.CONTROL_PADDING_Y), font=(Typography.UI, Typography.BODY, "bold"), foreground=Colors.ON_SURFACE, background=Colors.SURFACE_HIGH, borderwidth=0, relief="flat")
        style.map("Secondary.TButton", background=[("pressed", Colors.CONTROL_PRESSED), ("active", Colors.CONTROL_HOVER), ("disabled", Colors.SURFACE_HIGH)], foreground=[("disabled", Colors.OUTLINE)], bordercolor=[("focus", Colors.PRIMARY)])
        style.configure("Ghost.TButton", anchor="center", padding=(12, Metrics.CONTROL_PADDING_Y), font=(Typography.UI, Typography.BODY), foreground=Colors.ON_SURFACE_VARIANT, background=Colors.CARD, borderwidth=0, relief="flat")
        style.map("Ghost.TButton", background=[("pressed", Colors.SURFACE_HIGH), ("active", Colors.SURFACE_CONTAINER), ("disabled", Colors.CARD)], foreground=[("disabled", Colors.OUTLINE)], bordercolor=[("focus", Colors.PRIMARY), ("disabled", Colors.OUTLINE_VARIANT)], lightcolor=[("focus", Colors.PRIMARY)], darkcolor=[("focus", Colors.PRIMARY)])
        style.configure("TopAux.TButton", anchor="center", padding=(10, 6), font=(Typography.UI, Typography.SMALL), foreground=Colors.ON_SURFACE_VARIANT, background=Colors.CARD, borderwidth=0, relief="flat")
        style.map("TopAux.TButton", background=[("pressed", Colors.SURFACE_HIGH), ("active", Colors.SURFACE_CONTAINER)], foreground=[("focus", Colors.PRIMARY)], bordercolor=[("focus", Colors.PRIMARY)])
        style.configure("FieldAction.TButton", anchor="center", padding=(12, Metrics.CONTROL_PADDING_Y), font=(Typography.UI, Typography.BODY, "bold"), foreground=Colors.PRIMARY, background=Colors.SURFACE_HIGH, borderwidth=0, relief="flat")
        style.map("FieldAction.TButton", background=[("pressed", Colors.CONTROL_PRESSED), ("active", Colors.CONTROL_HOVER), ("disabled", Colors.SURFACE_HIGH)], foreground=[("disabled", Colors.OUTLINE)], bordercolor=[("focus", Colors.PRIMARY)])
        style.configure("Language.TButton", anchor="center", padding=(12, Metrics.CONTROL_PADDING_Y, 12, Metrics.CONTROL_PADDING_Y + 1), font=(Typography.UI, Typography.BODY), foreground=Colors.ON_SURFACE_VARIANT, background=Colors.SURFACE_HIGH, borderwidth=0, relief="flat")
        style.map("Language.TButton", background=[("focus", Colors.SURFACE_CONTAINER), ("pressed", Colors.SURFACE_CONTAINER), ("active", Colors.SURFACE_CONTAINER)], foreground=[("focus", Colors.PRIMARY)], bordercolor=[("focus", Colors.PRIMARY)])
        style.configure("LanguageActive.TButton", anchor="center", padding=(12, Metrics.CONTROL_PADDING_Y, 12, Metrics.CONTROL_PADDING_Y + 1), font=(Typography.UI, Typography.BODY, "bold"), foreground=Colors.ON_PRIMARY, background=Colors.PRIMARY, borderwidth=0, relief="flat")
        style.map("LanguageActive.TButton", background=[("pressed", Colors.CONTROL_PRESSED), ("active", Colors.CONTROL_HOVER)], bordercolor=[("focus", Colors.PRIMARY)])
        style.configure("Provider.TButton", anchor="w", padding=(14, 10), font=(Typography.UI, Typography.SMALL), foreground=Colors.ON_SURFACE, background=Colors.CARD, borderwidth=0, relief="flat")
        style.map("Provider.TButton", background=[("pressed", Colors.CONTROL_PRESSED), ("active", Colors.SURFACE_CONTAINER)], bordercolor=[("focus", Colors.PRIMARY)])
        style.configure("ProviderSelected.TButton", anchor="w", padding=(14, 10), font=(Typography.UI, Typography.SMALL, "bold"), foreground=Colors.ON_PRIMARY, background=Colors.PRIMARY, borderwidth=0, relief="flat")
        style.map("ProviderSelected.TButton", background=[("pressed", Colors.CONTROL_PRESSED), ("active", Colors.CONTROL_HOVER)], bordercolor=[("focus", Colors.PRIMARY)])
        style.configure("Nav.TButton", anchor="w", padding=(16, 13), font=(Typography.UI, Typography.BODY), foreground=Colors.NAV_MUTED, background=Colors.NAV, borderwidth=0, relief="flat")
        style.map("Nav.TButton", background=[("focus", Colors.NAV_ACTIVE), ("active", Colors.NAV_ACTIVE)], foreground=[("focus", Colors.NAV_TEXT), ("active", Colors.NAV_TEXT), ("disabled", Colors.NAV_MUTED)])
        style.configure("NavActive.TButton", anchor="w", padding=(16, 13), font=(Typography.UI, Typography.BODY, "bold"), foreground=Colors.NAV_TEXT, background=Colors.NAV_ACTIVE, borderwidth=0, relief="flat")
        style.map("NavActive.TButton", background=[("active", Colors.NAV_ACTIVE)], foreground=[("active", Colors.NAV_TEXT)])
        style.configure("NavNarrow.TButton", anchor="center", padding=(8, 13), font=(Typography.UI, Typography.BODY), foreground=Colors.NAV_MUTED, background=Colors.NAV, borderwidth=0, relief="flat")
        style.map("NavNarrow.TButton", background=[("focus", Colors.NAV_ACTIVE), ("active", Colors.NAV_ACTIVE)], foreground=[("focus", Colors.NAV_TEXT), ("active", Colors.NAV_TEXT), ("disabled", Colors.NAV_MUTED)])
        style.configure("NavNarrowActive.TButton", anchor="center", padding=(8, 13), font=(Typography.UI, Typography.BODY, "bold"), foreground=Colors.NAV_TEXT, background=Colors.NAV_ACTIVE, borderwidth=0, relief="flat")
        button_layout = [
            ("Button.border", {"sticky": "nswe", "children": [
                ("Button.padding", {"sticky": "nswe", "children": [
                    ("Button.label", {"sticky": "nswe"}),
                ]}),
            ]}),
        ]
        for button_style in (
            "Nav.TButton", "NavActive.TButton", "NavNarrow.TButton", "NavNarrowActive.TButton",
        ):
            style.layout(button_style, button_layout)
        try:
            accent_layout = style.layout("Accent.TButton")
            if accent_layout:
                style.layout("Primary.TButton", accent_layout)
                style.layout("ProviderSelected.TButton", accent_layout)
                style.layout("LanguageActive.TButton", accent_layout)
        except self.tk.TclError:
            pass
        style.configure("Workspace.TEntry", padding=(12, Metrics.CONTROL_PADDING_Y), font=(Typography.UI, Typography.BODY), fieldbackground=Colors.CARD, foreground=Colors.ON_SURFACE, bordercolor=Colors.OUTLINE_VARIANT, lightcolor=Colors.OUTLINE_VARIANT, darkcolor=Colors.OUTLINE_VARIANT, insertcolor=Colors.ON_SURFACE)
        style.map("Workspace.TEntry", bordercolor=[("focus", Colors.PRIMARY), ("disabled", Colors.OUTLINE_VARIANT)], lightcolor=[("focus", Colors.PRIMARY)], darkcolor=[("focus", Colors.PRIMARY)], fieldbackground=[("disabled", Colors.SURFACE_CONTAINER)], foreground=[("disabled", Colors.OUTLINE)])
        style.configure("Workspace.TCombobox", padding=(12, Metrics.CONTROL_PADDING_Y - 1), font=(Typography.UI, Typography.BODY), fieldbackground=Colors.CARD, foreground=Colors.ON_SURFACE, background=Colors.CARD, bordercolor=Colors.OUTLINE_VARIANT, arrowcolor=Colors.ON_SURFACE_VARIANT)
        style.map("Workspace.TCombobox", bordercolor=[("focus", Colors.PRIMARY)], fieldbackground=[("readonly", Colors.CARD), ("disabled", Colors.SURFACE_CONTAINER)], selectbackground=[("readonly", Colors.CARD)], selectforeground=[("readonly", Colors.ON_SURFACE)], arrowcolor=[("disabled", Colors.OUTLINE)])
        style.configure("Material.TCheckbutton", background=Colors.CARD, foreground=Colors.ON_SURFACE, font=(Typography.UI, Typography.BODY), padding=(0, 5), focuscolor=Colors.CARD)
        style.map("Material.TCheckbutton", background=[("active", Colors.CARD)], foreground=[("disabled", Colors.OUTLINE)])
        style.configure("Tint.TCheckbutton", background=Colors.SURFACE_CONTAINER, foreground=Colors.ON_SURFACE, font=(Typography.UI, Typography.BODY), padding=(0, 5), focuscolor=Colors.SURFACE_CONTAINER)
        style.map("Tint.TCheckbutton", background=[("active", Colors.SURFACE_CONTAINER)], foreground=[("disabled", Colors.OUTLINE)])
        style.configure("Material.TRadiobutton", background=Colors.CARD, foreground=Colors.ON_SURFACE, font=(Typography.UI, Typography.BODY), padding=(0, 5), focuscolor=Colors.CARD)
        style.map("Material.TRadiobutton", background=[("active", Colors.CARD)], foreground=[("focus", Colors.PRIMARY)])
        style.configure("ModelList.Treeview", background=Colors.CARD, fieldbackground=Colors.CARD, foreground=Colors.ON_SURFACE, bordercolor=Colors.OUTLINE_VARIANT, lightcolor=Colors.OUTLINE_VARIANT, darkcolor=Colors.OUTLINE_VARIANT, rowheight=Metrics.LIST_ROW_HEIGHT, font=(Typography.UI, Typography.BODY), relief="flat")
        style.map("ModelList.Treeview", background=[("selected", Colors.PRIMARY_CONTAINER)], foreground=[("selected", Colors.ON_PRIMARY_CONTAINER)])
        style.configure("Workspace.Vertical.TScrollbar", background=Colors.SCROLL_THUMB, troughcolor=Colors.SURFACE, bordercolor=Colors.SURFACE, arrowcolor=Colors.ON_SURFACE_VARIANT, lightcolor=Colors.SCROLL_THUMB, darkcolor=Colors.SCROLL_THUMB, width=10, arrowsize=10, relief="flat")
        style.map("Workspace.Vertical.TScrollbar", background=[("active", Colors.SCROLL_THUMB_HOVER), ("pressed", Colors.SCROLL_THUMB_HOVER)])
        style.configure("Horizontal.TProgressbar", background=Colors.PRIMARY, troughcolor=Colors.SURFACE_HIGH, bordercolor=Colors.SURFACE_HIGH, lightcolor=Colors.PRIMARY, darkcolor=Colors.PRIMARY, thickness=8)

    def _build_shell(self) -> None:
        self.shell = self.ttk.Frame(self.root, style="App.TFrame")
        self.shell.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        self.shell.columnconfigure(1, weight=1)
        self.shell.rowconfigure(1, weight=1)

        self.sidebar = self.tk.Frame(self.shell, background=Colors.NAV, width=Metrics.SIDEBAR_WIDTH)
        self.sidebar.grid(row=0, column=0, rowspan=3, sticky="nsew")
        self.sidebar.grid_propagate(False)
        self.sidebar.columnconfigure(0, weight=1)
        self.sidebar.rowconfigure(2, weight=1)

        self.brand = self.tk.Frame(self.sidebar, background=Colors.NAV)
        self.brand.grid(row=0, column=0, sticky="ew", padx=20, pady=(24, 20))
        self.brand.columnconfigure(1, weight=1)
        self.brand_mark = self.tk.Label(
            self.brand,
            image=self._app_icon,
            background=Colors.NAV,
            borderwidth=0,
        )
        self.brand_mark.grid(row=0, column=0, sticky="w")
        self.brand_title = self.tk.Label(
            self.brand,
            background=Colors.NAV,
            foreground=Colors.NAV_TEXT,
            font=(Typography.UI, Typography.TITLE, "bold"),
            anchor="w",
            justify="left",
            wraplength=215,
        )
        self.brand_title.grid(row=0, column=1, sticky="w", padx=(10, 0))
        self.brand_subtitle = self.tk.Label(
            self.brand,
            background=Colors.NAV,
            foreground=Colors.NAV_MUTED,
            font=(Typography.UI, Typography.SMALL),
            anchor="w",
            justify="left",
            wraplength=170,
        )
        self.brand_subtitle.grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 0))

        self.nav = self.tk.Frame(self.sidebar, background=Colors.NAV)
        self.nav.grid(row=1, column=0, sticky="ew", padx=14)
        self.privacy_label = self.tk.Label(
            self.sidebar,
            background=Colors.NAV_ACTIVE,
            foreground=Colors.NAV_MUTED,
            font=(Typography.UI, Typography.SMALL),
            justify="left",
            anchor="w",
            wraplength=160,
            padx=14,
            pady=12,
        )
        self.privacy_label.grid(row=3, column=0, sticky="ew", padx=18, pady=20)

        self.top = self.ttk.Frame(
            self.shell,
            style="TopBar.TFrame",
            padding=(Metrics.PAGE_INSET, 0, Metrics.PAGE_INSET + Metrics.SCROLLBAR_GUTTER, 0),
        )
        self.top.grid(row=0, column=1, sticky="ew")
        self.top.columnconfigure(1, weight=1)
        self.workspace_label = self.ttk.Label(self.top, style="TopLabel.TLabel")
        self.workspace_label.grid(row=0, column=0, sticky="w")
        self.settings_button = self._button(
            self.top, self.t("top.settings"), self._open_settings, kind="top", width=12
        )
        self.settings_button.grid(row=0, column=2, sticky="e", padx=(0, 10))
        self._guide(self.settings_button, "tip.settings")
        self.language_button = self._button(
            self.top,
            self._language_toggle_text(),
            self._toggle_locale,
            kind="top",
            width=12,
        )
        self.language_button.grid(row=0, column=3, sticky="e")

        content_host = self.ttk.Frame(self.shell, style="App.TFrame")
        content_host.grid(row=1, column=1, sticky="nsew")
        content_host.columnconfigure(0, weight=1)
        content_host.columnconfigure(1, minsize=Metrics.SCROLLBAR_GUTTER)
        content_host.rowconfigure(0, weight=1)
        self.content_canvas = self.tk.Canvas(
            content_host,
            background=Colors.SURFACE,
            borderwidth=0,
            highlightthickness=0,
            takefocus=False,
            yscrollincrement=Metrics.SCROLL_INCREMENT,
        )
        self.content_canvas.grid(row=0, column=0, sticky="nsew")
        self.content_scrollbar = self._scrollbar(content_host, command=self.content_canvas.yview)
        self.content_scrollbar.grid(row=0, column=1, sticky="ns")
        self.content_scrollbar.grid_remove()
        self.content_canvas.configure(yscrollcommand=self.content_scrollbar.set)
        self.content = self.ttk.Frame(
            self.content_canvas,
            style="Content.TFrame",
            padding=(Metrics.PAGE_INSET, 0, Metrics.PAGE_INSET, 0),
        )
        self.content_window = self.content_canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.content.columnconfigure(0, weight=1)
        self.content.rowconfigure(1, weight=1)
        self.content_canvas.bind("<Configure>", self._schedule_content_layout)
        self.content.bind("<Configure>", self._schedule_content_layout)

        self.footer = self.ttk.Frame(
            self.shell,
            style="Footer.TFrame",
            padding=(
                Metrics.PAGE_INSET,
                0,
                Metrics.PAGE_INSET + Metrics.SCROLLBAR_GUTTER,
                Metrics.SPACE_2,
            ),
        )
        self.footer.grid(row=2, column=1, sticky="ew")
        self.footer.columnconfigure(0, weight=1)

    def _change_locale(self, _event=None) -> None:
        self._set_locale("zh" if self.locale_display.get() == "简体中文" else "en")

    def _toggle_locale(self) -> None:
        self._set_locale("zh" if self.locale.get() == "en" else "en")

    def _language_toggle_text(self) -> str:
        return "中文" if self.locale.get() == "en" else "English"

    def _set_locale(self, locale: str) -> None:
        if locale not in {"en", "zh"}:
            return
        for tooltip in self._tooltips:
            tooltip._hide()
        self.locale.set(locale)
        self.locale_display.set("简体中文" if locale == "zh" else "English")
        self.reasoning_display.set(self.t(f"reasoning.{self.reasoning_level.get()}"))
        self._schedule_settings_save()
        self._render()

    def _on_root_configure(self, event) -> None:
        if event.widget is not self.root:
            return
        compact = event.width < Metrics.COMPACT_BREAKPOINT
        narrow = event.width < Metrics.NARROW_BREAKPOINT
        if compact == self.compact_layout and narrow == self.narrow_layout:
            return
        self.compact_layout = compact
        self.narrow_layout = narrow
        if self._responsive_render_id is not None:
            try:
                self.root.after_cancel(self._responsive_render_id)
            except self.tk.TclError:
                pass
        # Breakpoint work is lightweight. Apply it on the next event-loop turn so
        # the shell follows the resize gesture instead of trailing it by 100 ms.
        self._responsive_render_id = self.root.after(0, self._render_responsive)

    def _on_window_unmapped(self, event) -> None:
        """Hide the compositor layer while an iconified window has stale pixels."""
        if event.widget is not self.root:
            return
        self._restore_hidden = True
        try:
            self.root.attributes("-alpha", 0.0)
        except self.tk.TclError:
            self._restore_hidden = False

    def _on_window_restored(self, event) -> None:
        """Coalesce restore/visibility notifications into one immediate repaint."""
        if event.widget is not self.root:
            return
        try:
            if self.root.state() not in {"normal", "zoomed"}:
                return
        except self.tk.TclError:
            return
        if self._restore_redraw_id is not None:
            try:
                self.root.after_cancel(self._restore_redraw_id)
            except self.tk.TclError:
                pass
        self._restore_redraw_id = self.root.after_idle(self._redraw_after_restore)

    def _redraw_after_restore(self) -> None:
        self._restore_redraw_id = None
        try:
            if not self.root.winfo_exists() or self.root.state() not in {"normal", "zoomed"}:
                return
            self.root.update_idletasks()
            self._sync_content_layout()
        except self.tk.TclError:
            return
        try:
            if os.name == "nt":
                import ctypes

                widget_hwnd = self.root.winfo_id()
                wrapper_hwnd = ctypes.windll.user32.GetParent(widget_hwnd) or widget_hwnd
                # Invalidate all child surfaces without erasing cached pixels first.
                redraw_flags = 0x0001 | 0x0080 | 0x0100
                ctypes.windll.user32.RedrawWindow(wrapper_hwnd, None, None, redraw_flags)
                ctypes.windll.user32.UpdateWindow(wrapper_hwnd)
                self.root.update_idletasks()
                ctypes.windll.dwmapi.DwmFlush()
        except (AttributeError, OSError, self.tk.TclError):
            pass
        finally:
            if self._restore_hidden:
                try:
                    self.root.attributes("-alpha", 1.0)
                    self.root.update_idletasks()
                    if os.name == "nt":
                        import ctypes

                        ctypes.windll.dwmapi.DwmFlush()
                except (AttributeError, OSError, self.tk.TclError):
                    pass
                self._restore_hidden = False

    def _render_responsive(self) -> None:
        self._responsive_render_id = None
        self._apply_responsive_shell()

    def _on_content_mousewheel(self, event) -> None:
        if not self.content_canvas or not self.content_scrollbar or not self.content_scrollbar.winfo_ismapped():
            return
        try:
            pointer_x, pointer_y = self.root.winfo_pointerxy()
            left = self.content_canvas.winfo_rootx()
            top = self.content_canvas.winfo_rooty()
            right = left + self.content_canvas.winfo_width()
            bottom = top + self.content_canvas.winfo_height()
            pointed = self.root.winfo_containing(pointer_x, pointer_y)
        except self.tk.TclError:
            return
        if not (left <= pointer_x <= right and top <= pointer_y <= bottom):
            return
        if pointed is not None and pointed.winfo_class() in {"Text", "Treeview", "TCombobox"}:
            return
        if not event.delta:
            return
        notches = max(1, abs(int(event.delta)) // 120)
        units = -notches if event.delta > 0 else notches
        self.content_canvas.yview_scroll(units, "units")

    def _schedule_content_layout(self, _event=None) -> None:
        if _event is None:
            if self._content_layout_id is not None:
                try:
                    self.root.after_cancel(self._content_layout_id)
                except self.tk.TclError:
                    pass
            self._content_layout_id = self.root.after_idle(self._sync_content_layout)
            return
        if self._content_layout_id is not None:
            return
        # The nested canvas window is the expensive part of live resize. Keep
        # native window movement responsive and reflow the widget tree at a
        # steady 25 FPS, with the final Configure event guaranteeing the last
        # exact layout after the drag ends.
        self._content_layout_id = self.root.after(Metrics.RESIZE_LAYOUT_FRAME_MS, self._sync_content_layout)

    def _sync_content_layout(self) -> None:
        self._content_layout_id = None
        if not self.content_canvas or not self.content_window or not self.content_scrollbar:
            return
        try:
            viewport_width = max(1, self.content_canvas.winfo_width())
            viewport_height = max(1, self.content_canvas.winfo_height())
            requested_height = max(1, self.content.winfo_reqheight())
        except self.tk.TclError:
            return
        content_height = max(viewport_height, requested_height)
        bounds = self.content_canvas.bbox(self.content_window)
        current_width = 0 if bounds is None else bounds[2] - bounds[0]
        current_height = 0 if bounds is None else bounds[3] - bounds[1]
        if current_width != viewport_width or current_height != content_height:
            self.content_canvas.itemconfigure(self.content_window, width=viewport_width, height=content_height)
        self.content_canvas.configure(scrollregion=(0, 0, viewport_width, content_height))
        if requested_height > viewport_height + 1:
            self.content_scrollbar.grid()
        else:
            self.content_scrollbar.grid_remove()
            self.content_canvas.yview_moveto(0.0)

    def _apply_responsive_shell(self) -> None:
        sidebar_width = Metrics.NARROW_SIDEBAR_WIDTH if self.narrow_layout else Metrics.SIDEBAR_WIDTH
        self.sidebar.configure(width=sidebar_width)
        self.brand.grid_configure(
            padx=12 if self.narrow_layout else 20,
            pady=(20, 18) if self.narrow_layout else (24, 20),
        )
        brand_text = "RW" if self.narrow_layout and self.locale.get() == "en" else self.t("app_title")
        self.brand_title.configure(
            text=brand_text,
            font=(Typography.UI, Typography.TITLE, "bold"),
            wraplength=52 if self.narrow_layout else 188,
        )
        self.brand_subtitle.configure(text=self.t("app_subtitle"))
        if self.narrow_layout:
            self.brand_mark.grid_remove()
            self.brand_title.grid_configure(column=0, columnspan=2, sticky="ew", padx=0)
            self.brand_subtitle.grid_remove()
            self.privacy_label.grid_remove()
        else:
            self.brand_mark.grid()
            self.brand_title.grid_configure(column=1, columnspan=1, sticky="w", padx=(10, 0))
            self.brand_subtitle.grid()
            self.privacy_label.grid()
        self.workspace_label.configure(text=self.t("workspace_label"))
        self.settings_button.configure(text=self.t("top.settings"))
        self.language_button.configure(text=self._language_toggle_text())
        self.privacy_label.configure(text=self.t("nav_privacy"))
        inset = Metrics.COMPACT_PAGE_INSET if self.compact_layout else Metrics.PAGE_INSET
        aligned_right_inset = inset + Metrics.SCROLLBAR_GUTTER
        self.top.configure(padding=(inset, 10, aligned_right_inset, 10))
        self.content.configure(padding=(inset, 0, inset, 0))
        self.footer.configure(padding=(inset, 0, aligned_right_inset, Metrics.SPACE_2))
        for index, button in enumerate(getattr(self, "nav_buttons", [])):
            is_current = index == self.step
            prefix = "✓" if index < self.step else f"{index + 1:02d}"
            if self.narrow_layout:
                text = prefix
                button_style = "NavNarrowActive.TButton" if is_current else "NavNarrow.TButton"
            else:
                text = f"{prefix}    {self.t(f'steps.{self.STEPS[index]}')}"
                button_style = "NavActive.TButton" if is_current else "Nav.TButton"
            button.configure(text=text, style=button_style)
            self.nav_indicators[index].configure(
                background=Colors.PRIMARY if is_current else Colors.NAV
            )
        self._schedule_content_layout()

    def _render(self) -> None:
        if self.progress_activity is not None:
            try:
                self.progress_activity.stop()
            except self.tk.TclError:
                pass
            self.progress_activity = None
            self._progress_activity_running = False
        if self._progress_animation_id is not None:
            try:
                self.root.after_cancel(self._progress_animation_id)
            except self.tk.TclError:
                pass
            self._progress_animation_id = None
        self._apply_responsive_shell()
        self.next_button = None
        self.back_button = None
        self.start_button = None
        self.page_card = None
        self.review_options = None
        self.review_details = None
        self.review_pending_title = None
        self.review_detail_host = None
        self.review_detail_text = None
        for parent in (self.nav, self.content, self.footer):
            for child in parent.winfo_children():
                child.destroy()
        self._render_nav()
        self._render_header()
        getattr(self, f"_render_{self.STEPS[self.step]}")()
        self._render_footer()
        self.root.update_idletasks()
        self._sync_content_layout()
        self._schedule_content_layout()

    def _render_nav(self) -> None:
        self.nav_buttons = []
        self.nav_indicators = []
        for index, step in enumerate(self.STEPS):
            prefix = "✓" if index < self.step else f"{index + 1:02d}"
            is_current = index == self.step
            is_available = (self.step < 4 and index <= self.step) or (
                self.step == 4 and self._can_leave_translation() and index < 4
            )
            if self.narrow_layout:
                button_text = prefix
                button_style = "NavNarrowActive.TButton" if is_current else "NavNarrow.TButton"
            else:
                button_text = f"{prefix}    {self.t(f'steps.{step}')}"
                button_style = "NavActive.TButton" if is_current else "Nav.TButton"
            row = self.tk.Frame(self.nav, background=Colors.NAV)
            row.grid(row=index, column=0, sticky="ew", pady=2)
            row.columnconfigure(1, weight=1)
            indicator = self.tk.Frame(
                row,
                background=Colors.PRIMARY if is_current else Colors.NAV,
                width=3,
            )
            indicator.grid(row=0, column=0, sticky="ns")
            indicator.grid_propagate(False)
            button = self.ttk.Button(
                row,
                text=button_text,
                command=lambda selected=index: self._go_to_step(selected),
                style=button_style,
                cursor="hand2" if is_available else "arrow",
                state="normal" if is_available else "disabled",
                takefocus=is_available,
            )
            button.grid(row=0, column=1, sticky="ew")
            self.nav_buttons.append(button)
            self.nav_indicators.append(indicator)
        self.nav.columnconfigure(0, weight=1)

    def _render_header(self) -> None:
        step_name = self.STEPS[self.step]
        step_badge = self.tk.Label(
            self.content,
            text=self.t("step_count", current=self.step + 1, total=len(self.STEPS)),
            background=Colors.PRIMARY_CONTAINER,
            foreground=Colors.ON_PRIMARY_CONTAINER,
            font=(Typography.UI, Typography.SMALL, "bold"),
            padx=10,
            pady=5,
        )
        step_badge.grid(row=0, column=0, sticky="w")
        body = self.ttk.Frame(self.content, style="App.TFrame")
        body.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)
        self.page = body
        title_key = f"progress.task.{self._task_presentation().state.value}" if step_name == "progress" else f"{step_name}.title"
        self.page_title_label = self.ttk.Label(body, text=self.t(title_key), style="Headline.TLabel")
        self.page_title_label.grid(row=0, column=0, sticky="w")
        self.ttk.Label(
            body,
            text=self.t(f"{step_name}.body"),
            style="SurfaceBody.TLabel",
            wraplength=620 if self.compact_layout else 720,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(6, 14))

    def _card(self, parent=None, *, row: int = 2, padding: int = Metrics.CARD_PADDING):
        card = self.ttk.Frame(parent or self.page, style="Card.TFrame", padding=padding)
        card.grid(row=row, column=0, sticky="nsew")
        card.columnconfigure(0, weight=1)
        self.page_card = card
        return card

    def _field(self, parent, row: int, label: str, variable, *, hint: str = "", secret: bool = False):
        self.ttk.Label(parent, text=label, style="Field.TLabel").grid(row=row, column=0, sticky="w", pady=(0 if row == 0 else 14, 5))
        entry = self._entry(parent, variable, secret=secret and not self.show_key.get())
        entry.grid(row=row + 1, column=0, sticky="ew")
        if hint:
            self.ttk.Label(parent, text=hint, style="Hint.TLabel", wraplength=680, justify="left").grid(row=row + 2, column=0, sticky="w", pady=(4, 0))
        return entry

    def _render_model(self) -> None:
        card = self._card()
        card.columnconfigure(0, weight=1)

        title_row = self.ttk.Frame(card, style="CardBody.TFrame")
        title_row.grid(row=0, column=0, sticky="ew")
        title_row.columnconfigure(0, weight=1)
        self.ttk.Label(title_row, text=self.t("provider.choose"), style="Section.TLabel").grid(row=0, column=0, sticky="w")
        self.ttk.Label(
            title_row,
            text=self.t("model.settings_saved"),
            style="AutoSave.TLabel",
        ).grid(row=0, column=1, sticky="e")
        self.ttk.Label(card, text=self.t("provider.choose_hint"), style="Hint.TLabel").grid(row=1, column=0, sticky="w", pady=(4, 10))

        sequence = self.tk.Frame(card, background=Colors.SURFACE_CONTAINER, padx=14, pady=7)
        sequence.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        self.tk.Label(
            sequence,
            text=self.t("model.sequence"),
            background=Colors.SURFACE_CONTAINER,
            foreground=Colors.ON_SURFACE_VARIANT,
            font=(Typography.UI, Typography.SMALL, "bold"),
            anchor="w",
            justify="left",
            wraplength=680 if self.compact_layout else 760,
        ).pack(fill="x")

        preset_grid = self.tk.Frame(card, background=Colors.CARD)
        preset_grid.grid(row=3, column=0, sticky="ew")
        self.provider_grid = preset_grid
        self.provider_columns = 3 if self.compact_layout else 4
        for column in range(self.provider_columns):
            preset_grid.columnconfigure(column, weight=1, uniform="provider")
        selected_id = self.selected_provider_id.get()
        self.provider_buttons = {}
        self.provider_icons = {}
        for preset in PROVIDER_PRESETS:
            selected = preset.id == selected_id
            icon = self._provider_icon(preset)
            button = self.ttk.Button(
                preset_grid,
                text=self._provider_display_name(preset),
                image=icon,
                compound="left",
                command=lambda preset_id=preset.id: self._apply_provider_preset(preset_id),
                style="ProviderSelected.TButton" if selected else "Provider.TButton",
                cursor="hand2",
                takefocus=True,
            )
            self.provider_icons[preset.id] = icon
            self.provider_buttons[preset.id] = button
            self._guide(button, "tip.provider")
        self._layout_provider_buttons()

        preset = PROVIDER_PRESETS_BY_ID.get(selected_id, get_provider_preset("custom"))
        selection = self.tk.Frame(card, background=Colors.SURFACE_CONTAINER, padx=14, pady=9)
        selection.grid(row=4, column=0, sticky="ew", pady=(2, 14))
        category = self.t(f"provider.{preset.category}")
        self.provider_category = self.tk.Label(selection, text=category.upper(), background=preset.accent, foreground="#FFFFFF", font=(Typography.UI, Typography.SMALL, "bold"), padx=7, pady=3)
        self.provider_category.pack(side="left")
        self.provider_description = self.tk.Label(
            selection,
            text=preset.localized_description(self.locale.get()),
            background=Colors.SURFACE_CONTAINER,
            foreground=Colors.ON_SURFACE_VARIANT,
            font=(Typography.UI, Typography.SMALL),
            anchor="w",
            justify="left",
            wraplength=420 if self.compact_layout else 650,
        )
        self.provider_description.pack(side="left", fill="x", expand=True, padx=(10, 0))

        separator = self.tk.Frame(card, background=Colors.OUTLINE_VARIANT, height=1)
        separator.grid(row=5, column=0, sticky="ew", pady=(2, 16))
        config = self.ttk.Frame(card, style="CardBody.TFrame")
        config.grid(row=6, column=0, sticky="nsew")
        config.columnconfigure(0, weight=1)
        if not self.compact_layout:
            config.columnconfigure(1, weight=1)
        left = self.ttk.Frame(config, style="CardBody.TFrame")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 0 if self.compact_layout else 16))
        left.columnconfigure(0, weight=1)
        self.ttk.Label(left, text=self.t("model.config"), style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 7))
        key_header = self.ttk.Frame(left, style="CardBody.TFrame")
        key_header.grid(row=1, column=0, sticky="ew")
        key_header.columnconfigure(0, weight=1)
        self.ttk.Label(key_header, text=self.t("model.key"), style="Field.TLabel").grid(row=0, column=0, sticky="w")
        show_key_button = self.ttk.Checkbutton(key_header, text=self.t("model.show_key"), variable=self.show_key, style="Material.TCheckbutton", command=self._toggle_key)
        show_key_button.grid(row=0, column=1, sticky="e")
        self._guide(show_key_button, "tip.show_key")
        self.api_key_entry = self._entry(left, self.api_key, secret=not self.show_key.get())
        self.api_key_entry.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        self._guide(self.api_key_entry, "tip.api_key")
        self.ttk.Label(left, text=self.t("model.key_hint"), style="Hint.TLabel", wraplength=390, justify="left").grid(row=3, column=0, sticky="w", pady=(3, 0))
        self.connect_button = self._button(
            left,
            self.t("model.load"),
            self._connect_models,
            kind="secondary",
            width=22,
        )
        self.connect_button.grid(row=4, column=0, sticky="w", pady=(9, 0))
        self._guide(self.connect_button, "tip.load_models")
        self.ttk.Label(left, text=self.t("model.load_effect"), style="Hint.TLabel", wraplength=370, justify="left").grid(row=5, column=0, sticky="w", pady=(4, 0))

        right = self.ttk.Frame(config, style="CardBody.TFrame")
        right.grid(
            row=1 if self.compact_layout else 0,
            column=0 if self.compact_layout else 1,
            sticky="nsew",
            padx=(0, 0) if self.compact_layout else (16, 0),
            pady=(16, 0) if self.compact_layout else (0, 0),
        )
        right.columnconfigure(0, weight=1)
        self.ttk.Label(right, text=self.t("model.validation"), style="Section.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 7))
        self.ttk.Label(right, text=self.t("model.model"), style="Field.TLabel").grid(row=1, column=0, columnspan=2, sticky="w")
        self.model_box = self._entry(right, self.model)
        self.model_box.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        self._guide(self.model_box, "tip.model_id")
        self.browse_models_button = self._button(right, self.t("model.browse", count=len(self.model_choices)), self._browse_models, kind="field")
        self.browse_models_button.grid(row=2, column=1, sticky="e", padx=(7, 0), pady=(4, 0))
        self._guide(self.browse_models_button, "tip.browse_models")
        self.ttk.Label(right, text=self.t("model.model_hint"), style="Hint.TLabel", wraplength=350, justify="left").grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.verify_button = self._button(right, self.t("model.verify"), self._verify_model)
        self.verify_button.grid(row=4, column=0, columnspan=2, sticky="w", pady=(9, 0))
        self._guide(self.verify_button, "tip.verify_model")
        self.ttk.Label(right, text=self.t("model.verify_effect"), style="Hint.TLabel", wraplength=370, justify="left").grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))

        status_card = self.ttk.Frame(right, style="TintCard.TFrame", padding=10)
        status_card.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(7, 0))
        self.model_status_card = status_card
        status_card.columnconfigure(1, weight=1)
        state_key = self.connection_state if self.connection_state in {"idle", "connecting", "connected", "verifying", "verified", "failed", "changed", "discovery_failed"} else "idle"
        details = dict(self.connection_detail)
        if state_key in {"failed", "discovery_failed"}:
            details["message"] = self._friendly_error(details.get("message", ""))
        detail_key = f"model.{state_key}_body"
        title = self.t(f"model.{state_key}")
        body = self.t(detail_key, **details) if detail_key in COPY[self.locale.get()] else str(details.get("message", ""))
        if state_key == "failed":
            body = str(details.get("message", ""))
        self.model_status_indicator = self.tk.Frame(
            status_card,
            background=self._connection_state_color(state_key),
            width=4,
        )
        self.model_status_indicator.grid(row=0, column=0, rowspan=3, sticky="ns", padx=(0, 10))
        self.model_status_indicator.grid_propagate(False)
        self.model_status_title = self.ttk.Label(status_card, text=title, style="Status.TLabel")
        self.model_status_title.grid(row=0, column=1, sticky="w")
        self.model_retry_button = self._button(
            status_card,
            self.t("retry"),
            self._verify_model,
            kind="field",
            width=10,
        )
        self.model_retry_button.grid(row=0, column=2, sticky="e", padx=(8, 0))
        self.model_retry_button.grid_remove()
        self.model_status_body = self.ttk.Label(status_card, text=body, style="StatusBody.TLabel", wraplength=360, justify="left")
        self.model_status_body.grid(row=1, column=1, columnspan=2, sticky="w", pady=(4, 0))
        self.model_busy_progress = self.ttk.Progressbar(
            status_card,
            mode="indeterminate",
            style="Horizontal.TProgressbar",
        )
        self.model_busy_progress.grid(row=2, column=1, columnspan=2, sticky="ew", pady=(8, 0))
        self.model_busy_progress.grid_remove()

        advanced_toggle = self._button(
            selection,
            self.t("model.advanced_hide" if self.show_model_advanced.get() else "model.advanced_show"),
            lambda: (self.show_model_advanced.set(not self.show_model_advanced.get()), self._render()),
            kind="field",
        )
        self.model_advanced_toggle = advanced_toggle
        advanced_toggle.pack(side="right", padx=(10, 0))
        advanced = self.ttk.Frame(config, style="TintCard.TFrame", padding=12)
        advanced.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        advanced.columnconfigure(0, weight=1)
        advanced.columnconfigure(1, weight=1)
        self.model_advanced_panel = advanced
        endpoint_host = self.ttk.Frame(advanced, style="TintCard.TFrame")
        endpoint_host.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        endpoint_host.columnconfigure(0, weight=1)
        self.ttk.Label(endpoint_host, text=self.t("model.endpoint"), style="Status.TLabel").grid(row=0, column=0, sticky="w")
        self.endpoint_box = self._combobox(endpoint_host, self.base_url, preset.base_urls)
        self.endpoint_box.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self._guide(self.endpoint_box, "tip.endpoint")
        reasoning_host = self.ttk.Frame(advanced, style="TintCard.TFrame")
        reasoning_host.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        reasoning_host.columnconfigure(0, weight=1)
        self.ttk.Label(reasoning_host, text=self.t("model.reasoning"), style="Status.TLabel").grid(row=0, column=0, sticky="w")
        reasoning_values = tuple(self.t(f"reasoning.{level}") for level in ("auto", "low", "high", "maximum"))
        self.reasoning_box = self._combobox(reasoning_host, self.reasoning_display, reasoning_values, readonly=True)
        self.reasoning_box.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        if preset.reasoning_control == "none":
            self.reasoning_box.configure(state="disabled")
        self._guide(self.reasoning_box, "tip.reasoning")
        self.reasoning_hint_label = self.ttk.Label(
            reasoning_host,
            text=self.t("model.reasoning_unavailable" if preset.reasoning_control == "none" else "model.reasoning_hint"),
            style="TintHint.TLabel", wraplength=350, justify="left",
        )
        self.reasoning_hint_label.grid(row=2, column=0, sticky="w", pady=(4, 0))
        if not self.show_model_advanced.get():
            advanced.grid_remove()

        self._refresh_model_panel()

    def _layout_provider_buttons(self) -> None:
        for button in getattr(self, "provider_buttons", {}).values():
            button.grid_remove()
        columns = max(1, getattr(self, "provider_columns", 4))
        for index, preset in enumerate(PROVIDER_PRESETS):
            column = index % columns
            self.provider_buttons[preset.id].grid(
                row=index // columns,
                column=column,
                sticky="ew",
                padx=(0, 8 if column < columns - 1 else 0),
                pady=(0, 8),
            )

    def _browse_models(self) -> None:
        if self.model_choices:
            ModelPickerDialog(self)

    @staticmethod
    def _connection_state_color(state: str) -> str:
        if state in {"connected", "verified"}:
            return Colors.SUCCESS
        if state in {"failed", "discovery_failed"}:
            return Colors.ERROR
        if state in {"connecting", "verifying"}:
            return Colors.PRIMARY
        if state == "changed":
            return Colors.WARNING
        return Colors.OUTLINE

    def _friendly_error(self, error: object) -> str:
        message = str(error)
        lowered = message.casefold()
        if any(token in lowered for token in ("401", "403", "unauthorized", "forbidden", "api key", "authentication")):
            return self.t("error.api_key")
        if any(token in lowered for token in ("timeout", "timed out")):
            return self.t("error.timeout")
        if any(token in lowered for token in ("connection", "dns", "network", "name resolution")):
            return self.t("error.connection")
        return self.t("error.generic")

    def _refresh_model_panel(self) -> None:
        """Refresh model setup in place so button clicks never rebuild or jump the page."""
        if self.step != 0 or not hasattr(self, "model_status_title"):
            return
        selected_id = self.selected_provider_id.get()
        preset = PROVIDER_PRESETS_BY_ID.get(selected_id, get_provider_preset("custom"))
        for provider_id, button in getattr(self, "provider_buttons", {}).items():
            button.configure(style="ProviderSelected.TButton" if provider_id == selected_id else "Provider.TButton")
        self.provider_category.configure(
            text=self.t(f"provider.{preset.category}").upper(),
            background=preset.accent,
        )
        self.provider_description.configure(
            text=preset.localized_description(self.locale.get()),
            wraplength=420 if self.compact_layout else 650,
        )
        self.endpoint_box.configure(values=preset.base_urls)
        self.reasoning_box.configure(state="disabled" if preset.reasoning_control == "none" else "readonly")
        self.reasoning_hint_label.configure(
            text=self.t("model.reasoning_unavailable" if preset.reasoning_control == "none" else "model.reasoning_hint")
        )
        self.browse_models_button.configure(
            text=self.t("model.browse", count=len(self.model_choices)),
            state="normal" if self.model_choices else "disabled",
        )
        state_key = self.connection_state if self.connection_state in {
            "idle", "connecting", "connected", "verifying", "verified", "failed", "changed", "discovery_failed"
        } else "idle"
        details = dict(self.connection_detail)
        detail_key = f"model.{state_key}_body"
        title = self.t(f"model.{state_key}")
        body = self.t(detail_key, **details) if detail_key in COPY[self.locale.get()] else str(details.get("message", ""))
        if state_key == "failed":
            body = str(details.get("message", ""))
        self.model_status_title.configure(text=title)
        self.model_status_body.configure(text=body)
        self.model_status_indicator.configure(background=self._connection_state_color(state_key))
        busy = state_key in {"connecting", "verifying"}
        if busy:
            self.model_retry_button.grid_remove()
            self.model_busy_progress.grid()
            self.model_busy_progress.start(12)
        else:
            self.model_busy_progress.stop()
            self.model_busy_progress.grid_remove()
            if state_key in {"failed", "discovery_failed"}:
                retry_command = self._connect_models if state_key == "discovery_failed" else self._verify_model
                self.model_retry_button.configure(command=retry_command, text=self.t("retry"))
                self.model_retry_button.grid()
            else:
                self.model_retry_button.grid_remove()
        self.connect_button.configure(
            text=self.t("model.connecting") if state_key == "connecting" else self.t("model.load")
        )
        self.verify_button.configure(
            text=self.t("model.verifying") if state_key == "verifying" else self.t("model.verify")
        )
        self.connect_button.configure(state="disabled" if busy else "normal")
        self.verify_button.configure(state="disabled" if busy or not self.model.get().strip() else "normal")
        if self.next_button is not None:
            self.next_button.configure(state="normal" if state_key == "verified" else "disabled")
        self._schedule_content_layout()

    def _apply_provider_preset(self, preset_id: str) -> None:
        if preset_id == self.selected_provider_id.get() or (self.worker and self.worker.is_alive()):
            return
        preset = get_provider_preset(preset_id)
        self._save_api_key()
        self._suspend_provider_trace = True
        try:
            self.selected_provider_id.set(preset.id)
            self.provider_name.set(preset.name)
            self.base_url.set(preset.base_url)
            self.api_key_env.set(preset.api_key_env)
            self.supports_json.set(preset.supports_json_parameter)
            self.api_key.set("")
            self.model_choices = ()
            self.model.set("")
            self.provider.set("")
        finally:
            self._suspend_provider_trace = False
        self.connection_state = "idle"
        self.connection_detail = {}
        self._restore_api_key()
        self._schedule_settings_save()
        self._refresh_model_panel()

    def _toggle_key(self) -> None:
        if hasattr(self, "api_key_entry"):
            self.api_key_entry.configure(show="" if self.show_key.get() else "•")

    def _reasoning_selection_changed(self, *_args) -> None:
        selected = self.reasoning_display.get()
        for level in ("auto", "low", "high", "maximum"):
            if selected == self.t(f"reasoning.{level}") and self.reasoning_level.get() != level:
                self.reasoning_level.set(level)
                break

    def _open_settings(self) -> SettingsDialog:
        return SettingsDialog(self)

    def _forget_api_key(self) -> None:
        identity = self._credential_identity()
        self._session_keys.pop(identity, None)
        self.api_key.set("")
        try:
            self.credential_store.delete(*identity)
        except CredentialStorageError as exc:
            self._dialog(self.t("settings.key_error"), str(exc), error=True)

    def _check_updates(self, *, manual: bool = False) -> None:
        if self._update_worker and self._update_worker.is_alive():
            return
        self.update_check_state = "checking"

        def run() -> None:
            try:
                result = check_for_updates(__version__)
                self.events.put(("update_result", (result, manual)))
            except BaseException as exc:
                self.events.put(("update_error", (exc, manual)))

        self._update_worker = threading.Thread(target=run, daemon=True)
        self._update_worker.start()

    def _render_game(self) -> None:
        card = self._card()
        self._path_field(card, 0, self.t("game.project"), self.project, self.t("game.project_hint"), self._browse_project, "tip.game")
        self._path_field(card, 3, self.t("game.workspace"), self.workspace, self.t("game.workspace_hint"), self._browse_workspace, "tip.workspace")
        self.ttk.Label(card, text=self.t("game.advanced"), style="Section.TLabel").grid(row=6, column=0, sticky="w", pady=(20, 8))
        runtime_found = bool(self.renpy_sdk.get().strip())
        runtime = self.tk.Frame(
            card,
            background=Colors.SUCCESS_CONTAINER if runtime_found else Colors.SURFACE_CONTAINER,
            padx=12,
            pady=9,
        )
        runtime.grid(row=7, column=0, sticky="ew")
        self.tk.Label(
            runtime,
            text="✓" if runtime_found else "i",
            background=Colors.SUCCESS_CONTAINER if runtime_found else Colors.SURFACE_CONTAINER,
            foreground=Colors.SUCCESS if runtime_found else Colors.ON_SURFACE_VARIANT,
            font=(Typography.UI, Typography.BODY, "bold"),
        ).pack(side="left")
        self.tk.Label(
            runtime,
            text=(
                self.t("game.runtime_found", path=self._display_path(self.renpy_sdk.get(), max_chars=70))
                if runtime_found
                else self.t("game.runtime_missing")
            ),
            background=Colors.SUCCESS_CONTAINER if runtime_found else Colors.SURFACE_CONTAINER,
            foreground=Colors.ON_SURFACE_VARIANT,
            font=(Typography.UI, Typography.SMALL),
            wraplength=650,
            justify="left",
            anchor="w",
        ).pack(side="left", fill="x", expand=True, padx=(9, 0))
        change_runtime = self._button(runtime, self.t("game.change"), self._browse_sdk, kind="field", width=9)
        change_runtime.pack(side="right", padx=(10, 0))
        self._guide(change_runtime, "tip.sdk")
        self.ttk.Label(card, text=self.t("game.sdk_hint"), style="Hint.TLabel", wraplength=680, justify="left").grid(row=8, column=0, sticky="w", pady=(4, 0))
        self.ttk.Checkbutton(card, text=self.t("game.require_engine"), variable=self.require_engine, style="Material.TCheckbutton").grid(row=9, column=0, sticky="w", pady=(12, 0))
        safety = self.tk.Frame(card, background=Colors.SUCCESS_CONTAINER, padx=12, pady=9)
        safety.grid(row=10, column=0, sticky="ew", pady=(16, 0))
        self.tk.Label(safety, text="✓", background=Colors.SUCCESS_CONTAINER, foreground=Colors.SUCCESS, font=(Typography.UI, Typography.BODY, "bold")).pack(side="left")
        safety_copy = self.tk.Frame(safety, background=Colors.SUCCESS_CONTAINER)
        safety_copy.pack(side="left", fill="x", expand=True, padx=(9, 0))
        self.tk.Label(safety_copy, text=self.t("game.safety_title"), background=Colors.SUCCESS_CONTAINER, foreground=Colors.SUCCESS, font=(Typography.UI, Typography.BODY, "bold"), justify="left", anchor="w").pack(fill="x")
        self.tk.Label(safety_copy, text=self.t("game.safety_body"), background=Colors.SUCCESS_CONTAINER, foreground=Colors.ON_SURFACE_VARIANT, font=(Typography.UI, Typography.SMALL), wraplength=540 if self.compact_layout else 700, justify="left", anchor="w").pack(fill="x", pady=(2, 0))

    def _path_field(self, parent, row: int, label: str, variable, hint: str, command: Callable[[], None], tooltip_key: str) -> None:
        self.ttk.Label(parent, text=label, style="Field.TLabel").grid(row=row, column=0, sticky="w")
        value = variable.get().strip()
        selected = Path(value).expanduser() if value else None
        name = selected.name if selected and selected.name else self.t("game.not_selected")
        summary = self.ttk.Frame(parent, style="TintCard.TFrame", padding=(12, 9))
        summary.grid(row=row + 1, column=0, sticky="ew", pady=(5, 0))
        summary.columnconfigure(0, weight=1)
        self.ttk.Label(summary, text=name, style="Status.TLabel").grid(row=0, column=0, sticky="w")
        if value:
            self.ttk.Label(summary, text=self._display_path(value, max_chars=44 if self.compact_layout else 60), style="StatusBody.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 0))
        actions = self.ttk.Frame(summary, style="TintCard.TFrame")
        actions.grid(row=0, column=1, rowspan=2, sticky="e", padx=(12, 0))
        browse = self._button(actions, self.t("game.change"), command, kind="field", width=9)
        browse.pack(side="left")
        open_path = self._button(
            actions, self.t("game.open_folder"),
            lambda: self._open_folder(value, containing=bool(selected and selected.is_file())),
            kind="field", width=10,
        )
        open_path.pack(side="left", padx=(6, 0))
        if not value:
            open_path.configure(state="disabled")
        self._guide(browse, tooltip_key)
        manual_toggle = self._button(
            summary,
            self.t("game.hide_manual_path" if self.show_manual_paths.get() else "game.manual_path"),
            lambda: (self.show_manual_paths.set(not self.show_manual_paths.get()), self._render()),
            kind="ghost",
        )
        manual_toggle.grid(row=2, column=0, sticky="w", pady=(5, 0))
        if self.show_manual_paths.get():
            entry = self._entry(summary, variable)
            entry.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
            self._guide(entry, tooltip_key)
        self.ttk.Label(parent, text=hint, style="Hint.TLabel", wraplength=680, justify="left").grid(row=row + 2, column=0, sticky="w", pady=(4, 0))

    def _render_languages(self) -> None:
        card = self._card()
        card.columnconfigure(0, weight=1)
        card.columnconfigure(1, weight=1)
        row_offset = 0
        if self.existing_languages:
            existing = self.tk.Frame(card, background=Colors.SUCCESS_CONTAINER, padx=14, pady=12)
            existing.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 18))
            existing.columnconfigure(0, weight=1)
            self.tk.Label(
                existing,
                text=self.t("languages.existing_title"),
                background=Colors.SUCCESS_CONTAINER,
                foreground=Colors.SUCCESS,
                font=(Typography.UI, Typography.BODY, "bold"),
                anchor="w",
            ).grid(row=0, column=0, sticky="w")
            self.tk.Label(
                existing,
                text=self.t("languages.existing_body"),
                background=Colors.SUCCESS_CONTAINER,
                foreground=Colors.ON_SURFACE_VARIANT,
            font=(Typography.UI, Typography.SMALL),
                wraplength=700,
                justify="left",
                anchor="w",
            ).grid(row=1, column=0, sticky="w", pady=(3, 9))
            for item_index, item in enumerate(self.existing_languages):
                selected = self.target_language.get().strip().casefold() == item.language.casefold()
                button = self._button(
                    existing,
                    self.t(
                        "languages.existing_action",
                        language=self._display_language_name(item.language),
                        files=item.script_files + item.compiled_files,
                    ),
                    lambda language=item.language: self._select_existing_language(language),
                    kind="primary" if selected else "secondary",
                )
                button.grid(row=2 + item_index, column=0, sticky="ew", pady=(0 if item_index == 0 else 6, 0))
            if any(self.target_language.get().strip().casefold() == item.language.casefold() for item in self.existing_languages):
                self.tk.Label(
                    existing,
                    text=self.t("languages.incremental_selected"),
                    background=Colors.SUCCESS_CONTAINER,
                    foreground=Colors.SUCCESS,
            font=(Typography.UI, Typography.SMALL, "bold"),
                    anchor="w",
                ).grid(row=2 + len(self.existing_languages), column=0, sticky="w", pady=(8, 0))
            row_offset = 2
        self.ttk.Label(card, text=self.t("languages.source"), style="Field.TLabel").grid(row=row_offset, column=0, sticky="w", padx=(0, 12))
        source_values = ("auto",) + self.LANGUAGE_CHOICES
        self.source_language_display.set(self._display_language_name(self.source_language.get() or "auto"))
        self._language_display_snapshot["source"] = self.source_language_display.get()
        source = self._combobox(
            card,
            self.source_language_display,
            tuple(self._display_language_name(value) for value in source_values),
        )
        source.grid(row=row_offset + 1, column=0, sticky="ew", padx=(0, 12), pady=(5, 0))
        self.ttk.Label(card, text=self.t("languages.target"), style="Field.TLabel").grid(row=row_offset, column=1, sticky="w", padx=(12, 0))
        target_values = tuple(dict.fromkeys([*(item.language for item in self.existing_languages), *self.LANGUAGE_CHOICES]))
        self.target_language_display.set(self._display_language_name(self.target_language.get()))
        self._language_display_snapshot["target"] = self.target_language_display.get()
        target = self._combobox(
            card,
            self.target_language_display,
            tuple(self._display_language_name(value) for value in target_values),
        )
        target.grid(row=row_offset + 1, column=1, sticky="ew", padx=(12, 0), pady=(5, 0))
        source.bind("<<ComboboxSelected>>", lambda _event: self._commit_language_display("source", refresh=True), add="+")
        target.bind("<<ComboboxSelected>>", lambda _event: self._commit_language_display("target", refresh=True), add="+")
        source.bind("<FocusOut>", lambda _event: self._commit_language_display("source"), add="+")
        target.bind("<FocusOut>", lambda _event: self._commit_language_display("target"), add="+")
        self.ttk.Label(card, text=self.t("languages.hint"), style="Hint.TLabel", wraplength=680, justify="left").grid(row=row_offset + 2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.ttk.Label(card, text=self.t("languages.source_hint"), style="Hint.TLabel", wraplength=340, justify="left").grid(row=row_offset + 3, column=0, sticky="nw", padx=(0, 12), pady=(16, 0))
        self.ttk.Label(card, text=self.t("languages.target_hint"), style="Hint.TLabel", wraplength=340, justify="left").grid(row=row_offset + 3, column=1, sticky="nw", padx=(12, 0), pady=(16, 0))
        self._guide(source, "languages.source_hint")
        self._guide(target, "languages.target_hint")
        inventory = self.scope_preview_inventory if self.scope_preview_signature == self._scope_signature() else None
        if inventory is not None:
            summary = self.tk.Frame(card, background=Colors.SUCCESS_CONTAINER, padx=14, pady=12)
            summary.grid(row=row_offset + 4, column=0, columnspan=2, sticky="ew", pady=(18, 0))
            summary.columnconfigure(0, weight=1)
            self.tk.Label(
                summary, text=self.t("languages.scan_summary"), background=Colors.SUCCESS_CONTAINER,
                foreground=Colors.SUCCESS, font=(Typography.UI, Typography.BODY, "bold"), anchor="w",
            ).grid(row=0, column=0, sticky="w")
            self.tk.Label(
                summary,
                text=self.t("languages.scan_counts", reused=inventory.reusable_units, pending=inventory.model_units, files=inventory.files_scanned),
                background=Colors.SUCCESS_CONTAINER, foreground=Colors.ON_SURFACE_VARIANT,
                font=(Typography.UI, Typography.BODY), justify="left", anchor="w",
            ).grid(row=1, column=0, sticky="w", pady=(7, 0))
            self.tk.Label(
                summary, text=self.t("languages.incremental_choice"), background=Colors.SUCCESS_CONTAINER,
                foreground=Colors.SUCCESS, font=(Typography.UI, Typography.SMALL, "bold"), anchor="w",
            ).grid(row=2, column=0, sticky="w", pady=(8, 0))
        if not self.target_language.get():
            target.focus_set()

    def _display_language_name(self, language: str) -> str:
        names = {
            "auto": ("Auto detect", "自动检测"),
            "zh_hans": ("Simplified Chinese", "简体中文"),
            "zh_hant": ("Traditional Chinese", "繁体中文"),
            "简体中文": ("Simplified Chinese", "简体中文"),
            "繁體中文": ("Traditional Chinese", "繁体中文"),
            "english": ("English", "英语"),
            "japanese": ("Japanese", "日语"),
            "日本語": ("Japanese", "日语"),
            "korean": ("Korean", "韩语"),
            "한국어": ("Korean", "韩语"),
            "deutsch": ("German", "德语"),
            "français": ("French", "法语"),
            "español": ("Spanish", "西班牙语"),
            "português": ("Portuguese", "葡萄牙语"),
            "русский": ("Russian", "俄语"),
        }
        localized = names.get(language.casefold())
        if not localized:
            return language
        name = localized[1 if self.locale.get() == "zh" else 0]
        is_system_code = "_" in language or "-" in language or language.casefold() in {"zh_hans", "zh_hant"}
        return f"{name} ({language})" if is_system_code else name

    def _canonical_language_name(self, display: str) -> str:
        display = display.strip()
        candidates = tuple(dict.fromkeys((
            "auto",
            *(item.language for item in self.existing_languages),
            *self.LANGUAGE_CHOICES,
        )))
        for candidate in candidates:
            if display.casefold() == self._display_language_name(candidate).casefold():
                return candidate
        return display

    def _commit_language_display(self, field: str, *, refresh: bool = False) -> None:
        display = self.source_language_display if field == "source" else self.target_language_display
        value = self.source_language if field == "source" else self.target_language
        displayed_value = display.get().strip()
        if displayed_value != self._language_display_snapshot.get(field, "") or not value.get().strip():
            value.set(self._canonical_language_name(displayed_value))
        self._language_display_snapshot[field] = displayed_value
        self._start_scope_preview()
        if refresh:
            self._render()

    def _incremental_preview(self) -> str:
        if self.scope_preview_signature != self._scope_signature():
            return self.t("review.incremental_scope_unknown")
        if self.scope_preview_status == "scanning":
            return self.t("review.incremental_scanning")
        if self.scope_preview_status == "error":
            return self.t("review.incremental_scan_failed", error=self.scope_preview_error)
        inventory = self.scope_preview_inventory
        if inventory is not None:
            return self.t(
                "review.incremental_scope_exact",
                total=inventory.total_units,
                reused=inventory.reusable_units,
                units=inventory.model_units,
                files=inventory.files_scanned,
            )
        return self.t("review.incremental_scope_unknown")

    def _scope_signature(self) -> tuple[str, str, str, str]:
        return (
            self._normalized_path(self.project.get()),
            self._normalized_path(self.workspace.get()),
            self.source_language.get().strip() or "auto",
            self.target_language.get().strip().casefold(),
        )

    def _start_scope_preview(self) -> None:
        signature = self._scope_signature()
        if not all(signature[:2]) or not signature[3]:
            return
        if self.scope_preview_signature == signature and self.scope_preview_status in {"scanning", "ready"}:
            return
        self.scope_preview_signature = signature
        self.scope_preview_status = "scanning"
        self.scope_preview_inventory = None
        self.scope_preview_budget = None
        self.scope_preview_error = ""

        project, workspace, source_language, target_language = signature

        def run() -> None:
            try:
                result = RenWeavePipeline(workspace).preview_translation_scope(
                    project,
                    source_language=source_language,
                    target_language=target_language,
                )
                self.events.put(("scope_preview_ready", (signature, result)))
            except BaseException as exc:
                self.events.put(("scope_preview_error", (signature, str(exc))))

        self.scope_preview_worker = threading.Thread(target=run, daemon=True)
        self.scope_preview_worker.start()

    def _select_existing_language(self, language: str) -> None:
        self.target_language.set(language)
        if self.step == 2:
            self._start_scope_preview()
        self._render()

    def _render_review(self) -> None:
        card = self._card()
        card.columnconfigure(0, weight=1)
        self.resume_candidate = self._resume_state()
        inventory = self.scope_preview_inventory if self.scope_preview_signature == self._scope_signature() else None
        incremental = any(
            self.target_language.get().strip().casefold() == item.language.casefold()
            for item in self.existing_languages
        )
        pending = inventory.model_units if inventory is not None else 0
        reused = inventory.reusable_units if inventory is not None else 0
        game_name = Path(self.project.get().strip()).expanduser().name or self.t("review.game")

        task_card = self.tk.Frame(card, background=Colors.SURFACE_CONTAINER, padx=18, pady=16)
        task_card.grid(row=0, column=0, sticky="ew")
        task_card.columnconfigure(0, weight=1)
        self.tk.Label(task_card, text=game_name, background=Colors.SURFACE_CONTAINER, foreground=Colors.ON_SURFACE, font=(Typography.UI, Typography.TITLE, "bold"), anchor="w").grid(row=0, column=0, sticky="w")
        self.tk.Label(
            task_card,
            text=f"{self._display_language_name(self.source_language.get() or 'auto')}  →  {self._display_language_name(self.target_language.get())}",
            background=Colors.SURFACE_CONTAINER, foreground=Colors.ON_SURFACE_VARIANT,
            font=(Typography.UI, Typography.BODY, "bold"), anchor="w",
        ).grid(row=1, column=0, sticky="w", pady=(5, 0))
        if incremental:
            self.tk.Label(task_card, text=self.t("review.task_mode"), background=Colors.SURFACE_CONTAINER, foreground=Colors.PRIMARY, font=(Typography.UI, Typography.SMALL, "bold"), anchor="w").grid(row=2, column=0, sticky="w", pady=(8, 0))
            if inventory is not None:
                self.tk.Label(task_card, text=self.t("review.remaining", count=pending), background=Colors.SURFACE_CONTAINER, foreground=Colors.ON_SURFACE, font=(Typography.UI, Typography.BODY, "bold"), anchor="w").grid(row=3, column=0, sticky="w", pady=(4, 0))
                self.tk.Label(task_card, text=self.t("review.preserved", count=reused), background=Colors.SURFACE_CONTAINER, foreground=Colors.SUCCESS, font=(Typography.UI, Typography.SMALL), anchor="w").grid(row=4, column=0, sticky="w", pady=(3, 0))

        facts = self.ttk.Frame(card, style="TintCard.TFrame", padding=14)
        facts.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        facts.columnconfigure(1, weight=1)
        fact_rows = (
            (self.t("review.model"), f"{self.provider_name.get()} · {self.model.get()}"),
            (self.t("review.languages"), self.t("review.task_mode") if incremental else f"{self.source_language.get() or 'auto'} → {self.target_language.get()}"),
            (self.t("review.game"), self.t("review.game_unchanged")),
            (self.t("review.options"), self.t("review.output_plain")),
        )
        for row, (label, value) in enumerate(fact_rows):
            self.ttk.Label(facts, text=label, style="StatusBody.TLabel").grid(row=row, column=0, sticky="nw", padx=(0, 20), pady=(0 if row == 0 else 6, 0))
            self.ttk.Label(facts, text=value, style="Status.TLabel", wraplength=620, justify="left").grid(row=row, column=1, sticky="nw", pady=(0 if row == 0 else 6, 0))

        details_button = self._button(
            card, self.t("review.hide_details" if self.show_review_details.get() else "review.details"),
            lambda: (self.show_review_details.set(not self.show_review_details.get()), self._render()), kind="field",
        )
        details_button.grid(row=2, column=0, sticky="w", pady=(10, 0))
        if self.show_review_details.get():
            detail_text = "\n".join((
                f"API: {self.base_url.get()}",
                f"{self.t('game.project')}: {self.project.get()}",
                f"{self.t('game.workspace')}: {self.workspace.get()}",
                f"{self.t('game.sdk')}: {self.renpy_sdk.get() or self.t('optional')}",
                self.t("review.engine_yes") if self.require_engine.get() else self.t("review.engine_no"),
            ))
            self.ttk.Label(card, text=detail_text, style="Hint.TLabel", wraplength=790, justify="left").grid(row=3, column=0, sticky="w", pady=(7, 0))

        budget = self._get_token_budget()
        budget_card = self.tk.Frame(card, background=Colors.PRIMARY_CONTAINER, padx=16, pady=13)
        budget_card.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        if budget and budget.estimated_total_high > 0:
            low, high = self._format_token_count(budget.estimated_total_low), self._format_token_count(budget.estimated_total_high)
            budget_text = self.t("budget.range", low=low, high=high)
            budget_note = self.t("budget.note", scripts=budget.script_count, confidence=self.t(f"budget.confidence.{budget.confidence}"))
        else:
            budget_text, budget_note = self.t("budget.unavailable"), ""
        self.tk.Label(budget_card, text=self.t("budget.title"), background=Colors.PRIMARY_CONTAINER, foreground=Colors.ON_PRIMARY_CONTAINER, font=(Typography.UI, Typography.SMALL, "bold"), anchor="w").pack(fill="x")
        self.tk.Label(budget_card, text=budget_text, background=Colors.PRIMARY_CONTAINER, foreground=Colors.ON_PRIMARY_CONTAINER, font=(Typography.UI, Typography.TITLE, "bold"), anchor="w").pack(fill="x", pady=(4, 0))
        if budget_note:
            self.tk.Label(budget_card, text=budget_note, background=Colors.PRIMARY_CONTAINER, foreground=Colors.ON_SURFACE_VARIANT, font=(Typography.UI, Typography.SMALL), wraplength=760, justify="left", anchor="w").pack(fill="x", pady=(5, 0))

        options = self.ttk.Frame(card, style="TintCard.TFrame", padding=(14, 10))
        options.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        options.columnconfigure(0, weight=1)
        self.review_options = options
        self.ttk.Checkbutton(options, text=self.t("review.rpa"), variable=self.generate_rpa, style="Tint.TCheckbutton").grid(row=0, column=0, sticky="w")
        self.ttk.Label(options, text=self.t("review.rpa_hint"), style="TintHint.TLabel", wraplength=760, justify="left").grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.package_technical_toggle = self._button(
            options,
            self.t("review.technical_hide" if self.show_package_technical.get() else "review.technical_show"),
            lambda: (self.show_package_technical.set(not self.show_package_technical.get()), self._render()),
            kind="ghost",
        )
        self.package_technical_toggle.grid(row=2, column=0, sticky="w", pady=(3, 0))
        if self.show_package_technical.get():
            self.ttk.Label(options, text=self.t("review.rpa_technical"), style="TintHint.TLabel", wraplength=760, justify="left").grid(row=3, column=0, sticky="w", pady=(2, 0))
        self.ttk.Checkbutton(
            options,
            text=self.t("review.install"),
            variable=self.install,
            command=self._render,
            style="Tint.TCheckbutton",
        ).grid(row=4, column=0, sticky="w", pady=(8, 0))
        self.ttk.Label(options, text=self.t("review.install_hint"), style="TintHint.TLabel", wraplength=760, justify="left").grid(row=5, column=0, sticky="w", pady=(2, 0))
        if self.install.get():
            warning = self.tk.Frame(options, background=Colors.WARNING_CONTAINER, padx=10, pady=7)
            warning.grid(row=6, column=0, sticky="ew", pady=(7, 0))
            self.tk.Label(
                warning,
                text=self.t("review.install_warning"),
                background=Colors.WARNING_CONTAINER,
                foreground=Colors.WARNING,
                font=(Typography.UI, Typography.SMALL, "bold"),
                wraplength=730,
                justify="left",
                anchor="w",
            ).pack(fill="x")

        if self.resume_candidate:
            completed = len(self.resume_candidate.get("completed_scene_ids", []))
            total = self._safe_count(self.resume_candidate.get("total_scenes", 0))
            resume_text = self.t("review.resume_body", completed=completed, total=total) if total > 0 else self.t("review.resume_body_unknown", completed=completed)
            self.ttk.Label(card, text=f"{self.t('review.resume_found')}\n{resume_text}", style="StatusBody.TLabel", wraplength=790, justify="left").grid(row=6, column=0, sticky="w", pady=(12, 0))

        if incremental and inventory is not None and inventory.pending_units:
            pending_header = self.ttk.Frame(card, style="CardBody.TFrame")
            pending_header.grid(row=7, column=0, sticky="ew", pady=(12, 0))
            pending_header.columnconfigure(0, weight=1)
            self.review_pending_title = self.ttk.Label(pending_header, text=self.t("review.pending_title", count=inventory.model_units), style="Status.TLabel")
            self.review_pending_title.grid(row=0, column=0, sticky="w")
            pending_button = self._button(pending_header, self.t("review.pending_hide" if self.show_pending_details.get() else "review.pending_show"), lambda: (self.show_pending_details.set(not self.show_pending_details.get()), self._render()), kind="field")
            pending_button.grid(row=0, column=1, sticky="e")
            if self.show_pending_details.get():
                rows = [self.t("review.pending_row", file=item.get("file", ""), line=item.get("line", 0), source=item.get("source", ""), detail=item.get("detail", "")) for item in inventory.pending_units[:50]]
                self.review_details = self.ttk.Frame(card, style="TintCard.TFrame", padding=10)
                self.review_details.grid(row=8, column=0, sticky="nsew", pady=(7, 0))
                self.review_details.columnconfigure(0, weight=1)
                self.review_detail_host = self.review_details
                self.review_detail_text = self.tk.Text(self.review_details, height=7, wrap="word", borderwidth=0, highlightthickness=1, highlightbackground=Colors.OUTLINE_VARIANT, background=Colors.CARD, foreground=Colors.ON_SURFACE_VARIANT, font=(Typography.UI, Typography.SMALL), padx=10, pady=8)
                self.review_detail_text.grid(row=0, column=0, sticky="nsew")
                scrollbar = self._scrollbar(self.review_details, command=self.review_detail_text.yview)
                scrollbar.grid(row=0, column=1, sticky="ns")
                self.review_detail_text.configure(yscrollcommand=scrollbar.set)
                self.review_detail_text.insert("1.0", "\n\n".join(rows))
                self.review_detail_text.configure(state="disabled")

    def _render_review_legacy(self) -> None:
        card = self._card()
        self.resume_candidate = self._resume_state()
        incremental = any(
            self.target_language.get().strip().casefold() == item.language.casefold()
            for item in self.existing_languages
        )
        language_summary = f"{self.source_language.get() or 'auto'}  →  {self.target_language.get()}"
        if incremental:
            language_summary += f"\n{self.t('languages.incremental_selected')}"
            preview = self._incremental_preview()
            if preview:
                language_summary += f"\n{preview}"
        summaries = (
            (self.t("review.model"), f"{self.provider_name.get()}  ·  {self.model.get()}\n{self.base_url.get()}"),
            (
                self.t("review.game"),
                f"{self._display_path(self.project.get())}\n{self._display_path(self.workspace.get())}",
            ),
            (self.t("review.languages"), language_summary),
            (self.t("review.options"), self.t("review.engine_yes") if self.require_engine.get() else self.t("review.engine_no")),
        )
        card.columnconfigure(0, weight=1, uniform="review")
        card.columnconfigure(1, weight=1, uniform="review")
        for index, (title, body) in enumerate(summaries):
            summary = self.ttk.Frame(card, style="TintCard.TFrame", padding=14)
            summary.grid(
                row=index // 2,
                column=index % 2,
                sticky="nsew",
                padx=(0, 4) if index % 2 == 0 else (4, 0),
                pady=(0 if index < 2 else 8, 0),
            )
            summary.columnconfigure(0, weight=1)
            self.ttk.Label(summary, text=title, style="Status.TLabel").grid(row=0, column=0, sticky="w")
            self.ttk.Label(
                summary,
                text=body,
                style="StatusBody.TLabel",
                wraplength=260 if self.compact_layout else 330,
                justify="left",
            ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        budget = self._get_token_budget()
        budget_card = self.tk.Frame(card, background=Colors.PRIMARY_CONTAINER, padx=16, pady=13)
        budget_card.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        budget_card.columnconfigure(0, weight=1)
        self.tk.Label(budget_card, text=self.t("budget.title").upper(), background=Colors.PRIMARY_CONTAINER, foreground=Colors.ON_PRIMARY_CONTAINER, font=(Typography.UI, Typography.SMALL, "bold"), anchor="w").grid(row=0, column=0, sticky="w")
        if budget:
            low = self._format_token_count(budget.estimated_total_low)
            high = self._format_token_count(budget.estimated_total_high)
            self.tk.Label(budget_card, text=self.t("budget.range", low=low, high=high), background=Colors.PRIMARY_CONTAINER, foreground=Colors.ON_PRIMARY_CONTAINER, font=(Typography.UI, Typography.TITLE, "bold"), anchor="w").grid(row=1, column=0, sticky="w", pady=(4, 0))
            self.tk.Label(
                budget_card,
                text=self.t(
                    "budget.breakdown",
                    input_low=self._format_token_count(budget.estimated_input_low),
                    input_high=self._format_token_count(budget.estimated_input_high),
                    output_low=self._format_token_count(budget.estimated_output_low),
                    output_high=self._format_token_count(budget.estimated_output_high),
                ),
                background=Colors.PRIMARY_CONTAINER,
                foreground=Colors.ON_PRIMARY_CONTAINER,
            font=(Typography.UI, Typography.SMALL, "bold"),
                anchor="w",
            ).grid(row=2, column=0, sticky="w", pady=(4, 0))
            self.tk.Label(
                budget_card,
                text=self.t(
                    "budget.note",
                    scripts=budget.script_count,
                    confidence=self.t(f"budget.confidence.{budget.confidence}"),
                ),
                background=Colors.PRIMARY_CONTAINER,
                foreground=Colors.ON_SURFACE_VARIANT,
            font=(Typography.UI, Typography.SMALL),
                wraplength=560 if self.compact_layout else 720,
                justify="left",
                anchor="w",
            ).grid(row=3, column=0, sticky="w", pady=(5, 0))
        else:
            self.tk.Label(budget_card, text=self.t("budget.unavailable"), background=Colors.PRIMARY_CONTAINER, foreground=Colors.ON_SURFACE_VARIANT, font=(Typography.UI, Typography.BODY), anchor="w").grid(row=1, column=0, sticky="w", pady=(5, 0))

        options = self.ttk.Frame(card, style="TintCard.TFrame", padding=(14, 10))
        options.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        options.columnconfigure(0, weight=1)
        self.review_options = options
        self.ttk.Checkbutton(
            options,
            text=self.t("review.rpa"),
            variable=self.generate_rpa,
            style="Tint.TCheckbutton",
        ).grid(row=0, column=0, sticky="w")
        self.ttk.Label(
            options,
            text=self.t("review.rpa_hint"),
            style="TintHint.TLabel",
            wraplength=720,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.ttk.Checkbutton(
            options,
            text=self.t("review.install"),
            variable=self.install,
            style="Tint.TCheckbutton",
        ).grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.ttk.Label(
            options, text=self.t("review.install_hint"), style="TintHint.TLabel"
        ).grid(row=3, column=0, sticky="w", pady=(2, 0))
        self.ttk.Label(
            options, text=self.t("review.key_safe"), style="TintHint.TLabel"
        ).grid(row=4, column=0, sticky="w", pady=(8, 0))
        if self.resume_candidate:
            resume = self.ttk.Frame(card, style="TintCard.TFrame", padding=14)
            resume.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(12, 0))
            self.ttk.Label(resume, text=self.t("review.resume_found"), style="Status.TLabel").grid(row=0, column=0, sticky="w")
            self.ttk.Label(
                resume,
                text=self.t(
                    "review.resume_body",
                    completed=len(self.resume_candidate.get("completed_scene_ids", [])),
                    total=int(self.resume_candidate.get("total_scenes", 0)),
                ),
                style="StatusBody.TLabel",
            ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        inventory = self.scope_preview_inventory if self.scope_preview_signature == self._scope_signature() else None
        if incremental and inventory is not None and inventory.pending_units:
            details = self.ttk.Frame(card, style="TintCard.TFrame", padding=14)
            details.grid(row=5, column=0, columnspan=2, sticky="nsew", pady=(12, 0))
            details.columnconfigure(0, weight=1)
            details.rowconfigure(1, weight=1)
            card.rowconfigure(5, weight=1)
            self.review_details = details
            self.review_pending_title = self.ttk.Label(
                details,
                text=self.t("review.pending_title", count=inventory.model_units),
                style="Status.TLabel",
            )
            self.review_pending_title.grid(row=0, column=0, sticky="w")
            detail_host = self.ttk.Frame(details, style="TintCard.TFrame")
            detail_host.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
            detail_host.columnconfigure(0, weight=1)
            detail_host.rowconfigure(0, weight=1)
            self.review_detail_host = detail_host
            detail_text = self.tk.Text(
                detail_host,
                height=9,
                wrap="word",
                state="normal",
                borderwidth=0,
                relief="flat",
                highlightthickness=1,
                highlightbackground=Colors.OUTLINE_VARIANT,
                highlightcolor=Colors.PRIMARY,
                background=Colors.CARD,
                foreground=Colors.ON_SURFACE_VARIANT,
                selectbackground=Colors.PRIMARY_CONTAINER,
                selectforeground=Colors.ON_PRIMARY_CONTAINER,
                font=(Typography.UI, Typography.SMALL),
                padx=12,
                pady=10,
                takefocus=True,
            )
            detail_text.grid(row=0, column=0, sticky="nsew")
            self.review_detail_text = detail_text
            detail_scrollbar = self._scrollbar(detail_host, command=detail_text.yview)
            detail_scrollbar.grid(row=0, column=1, sticky="ns")
            detail_text.configure(yscrollcommand=detail_scrollbar.set)
            rows = []
            for item in inventory.pending_units[:50]:
                rows.append(self.t(
                    "review.pending_row",
                    file=item.get("file", ""),
                    line=item.get("line", 0),
                    source=item.get("source", ""),
                    detail=item.get("detail", ""),
                ))
            detail_text.insert("1.0", "\n\n".join(rows))
            detail_text.configure(state="disabled")

    @staticmethod
    def _safe_count(value: object) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    def _task_presentation(self) -> TaskPresentation:
        """Resolve all visible progress state from one contradiction-free source."""
        payload_stage = str(self.progress_payload.get("stage", "") or "")
        running = bool(self.worker and self.worker.is_alive())
        pausing = bool(running and self.cancel_token is not None and self.cancel_token.cancelled)
        if pausing:
            state = TaskState.PAUSING
        elif self.last_stage == "complete" or (not running and payload_stage == "complete"):
            state = TaskState.COMPLETED
        elif self.last_stage == "paused" or (not running and payload_stage == "paused"):
            state = TaskState.PAUSED
        elif self.last_stage == "failed" or (not running and payload_stage == "failed"):
            state = TaskState.FAILED
        elif running:
            state = TASK_STATE_BY_STAGE.get(payload_stage or self.last_active_stage, TaskState.PREPARING)
        elif not self.translation_started:
            state = TaskState.IDLE
        else:
            state = TASK_STATE_BY_STAGE.get(payload_stage, TaskState.PREPARING)

        stage = payload_stage
        if state == TaskState.COMPLETED:
            stage = "complete"
        if stage in {"paused", "failed"}:
            stage = self.last_active_stage
        completed = self._safe_count(
            self.progress_payload.get("completed_scenes", len(self.progress_payload.get("completed_scene_ids", [])))
        )
        total = self._safe_count(self.progress_payload.get("total_scenes", 0))
        percent: float | None = None
        if state == TaskState.COMPLETED:
            percent = 100.0
        elif total > 0:
            try:
                raw_percent = float(self.progress_payload.get("progress_percent", 0.0) or 0.0)
            except (TypeError, ValueError, OverflowError):
                raw_percent = 0.0
            if not math.isfinite(raw_percent):
                raw_percent = 0.0
            if raw_percent >= 100.0:
                raw_percent = completed / total * 100.0
            percent = max(0.0, min(99.0, raw_percent))
        phase = "done" if state == TaskState.COMPLETED else PROGRESS_STAGE_PHASE.get(stage, TASK_PHASE[state])
        phase = {"understand": "analyze", "refine": "validate"}.get(phase, phase)
        return TaskPresentation(state, stage, phase, percent, completed, total)

    def _render_progress(self) -> None:
        payload = self.progress_payload
        task = self._task_presentation()
        percent = task.percent or 0.0
        self._displayed_progress_percent = percent
        self.progress_value = self.tk.DoubleVar(value=percent)
        self.progress_percent_text = self.tk.StringVar(value=f"{percent:.0f}%" if task.percent is not None else "")
        card = self._card()
        card.columnconfigure(0, weight=1)
        heading = self.ttk.Frame(card, style="CardBody.TFrame")
        heading.grid(row=0, column=0, sticky="ew")
        heading.columnconfigure(0, weight=1)
        self.progress_heading = self.ttk.Label(
            heading,
            text=self.t(f"progress.task.{task.state.value}"),
            style="Field.TLabel",
        )
        self.progress_heading.grid(row=0, column=0, sticky="w")
        self.progress_percent_label = self.tk.Label(
            heading,
            textvariable=self.progress_percent_text,
            background=Colors.CARD,
            foreground=Colors.PRIMARY,
            font=(Typography.UI, Typography.DISPLAY, "bold"),
        )
        self.progress_percent_label.grid(row=0, column=1, rowspan=2, sticky="e")
        if task.percent is None:
            self.progress_percent_label.grid_remove()
        scene_summary = self.t(
            "progress.scene_ratio" if task.total > 0 else "progress.scene_total_unknown",
            completed=task.completed,
            total=task.total,
        )
        self.progress_scene_summary = self.ttk.Label(heading, text=scene_summary, style="Section.TLabel", wraplength=650)
        self.progress_scene_summary.grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.progress = self.ttk.Progressbar(
            card,
            mode="determinate",
            maximum=100,
            variable=self.progress_value,
        )
        self.progress.grid(row=1, column=0, sticky="ew", pady=(12, 12))
        if task.percent is None:
            self.progress.grid_remove()

        runtime = self.tk.Frame(card, background=Colors.SUCCESS_CONTAINER, padx=13, pady=10)
        runtime.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        runtime.columnconfigure(1, weight=1)
        self.progress_runtime_panel = runtime
        self.progress_runtime_title = self.tk.Label(
            runtime,
            text=self.t("progress.runtime").upper(),
            background=Colors.SUCCESS_CONTAINER,
            foreground=Colors.SUCCESS,
            font=(Typography.UI, Typography.SMALL, "bold"),
        )
        self.progress_runtime_title.grid(row=0, column=0, sticky="w")
        self.progress_runtime_state = self.tk.Label(
            runtime,
            text="",
            background=Colors.SUCCESS_CONTAINER,
            foreground=Colors.SUCCESS,
            font=(Typography.UI, Typography.BODY, "bold"),
        )
        self.progress_runtime_state.grid(row=0, column=1, sticky="w", padx=(14, 0))
        self.progress_stage_counter = self.tk.Label(
            runtime,
            text="",
            background=Colors.SUCCESS_CONTAINER,
            foreground=Colors.ON_SURFACE_VARIANT,
            font=(Typography.UI, Typography.SMALL, "bold"),
        )
        self.progress_stage_counter.grid(row=0, column=2, sticky="e")
        self.progress_activity = self.ttk.Progressbar(
            runtime,
            mode="indeterminate",
            style="Horizontal.TProgressbar",
        )
        self.progress_activity.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(9, 0))

        phases = (("prepare", 0), ("analyze", 0), ("translate", 0), ("validate", 0), ("build", 0), ("done", 0))
        phase_row = self.tk.Frame(card, background=Colors.CARD)
        phase_row.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        self.progress_phase_labels = []
        for column, (name, threshold) in enumerate(phases):
            phase_row.columnconfigure(column, weight=1)
            active = percent >= threshold or (threshold == 0 and self.last_stage)
            phase_label = self.tk.Label(
                phase_row,
                text=("●  " if active else "○  ") + self.t(f"progress.phase.{name}"),
                background=Colors.CARD,
                foreground=Colors.PRIMARY if active else Colors.OUTLINE,
                font=(Typography.UI, Typography.SMALL, "bold" if active else "normal"),
            )
            phase_label.grid(row=0, column=column, sticky="w")
            self.progress_phase_labels.append((name, threshold, phase_label))

        stats = self.tk.Frame(card, background=Colors.CARD)
        stats.grid(row=4, column=0, sticky="ew", pady=(0, 12))
        stat_columns = 2 if self.compact_layout else 4
        for column in range(stat_columns):
            stats.columnconfigure(column, weight=1, uniform="progress_stat")
        raw_eta = payload.get("eta_seconds", -1)
        eta = self._format_duration(int(raw_eta) if isinstance(raw_eta, (int, float)) else -1)
        calls = int(payload.get("total_model_calls", 0) or 0)
        tokens = int(payload.get("total_prompt_tokens", 0) or 0) + int(payload.get("total_completion_tokens", 0) or 0)
        stage_labels = STAGE_LABELS_ZH if self.locale.get() == "zh" else STAGE_LABELS
        stage = self._effective_progress_stage()
        current = self._progress_current_display(payload, stage_labels, stage)
        values = (
            (self.t("progress.current"), current),
            (self.t("progress.file_count"), self._progress_file_display(payload)),
            (self.t("progress.eta"), eta),
            (self.t("progress.model_usage"), self.t("progress.calls_tokens", calls=calls, tokens=self._format_token_count(tokens))),
        )
        self.progress_stat_value_labels = []
        self.progress_stat_tiles = []
        for index, (label, value) in enumerate(values):
            column = index % stat_columns
            row = index // stat_columns
            tile = self.tk.Frame(stats, background=Colors.SURFACE_CONTAINER, padx=12, pady=10)
            tile.grid(
                row=row,
                column=column,
                sticky="nsew",
                padx=(0 if column == 0 else 5, 0),
                pady=(0 if row == 0 else 5, 0),
            )
            self.tk.Label(tile, text=label.upper(), background=Colors.SURFACE_CONTAINER, foreground=Colors.ON_SURFACE_VARIANT, font=(Typography.UI, Typography.SMALL, "bold"), anchor="w").pack(fill="x")
            value_label = self.tk.Label(tile, text=value, background=Colors.SURFACE_CONTAINER, foreground=Colors.ON_SURFACE, font=(Typography.UI, Typography.BODY, "bold"), anchor="w", wraplength=210, justify="left")
            value_label.pack(fill="x", pady=(5, 0))
            self.progress_stat_tiles.append(tile)
            self.progress_stat_value_labels.append(value_label)
        self._layout_progress_stat_tiles(self._progress_file_display(payload) != "—")

        usage_status = str(payload.get("usage_reporting_status", "pending") or "pending")
        if usage_status not in {"reported", "unavailable", "pending"}:
            usage_status = "pending"
        token_strip = self.tk.Frame(
            card,
            background=Colors.WARNING_CONTAINER if usage_status == "unavailable" else Colors.PRIMARY_CONTAINER,
            padx=13,
            pady=9,
        )
        token_strip.grid(row=5, column=0, sticky="ew", pady=(0, 12))
        self.progress_token_strip = token_strip
        token_strip.columnconfigure(1, weight=1)
        input_tokens = int(payload.get("total_prompt_tokens", 0) or 0)
        output_tokens = int(payload.get("total_completion_tokens", 0) or 0)
        estimate_low = int(payload.get("estimated_total_tokens_low", 0) or 0)
        estimate_high = int(payload.get("estimated_total_tokens_high", 0) or 0)
        strip_background = Colors.WARNING_CONTAINER if usage_status == "unavailable" else Colors.PRIMARY_CONTAINER
        strip_foreground = Colors.WARNING if usage_status == "unavailable" else Colors.ON_PRIMARY_CONTAINER
        actual_usage = self.tk.Label(
            token_strip,
            text=self.t("progress.usage_actual", total=self._format_token_count(tokens)),
            background=strip_background,
            foreground=strip_foreground,
            font=(Typography.UI, Typography.SMALL, "bold"),
        )
        actual_usage.grid(row=0, column=0, columnspan=2 if self.compact_layout else 1, sticky="w")
        self.progress_actual_usage = actual_usage
        breakdown = self.t("progress.usage_breakdown", input=self._format_token_count(input_tokens), output=self._format_token_count(output_tokens))
        projected_usage = self.tk.Label(token_strip, text=breakdown, background=strip_background, foreground=Colors.ON_SURFACE_VARIANT, font=(Typography.UI, Typography.SMALL))
        projected_usage.grid(row=1 if self.compact_layout else 0, column=0 if self.compact_layout else 1, sticky="w", padx=(0 if self.compact_layout else 18, 0), pady=(4, 0) if self.compact_layout else (0, 0))
        self.progress_projected_usage = projected_usage
        estimate = self.t("budget.projected", low=self._format_token_count(estimate_low), high=self._format_token_count(estimate_high)) if estimate_high else self.t(f"budget.reporting.{usage_status}")
        reporting = self.tk.Label(token_strip, text=estimate, background=strip_background, foreground=strip_foreground, font=(Typography.UI, Typography.SMALL, "bold"))
        reporting.grid(row=2 if self.compact_layout else 0, column=0 if self.compact_layout else 2, columnspan=2 if self.compact_layout else 1, sticky="w" if self.compact_layout else "e", pady=(4, 0) if self.compact_layout else (0, 0))
        self.progress_reporting = reporting

        if self.last_stage == "paused":
            notice = self.tk.Frame(card, background=Colors.WARNING_CONTAINER, padx=12, pady=9)
            notice.grid(row=6, column=0, sticky="ew", pady=(0, 12))
            self.tk.Label(notice, text=self.t("progress.paused"), background=Colors.WARNING_CONTAINER, foreground=Colors.WARNING, font=(Typography.UI, Typography.SMALL, "bold")).pack(side="left")
            self.tk.Label(notice, text=self.t("progress.safe_to_close"), background=Colors.WARNING_CONTAINER, foreground=Colors.ON_SURFACE_VARIANT, font=(Typography.UI, Typography.SMALL), wraplength=650, justify="left").pack(side="left", padx=(10, 0))
        elif self.last_stage == "complete":
            output_dir = str(payload.get("output_dir", "") or "")
            package_path = str(payload.get("package_path", "") or "")
            installed_dir = str(payload.get("installed_dir", "") or "")
            outputs = self.tk.Frame(card, background=Colors.SUCCESS_CONTAINER, padx=13, pady=11)
            outputs.grid(row=6, column=0, sticky="ew", pady=(0, 12))
            outputs.columnconfigure(0, weight=1)
            self.tk.Label(
                outputs,
                text=self.t("progress.package_ready"),
                background=Colors.SUCCESS_CONTAINER,
                foreground=Colors.SUCCESS,
                font=(Typography.UI, Typography.BODY, "bold"),
                anchor="w",
            ).grid(row=0, column=0, sticky="w")
            self.tk.Label(
                outputs,
                text=self.t("progress.outputs_hint"),
                background=Colors.SUCCESS_CONTAINER,
                foreground=Colors.ON_SURFACE_VARIANT,
                font=(Typography.UI, Typography.SMALL),
                anchor="w",
                justify="left",
            ).grid(row=1, column=0, sticky="w", pady=(3, 0))
            if installed_dir:
                self.tk.Label(
                    outputs,
                    text=self.t("progress.installed_output"),
                    background=Colors.SUCCESS_CONTAINER,
                    foreground=Colors.SUCCESS,
                    font=(Typography.UI, Typography.SMALL, "bold"),
                    anchor="w",
                ).grid(row=2, column=0, sticky="w", pady=(5, 0))
            output_actions = self.ttk.Frame(outputs, style="SuccessActions.TFrame")
            if self.compact_layout:
                output_actions.grid(row=3 if installed_dir else 2, column=0, sticky="w", pady=(10, 0))
            else:
                output_actions.grid(row=0, column=1, rowspan=3 if installed_dir else 2, sticky="e", padx=(16, 0))
            open_rpy = self._button(
                output_actions,
                self.t("progress.open_rpy"),
                lambda path=output_dir: self._open_folder(path),
                kind="secondary",
            )
            open_rpy.pack(side="left")
            if not output_dir:
                open_rpy.configure(state="disabled")
            if package_path:
                open_rpa = self._button(
                    output_actions,
                    self.t("progress.open_rpa"),
                    lambda path=package_path: self._open_folder(path, containing=True),
                    kind="secondary",
                )
                open_rpa.pack(side="left", padx=(8, 0))

        log_header = self.ttk.Frame(card, style="CardBody.TFrame")
        log_header.grid(row=7, column=0, sticky="ew")
        log_header.columnconfigure(0, weight=1)
        self.ttk.Label(log_header, text=self.t("progress.activity_recent"), style="Field.TLabel").grid(row=0, column=0, sticky="w")
        toggle_log = self._button(
            log_header,
            self.t("progress.log_hide" if self.show_log_details.get() else "progress.log_show"),
            lambda: (self.show_log_details.set(not self.show_log_details.get()), self._render()),
            kind="field",
        )
        toggle_log.grid(row=0, column=1, sticky="e")
        recent = "\n".join(self.logs[-3:]) if self.logs else self.t(f"progress.task.{task.state.value}")
        self.ttk.Label(card, text=recent, style="Hint.TLabel", wraplength=790, justify="left").grid(
            row=8, column=0, sticky="w", pady=(5, 0)
        )
        log_path = str(payload.get("log_path", "") or (Path(self.workspace.get().strip()) / "logs" / "renweave.log"))
        if self.show_log_details.get():
            copy_log = self._button(log_header, self.t("progress.copy_log"), lambda: self._copy_text(log_path), kind="field")
            copy_log.grid(row=0, column=2, sticky="e", padx=(8, 0))
            self._guide(copy_log, "tip.copy_log")
            usage_path = str(Path(self.workspace.get().strip()) / "usage.json")
            self.ttk.Label(card, text=f"{self.t('progress.log_path', path=log_path)}   ·   {self.t('budget.ledger', path=usage_path)}", style="Hint.TLabel", wraplength=600 if self.compact_layout else 790).grid(row=9, column=0, sticky="w", pady=(8, 5))
            log_frame = self.ttk.Frame(card, style="CardBody.TFrame")
            log_frame.grid(row=10, column=0, sticky="nsew")
            log_frame.columnconfigure(0, weight=1)
            log_frame.rowconfigure(0, weight=1)
            card.rowconfigure(10, weight=1)
            self.log = self.tk.Text(
                log_frame, height=5, wrap="word", state="normal", borderwidth=0, relief="flat",
                highlightthickness=1, highlightbackground=Colors.OUTLINE_VARIANT,
                highlightcolor=Colors.PRIMARY, background=Colors.CARD, foreground=Colors.ON_SURFACE,
                selectbackground=Colors.PRIMARY_CONTAINER, selectforeground=Colors.ON_PRIMARY_CONTAINER,
                insertbackground=Colors.ON_SURFACE, font=(Typography.MONO, Typography.SMALL),
                padx=12, pady=10, takefocus=True,
            )
            self.log.grid(row=0, column=0, sticky="nsew")
            scrollbar = self._scrollbar(log_frame, command=self.log.yview)
            scrollbar.grid(row=0, column=1, sticky="ns")
            self.log.configure(yscrollcommand=scrollbar.set)
            for line in self.logs:
                self.log.insert("end", line.rstrip() + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        self._refresh_progress_runtime()
        self._refresh_progress_phases()

    def _effective_progress_stage(self) -> str:
        return self._task_presentation().pipeline_stage

    def _progress_current_display(self, payload: dict[str, object], stage_labels: dict[str, str], stage: str) -> str:
        stage_text = stage_labels.get(stage, str(payload.get("current_operation", "") or "—"))
        current_file = str(payload.get("current_file", "") or "")
        scene_label = str(payload.get("current_scene_label", "") or "")
        details = []
        if current_file:
            details.append(current_file)
        if scene_label and scene_label not in current_file:
            details.append(scene_label)
        return stage_text + ("\n" + " · ".join(details) if details else "")

    def _progress_file_display(self, payload: dict[str, object]) -> str:
        total_files = int(payload.get("total_files", 0) or 0)
        if total_files:
            completed_files = int(payload.get("completed_files", 0) or 0)
            remaining_files = int(payload.get("remaining_files", max(0, total_files - completed_files)) or 0)
            return self.t(
                "progress.files_remaining",
                completed=completed_files,
                total=total_files,
                remaining=remaining_files,
            )
        phase_total = int(payload.get("phase_total", 0) or 0)
        phase_completed = int(payload.get("phase_completed", 0) or 0)
        if phase_total:
            return self.t("progress.phase_units", completed=phase_completed, total=phase_total)
        return "—"

    def _layout_progress_stat_tiles(self, has_file_progress: bool) -> None:
        """Hide unavailable file metrics and close the resulting layout gap."""
        if has_file_progress:
            positions = ((0, 0, 1), (0, 1, 1), (0 if not self.compact_layout else 1, 2 if not self.compact_layout else 0, 1), (0 if not self.compact_layout else 1, 3 if not self.compact_layout else 1, 1))
            for tile, (row, column, span) in zip(self.progress_stat_tiles, positions):
                tile.grid()
                tile.grid_configure(row=row, column=column, columnspan=span)
            return
        self.progress_stat_tiles[1].grid_remove()
        if self.compact_layout:
            positions = ((0, 0, 1), (0, 1, 1), (0, 1, 1), (1, 0, 2))
        else:
            positions = ((0, 0, 1), (0, 1, 1), (0, 1, 1), (0, 2, 1))
        for index in (0, 2, 3):
            row, column, span = positions[index]
            self.progress_stat_tiles[index].grid()
            self.progress_stat_tiles[index].grid_configure(row=row, column=column, columnspan=span)

    def _refresh_progress_runtime(self) -> None:
        if self.progress_activity is None or not self.progress_activity.winfo_exists():
            return
        task = self._task_presentation()
        running = task.state in {
            TaskState.PREPARING, TaskState.ANALYZING, TaskState.TRANSLATING,
            TaskState.VALIDATING, TaskState.BUILDING, TaskState.PAUSING,
        }
        key = f"progress.task.{task.state.value}"
        if task.state == TaskState.COMPLETED:
            background, foreground = Colors.SUCCESS_CONTAINER, Colors.SUCCESS
        elif task.state in {TaskState.PAUSED, TaskState.PAUSING}:
            background, foreground = Colors.WARNING_CONTAINER, Colors.WARNING
        elif task.state in {TaskState.FAILED, TaskState.CANCELLED}:
            background, foreground = Colors.ERROR_CONTAINER, Colors.ERROR
        else:
            background, foreground = Colors.PRIMARY_CONTAINER, Colors.PRIMARY
        self.progress_runtime_panel.configure(background=background)
        self.progress_runtime_title.configure(background=background, foreground=foreground)
        self.progress_runtime_state.configure(
            text=f"●  {self.t(key)}",
            background=background,
            foreground=foreground,
        )
        stage_text = self.t(f"progress.phase.{task.phase}")
        self.progress_stage_counter.configure(text=stage_text, background=background)
        active = running and task.state != TaskState.PAUSING
        if active and not self._progress_activity_running:
            self.progress_activity.grid()
            self.progress_activity.start(Metrics.ACTIVITY_FRAME_MS)
            self._progress_activity_running = True
        elif not active and self._progress_activity_running:
            self.progress_activity.stop()
            self.progress_activity.grid_remove()
            self._progress_activity_running = False
        elif not active:
            self.progress_activity.grid_remove()

    def _refresh_progress_phases(self) -> None:
        phase_names = tuple(name for name, _threshold, _label in self.progress_phase_labels)
        task = self._task_presentation()
        current_phase = task.phase
        current_index = phase_names.index(current_phase)
        complete = task.state == TaskState.COMPLETED
        for index, (name, _threshold, label) in enumerate(self.progress_phase_labels):
            if complete or index < current_index:
                marker, foreground, weight = "✓  ", Colors.SUCCESS, "bold"
            elif index == current_index:
                marker, foreground, weight = "●  ", Colors.PRIMARY, "bold"
            else:
                marker, foreground, weight = "○  ", Colors.OUTLINE, "normal"
            label.configure(
                text=marker + self.t(f"progress.phase.{name}"),
                foreground=foreground,
                font=(Typography.UI, Typography.SMALL, weight),
            )

    def _refresh_progress_panel(self, *, animate: bool = True) -> None:
        """Update mounted progress widgets without rebuilding the page tree."""
        if self.step != 4 or self.progress is None or not self.progress.winfo_exists():
            return
        payload = self.progress_payload
        task = self._task_presentation()
        if hasattr(self, "page_title_label") and self.page_title_label.winfo_exists():
            self.page_title_label.configure(text=self.t(f"progress.task.{task.state.value}"))
        self.progress_heading.configure(text=self.t(f"progress.task.{task.state.value}"))
        self.progress_scene_summary.configure(
            text=self.t(
                "progress.scene_ratio" if task.total > 0 else "progress.scene_total_unknown",
                completed=task.completed,
                total=task.total,
            )
        )
        if task.percent is None:
            self.progress.grid_remove()
            self.progress_percent_label.grid_remove()
            self.progress_percent_text.set("")
        else:
            self.progress.grid()
            self.progress_percent_label.grid()
            if animate:
                self._animate_progress_to(task.percent)
            else:
                self._set_displayed_progress(task.percent)

        self._refresh_progress_runtime()
        self._refresh_progress_phases()

        raw_eta = payload.get("eta_seconds", -1)
        eta = self._format_duration(int(raw_eta) if isinstance(raw_eta, (int, float)) else -1)
        calls = int(payload.get("total_model_calls", 0) or 0)
        input_tokens = int(payload.get("total_prompt_tokens", 0) or 0)
        output_tokens = int(payload.get("total_completion_tokens", 0) or 0)
        tokens = input_tokens + output_tokens
        stage_labels = STAGE_LABELS_ZH if self.locale.get() == "zh" else STAGE_LABELS
        stage = task.pipeline_stage
        current = self._progress_current_display(payload, stage_labels, stage)
        values = (
            current,
            self._progress_file_display(payload),
            eta,
            self.t("progress.calls_tokens", calls=calls, tokens=self._format_token_count(tokens)),
        )
        for label, value in zip(self.progress_stat_value_labels, values):
            label.configure(text=value)
        self._layout_progress_stat_tiles(values[1] != "—")

        usage_status = str(payload.get("usage_reporting_status", "pending") or "pending")
        if usage_status not in {"reported", "unavailable", "pending"}:
            usage_status = "pending"
        estimate_low = int(payload.get("estimated_total_tokens_low", 0) or 0)
        estimate_high = int(payload.get("estimated_total_tokens_high", 0) or 0)
        background = Colors.WARNING_CONTAINER if usage_status == "unavailable" else Colors.PRIMARY_CONTAINER
        foreground = Colors.WARNING if usage_status == "unavailable" else Colors.ON_PRIMARY_CONTAINER
        self.progress_token_strip.configure(background=background)
        self.progress_actual_usage.configure(
            text=self.t("progress.usage_actual", total=self._format_token_count(tokens)),
            background=background,
            foreground=foreground,
        )
        self.progress_projected_usage.configure(
            text=self.t("progress.usage_breakdown", input=self._format_token_count(input_tokens), output=self._format_token_count(output_tokens)),
            background=background,
        )
        self.progress_reporting.configure(
            text=(
                self.t("budget.projected", low=self._format_token_count(estimate_low), high=self._format_token_count(estimate_high))
                if estimate_high else self.t(f"budget.reporting.{usage_status}")
            ),
            background=background,
            foreground=foreground,
        )

    def _set_displayed_progress(self, value: float) -> None:
        self._displayed_progress_percent = max(0.0, min(100.0, value))
        self.progress_value.set(self._displayed_progress_percent)
        self.progress_percent_text.set(f"{self._displayed_progress_percent:.0f}%")

    def _animate_progress_to(self, target: float) -> None:
        if self._progress_animation_id is not None:
            try:
                self.root.after_cancel(self._progress_animation_id)
            except self.tk.TclError:
                pass
        start = self._displayed_progress_percent
        target = max(0.0, min(100.0, target))
        frames = max(1, Metrics.ANIMATION_DURATION_MS // Metrics.ANIMATION_FRAME_MS)

        def advance(frame: int) -> None:
            progress = min(1.0, frame / frames)
            eased = 1.0 - (1.0 - progress) ** 3
            self._set_displayed_progress(start + (target - start) * eased)
            if frame < frames:
                self._progress_animation_id = self.root.after(
                    Metrics.ANIMATION_FRAME_MS,
                    lambda: advance(frame + 1),
                )
            else:
                self._progress_animation_id = None

        advance(1)

    def _render_footer(self) -> None:
        slot_width = Metrics.COMPACT_FOOTER_SLOT_WIDTH if self.compact_layout else Metrics.FOOTER_SLOT_WIDTH
        rule = self.ttk.Frame(self.footer, style="FooterRule.TFrame", height=1)
        rule.grid(row=0, column=0, sticky="ew")
        rule.grid_propagate(False)
        action_bar = self.ttk.Frame(self.footer, style="ActionBar.TFrame")
        action_bar.grid(row=1, column=0, sticky="ew", pady=(Metrics.SPACE_2, 0))
        action_bar.columnconfigure(0, minsize=slot_width, uniform="footer_actions")
        action_bar.columnconfigure(1, weight=1)
        action_bar.columnconfigure(2, minsize=slot_width, uniform="footer_actions")
        self.footer_action_bar = action_bar
        model_blocked = self.step == 0 and self.connection_state != "verified"
        effect = self.ttk.Label(
            action_bar,
            text=self.t("model.required") if model_blocked else self.t(f"footer.effect.{self.STEPS[self.step]}"),
            style="ActionWarning.TLabel" if model_blocked else "ActionHint.TLabel",
            wraplength=260 if self.compact_layout else 560,
            anchor="center",
            justify="center",
        )
        effect_padding = Metrics.SPACE_3 if self.compact_layout else Metrics.SPACE_4
        effect.grid(row=0, column=1, sticky="ew", padx=effect_padding)
        if (self.step > 0 and self.step < 4) or self._can_leave_translation():
            self.back_button = self._button(
                action_bar,
                self.t("back"),
                lambda: self._go_to_step(3 if self.step == 4 else self.step - 1),
                kind="secondary",
                width=Metrics.FOOTER_BACK_WIDTH,
            )
            self.back_button.grid(row=0, column=0, sticky="ew")
            self._guide(self.back_button, "tip.back")
        inventory = self.scope_preview_inventory if self.scope_preview_signature == self._scope_signature() else None
        action_text = self.t("continue")
        if self.step == 3:
            if self.resume_candidate:
                action_text = self.t("progress.resume")
            elif inventory is not None and inventory.model_units:
                action_text = self.t("review.start_remaining", count=inventory.model_units)
            else:
                action_text = self.t("start")
        if self.step < 4:
            # Recoverable tasks enter step 05 stopped; resuming is an explicit user action.
            command = self._enter_translation if self.step == 3 and self.resume_candidate else (self._start if self.step == 3 else self._continue)
            self.next_button = self._button(action_bar, action_text, command, width=Metrics.FOOTER_ACTION_WIDTH)
            self.next_button.grid(row=0, column=2, sticky="ew")
            self._guide(self.next_button, "tip.start" if self.step == 3 else "tip.continue")
            if self.step == 3:
                self.start_button = self.next_button
                if self.scope_preview_status == "scanning":
                    self.next_button.configure(state="disabled")
            if self.step == 0 and self.connection_state != "verified":
                self.next_button.configure(state="disabled")
        elif self.step == 4:
            running = bool(self.worker and self.worker.is_alive())
            if running:
                self.pause_button = self._button(action_bar, self.t("progress.pause"), self._request_pause, width=Metrics.FOOTER_ACTION_WIDTH)
                self.pause_button.grid(row=0, column=2, sticky="ew")
                self._guide(self.pause_button, "tip.pause")
            elif not self.translation_started:
                start_button = self._button(action_bar, self.t("start"), self._start, width=Metrics.FOOTER_ACTION_WIDTH)
                start_button.grid(row=0, column=2, sticky="ew")
                self.start_button = start_button
                self._guide(start_button, "tip.start")
            elif self.last_stage in {"paused", "failed"}:
                label = self.t("progress.resume") if self.last_stage == "paused" else self.t("progress.retry")
                resume_button = self._button(action_bar, label, self._start, width=Metrics.FOOTER_ACTION_WIDTH)
                resume_button.grid(row=0, column=2, sticky="ew")
                self._guide(resume_button, "tip.resume")
            elif self.last_stage == "complete":
                output_dir = str(self.progress_payload.get("output_dir", "") or "")
                output_button = self._button(
                    action_bar, self.t("progress.open_rpy"),
                    lambda path=output_dir: self._open_folder(path), width=Metrics.FOOTER_ACTION_WIDTH,
                )
                output_button.grid(row=0, column=2, sticky="ew")
                if not output_dir:
                    output_button.configure(state="disabled")
            else:
                close_button = self._button(action_bar, self.t("close"), self._close_window, width=Metrics.FOOTER_ACTION_WIDTH)
                close_button.grid(row=0, column=2, sticky="ew")

    def _bind_provider_changes(self) -> None:
        for setting_name, variable in (
            ("provider_name", self.provider_name),
            ("base_url", self.base_url),
            ("api_key", self.api_key),
            ("model", self.model),
            ("reasoning_level", self.reasoning_level),
        ):
            variable.trace_add(
                "write",
                lambda *_args, changed_setting=setting_name: self._provider_changed(changed_setting),
            )

    def _provider_changed(self, changed_setting: str) -> None:
        if self._suspend_provider_trace or self.connection_state in {"connecting", "verifying"}:
            return
        if changed_setting != "api_key":
            self._schedule_settings_save()
        if self.connection_state != "idle":
            self.connection_state = "changed"
            self.connection_detail = {}
        if self.step == 0:
            # Update state in place so typing never destroys the focused field.
            if self.next_button is not None:
                self.next_button.configure(state="disabled")
            if hasattr(self, "verify_button"):
                self._refresh_model_panel()

    def _profile(self, *, require_model: bool = False) -> ModelProfile:
        profile = ModelProfile(
            name=self.provider_name.get().strip(),
            model=self.model.get().strip(),
            base_url=self.base_url.get().strip(),
            provider_id=self.selected_provider_id.get().strip() or "custom",
            api_key=self.api_key.get(),
            api_key_env=self.api_key_env.get().strip(),
            supports_json=self.supports_json.get(),
            reasoning_level=self.reasoning_level.get(),
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
        self._refresh_model_panel()

        def run() -> None:
            try:
                self.events.put(("catalog", OpenAICompatibleCatalog(profile).list_models()))
            except BaseException as exc:
                self.events.put(("catalog_error", exc))

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
        self._refresh_model_panel()

        def run() -> None:
            try:
                self.events.put(("verified", OpenAICompatibleCatalog(profile).verify_model()))
            except BaseException as exc:
                self.events.put(("connection_error", exc))

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    def _browse_project(self) -> None:
        from tkinter import filedialog

        selected = filedialog.askdirectory(title=self.t("game.project_picker"))
        if selected:
            self.project.set(selected)
            if not self.workspace.get().strip():
                self._suggest_workspace(selected)
            self._inspect_project_selection()
            if self.step in {1, 2}:
                self._render()

    def _schedule_project_inspection(self, *_args) -> None:
        if self._project_inspection_id is not None:
            try:
                self.root.after_cancel(self._project_inspection_id)
            except self.tk.TclError:
                pass
        self._project_inspection_id = self.root.after(350, self._inspect_project_selection)

    def _inspect_project_selection(self) -> None:
        self._project_inspection_id = None
        selected = self.project.get().strip()
        if not selected:
            self.existing_languages = []
            return
        try:
            project = ProjectDiscovery().discover(selected)
            summaries = discover_existing_languages(selected)
            sdk = RenpySdkLocator().resolve(project_root=project.project_root)
        except (OSError, ValueError, RuntimeError):
            self.existing_languages = []
            return
        self.existing_languages = summaries
        current_sdk = self.renpy_sdk.get().strip()
        if sdk is not None and (not current_sdk or current_sdk == self._auto_sdk_path):
            self._auto_sdk_path = str(sdk.root)
            self.renpy_sdk.set(self._auto_sdk_path)
            self.require_engine.set(True)
        elif sdk is None and current_sdk == self._auto_sdk_path:
            self._auto_sdk_path = ""
            self.renpy_sdk.set("")
            self.require_engine.set(False)

    def _suggest_workspace(self, project: str) -> None:
        source = Path(project).expanduser()
        name = source.parent.name if source.name.casefold() == "game" else source.name
        base = _user_home_fallback() / "Documents" / "RenWeaveWork"
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
            self._auto_sdk_path = ""
            self.renpy_sdk.set(selected)

    def _go_to_step(self, selected: int) -> None:
        leaving_translation = self.step == 4 and selected < 4
        if not (0 <= selected <= self.step and (self.step < 4 or self._can_leave_translation())):
            return
        if leaving_translation:
            changes = self._critical_config_changes()
            if changes:
                self._dialog(
                    self.t("progress.config_changed_title"),
                    self.t("progress.config_changed_body", fields="\n".join(f"• {field}" for field in changes)),
                    warning=True,
                    confirm_text=self.t("progress.continue_changed"),
                    on_confirm=lambda: self._go_to_step_after_warning(selected),
                )
                return
        self._go_to_step_after_warning(selected)

    def _go_to_step_after_warning(self, selected: int) -> None:
        self.step = selected
        self.content_canvas.yview_moveto(0.0)
        self._render()

    def _can_leave_translation(self) -> bool:
        running = bool(self.worker and self.worker.is_alive())
        return self.step == 4 and not running

    @staticmethod
    def _normalized_path(value: str) -> str:
        if not value.strip():
            return ""
        return os.path.normcase(str(Path(value.strip()).expanduser().resolve(strict=False)))

    def _critical_translation_config(self) -> dict[str, str]:
        return {
            "game": self._normalized_path(self.project.get()),
            "workspace": self._normalized_path(self.workspace.get()),
            "provider": self.selected_provider_id.get().strip(),
            "endpoint": self.base_url.get().strip().rstrip("/"),
            "model": self.model.get().strip(),
            "reasoning": self.reasoning_level.get().strip(),
            "source_language": self.source_language.get().strip() or "auto",
            "target_language": self.target_language.get().strip(),
            "renpy_sdk": self._normalized_path(self.renpy_sdk.get()),
            "engine_validation": str(bool(self.require_engine.get())),
        }

    def _critical_config_changes(self) -> tuple[str, ...]:
        if self.translation_baseline is None:
            return ()
        current = self._critical_translation_config()
        labels = {
            "game": self.t("game.project"),
            "workspace": self.t("game.workspace"),
            "provider": self.t("provider.selected"),
            "endpoint": self.t("model.endpoint"),
            "model": self.t("model.model"),
            "reasoning": self.t("model.reasoning"),
            "source_language": self.t("languages.source"),
            "target_language": self.t("languages.target"),
            "renpy_sdk": self.t("game.sdk"),
            "engine_validation": self.t("game.advanced"),
        }
        return tuple(labels[key] for key, value in current.items() if self.translation_baseline.get(key) != value)

    def _continue(self) -> None:
        if self.step == 0:
            if self.connection_state != "verified":
                self._dialog(self.t("dialog.cannot_continue"), self.t("model.required"), error=True)
                return
        elif self.step == 1:
            self._inspect_project_selection()
            if not Path(self.project.get().strip()).expanduser().exists() or not self.workspace.get().strip():
                self._dialog(self.t("dialog.cannot_continue"), self.t("game.invalid"), error=True)
                return
            if self.renpy_sdk.get().strip() and not Path(self.renpy_sdk.get().strip()).expanduser().exists():
                self._dialog(self.t("dialog.cannot_continue"), self.t("game.invalid"), error=True)
                return
        elif self.step == 2:
            self._commit_language_display("source")
            self._commit_language_display("target")
            if not self.target_language.get().strip():
                self._dialog(self.t("dialog.cannot_continue"), self.t("languages.required"), error=True)
                return
        self.step = min(3, self.step + 1)
        self.content_canvas.yview_moveto(0.0)
        if self.step == 3:
            self._start_scope_preview()
        self._render()

    def _enter_translation(self) -> None:
        """Open step 05 without starting any translation work."""
        if self.step != 3:
            return
        self.step = 4
        self.translation_started = False
        existing = self._resume_state()
        if existing:
            self.progress_payload = existing
            self.last_stage = str(existing.get("stage", "paused") or "paused")
            self.last_active_stage = self.last_stage if self.last_stage not in {"paused", "failed", "complete"} else "translating"
            self.logs = self._load_existing_log()
            self.status.set(self.t("progress.task.paused" if self.last_stage == "paused" else "progress.task.failed"))
        else:
            self.progress_payload = {}
            self.last_stage = ""
            self.last_active_stage = ""
            self.status.set(self.t("progress.ready"))
        self.content_canvas.yview_moveto(0.0)
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
            generate_rpa=self.generate_rpa.get(),
            install=self.install.get(),
            renpy_sdk=self.renpy_sdk.get().strip(),
            require_engine_validation=self.require_engine.get(),
        )

    def _resume_state(self) -> dict[str, object] | None:
        workspace = self.workspace.get().strip()
        project = self.project.get().strip()
        if not workspace or not project:
            return None
        state_path = Path(workspace).expanduser() / "state.json"
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8-sig"))
            same_project = str(Path(project).expanduser().resolve()) == str(payload.get("project_target", ""))
            same_languages = (
                str(payload.get("source_language", "auto")) == (self.source_language.get().strip() or "auto")
                and str(payload.get("target_language", "")) == self.target_language.get().strip()
            )
            recoverable_stage = str(payload.get("stage", "")) not in {"", "complete"}
            if same_project and same_languages and recoverable_stage and payload.get("completed_scene_ids"):
                return payload
        except (OSError, ValueError, TypeError):
            return None
        return None

    def _load_existing_log(self) -> list[str]:
        path = Path(self.workspace.get().strip()).expanduser() / "logs" / "renweave.log"
        try:
            return path.read_text(encoding="utf-8-sig", errors="replace").splitlines()[-120:]
        except OSError:
            return []

    def _copy_text(self, value: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(value)

    def _open_folder(self, value: str, *, containing: bool = False) -> None:
        """Open a generated output directory without blocking the Tk event loop."""
        if not value.strip():
            self._dialog(self.t("dialog.open_failed"), self.t("dialog.open_failed"), error=True)
            return
        target = Path(value).expanduser()
        if containing:
            target = target.parent
        try:
            target = target.resolve(strict=True)
            if not target.is_dir():
                raise NotADirectoryError(str(target))
            if os.name == "nt":
                os.startfile(str(target))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target)])
        except (OSError, ValueError) as exc:
            self._dialog(self.t("dialog.open_failed"), str(exc), error=True, details=str(exc))

    def _format_duration(self, seconds: int) -> str:
        if seconds < 0:
            return self.t("progress.estimating")
        if seconds < 60:
            return "< 1 min" if self.locale.get() == "en" else "少于 1 分钟"
        hours, remainder = divmod(seconds, 3600)
        minutes = max(1, remainder // 60)
        if hours:
            return f"{hours} h {minutes} min" if self.locale.get() == "en" else f"{hours} 小时 {minutes} 分钟"
        return f"{minutes} min" if self.locale.get() == "en" else f"{minutes} 分钟"

    def _request_pause(self) -> None:
        if self.cancel_token is None or self.cancel_token.cancelled:
            return
        self.cancel_token.cancel()
        self.status.set(self.t("progress.pausing"))
        if hasattr(self, "pause_button"):
            self.pause_button.configure(text=self.t("progress.pausing_action"), state="disabled")
        self._append_log(self.t("progress.pausing"))
        self._refresh_progress_panel(animate=False)

    def _close_window(self) -> None:
        if self.worker and self.worker.is_alive() and self.cancel_token is not None:
            self.cancel_token.cancel()
        if self._settings_save_id is not None:
            try:
                self.root.after_cancel(self._settings_save_id)
            except self.tk.TclError:
                pass
            self._save_desktop_settings()
        for after_id in (
            self._content_layout_id,
            self._progress_animation_id,
            self._responsive_render_id,
            self._restore_redraw_id,
            self._project_inspection_id,
        ):
            if after_id is None:
                continue
            try:
                self.root.after_cancel(after_id)
            except self.tk.TclError:
                pass
        self._save_api_key()
        self.root.destroy()

    def _start(self, *, skip_config_warning: bool = False) -> None:
        incremental = any(
            self.target_language.get().strip().casefold() == item.language.casefold()
            for item in self.existing_languages
        )
        if incremental and (
            self.scope_preview_signature != self._scope_signature()
            or self.scope_preview_status != "ready"
        ):
            self._start_scope_preview()
            self._render()
            return
        if self.worker and self.worker.is_alive():
            return
        changes = self._critical_config_changes()
        if changes and not skip_config_warning:
            self._dialog(
                self.t("progress.config_changed_title"),
                self.t("progress.config_changed_body", fields="\n".join(f"• {field}" for field in changes)),
                warning=True,
                confirm_text=self.t("progress.continue_changed"),
                on_confirm=lambda: self._start(skip_config_warning=True),
            )
            return
        try:
            if self.connection_state != "verified":
                raise ValueError(self.t("model.required"))
            request = self._request()
            request.validate()
        except (OSError, ValueError, TypeError) as exc:
            self._dialog(self.t("dialog.cannot_start"), str(exc), error=True)
            return
        self.translation_baseline = self._critical_translation_config()
        self.translation_started = True
        existing = self._resume_state()
        self.step = 4
        self.content_canvas.yview_moveto(0.0)
        self.status.set(self.t("progress.ready"))
        self.last_stage = str(existing.get("stage", "")) if existing else ""
        self.last_active_stage = (
            self.last_stage if self.last_stage not in {"paused", "failed", "complete"}
            else ("translating" if existing else "")
        )
        self.last_state_updated_at = ""
        self.progress_payload = existing or {}
        self.logs = self._load_existing_log() if existing else []
        self._append_log(self.t("progress.started"))
        self.cancel_token = CancellationToken()
        self.worker = threading.Thread(target=self._run_worker, args=(request,), daemon=True)
        self.worker.start()
        self._render()

    def _run_worker(self, request: TranslationRequest) -> None:
        try:
            state = execute_translation(
                request,
                cancel_token=self.cancel_token,
                progress_callback=lambda current: self.events.put(("progress", current.to_dict())),
            )
            self.events.put(("paused" if state.stage == PipelineStage.PAUSED else "complete", state))
        except BaseException as exc:
            self.events.put(("translation_error", (exc, traceback.format_exc())))

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
                    if not self.model.get().strip() and self.model_choices:
                        self.model.set(self.model_choices[0])
                finally:
                    self._suspend_provider_trace = False
                self.connection_state = "connected"
                self.connection_detail = {"count": len(catalog.models), "latency": catalog.latency_ms}
                self._refresh_model_panel()
            elif kind == "catalog_error":
                self.connection_state = "discovery_failed"
                self.connection_detail = {"message": str(value)}
                self._refresh_model_panel()
            elif kind == "verified":
                verified = value
                assert isinstance(verified, ModelVerification)
                self.connection_state = "verified"
                self.connection_detail = {"model": verified.model, "latency": verified.latency_ms}
                if not self._save_api_key():
                    self._dialog(
                        self.t("settings.key_error"),
                        self.t("settings.key_save_failed"),
                        error=True,
                    )
                self._schedule_settings_save()
                self._refresh_model_panel()
            elif kind == "connection_error":
                self.connection_state = "failed"
                self.connection_detail = {"message": str(value)}
                self._refresh_model_panel()
            elif kind == "update_result":
                result, manual = value
                assert isinstance(result, UpdateResult)
                self.update_result = result
                self.update_check_state = "available" if result.update_available else "current"
                if manual or result.update_available:
                    key = "settings.update_available" if result.update_available else "settings.up_to_date"
                    self._dialog(
                        self.t("settings.update_title"),
                        self.t(key, current=result.current_version, latest=result.latest_version)
                        + f"\n\n{result.release_url}",
                    )
            elif kind == "update_error":
                error, manual = value
                self.update_check_state = "failed"
                if manual:
                    self._dialog(self.t("settings.update_title"), str(error), error=True)
            elif kind == "scope_preview_ready":
                signature, result = value
                if signature == self._scope_signature():
                    inventory, budget = result
                    self.scope_preview_signature = signature
                    self.scope_preview_inventory = inventory
                    self.scope_preview_budget = budget
                    self.scope_preview_status = "ready"
                    self.scope_preview_worker = None
                    if self.step in {2, 3}:
                        self._render()
            elif kind == "scope_preview_error":
                signature, message = value
                if signature == self._scope_signature():
                    self.scope_preview_signature = signature
                    self.scope_preview_inventory = None
                    self.scope_preview_budget = None
                    self.scope_preview_status = "error"
                    self.scope_preview_error = message
                    self.scope_preview_worker = None
                    if self.step in {2, 3}:
                        self._render()
            elif kind == "progress":
                assert isinstance(value, dict)
                self._apply_progress_payload(value)
            elif kind == "complete":
                state = value
                self.worker = None
                self.progress_payload = state.to_dict()
                self.last_stage = "complete"
                self.status.set(self.t("progress.complete"))
                self._append_log(f"{self.t('progress.rpy_output')}: {state.output_dir}")
                if state.package_path:
                    self._append_log(f"{self.t('progress.rpa_output')}: {state.package_path}")
                if state.installed_dir:
                    self._append_log(f"{self.t('progress.installed_output')}: {state.installed_dir}")
                self._render()
                body_key = (
                    "progress.complete_body_rpa"
                    if state.package_path
                    else "progress.complete_body_rpy"
                )
                outputs = [f"{self.t('progress.rpy_output')}: {state.output_dir}"]
                if state.package_path:
                    outputs.append(f"{self.t('progress.rpa_output')}: {state.package_path}")
                dialog_actions = [
                    (
                        self.t("progress.open_rpy"),
                        lambda path=state.output_dir: self._open_folder(path),
                    )
                ]
                if state.package_path:
                    dialog_actions.append(
                        (
                            self.t("progress.open_rpa"),
                            lambda path=state.package_path: self._open_folder(path, containing=True),
                        )
                    )
                self._dialog(
                    self.t("dialog.complete"),
                    self.t(body_key) + "\n\n" + "\n".join(outputs),
                    extra_actions=tuple(dialog_actions),
                )
            elif kind == "paused":
                state = value
                self.worker = None
                self.progress_payload = state.to_dict()
                self.last_stage = "paused"
                self.status.set(self.t("progress.paused"))
                self._append_log(self.t("progress.paused_body"))
                self._render()
            elif kind == "translation_error":
                error, details = value
                self.worker = None
                self._read_pipeline_state()
                self.last_stage = "failed"
                self.status.set(self.t("progress.failed"))
                self._append_log(f"Error: {error}")
                self._render()
                self._dialog(
                    self.t("dialog.failed"),
                    f"{self.t('progress.failed_body')}\n\n{self._friendly_error(error)}",
                    error=True,
                    details=details,
                )
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
            updated_at = str(payload.get("updated_at", ""))
            if not updated_at or updated_at == self.last_state_updated_at:
                return
            self.last_state_updated_at = updated_at
            self._apply_progress_payload(payload)
        except (OSError, ValueError, TypeError):
            pass

    def _apply_progress_payload(self, payload: dict[str, object]) -> None:
        stage = str(payload.get("stage", ""))
        if not stage:
            return
        previous_stage = self.last_stage
        previous_completed = int(self.progress_payload.get("completed_scenes", 0) or 0)
        previous_file = str(self.progress_payload.get("current_file", "") or "")
        completed = int(payload.get("completed_scenes", len(payload.get("completed_scene_ids", []))) or 0)
        self.progress_payload = payload
        self.last_state_updated_at = str(payload.get("updated_at", self.last_state_updated_at))
        self.last_stage = stage
        if stage not in {"paused", "failed", "complete"}:
            self.last_active_stage = stage
        labels = STAGE_LABELS_ZH if self.locale.get() == "zh" else STAGE_LABELS
        label = labels.get(stage, stage)
        operation = str(payload.get("current_operation", "") or label)
        display_operation = self._progress_current_display(payload, labels, stage).replace("\n", " · ")
        self.status.set(display_operation)
        current_file = str(payload.get("current_file", "") or "")
        if stage != previous_stage or completed != previous_completed or current_file != previous_file:
            total = int(payload.get("total_scenes", 0) or 0)
            suffix = f" · {completed}/{total}" if total else ""
            self._append_log(f"{label}{suffix} — {operation}")
        if self.step == 4:
            self._refresh_progress_panel(animate=True)

    def _append_log(self, text: str) -> None:
        self.logs.append(text.rstrip())
        if len(self.logs) > 500:
            self.logs = self.logs[-500:]
        if self.log is not None and self.log.winfo_exists():
            self.log.configure(state="normal")
            self.log.insert("end", text.rstrip() + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")

    def _dialog(
        self,
        title: str,
        body: str,
        *,
        error: bool = False,
        details: str = "",
        warning: bool = False,
        confirm_text: str = "",
        on_confirm: Callable[[], None] | None = None,
        extra_actions: tuple[tuple[str, Callable[[], None]], ...] = (),
    ) -> MaterialDialog:
        return MaterialDialog(
            self,
            title,
            body,
            error=error,
            details=details,
            warning=warning,
            confirm_text=confirm_text,
            on_confirm=on_confirm,
            extra_actions=extra_actions,
        )


def _show_startup_error(root, exception: Exception, details: str) -> None:
    """Render a last-resort branded error surface instead of a bootloader popup."""
    import tkinter as tk

    for child in root.winfo_children():
        child.destroy()
    root.title("RenWeave")
    root.configure(background=Colors.SURFACE)
    root.geometry("720x360")
    root.minsize(620, 320)
    root.resizable(True, True)
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)
    card = tk.Frame(
        root,
        background=Colors.CARD,
        highlightbackground=Colors.OUTLINE_VARIANT,
        highlightthickness=1,
        padx=28,
        pady=24,
    )
    card.grid(row=0, column=0, sticky="nsew", padx=32, pady=32)
    card.columnconfigure(1, weight=1)
    accent = tk.Frame(card, background=Colors.ERROR, width=4)
    accent.grid(row=0, column=0, rowspan=3, sticky="ns", padx=(0, 18))
    accent.grid_propagate(False)
    locale_zh = os.environ.get("LANG", "").casefold().startswith("zh")
    title = "RenWeave 无法启动" if locale_zh else "RenWeave could not start"
    body = (
        f"桌面界面启动时遇到问题。复制详细信息后可用于诊断。\n\n{exception}"
        if locale_zh
        else f"The desktop interface encountered a startup problem. Copy the details for diagnosis.\n\n{exception}"
    )
    tk.Label(card, text=title, background=Colors.CARD, foreground=Colors.ON_SURFACE, font=(Typography.UI, Typography.TITLE, "bold"), anchor="w").grid(row=0, column=1, sticky="ew")
    tk.Label(card, text=body, background=Colors.CARD, foreground=Colors.ON_SURFACE_VARIANT, font=(Typography.UI, Typography.BODY), anchor="w", justify="left", wraplength=560).grid(row=1, column=1, sticky="ew", pady=(12, 20))
    actions = tk.Frame(card, background=Colors.CARD)
    actions.grid(row=2, column=1, sticky="e")

    def copy_details() -> None:
        root.clipboard_clear()
        root.clipboard_append(details)

    button_options = {
        "font": (Typography.UI, Typography.BODY, "bold"),
        "relief": "flat",
        "borderwidth": 0,
        "cursor": "hand2",
        "padx": 18,
        "pady": 10,
    }
    tk.Button(actions, text="复制详细信息" if locale_zh else "Copy details", command=copy_details, background=Colors.SURFACE_HIGH, foreground=Colors.ON_SURFACE, activebackground=Colors.CONTROL_HOVER, **button_options).pack(side="left", padx=(0, 8))
    close = tk.Button(actions, text="关闭" if locale_zh else "Close", command=root.destroy, background=Colors.PRIMARY, foreground=Colors.ON_PRIMARY, activebackground=Colors.PRIMARY_HOVER, **button_options)
    close.pack(side="left")
    root.bind("<Escape>", lambda _event: root.destroy())
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    RenWeaveDesktopApp._style_native_window(root, dark=False)
    close.focus_set()


def launch_gui(*, initial_project: str = "", initial_workspace: str = "") -> int:
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            pass
    try:
        import tkinter as tk
    except ImportError as exc:
        raise RuntimeError("Tk is not installed for this Python environment") from exc
    try:
        root = tk.Tk()
    except Exception as exc:
        raise RuntimeError(f"Unable to start the desktop interface: {exc}") from exc
    root.withdraw()
    try:
        RenWeaveDesktopApp(root, initial_project=initial_project, initial_workspace=initial_workspace)
    except Exception as exc:
        details = traceback.format_exc()
        _show_startup_error(root, exc, details)
        root.update_idletasks()
        root.deiconify()
        root.mainloop()
        return 1
    root.update_idletasks()
    root.deiconify()
    root.mainloop()
    return 0


def main() -> int:
    return launch_gui()
