from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
from pathlib import Path

from .acquisition import ArchiveAcquirer
from .context import ContextPlanner
from .decompiler import DecompilationError, DecompilationManifest, UnrpycDecompiler, UnrpycToolManager
from .discovery import ProjectDiscovery
from .emitter import RenpyTranslationEmitter, normalize_renpy_language
from .indexer import ProjectIndexer
from .installer import TranslationInstaller
from .io import atomic_write_json, read_json
from .knowledge import DeterministicKnowledgeBuilder, KnowledgeBase
from .models import ProjectIndex, TextChannel, TextUnit
from .narrative import NarrativeKnowledge, NarrativeKnowledgeSynthesizer
from .provider import ModelProfile, OpenAICompatibleGateway
from .refinement import GlobalTranslationRefiner
from .translation import SceneTranslator
from .validation import TranslationValidator


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
    COMPLETE = "complete"
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
    project_fingerprint: str = ""
    knowledge_model_calls: int = 0
    knowledge_cache_hits: int = 0
    knowledge_warnings: int = 0
    refinement_model_calls: int = 0
    refinement_cache_hits: int = 0
    refinement_changes: int = 0

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

    def analyze(
        self,
        target: str | Path,
        *,
        source_language: str = "auto",
        target_language: str = "und",
        unrpyc_path: str | Path | None = None,
        allow_tool_download: bool = True,
    ) -> tuple[ProjectIndex, KnowledgeBase]:
        resolved_target = str(Path(target).expanduser().resolve())
        project = ProjectDiscovery().discover(target)
        project_fingerprint = self._project_fingerprint(project)
        if self.state_path.is_file() and self.index_path.is_file() and self.knowledge_path.is_file():
            existing = self._load_state()
            if (
                existing.schema_version >= 2
                and existing.project_target == resolved_target
                and existing.project_fingerprint == project_fingerprint
                and existing.source_language == source_language
                and existing.target_language == target_language
                and existing.stage not in {PipelineStage.CREATED, PipelineStage.DISCOVERED, PipelineStage.FAILED}
            ):
                return (
                    ProjectIndex.from_dict(read_json(self.index_path)),
                    KnowledgeBase.from_dict(read_json(self.knowledge_path)),
                )
        state = self._new_state(
            target,
            source_language,
            target_language,
            project_fingerprint=project_fingerprint,
        )
        try:
            state.stage = PipelineStage.DISCOVERED
            self._save_state(state)
            acquisition = ArchiveAcquirer().acquire(project, self.acquired_dir, scripts_only=True)
            atomic_write_json(self.acquisition_path, acquisition.to_dict())
            state.stage = PipelineStage.ACQUIRED
            self._save_state(state)
            decompilation = self._decompile_roots(
                [Path(project.game_dir), *acquisition.source_roots],
                unrpyc_path=unrpyc_path,
                allow_tool_download=allow_tool_download,
            )
            state.stage = PipelineStage.DECOMPILED
            self._save_state(state)
            index = ProjectIndexer().build(
                target,
                additional_source_roots=[
                    *acquisition.source_roots,
                    *decompilation.output_roots,
                ],
            )
            atomic_write_json(self.index_path, index.to_dict())
            state.stage = PipelineStage.INDEXED
            self._save_state(state)
            knowledge = DeterministicKnowledgeBuilder().build(index)
            atomic_write_json(self.knowledge_path, knowledge.to_dict())
            state.stage = PipelineStage.KNOWLEDGE_READY
            self._save_state(state)
            return index, knowledge
        except BaseException as exc:
            state.stage = PipelineStage.FAILED
            state.error = str(exc)
            self._save_state(state)
            raise

    def decompile(
        self,
        target: str | Path,
        *,
        unrpyc_path: str | Path | None = None,
        allow_tool_download: bool = True,
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
        allow_tool_download: bool = True,
        synthesize_knowledge: bool = True,
        refine_translations: bool = True,
    ) -> PipelineState:
        if not target_language.strip() or target_language.casefold() == "und":
            raise ValueError("翻译任务必须指定明确的目标语言")
        index, knowledge = self.analyze(
            target,
            source_language=source_language,
            target_language=target_language,
            unrpyc_path=unrpyc_path,
            allow_tool_download=allow_tool_download,
        )
        state = self._load_state()
        gateway = gateway or OpenAICompatibleGateway(profile)
        narrative: NarrativeKnowledge | None = None
        text_scene_count = sum(1 for scene in index.scenes if scene.text_units)
        if synthesize_knowledge and text_scene_count >= 4:
            state.stage = PipelineStage.SYNTHESIZING
            self._save_state(state)
            chunk_characters = 24000
            if profile.context_window > 0:
                chunk_characters = max(4000, min(36000, profile.context_window // 2))
            narrative = NarrativeKnowledgeSynthesizer(
                gateway,
                self.knowledge_cache_dir,
                max_chunk_characters=chunk_characters,
            ).synthesize(
                index,
                knowledge,
                project_fingerprint=state.project_fingerprint,
                source_language=source_language,
            )
            atomic_write_json(self.narrative_path, narrative.to_dict())
            state.knowledge_model_calls = narrative.usage.model_calls
            state.knowledge_cache_hits = narrative.usage.cache_hits
            state.knowledge_warnings = len(narrative.warnings)
            state.stage = PipelineStage.NARRATIVE_READY
            self._save_state(state)
        state.stage = PipelineStage.TRANSLATING
        self._save_state(state)
        translator = SceneTranslator(gateway)
        planner = ContextPlanner()
        validator = TranslationValidator()
        string_memory = self._load_string_memory(index, state.completed_scene_ids)
        candidates = [scene for scene in index.scenes if scene.text_units]
        if limit > 0:
            candidates = candidates[:limit]
        self.translations_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        for scene in candidates:
            if scene.id in state.completed_scene_ids:
                continue
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
                    if scene.id in state.failed_scene_ids:
                        state.failed_scene_ids.remove(scene.id)
                    state.completed_scene_ids.append(scene.id)
                else:
                    if scene.id in state.completed_scene_ids:
                        state.completed_scene_ids.remove(scene.id)
                    if scene.id not in state.failed_scene_ids:
                        state.failed_scene_ids.append(scene.id)
            except BaseException:
                if scene.id in state.completed_scene_ids:
                    state.completed_scene_ids.remove(scene.id)
                if scene.id not in state.failed_scene_ids:
                    state.failed_scene_ids.append(scene.id)
            self._save_state(state)

        expected_scene_ids = {scene.id for scene in index.scenes if scene.text_units}
        completed_scene_ids = set(state.completed_scene_ids)
        if expected_scene_ids <= completed_scene_ids and not state.failed_scene_ids:
            collected = self._collect_translations(state.completed_scene_ids)
            if refine_translations:
                state.stage = PipelineStage.REFINING
                self._save_state(state)
                batch_characters = 24000
                if profile.context_window > 0:
                    batch_characters = max(4000, min(36000, profile.context_window // 2))
                collected, refinement = GlobalTranslationRefiner(
                    gateway,
                    self.refinement_cache_dir,
                    max_batch_characters=batch_characters,
                ).refine(
                    index,
                    collected,
                    narrative,
                    source_language=source_language,
                    target_language=target_language,
                )
                self._persist_translations(index, collected)
                atomic_write_json(self.refinement_path, refinement.to_dict())
                state.refinement_model_calls = refinement.usage.model_calls
                state.refinement_cache_hits = refinement.usage.cache_hits
                state.refinement_changes = len(refinement.changes)
                state.stage = PipelineStage.REFINED
                self._save_state(state)
            state.stage = PipelineStage.BUILDING
            self._save_state(state)
            try:
                manifest = RenpyTranslationEmitter().emit(
                    index,
                    collected,
                    target_language,
                    self.output_dir,
                )
                state.renpy_language = manifest.renpy_language
                state.output_dir = manifest.output_dir
                if install:
                    installed = TranslationInstaller().install(
                        manifest,
                        index.project.game_dir,
                        overwrite_existing=overwrite_existing,
                    )
                    atomic_write_json(self.workspace / "install.json", installed.to_dict())
                    state.installed_dir = installed.destination
                state.stage = PipelineStage.COMPLETE
                state.error = ""
            except BaseException as exc:
                state.stage = PipelineStage.FAILED
                state.error = str(exc)
                self._save_state(state)
                raise
        else:
            state.stage = PipelineStage.VALIDATED
        self._save_state(state)
        return state

    def build(
        self,
        *,
        requested_language: str | None = None,
        install: bool = False,
        overwrite_existing: bool = False,
    ):
        """Build translation scripts from all validated scene artifacts."""
        state = self._load_state()
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
        state.renpy_language = manifest.renpy_language
        state.output_dir = manifest.output_dir
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
            schema_version=2,
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
            project_fingerprint=project_fingerprint,
            knowledge_model_calls=0,
            knowledge_cache_hits=0,
            knowledge_warnings=0,
            refinement_model_calls=0,
            refinement_cache_hits=0,
            refinement_changes=0,
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
        payload.setdefault("project_fingerprint", "")
        payload.setdefault("knowledge_model_calls", 0)
        payload.setdefault("knowledge_cache_hits", 0)
        payload.setdefault("knowledge_warnings", 0)
        payload.setdefault("refinement_model_calls", 0)
        payload.setdefault("refinement_cache_hits", 0)
        payload.setdefault("refinement_changes", 0)
        return PipelineState(**payload)

    def _save_state(self, state: PipelineState) -> None:
        state.updated_at = self._now()
        atomic_write_json(self.state_path, state.to_dict())

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
                stat = path.stat()
                digest.update(kind.encode("ascii"))
                digest.update(relative.encode("utf-8"))
                digest.update(str(stat.st_size).encode("ascii"))
                digest.update(str(stat.st_mtime_ns).encode("ascii"))
        return digest.hexdigest()
