from __future__ import annotations

from dataclasses import asdict, dataclass

from .knowledge import KnowledgeBase
from .models import ProjectIndex, Scene


@dataclass(slots=True)
class SceneContext:
    scene_id: str
    label: str
    relative_path: str
    speakers: tuple[str, ...]
    previous_summary: str
    next_summary: str
    related_character_evidence: list[dict]
    lines: list[dict]

    def to_dict(self) -> dict:
        return asdict(self)


class ContextPlanner:
    def build(self, index: ProjectIndex, knowledge: KnowledgeBase, scene_id: str) -> SceneContext:
        position = next((i for i, scene in enumerate(index.scenes) if scene.id == scene_id), None)
        if position is None:
            raise KeyError(f"未知场景：{scene_id}")
        scene = index.scenes[position]
        previous = index.scenes[position - 1] if position > 0 else None
        following = index.scenes[position + 1] if position + 1 < len(index.scenes) else None
        character_map = {character.name: character for character in knowledge.characters}
        evidence = [
            asdict(character_map[name])
            for name in scene.speakers
            if name in character_map
        ]
        return SceneContext(
            scene_id=scene.id,
            label=scene.label,
            relative_path=scene.relative_path,
            speakers=scene.speakers,
            previous_summary=self._summary(knowledge, previous),
            next_summary=self._summary(knowledge, following),
            related_character_evidence=evidence,
            lines=[{
                "id": unit.id,
                "speaker": unit.speaker,
                "channel": str(unit.channel),
                "source": unit.source,
                "condition": unit.condition,
                "tags": list(unit.tags),
                "placeholders": list(unit.placeholders),
            } for unit in scene.text_units],
        )

    @staticmethod
    def _summary(knowledge: KnowledgeBase, scene: Scene | None) -> str:
        return knowledge.scene_summaries.get(scene.id, "") if scene else ""
