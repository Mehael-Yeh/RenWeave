from __future__ import annotations

from dataclasses import asdict, dataclass

from .knowledge import KnowledgeBase
from .models import ProjectIndex, Scene
from .narrative import NarrativeKnowledge


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
    world_context: str
    style_guidance: list[str]
    storyline_context: list[dict]
    character_profiles: list[dict]
    term_hints: list[dict]

    def to_dict(self) -> dict:
        return asdict(self)


class ContextPlanner:
    def build(
        self,
        index: ProjectIndex,
        knowledge: KnowledgeBase,
        scene_id: str,
        narrative: NarrativeKnowledge | None = None,
    ) -> SceneContext:
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
        storylines = []
        profiles = []
        terms = []
        world_context = ""
        style_guidance: list[str] = []
        if narrative is not None:
            storylines = [
                {
                    "key": item.key,
                    "summary": item.summary[:1400],
                    "themes": item.themes[:12],
                }
                for item in narrative.storylines if scene.id in item.scene_ids
            ][:6]
            profile_map = {item.name.casefold(): item for item in narrative.characters}
            profiles = [
                {
                    "name": profile_map[name.casefold()].name,
                    "role": profile_map[name.casefold()].role,
                    "traits": profile_map[name.casefold()].traits[:12],
                    "voice": profile_map[name.casefold()].voice[:12],
                    "relationships": profile_map[name.casefold()].relationships,
                    "evidence_scene_ids": profile_map[name.casefold()].scene_ids[:12],
                }
                for name in scene.speakers if name.casefold() in profile_map
            ][:12]
            source_text = "\n".join(unit.source for unit in scene.text_units).casefold()
            terms = [
                {
                    "source": item.source,
                    "meaning": item.meaning,
                    "guidance": item.guidance,
                    "evidence_scene_ids": item.scene_ids[:12],
                }
                for item in narrative.terms
                if item.source.casefold() in source_text
            ][:30]
            relevant_facts = [
                item.text for item in narrative.world_facts if scene.id in item.scene_ids
            ][:20]
            world_context = narrative.world_summary[:800]
            if relevant_facts:
                world_context += "\nRelevant facts: " + " | ".join(relevant_facts)
            style_guidance = narrative.style_guidance[:30]
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
            world_context=world_context,
            style_guidance=style_guidance,
            storyline_context=storylines,
            character_profiles=profiles,
            term_hints=terms,
        )

    @staticmethod
    def _summary(knowledge: KnowledgeBase, scene: Scene | None) -> str:
        return knowledge.scene_summaries.get(scene.id, "") if scene else ""
