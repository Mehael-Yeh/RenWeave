from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
from pathlib import Path
import time
from typing import Callable

from .acquisition import ArchiveAcquirer
from .build_validation import RenpyBuildValidator
from .context import ContextPlanner
from .decompiler import DecompilationError, DecompilationManifest, UnrpycDecompiler, UnrpycToolManager
from .discovery import ProjectDiscovery
from .emitter import BuildManifest, RenpyTranslationEmitter, normalize_renpy_language
from .indexer import ProjectIndexer
from .installer import TranslationInstaller
from .io import atomic_write_json, read_json
from .knowledge import DeterministicKnowledgeBuilder, KnowledgeBase
from .models import ProjectIndex, TextChannel, TextUnit
from .narrative import NarrativeKnowledge, NarrativeKnowledgeSynthesizer
from .packaging import PackageManifest, TranslationPackager
from .provider import ModelProfile, OpenAICompatibleGateway
from .refinement import GlobalTranslationRefiner
from .runtime import CancellationRequested, CancellationToken, RunLogger, exclusive_workspace_run
from .translation import SceneTranslator
from .usage import estimate_index_tokens
from .validation import TranslationValidator


ANALYSIS_SCHEMA_VERSION = 6


class PipelineStage(str, Enum):
    def __str__(self) -> str:
        return self.value

    CREATED = "created"
    DISCOVERED = "discovered"
    ACQUIRED = "acquired"
    DECOMPILED = "decompiled"
    INDEXED = "indexed"
    KNOWLEDGE_READY = "knowledge_ready"
    SYNTHESIZING = "synthesizing"
    NARRATIVE_READY = "narrative_ready"
    TRANSLATING = "translating"
    VALIDATED = "validated"
    REFINING = "refining"
    REFINED = "refined"
    BUILDING = "building"
    VALIDATING_BUILD = "validating_build"
    COMPLETE = "complete"
    PAUSED = "paused"
    FAILED = "failed"


@dataclass(slots=True)
class PipelineState:
    schema_version: int
    project_target: str
    source_language: str
    target_language: str
    stage: PipelineStage
    completed_scene_ids: list[str]
    failed_scene_ids: list[str]
    updated_at: str
    error: str = ""
    renpy_language: str = ""
    output_dir: str = ""
    installed_dir: str = ""
    generate_rpa: bool = True
    package_path: str = ""
    package_sha256: str = ""
    build_validation_status: str = ""
    engine_validation_status: str = ""
    project_fingerprint: str = ""
    analysis_schema_version: int = 0
    knowledge_model_calls: int = 0
    knowledge_cache_hits: int = 0
    knowledge_warnings: int = 0
    refinement_model_calls: int = 0
    refinement_cache_hits: int = 0
    refinement_changes: int = 0
    total_model_calls: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    model_requests_attempted: int = 0
    run_status: str = "idle"
    started_at: str = ""
    current_operation: str = ""
    current_scene_id: str = ""
    current_scene_label: str = ""
    total_scenes: int = 0
    completed_scenes: int = 0
    total_text_units: int = 0
    completed_text_units: int = 0
    progress_percent: float = 0.0
    eta_seconds: int = -1
    translation_seconds: float = 0.0
    scene_attempts: int = 0
    resumed_count: int = 0
    pause_reason: str = ""
    log_path: str = ""
    phase_completed: int = 0
    phase_total: int = 0
    estimated_input_tokens_low: int = 0
    estimated_input_tokens_high: int = 0
    estimated_output_tokens_low: int = 0
    estimated_output_tokens_high: int = 0
    estimated_total_tokens_low: int = 0
    estimated_total_tokens_high: int = 0
    source_token_equivalent: int = 0
    token_estimate_confidence: str = ""
    usage_reporting_status: str = "pending"
    knowledge_prompt_tokens: int = 0
    knowledge_completion_tokens: int = 0
    refinement_prompt_tokens: int = 0
    refinement_completion_tokens: int = 0

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["stage"] = str(self.stage)
        return payload


