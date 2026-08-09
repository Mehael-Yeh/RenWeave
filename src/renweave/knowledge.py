from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

from .models import ProjectIndex, TextChannel


@dataclass(slots=True)
class CharacterEvidence:
    name: str
    line_count: int
    scene_count: int
    first_scene_id: str
    sample_text_ids: list[str] = field(default_factory=list)
    co_occurs_with: list[str] = field(default_factory=list)


@dataclass(slots=True)
class StorylineEvidence:
    key: str
    scene_ids: list[str]


@dataclass(slots=True)
class KnowledgeBase:
    schema_version: int
    characters: list[CharacterEvidence]
    storylines: list[StorylineEvidence]
    recurring_candidates: list[dict[str, Any]]
    scene_summaries: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "KnowledgeBase":
        return cls(
            schema_version=int(payload.get("schema_version", 1)),
            characters=[CharacterEvidence(**item) for item in payload.get("characters", [])],
            storylines=[StorylineEvidence(**item) for item in payload.get("storylines", [])],
            recurring_candidates=list(payload.get("recurring_candidates", [])),
            scene_summaries=dict(payload.get("scene_summaries", {})),
        )


class DeterministicKnowledgeBuilder:
    """Builds the zero-token evidence layer used before any model analysis."""

    STORY_FILE_RE = re.compile(r"^(?P<key>[A-Za-z_]+?)(?:\d+)?$")
    TERM_RE = re.compile(r"\b(?:[A-Z][A-Za-z0-9'’-]{2,})(?:\s+[A-Z][A-Za-z0-9'’-]{2,}){0,3}\b")

    def build(self, index: ProjectIndex) -> KnowledgeBase:
        speaker_lines: Counter[str] = Counter()
        speaker_scenes: dict[str, set[str]] = defaultdict(set)
        first_scene: dict[str, str] = {}
        samples: dict[str, list[str]] = defaultdict(list)
        co_occurrence: dict[str, Counter[str]] = defaultdict(Counter)
        storylines: dict[str, list[str]] = defaultdict(list)
        terms: Counter[str] = Counter()
        summaries: dict[str, str] = {}

        for scene in index.scenes:
            speakers = list(scene.speakers)
            for speaker in speakers:
                speaker_scenes[speaker].add(scene.id)
                first_scene.setdefault(speaker, scene.id)
                for peer in speakers:
                    if peer != speaker:
                        co_occurrence[speaker][peer] += 1
            for unit in scene.text_units:
                if unit.speaker:
                    speaker_lines[unit.speaker] += 1
                    if len(samples[unit.speaker]) < 8:
                        samples[unit.speaker].append(unit.id)
                if unit.channel in {TextChannel.DIALOGUE, TextChannel.NARRATION, TextChannel.MENU}:
                    for term in self.TERM_RE.findall(unit.source):
                        terms[term] += 1

            stem = scene.relative_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            match = self.STORY_FILE_RE.match(stem)
            key = match.group("key").rstrip("_") if match else stem
            storylines[key or stem].append(scene.id)
            summaries[scene.id] = self._summary(scene)

        characters = [
            CharacterEvidence(
                name=name,
                line_count=speaker_lines[name],
                scene_count=len(speaker_scenes[name]),
                first_scene_id=first_scene[name],
                sample_text_ids=samples[name],
                co_occurs_with=[peer for peer, _ in co_occurrence[name].most_common(12)],
            )
            for name in sorted(speaker_lines, key=lambda value: (-speaker_lines[value], value.casefold()))
        ]
        recurring = [
            {"source": term, "occurrences": count}
            for term, count in terms.most_common(200)
            if count >= 2
        ]
        return KnowledgeBase(
            schema_version=1,
            characters=characters,
            storylines=[
                StorylineEvidence(key=key, scene_ids=scene_ids)
                for key, scene_ids in sorted(storylines.items())
            ],
            recurring_candidates=recurring,
            scene_summaries=summaries,
        )

    @staticmethod
    def _summary(scene) -> str:
        pieces = []
        for unit in scene.text_units[:8]:
            prefix = f"{unit.speaker}: " if unit.speaker else ""
            pieces.append(prefix + unit.source.replace("\n", " ")[:120])
        return " | ".join(pieces)[:900]
