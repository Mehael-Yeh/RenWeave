from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from .acquisition import ArchiveAcquirer
from .context import ContextPlanner
from .discovery import ProjectDiscovery
from .indexer import ProjectIndexer
from .io import atomic_write_json, read_json
from .knowledge import DeterministicKnowledgeBuilder, KnowledgeBase
from .models import ProjectIndex
from .provider import ModelProfile, OpenAICompatibleGateway
from .translation import SceneTranslator
from .validation import TranslationValidator


class PipelineStage(str, Enum):
    def __str__(self) -> str:
        return self.value

    CREATED = "created"
    DISCOVERED = "discovered"
    ACQUIRED = "acquired"
    INDEXED = "indexed"
    KNOWLEDGE_READY = "knowledge_ready"
    TRANSLATING = "translating"
    VALIDATED = "validated"
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
        self.acquisition_path = self.workspace / "acquisition.json"
        self.acquired_dir = self.workspace / "acquired"
        self.translations_dir = self.workspace / "translations"
        self.reports_dir = self.workspace / "reports"

    def analyze(
        self,
        target: str | Path,
        *,
        source_language: str = "auto",
        target_language: str = "und",
    ) -> tuple[ProjectIndex, KnowledgeBase]:
        resolved_target = str(Path(target).expanduser().resolve())
        if self.state_path.is_file() and self.index_path.is_file() and self.knowledge_path.is_file():
            existing = self._load_state()
            if (
                existing.project_target == resolved_target
                and existing.source_language == source_language
                and existing.target_language == target_language
                and existing.stage not in {PipelineStage.CREATED, PipelineStage.DISCOVERED, PipelineStage.FAILED}
            ):
                return (
                    ProjectIndex.from_dict(read_json(self.index_path)),
                    KnowledgeBase.from_dict(read_json(self.knowledge_path)),
                )
        state = self._new_state(target, source_language, target_language)
        try:
            state.stage = PipelineStage.DISCOVERED
            self._save_state(state)
            project = ProjectDiscovery().discover(target)
            acquisition = ArchiveAcquirer().acquire(project, self.acquired_dir, scripts_only=True)
            atomic_write_json(self.acquisition_path, acquisition.to_dict())
            state.stage = PipelineStage.ACQUIRED
            self._save_state(state)
            index = ProjectIndexer().build(
                target,
                additional_source_roots=acquisition.source_roots,
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

    def translate(
        self,
        target: str | Path,
        source_language: str,
        target_language: str,
        profile: ModelProfile,
        *,
        limit: int = 0,
    ) -> PipelineState:
        if not target_language.strip() or target_language.casefold() == "und":
            raise ValueError("翻译任务必须指定明确的目标语言")
        index, knowledge = self.analyze(
            target,
            source_language=source_language,
            target_language=target_language,
        )
        state = self._load_state()
        state.stage = PipelineStage.TRANSLATING
        self._save_state(state)
        gateway = OpenAICompatibleGateway(profile)
        translator = SceneTranslator(gateway)
        planner = ContextPlanner()
        validator = TranslationValidator()
        candidates = [scene for scene in index.scenes if scene.text_units]
        if limit > 0:
            candidates = candidates[:limit]
        self.translations_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        for scene in candidates:
            if scene.id in state.completed_scene_ids:
                continue
            try:
                context = planner.build(index, knowledge, scene.id)
                result = translator.translate(
                    context,
                    target_language,
                    source_language=source_language,
                )
                report = validator.validate_scene(index, scene.id, result.translations)
                atomic_write_json(self.translations_dir / f"{scene.id}.json", {
                    "scene_id": scene.id,
                    "translations": result.translations,
                })
                atomic_write_json(self.reports_dir / f"{scene.id}.json", report.to_dict())
                if report.passed:
                    state.completed_scene_ids.append(scene.id)
                else:
                    state.failed_scene_ids.append(scene.id)
            except BaseException:
                if scene.id not in state.failed_scene_ids:
                    state.failed_scene_ids.append(scene.id)
            self._save_state(state)
        state.stage = PipelineStage.COMPLETE if not state.failed_scene_ids else PipelineStage.VALIDATED
        self._save_state(state)
        return state

    def _new_state(
        self,
        target: str | Path,
        source_language: str,
        target_language: str,
    ) -> PipelineState:
        state = PipelineState(
            schema_version=1,
            project_target=str(Path(target).expanduser().resolve()),
            source_language=source_language,
            target_language=target_language,
            stage=PipelineStage.CREATED,
            completed_scene_ids=[],
            failed_scene_ids=[],
            updated_at=self._now(),
        )
        self._save_state(state)
        return state

    def _load_state(self) -> PipelineState:
        payload = read_json(self.state_path)
        payload["stage"] = PipelineStage(payload["stage"])
        payload.setdefault("source_language", "auto")
        return PipelineState(**payload)

    def _save_state(self, state: PipelineState) -> None:
        state.updated_at = self._now()
        atomic_write_json(self.state_path, state.to_dict())

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