class RenWeavePipeline:
    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.state_path = self.workspace / "state.json"
        self.index_path = self.workspace / "project-index.json"
        self.knowledge_path = self.workspace / "knowledge.json"
        self.narrative_path = self.workspace / "narrative-knowledge.json"
        self.knowledge_cache_dir = self.workspace / "knowledge-cache"
        self.refinement_path = self.workspace / "refinement.json"
        self.refinement_cache_dir = self.workspace / "refinement-cache"
        self.acquisition_path = self.workspace / "acquisition.json"
        self.decompilation_path = self.workspace / "decompilation.json"
        self.acquired_dir = self.workspace / "acquired"
        self.decompiled_dir = self.workspace / "decompiled"
        self.tools_dir = self.workspace / "tools"
        self.translations_dir = self.workspace / "translations"
        self.reports_dir = self.workspace / "reports"
        self.output_dir = self.workspace / "output"
        self.packages_dir = self.workspace / "packages"
        self.package_path = self.workspace / "package.json"
        self.validation_dir = self.workspace / "validation"
        self.build_validation_path = self.workspace / "build-validation.json"
        self.usage_path = self.workspace / "usage.json"
        self.logger = RunLogger(self.workspace)
        self._progress_callback: Callable[[PipelineState], None] | None = None
        self._last_logged_stage = ""

    def analyze(
        self,
        target: str | Path,
        *,
        source_language: str = "auto",
        target_language: str = "und",
        unrpyc_path: str | Path | None = None,
        allow_tool_download: bool = False,
        cancel_token: CancellationToken | None = None,
        progress_callback: Callable[[PipelineState], None] | None = None,
    ) -> tuple[ProjectIndex, KnowledgeBase]:
        if progress_callback is not None:
            self._progress_callback = progress_callback
        resolved_target = str(Path(target).expanduser().resolve())
        project = ProjectDiscovery().discover(target)
        project_fingerprint = self._project_fingerprint(project)
        reusable_state: PipelineState | None = None
        if self.state_path.is_file() and self.index_path.is_file() and self.knowledge_path.is_file():
            try:
                existing = self._load_state()
                cached_index = ProjectIndex.from_dict(read_json(self.index_path))
                cached_knowledge = KnowledgeBase.from_dict(read_json(self.knowledge_path))
            except (OSError, ValueError, TypeError, KeyError) as exc:
                self.logger.event(
                    "WARNING",
                    "analysis_cache_rejected",
                    str(exc),
                    state_path=str(self.state_path),
                )
            else:
                same_inputs = (
                    existing.schema_version >= 4
                    and existing.project_target == resolved_target
                    and existing.project_fingerprint == project_fingerprint
                    and existing.source_language == source_language
                    and existing.target_language == target_language
                )
                if same_inputs and existing.analysis_schema_version == ANALYSIS_SCHEMA_VERSION:
                    existing.resumed_count += 1
                    existing.run_status = "running"
                    existing.pause_reason = ""
                    self.logger.event(
                        "INFO",
                        "analysis_resumed",
                        "Reused unchanged project analysis",
                        stage=str(existing.stage),
                        completed_scenes=len(existing.completed_scene_ids),
                    )
                    self._save_state(existing)
                    return cached_index, cached_knowledge
                if same_inputs:
                    reusable_state = existing
                    self.logger.event(
                        "INFO",
                        "analysis_cache_upgraded",
                        "Rebuilding analysis for a newer parser while preserving scene checkpoints",
                        previous_schema=existing.analysis_schema_version,
                        current_schema=ANALYSIS_SCHEMA_VERSION,
                        completed_scenes=len(existing.completed_scene_ids),
                    )
        if reusable_state is None:
            state = self._new_state(
                target,
                source_language,
                target_language,
                project_fingerprint=project_fingerprint,
            )
        else:
            state = reusable_state
            state.analysis_schema_version = ANALYSIS_SCHEMA_VERSION
            state.stage = PipelineStage.CREATED
            state.run_status = "running"
            state.pause_reason = ""
            state.error = ""
            state.current_operation = "Upgrading the project analysis"
            self._save_state(state)
        try:
            state.stage = PipelineStage.DISCOVERED
            state.current_operation = "Discovering the Ren'Py project"
            self._save_state(state)
            self._raise_if_cancelled(state, cancel_token, "Cancellation requested after project discovery")
            acquisition = ArchiveAcquirer().acquire(project, self.acquired_dir, scripts_only=True)
            atomic_write_json(self.acquisition_path, acquisition.to_dict())
            state.stage = PipelineStage.ACQUIRED
            state.current_operation = "Collecting scripts from game files and archives"
            self._save_state(state)
            self._raise_if_cancelled(state, cancel_token, "Cancellation requested after script acquisition")
            decompilation = self._decompile_roots(
                [Path(project.game_dir), *acquisition.source_roots],
                unrpyc_path=unrpyc_path,
                allow_tool_download=allow_tool_download,
            )
            state.stage = PipelineStage.DECOMPILED
            state.current_operation = "Decompiling compiled Ren'Py scripts"
            self._save_state(state)
            self._raise_if_cancelled(state, cancel_token, "Cancellation requested after script decompilation")
            index = ProjectIndexer().build(
                target,
                additional_source_roots=[
                    *acquisition.source_roots,
                    *decompilation.output_roots,
                ],
            )
            atomic_write_json(self.index_path, index.to_dict())
            state.stage = PipelineStage.INDEXED
            state.current_operation = "Building the scene and control-flow index"
            self._save_state(state)
            self._raise_if_cancelled(state, cancel_token, "Cancellation requested after scene indexing")
            knowledge = DeterministicKnowledgeBuilder().build(index)
            atomic_write_json(self.knowledge_path, knowledge.to_dict())
            state.stage = PipelineStage.KNOWLEDGE_READY
            state.current_operation = "Deterministic game knowledge is ready"
            self._save_state(state)
            self._raise_if_cancelled(state, cancel_token, "Cancellation requested after deterministic analysis")
            return index, knowledge
        except CancellationRequested:
            raise
        except Exception as exc:
            state.stage = PipelineStage.FAILED
            state.run_status = "failed"
            state.error = str(exc)
            self.logger.exception("analysis_failed", exc, stage=str(state.stage))
            self._save_state(state)
            raise

    def decompile(
        self,
        target: str | Path,
        *,
        unrpyc_path: str | Path | None = None,
        allow_tool_download: bool = False,
    ) -> DecompilationManifest:
        project = ProjectDiscovery().discover(target)
        acquisition = ArchiveAcquirer().acquire(project, self.acquired_dir, scripts_only=True)
        atomic_write_json(self.acquisition_path, acquisition.to_dict())
        return self._decompile_roots(
            [Path(project.game_dir), *acquisition.source_roots],
            unrpyc_path=unrpyc_path,
            allow_tool_download=allow_tool_download,
        )

    def _decompile_roots(
        self,
        source_roots: list[Path],
        *,
        unrpyc_path: str | Path | None,
        allow_tool_download: bool,
    ) -> DecompilationManifest:
        if self._requires_decompilation(source_roots):
            entrypoint = UnrpycToolManager(self.tools_dir).resolve(
                unrpyc_path,
                allow_download=allow_tool_download,
            )
            try:
                manifest = UnrpycDecompiler(entrypoint).decompile(
                    source_roots,
                    self.decompiled_dir,
                )
            except DecompilationError as exc:
                if exc.manifest is not None:
                    atomic_write_json(self.decompilation_path, exc.manifest.to_dict())
                raise
        else:
            manifest = DecompilationManifest(
                schema_version=1,
                tool="not-required",
                tool_version="",
                output_roots=[],
                skipped_with_source=[],
                files=[],
            )
        atomic_write_json(self.decompilation_path, manifest.to_dict())
        return manifest

    @exclusive_workspace_run
    def translate(
        self,
        target: str | Path,
        source_language: str,
        target_language: str,
        profile: ModelProfile,
        *,
        limit: int = 0,
        gateway: OpenAICompatibleGateway | None = None,
        install: bool = False,
        overwrite_existing: bool = False,
        repair_attempts: int = 2,
        unrpyc_path: str | Path | None = None,
        allow_tool_download: bool = False,
        synthesize_knowledge: bool = True,
        refine_translations: bool = True,
        generate_rpa: bool = True,
        renpy_sdk_path: str | Path | None = None,
        require_engine_validation: bool = False,
        cancel_token: CancellationToken | None = None,
        progress_callback: Callable[[PipelineState], None] | None = None,
    ) -> PipelineState:
        self._progress_callback = progress_callback
        if not target_language.strip() or target_language.casefold() == "und":
            raise ValueError("翻译任务必须指定明确的目标语言")
        try:
            index, knowledge = self.analyze(
                target,
                source_language=source_language,
                target_language=target_language,
                unrpyc_path=unrpyc_path,
                allow_tool_download=allow_tool_download,
                cancel_token=cancel_token,
                progress_callback=progress_callback,
            )
        except CancellationRequested:
            return self._load_state()
        state = self._load_state()
        completed_run_before_resume = state.stage == PipelineStage.COMPLETE
        state.run_status = "running"
        state.generate_rpa = bool(generate_rpa)
        if not install:
            state.installed_dir = ""
        state.pause_reason = ""
        state.error = ""
        if not state.started_at:
            state.started_at = self._now()
        gateway = gateway or OpenAICompatibleGateway(profile)
        usage_base = (
            state.total_model_calls,
            state.total_prompt_tokens,
            state.total_completion_tokens,
            state.model_requests_attempted,
        )
        narrative: NarrativeKnowledge | None = None
        candidates = [scene for scene in index.scenes if scene.text_units]
        if limit > 0:
            candidates = candidates[:limit]
        state.total_scenes = len(candidates)
        state.total_text_units = sum(len(scene.text_units) for scene in candidates)
        budget = estimate_index_tokens(index)
        state.estimated_input_tokens_low = budget.estimated_input_low
        state.estimated_input_tokens_high = budget.estimated_input_high
        state.estimated_output_tokens_low = budget.estimated_output_low
        state.estimated_output_tokens_high = budget.estimated_output_high
        state.estimated_total_tokens_low = budget.estimated_total_low
        state.estimated_total_tokens_high = budget.estimated_total_high
        state.source_token_equivalent = budget.source_token_equivalent
        state.token_estimate_confidence = budget.confidence
        validator = TranslationValidator()
        self._reconcile_completed_scenes(index, candidates, state, validator)
        expected_scene_ids = {scene.id for scene in index.scenes if scene.text_units}
        reuse_completed_model_outputs = (
            completed_run_before_resume
            and expected_scene_ids <= set(state.completed_scene_ids)
            and not state.failed_scene_ids
        )
        state.completed_text_units = sum(
            len(scene.text_units) for scene in candidates if scene.id in state.completed_scene_ids
        )
        state.current_operation = "Preparing or restoring project context"
        self.logger.event(
            "INFO",
            "run_started",
            "Translation run started",
            resumed=bool(state.completed_scene_ids),
            completed_scenes=len(state.completed_scene_ids),
            total_scenes=state.total_scenes,
            project_fingerprint=state.project_fingerprint,
        )
        self._save_state(state)
        if self._cancelled(cancel_token):
            return self._pause(state, "Cancellation requested before model work")
        text_scene_count = len(candidates)
        if synthesize_knowledge and text_scene_count >= 4 and not reuse_completed_model_outputs:
            state.stage = PipelineStage.SYNTHESIZING
            state.current_operation = "Understanding storylines, characters, and terminology"
            state.phase_completed = 0
            state.phase_total = 1
            self._save_state(state)
            chunk_characters = 24000
            if profile.context_window > 0:
                chunk_characters = max(4000, min(36000, profile.context_window // 2))
            try:
                narrative = NarrativeKnowledgeSynthesizer(
                    gateway,
                    self.knowledge_cache_dir,
                    max_chunk_characters=chunk_characters,
                    cancel_check=lambda: self._cancelled(cancel_token),
                    progress_callback=lambda done, total, message: self._phase_progress(
                        state, done, total, message, gateway, usage_base
                    ),
                ).synthesize(
                    index,
                    knowledge,
                    project_fingerprint=state.project_fingerprint,
                    source_language=source_language,
                )
            except CancellationRequested as exc:
                return self._pause(state, str(exc))
            atomic_write_json(self.narrative_path, narrative.to_dict())
            state.knowledge_model_calls += narrative.usage.model_calls
            state.knowledge_cache_hits += narrative.usage.cache_hits
            state.knowledge_prompt_tokens += narrative.usage.prompt_tokens
            state.knowledge_completion_tokens += narrative.usage.completion_tokens
            state.knowledge_warnings = len(narrative.warnings)
            state.stage = PipelineStage.NARRATIVE_READY
            state.current_operation = "Narrative context is ready"
            self._sync_gateway_usage(state, gateway, usage_base)
            self._save_state(state)
            if self._cancelled(cancel_token):
                return self._pause(state, "Cancellation requested after narrative analysis")
        state.stage = PipelineStage.TRANSLATING
        state.current_operation = "Translating scenes with narrative context"
        state.phase_completed = 0
        state.phase_total = 0
        self._save_state(state)
        translator = SceneTranslator(gateway)
        planner = ContextPlanner()
        string_memory = self._load_string_memory(index, state.completed_scene_ids)
        self.translations_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        consecutive_failures = 0
        for scene in candidates:
            if scene.id in state.completed_scene_ids:
                continue
            if self._cancelled(cancel_token):
                return self._pause(state, "Cancellation requested before the next scene")
            scene_started = time.perf_counter()
            state.current_scene_id = scene.id
            state.current_scene_label = scene.label
            state.current_operation = f"Translating scene: {scene.label}"
            self._save_state(state)
            try:
                context = planner.build(index, knowledge, scene.id, narrative)
                result = translator.translate(
                    context,
                    target_language,
                    source_language=source_language,
                )
                self._apply_string_memory(scene.text_units, result.translations, string_memory)
                report = validator.validate_scene(index, scene.id, result.translations)
                expected_ids = {unit.id for unit in scene.text_units}
                for _attempt in range(max(0, repair_attempts)):
                    for text_id in list(result.translations):
                        if text_id not in expected_ids:
                            result.translations.pop(text_id)
                    report = validator.validate_scene(index, scene.id, result.translations)
                    if report.passed:
                        break
                    issues: dict[str, list[str]] = {}
                    for issue in report.issues:
                        if issue.text_id in expected_ids:
                            issues.setdefault(issue.text_id, []).append(issue.code)
                    repairs = translator.repair(
                        context,
                        target_language,
                        result.translations,
                        issues,
                        source_language=source_language,
                    )
                    for text_id, translated in repairs.items():
                        if text_id in expected_ids:
                            result.translations[text_id] = translated
                    self._apply_string_memory(scene.text_units, result.translations, string_memory)
                report = validator.validate_scene(index, scene.id, result.translations)
                atomic_write_json(self.translations_dir / f"{scene.id}.json", {
                    "scene_id": scene.id,
                    "translations": result.translations,
                })
                atomic_write_json(self.reports_dir / f"{scene.id}.json", report.to_dict())
                if report.passed:
                    consecutive_failures = 0
                    if scene.id in state.failed_scene_ids:
                        state.failed_scene_ids.remove(scene.id)
                    state.completed_scene_ids.append(scene.id)
                else:
                    consecutive_failures += 1
                    if scene.id in state.completed_scene_ids:
                        state.completed_scene_ids.remove(scene.id)
                    if scene.id not in state.failed_scene_ids:
                        state.failed_scene_ids.append(scene.id)
            except Exception as exc:
                consecutive_failures += 1
                if scene.id in state.completed_scene_ids:
                    state.completed_scene_ids.remove(scene.id)
                if scene.id not in state.failed_scene_ids:
                    state.failed_scene_ids.append(scene.id)
                atomic_write_json(self.reports_dir / f"{scene.id}.json", {
                    "expected": len(scene.text_units),
                    "received": 0,
                    "passed": False,
                    "issues": [{
                        "code": "SCENE_EXCEPTION",
                        "text_id": "",
                        "message": f"{type(exc).__name__}: {exc}"[:2000],
                    }],
                })
                state.error = f"场景 {scene.label} 翻译失败：{exc}"[:2000]
                self.logger.exception(
                    "scene_failed",
                    exc,
                    scene_id=scene.id,
                    scene_label=scene.label,
                )
            duration = max(0.0, time.perf_counter() - scene_started)
            state.translation_seconds += duration
            state.scene_attempts += 1
            state.completed_text_units = sum(
                len(item.text_units)
                for item in candidates
                if item.id in state.completed_scene_ids
            )
            remaining = max(0, state.total_scenes - len(state.completed_scene_ids))
            average = state.translation_seconds / max(1, state.scene_attempts)
            state.eta_seconds = max(0, round(average * remaining + min(120.0, average * 2)))
            self._sync_gateway_usage(state, gateway, usage_base)
            self._save_state(state)
            self.logger.event(
                "INFO",
                "scene_checkpoint",
                "Scene checkpoint saved",
                scene_id=scene.id,
                scene_label=scene.label,
                duration_seconds=round(duration, 2),
                passed=scene.id in state.completed_scene_ids,
                completed_scenes=len(state.completed_scene_ids),
                total_scenes=state.total_scenes,
                eta_seconds=state.eta_seconds,
            )
            if self._cancelled(cancel_token):
                return self._pause(state, "Cancellation requested after the latest scene checkpoint")
            if consecutive_failures >= 3:
                state.error = "Three consecutive scenes failed; the run stopped to avoid repeated API errors"
                state.current_operation = "Stopped after repeated scene failures"
                self.logger.event(
                    "ERROR",
                    "failure_circuit_opened",
                    state.error,
                    failed_scene_ids=state.failed_scene_ids[-3:],
                )
                self._save_state(state)
                break

        completed_scene_ids = set(state.completed_scene_ids)
        if expected_scene_ids <= completed_scene_ids and not state.failed_scene_ids:
            collected = self._collect_translations(state.completed_scene_ids)
            if reuse_completed_model_outputs:
                self.logger.event(
                    "INFO",
                    "completed_model_outputs_reused",
                    "Reused all model outputs from the unchanged completed run",
                    completed_scenes=len(state.completed_scene_ids),
                    total_model_calls=state.total_model_calls,
                )
            if refine_translations and not reuse_completed_model_outputs:
                if self._cancelled(cancel_token):
                    return self._pause(state, "Cancellation requested before global refinement")
                state.stage = PipelineStage.REFINING
                state.current_operation = "Reviewing terminology and voice consistency"
                state.phase_completed = 0
                state.phase_total = 1
                self._save_state(state)
                batch_characters = 24000
                if profile.context_window > 0:
                    batch_characters = max(4000, min(36000, profile.context_window // 2))
                try:
                    collected, refinement = GlobalTranslationRefiner(
                        gateway,
                        self.refinement_cache_dir,
                        max_batch_characters=batch_characters,
                        cancel_check=lambda: self._cancelled(cancel_token),
                        progress_callback=lambda done, total, message: self._phase_progress(
                            state, done, total, message, gateway, usage_base
                        ),
                    ).refine(
                        index,
                        collected,
                        narrative,
                        source_language=source_language,
                        target_language=target_language,
                    )
                except CancellationRequested as exc:
                    return self._pause(state, str(exc))
                self._persist_translations(index, collected)
                atomic_write_json(self.refinement_path, refinement.to_dict())
                state.refinement_model_calls += refinement.usage.model_calls
                state.refinement_cache_hits += refinement.usage.cache_hits
                state.refinement_prompt_tokens += refinement.usage.prompt_tokens
                state.refinement_completion_tokens += refinement.usage.completion_tokens
                state.refinement_changes = len(refinement.changes)
                state.stage = PipelineStage.REFINED
                state.current_operation = "Global refinement is complete"
                self._sync_gateway_usage(state, gateway, usage_base)
                self._save_state(state)
                if self._cancelled(cancel_token):
                    return self._pause(state, "Cancellation requested after global refinement")
            state.stage = PipelineStage.BUILDING
            state.current_operation = "Generating Ren'Py translation scripts"
            self._save_state(state)
            try:
                manifest = RenpyTranslationEmitter().emit(
                    index,
                    collected,
                    target_language,
                    self.output_dir,
                )
                state.stage = PipelineStage.VALIDATING_BUILD
                state.current_operation = "Validating generated Ren'Py scripts"
                self._save_state(state)
                validation = self._validate_build(
                    manifest,
                    index,
                    sdk_path=renpy_sdk_path,
                    require_engine=require_engine_validation,
                )
                state.build_validation_status = "passed" if validation.static_passed else "failed"
                state.engine_validation_status = validation.engine.status
                if not validation.passed:
                    raise ValueError("生成的语言包未通过构建验证，详见 build-validation.json")
                state.renpy_language = manifest.renpy_language
                state.output_dir = manifest.output_dir
                self._apply_package_choice(
                    manifest,
                    state,
                    generate_rpa=generate_rpa,
                    compiled_project=(
                        validation.staged_project
                        if validation.engine.status == "passed"
                        else None
                    ),
                )
                if install:
                    installed = TranslationInstaller().install(
                        manifest,
                        index.project.game_dir,
                        overwrite_existing=overwrite_existing,
                    )
                    atomic_write_json(self.workspace / "install.json", installed.to_dict())
                    state.installed_dir = installed.destination
                state.stage = PipelineStage.COMPLETE
                state.run_status = "complete"
                state.current_operation = (
                    "Translation scripts and RPA archive are ready"
                    if generate_rpa
                    else "Translation scripts are ready"
                )
                state.current_scene_id = ""
                state.current_scene_label = ""
                state.eta_seconds = 0
                state.error = ""
            except Exception as exc:
                state.stage = PipelineStage.FAILED
                state.run_status = "failed"
                state.current_operation = "Build validation or output generation failed"
                state.error = str(exc)
                self.logger.exception("build_failed", exc, stage=str(state.stage))
                self._save_state(state)
                raise
        else:
            state.stage = PipelineStage.VALIDATED
            state.run_status = "failed"
            state.current_operation = "Some scenes require attention before packaging"
            remaining = len(expected_scene_ids - completed_scene_ids)
            if state.failed_scene_ids:
                state.error = (
                    f"{len(state.failed_scene_ids)} 个场景翻译失败，"
                    f"仍有 {remaining} 个场景未完成；详见 reports 目录"
                )
            elif remaining:
                state.error = f"仍有 {remaining} 个场景未完成"
        self._sync_gateway_usage(state, gateway, usage_base)
        self._save_state(state)
        return state

    def pause(self, reason: str = "Interrupted by user") -> PipelineState:
        """Mark an existing workspace as safely resumable after an external interrupt."""
        if not self.state_path.is_file():
            raise ValueError("No translation state exists in this workspace")
        return self._pause(self._load_state(), reason)

    @exclusive_workspace_run
    def build(
        self,
        *,
        requested_language: str | None = None,
        install: bool = False,
        overwrite_existing: bool = False,
        generate_rpa: bool = True,
        renpy_sdk_path: str | Path | None = None,
        require_engine_validation: bool = False,
    ):
        """Build translation scripts from all validated scene artifacts."""
        state = self._load_state()
        state.generate_rpa = bool(generate_rpa)
        if not install:
            state.installed_dir = ""
        index = ProjectIndex.from_dict(read_json(self.index_path))
        language = requested_language or state.target_language
        expected_scene_ids = {scene.id for scene in index.scenes if scene.text_units}
        if not expected_scene_ids <= set(state.completed_scene_ids):
            missing = len(expected_scene_ids - set(state.completed_scene_ids))
            raise ValueError(f"仍有 {missing} 个场景没有通过验证，不能构建语言包")
        manifest = RenpyTranslationEmitter().emit(
            index,
            self._collect_translations(state.completed_scene_ids),
            language,
            self.output_dir,
        )
        state.stage = PipelineStage.VALIDATING_BUILD
        self._save_state(state)
        validation = self._validate_build(
            manifest,
            index,
            sdk_path=renpy_sdk_path,
            require_engine=require_engine_validation,
        )
        state.build_validation_status = "passed" if validation.static_passed else "failed"
        state.engine_validation_status = validation.engine.status
        if not validation.passed:
            state.stage = PipelineStage.FAILED
            state.error = "生成的语言包未通过构建验证，详见 build-validation.json"
            self._save_state(state)
            raise ValueError(state.error)
        state.renpy_language = manifest.renpy_language
        state.output_dir = manifest.output_dir
        self._apply_package_choice(
            manifest,
            state,
            generate_rpa=generate_rpa,
            compiled_project=(
                validation.staged_project
                if validation.engine.status == "passed"
                else None
            ),
        )
        if install:
            installed = TranslationInstaller().install(
                manifest,
                index.project.game_dir,
                overwrite_existing=overwrite_existing,
            )
            atomic_write_json(self.workspace / "install.json", installed.to_dict())
            state.installed_dir = installed.destination
        state.stage = PipelineStage.COMPLETE
        self._save_state(state)
        return manifest

    def _apply_package_choice(
        self,
        manifest: BuildManifest,
        state: PipelineState,
        *,
        generate_rpa: bool,
        compiled_project: str | Path | None = None,
    ) -> None:
        state.generate_rpa = bool(generate_rpa)
        if generate_rpa:
            package = self._package(manifest, compiled_project=compiled_project)
            state.package_path = package.archive_path
            state.package_sha256 = package.archive_sha256
            return
        manifest.archive_path = ""
        manifest.archive_sha256 = ""
        state.package_path = ""
        state.package_sha256 = ""
        atomic_write_json(self.output_dir / "build.json", manifest.to_dict())
        atomic_write_json(
            self.package_path,
            {
                "schema_version": 1,
                "generated": False,
                "format": "",
                "archive_path": "",
                "archive_size": 0,
                "archive_sha256": "",
                "members": [],
                "runtime_ready": False,
                "source_members": 0,
                "compiled_members": 0,
            },
        )

    def _package(
        self,
        manifest: BuildManifest,
        *,
        compiled_project: str | Path | None = None,
    ) -> PackageManifest:
        package = TranslationPackager().package(
            manifest,
            self.packages_dir,
            compiled_project=compiled_project,
        )
        manifest.archive_path = package.archive_path
        manifest.archive_sha256 = package.archive_sha256
        atomic_write_json(self.output_dir / "build.json", manifest.to_dict())
        package_payload = package.to_dict()
        package_payload["generated"] = True
        atomic_write_json(self.package_path, package_payload)
        return package

    def _validate_build(
        self,
        manifest: BuildManifest,
        index: ProjectIndex,
        *,
        sdk_path: str | Path | None,
        require_engine: bool,
    ):
        report = RenpyBuildValidator().validate(
            manifest,
            self.validation_dir,
            sdk_path=sdk_path,
            project_root=index.project.project_root,
            require_engine=require_engine,
        )
        atomic_write_json(self.build_validation_path, report.to_dict())
        return report

    def _collect_translations(self, scene_ids: list[str]) -> dict[str, str]:
        translations: dict[str, str] = {}
        for scene_id in scene_ids:
            path = self.translations_dir / f"{scene_id}.json"
            if not path.is_file():
                raise ValueError(f"缺少场景翻译产物：{scene_id}")
            payload = read_json(path)
            for text_id, translated in payload.get("translations", {}).items():
                if text_id in translations and translations[text_id] != translated:
                    raise ValueError(f"文本 ID 存在冲突译文：{text_id}")
                translations[str(text_id)] = str(translated)
        return translations

    def _reconcile_completed_scenes(
        self,
        index: ProjectIndex,
        candidates,
        state: PipelineState,
        validator: TranslationValidator,
    ) -> None:
        """Trust a checkpoint only when its artifact still exists and validates."""
        candidate_ids = {scene.id for scene in candidates}
        valid: list[str] = []
        invalid: list[str] = []
        for scene_id in dict.fromkeys(state.completed_scene_ids):
            if scene_id not in candidate_ids:
                invalid.append(scene_id)
                continue
            path = self.translations_dir / f"{scene_id}.json"
            try:
                payload = read_json(path)
                translations = payload.get("translations", {})
                if not isinstance(translations, dict):
                    raise ValueError("translations must be an object")
                report = validator.validate_scene(index, scene_id, translations)
                if not report.passed:
                    raise ValueError("saved translation no longer passes structural validation")
            except (OSError, ValueError, TypeError, KeyError) as exc:
                invalid.append(scene_id)
                self.logger.event(
                    "WARNING",
                    "checkpoint_rejected",
                    str(exc),
                    scene_id=scene_id,
                    artifact=str(path),
                )
                continue
            valid.append(scene_id)
        state.completed_scene_ids = valid
        state.failed_scene_ids = [
            scene_id for scene_id in dict.fromkeys(state.failed_scene_ids) if scene_id in candidate_ids
        ]
        for scene_id in invalid:
            if scene_id in candidate_ids and scene_id not in state.failed_scene_ids:
                state.failed_scene_ids.append(scene_id)

    def _persist_translations(
        self,
        index: ProjectIndex,
        translations: dict[str, str],
    ) -> None:
        for scene in index.scenes:
            if not scene.text_units:
                continue
            scene_translations = {
                unit.id: translations[unit.id]
                for unit in scene.text_units
                if unit.id in translations
            }
            atomic_write_json(self.translations_dir / f"{scene.id}.json", {
                "scene_id": scene.id,
                "translations": scene_translations,
            })

    def _load_string_memory(
        self,
        index: ProjectIndex,
        completed_scene_ids: list[str],
    ) -> dict[str, str]:
        completed = set(completed_scene_ids)
        memory: dict[str, str] = {}
        for scene in index.scenes:
            if scene.id not in completed:
                continue
            path = self.translations_dir / f"{scene.id}.json"
            if not path.is_file():
                continue
            translations = read_json(path).get("translations", {})
            for unit in scene.text_units:
                if unit.channel not in {TextChannel.MENU, TextChannel.UI, TextChannel.TRANSLATE_STRING}:
                    continue
                if unit.id in translations:
                    memory.setdefault(unit.source, str(translations[unit.id]))
        return memory

    @staticmethod
    def _apply_string_memory(
        units: list[TextUnit],
        translations: dict[str, str],
        memory: dict[str, str],
    ) -> None:
        for unit in units:
            if unit.channel not in {TextChannel.MENU, TextChannel.UI, TextChannel.TRANSLATE_STRING}:
                continue
            if unit.id not in translations:
                continue
            if unit.source in memory:
                translations[unit.id] = memory[unit.source]
            else:
                memory[unit.source] = translations[unit.id]

    def _new_state(
        self,
        target: str | Path,
        source_language: str,
        target_language: str,
        *,
        project_fingerprint: str,
    ) -> PipelineState:
        state = PipelineState(
            schema_version=4,
            project_target=str(Path(target).expanduser().resolve()),
            source_language=source_language,
            target_language=target_language,
            stage=PipelineStage.CREATED,
            completed_scene_ids=[],
            failed_scene_ids=[],
            updated_at=self._now(),
            renpy_language=normalize_renpy_language(target_language)
            if target_language.casefold() != "und"
            else "",
            installed_dir="",
            generate_rpa=True,
            package_path="",
            package_sha256="",
            build_validation_status="",
            engine_validation_status="",
            project_fingerprint=project_fingerprint,
            analysis_schema_version=ANALYSIS_SCHEMA_VERSION,
            knowledge_model_calls=0,
            knowledge_cache_hits=0,
            knowledge_warnings=0,
            refinement_model_calls=0,
            refinement_cache_hits=0,
            refinement_changes=0,
            total_model_calls=0,
            total_prompt_tokens=0,
            total_completion_tokens=0,
            model_requests_attempted=0,
            run_status="idle",
            started_at=self._now(),
            current_operation="Preparing the translation workspace",
            log_path=str(self.logger.text_path),
        )
        self._save_state(state)
        return state

    def _load_state(self) -> PipelineState:
        payload = read_json(self.state_path)
        payload["stage"] = PipelineStage(payload["stage"])
        payload.setdefault("source_language", "auto")
        payload.setdefault("renpy_language", "")
        payload.setdefault("output_dir", "")
        payload.setdefault("installed_dir", "")
        payload.setdefault("generate_rpa", True)
        payload.setdefault("package_path", "")
        payload.setdefault("package_sha256", "")
        payload.setdefault("build_validation_status", "")
        payload.setdefault("engine_validation_status", "")
        payload.setdefault("project_fingerprint", "")
        payload.setdefault("analysis_schema_version", 0)
        payload.setdefault("knowledge_model_calls", 0)
        payload.setdefault("knowledge_cache_hits", 0)
        payload.setdefault("knowledge_warnings", 0)
        payload.setdefault("refinement_model_calls", 0)
        payload.setdefault("refinement_cache_hits", 0)
        payload.setdefault("refinement_changes", 0)
        payload.setdefault("total_model_calls", 0)
        payload.setdefault("total_prompt_tokens", 0)
        payload.setdefault("total_completion_tokens", 0)
        payload.setdefault("model_requests_attempted", 0)
        payload.setdefault("run_status", "idle")
        payload.setdefault("started_at", "")
        payload.setdefault("current_operation", "")
        payload.setdefault("current_scene_id", "")
        payload.setdefault("current_scene_label", "")
        payload.setdefault("total_scenes", 0)
        payload.setdefault("completed_scenes", len(payload.get("completed_scene_ids", [])))
        payload.setdefault("total_text_units", 0)
        payload.setdefault("completed_text_units", 0)
        payload.setdefault("progress_percent", 0.0)
        payload.setdefault("eta_seconds", -1)
        payload.setdefault("translation_seconds", 0.0)
        payload.setdefault("scene_attempts", 0)
        payload.setdefault("resumed_count", 0)
        payload.setdefault("pause_reason", "")
        payload.setdefault("log_path", str(self.logger.text_path))
        payload.setdefault("phase_completed", 0)
        payload.setdefault("phase_total", 0)
        payload.setdefault("estimated_input_tokens_low", 0)
        payload.setdefault("estimated_input_tokens_high", 0)
        payload.setdefault("estimated_output_tokens_low", 0)
        payload.setdefault("estimated_output_tokens_high", 0)
        payload.setdefault("estimated_total_tokens_low", 0)
        payload.setdefault("estimated_total_tokens_high", 0)
        payload.setdefault("source_token_equivalent", 0)
        payload.setdefault("token_estimate_confidence", "")
        payload.setdefault("usage_reporting_status", "pending")
        payload.setdefault("knowledge_prompt_tokens", 0)
        payload.setdefault("knowledge_completion_tokens", 0)
        payload.setdefault("refinement_prompt_tokens", 0)
        payload.setdefault("refinement_completion_tokens", 0)
        return PipelineState(**payload)

    def _save_state(self, state: PipelineState) -> None:
        state.updated_at = self._now()
        state.completed_scenes = len(state.completed_scene_ids)
        state.progress_percent = self._progress_percent(state)
        state.log_path = str(self.logger.text_path)
        atomic_write_json(self.state_path, state.to_dict())
        self._save_usage(state)
        stage = str(state.stage)
        if stage != self._last_logged_stage:
            self._last_logged_stage = stage
            self.logger.event(
                "INFO",
                "stage_changed",
                state.current_operation or stage,
                stage=stage,
                progress_percent=state.progress_percent,
                completed_scenes=state.completed_scenes,
                total_scenes=state.total_scenes,
            )
        if self._progress_callback is not None:
            try:
                self._progress_callback(state)
            except Exception as exc:
                self.logger.event("WARNING", "progress_callback_failed", str(exc))
                self._progress_callback = None

    def _phase_progress(
        self,
        state: PipelineState,
        completed: int,
        total: int,
        message: str,
        gateway,
        usage_base: tuple[int, int, int, int],
    ) -> None:
        state.phase_completed = max(0, int(completed))
        state.phase_total = max(1, int(total), state.phase_completed)
        state.current_operation = message
        self._sync_gateway_usage(state, gateway, usage_base)
        self._save_state(state)

    @staticmethod
    def _progress_percent(state: PipelineState) -> float:
        fixed = {
            PipelineStage.CREATED: 0.0,
            PipelineStage.DISCOVERED: 3.0,
            PipelineStage.ACQUIRED: 8.0,
            PipelineStage.DECOMPILED: 13.0,
            PipelineStage.INDEXED: 18.0,
            PipelineStage.KNOWLEDGE_READY: 23.0,
            PipelineStage.SYNTHESIZING: 26.0,
            PipelineStage.NARRATIVE_READY: 32.0,
            PipelineStage.VALIDATED: 85.0,
            PipelineStage.REFINING: 88.0,
            PipelineStage.REFINED: 93.0,
            PipelineStage.BUILDING: 95.0,
            PipelineStage.VALIDATING_BUILD: 98.0,
            PipelineStage.COMPLETE: 100.0,
        }
        if state.stage == PipelineStage.TRANSLATING:
            ratio = state.completed_scenes / max(1, state.total_scenes)
            return round(32.0 + 53.0 * min(1.0, ratio), 1)
        if state.stage == PipelineStage.SYNTHESIZING and state.phase_total > 0:
            ratio = state.phase_completed / state.phase_total
            return round(26.0 + 6.0 * min(1.0, ratio), 1)
        if state.stage == PipelineStage.REFINING and state.phase_total > 0:
            ratio = state.phase_completed / state.phase_total
            return round(88.0 + 5.0 * min(1.0, ratio), 1)
        if state.stage in {PipelineStage.PAUSED, PipelineStage.FAILED}:
            return max(0.0, min(100.0, float(state.progress_percent)))
        return fixed.get(state.stage, 0.0)

    def _pause(self, state: PipelineState, reason: str) -> PipelineState:
        state.stage = PipelineStage.PAUSED
        state.run_status = "paused"
        state.pause_reason = reason
        state.current_operation = "Paused safely after saving the latest checkpoint"
        self.logger.event(
            "INFO",
            "run_paused",
            reason,
            completed_scenes=len(state.completed_scene_ids),
            total_scenes=state.total_scenes,
        )
        self._save_state(state)
        return state

    def _raise_if_cancelled(
        self,
        state: PipelineState,
        token: CancellationToken | None,
        reason: str,
    ) -> None:
        if self._cancelled(token):
            self._pause(state, reason)
            raise CancellationRequested(reason)

    @staticmethod
    def _cancelled(token: CancellationToken | None) -> bool:
        return bool(token and token.cancelled)

    @staticmethod
    def _sync_gateway_usage(state: PipelineState, gateway, base: tuple[int, int, int, int]) -> None:
        state.total_model_calls = base[0] + max(0, int(getattr(gateway, "model_calls", 0)))
        state.total_prompt_tokens = base[1] + max(0, int(getattr(gateway, "prompt_tokens", 0)))
        state.total_completion_tokens = base[2] + max(
            0, int(getattr(gateway, "completion_tokens", 0))
        )
        state.model_requests_attempted = base[3] + max(
            0, int(getattr(gateway, "requests_attempted", 0))
        )
        if state.total_prompt_tokens + state.total_completion_tokens > 0:
            state.usage_reporting_status = "reported"
        elif state.total_model_calls > 0:
            state.usage_reporting_status = "unavailable"
        else:
            state.usage_reporting_status = "pending"

    def _save_usage(self, state: PipelineState) -> None:
        known_prompt = state.knowledge_prompt_tokens + state.refinement_prompt_tokens
        known_completion = state.knowledge_completion_tokens + state.refinement_completion_tokens
        atomic_write_json(self.usage_path, {
            "schema_version": 1,
            "updated_at": state.updated_at,
            "reporting_status": state.usage_reporting_status,
            "estimate": {
                "confidence": state.token_estimate_confidence,
                "source_token_equivalent": state.source_token_equivalent,
                "input_low": state.estimated_input_tokens_low,
                "input_high": state.estimated_input_tokens_high,
                "output_low": state.estimated_output_tokens_low,
                "output_high": state.estimated_output_tokens_high,
                "total_low": state.estimated_total_tokens_low,
                "total_high": state.estimated_total_tokens_high,
                "excludes_provider_retries": True,
            },
            "actual": {
                "requests_attempted": state.model_requests_attempted,
                "successful_model_calls": state.total_model_calls,
                "input_tokens": state.total_prompt_tokens,
                "output_tokens": state.total_completion_tokens,
                "total_tokens": state.total_prompt_tokens + state.total_completion_tokens,
            },
            "breakdown": {
                "knowledge": {
                    "model_calls": state.knowledge_model_calls,
                    "input_tokens": state.knowledge_prompt_tokens,
                    "output_tokens": state.knowledge_completion_tokens,
                },
                "scene_translation_and_repairs": {
                    "model_calls": max(
                        0,
                        state.total_model_calls
                        - state.knowledge_model_calls
                        - state.refinement_model_calls,
                    ),
                    "input_tokens": max(0, state.total_prompt_tokens - known_prompt),
                    "output_tokens": max(0, state.total_completion_tokens - known_completion),
                },
                "refinement": {
                    "model_calls": state.refinement_model_calls,
                    "input_tokens": state.refinement_prompt_tokens,
                    "output_tokens": state.refinement_completion_tokens,
                },
            },
        })

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _requires_decompilation(source_roots: list[Path]) -> bool:
        suffixes = {".rpyc": ".rpy", ".rpymc": ".rpym"}
        for root in source_roots:
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                source_suffix = suffixes.get(path.suffix.casefold())
                if source_suffix and path.is_file() and not path.with_suffix(source_suffix).is_file():
                    return True
        return False

    @staticmethod
    def _project_fingerprint(project) -> str:
        digest = hashlib.sha256()
        game_dir = Path(project.game_dir)
        digest.update(str(game_dir).encode("utf-8"))
        for kind, paths in (
            ("source", project.source_scripts),
            ("compiled", project.compiled_scripts),
            ("archive", project.archives),
        ):
            for relative in paths:
                path = game_dir / relative
                digest.update(kind.encode("ascii"))
                digest.update(relative.encode("utf-8"))
                with path.open("rb") as reader:
                    for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                        digest.update(chunk)
        return digest.hexdigest()
