from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class ValueEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class TextChannel(ValueEnum):
    DIALOGUE = "dialogue"
    NARRATION = "narration"
    MENU = "menu"
    UI = "ui"
    TRANSLATE_STRING = "translate_string"


class EdgeKind(ValueEnum):
    CALL = "call"
    JUMP = "jump"
    FALLTHROUGH = "fallthrough"


def stable_id(*parts: object, prefix: str = "") -> str:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:16]
    return f"{prefix}{digest}"


@dataclass(slots=True, frozen=True)
class SourceRef:
    relative_path: str
    line: int
    column: int = 1


@dataclass(slots=True)
class TextUnit:
    id: str
    channel: TextChannel
    source: str
    location: SourceRef
    scene_id: str
    speaker: str = ""
    attributes: tuple[str, ...] = ()
    condition: str = ""
    raw_statement: str = ""
    tags: tuple[str, ...] = ()
    placeholders: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class ControlFlowEdge:
    source_scene_id: str
    target_label: str
    kind: EdgeKind
    location: SourceRef
    condition: str = ""


@dataclass(slots=True)
class Scene:
    id: str
    label: str
    relative_path: str
    start_line: int
    end_line: int
    text_units: list[TextUnit] = field(default_factory=list)
    edges: list[ControlFlowEdge] = field(default_factory=list)

    @property
    def speakers(self) -> tuple[str, ...]:
        return tuple(sorted({unit.speaker for unit in self.text_units if unit.speaker}))


@dataclass(slots=True)
class ScriptFile:
    relative_path: str
    sha256: str
    encoding: str
    newline: str
    has_bom: bool
    scene_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ProjectInfo:
    project_root: str
    game_dir: str
    name: str
    renpy_version: str = "unknown"
    source_scripts: list[str] = field(default_factory=list)
    compiled_scripts: list[str] = field(default_factory=list)
    archives: list[str] = field(default_factory=list)
    translation_languages: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ProjectIndex:
    schema_version: int
    project: ProjectInfo
    files: list[ScriptFile]
    scenes: list[Scene]

    @property
    def text_units(self) -> list[TextUnit]:
        return [unit for scene in self.scenes for unit in scene.text_units]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for scene in payload["scenes"]:
            for unit in scene["text_units"]:
                unit["channel"] = str(unit["channel"])
            for edge in scene["edges"]:
                edge["kind"] = str(edge["kind"])
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ProjectIndex":
        project = ProjectInfo(**payload["project"])
        files = [ScriptFile(**item) for item in payload.get("files", [])]
        scenes: list[Scene] = []
        for raw_scene in payload.get("scenes", []):
            units = []
            for raw_unit in raw_scene.get("text_units", []):
                item = dict(raw_unit)
                item["channel"] = TextChannel(item["channel"])
                item["location"] = SourceRef(**item["location"])
                item["attributes"] = tuple(item.get("attributes", ()))
                item["tags"] = tuple(item.get("tags", ()))
                item["placeholders"] = tuple(item.get("placeholders", ()))
                units.append(TextUnit(**item))
            edges = []
            for raw_edge in raw_scene.get("edges", []):
                item = dict(raw_edge)
                item["kind"] = EdgeKind(item["kind"])
                item["location"] = SourceRef(**item["location"])
                edges.append(ControlFlowEdge(**item))
            scenes.append(Scene(
                id=raw_scene["id"],
                label=raw_scene["label"],
                relative_path=raw_scene["relative_path"],
                start_line=raw_scene["start_line"],
                end_line=raw_scene["end_line"],
                text_units=units,
                edges=edges,
            ))
        return cls(
            schema_version=int(payload.get("schema_version", 1)),
            project=project,
            files=files,
            scenes=scenes,
        )


def relative_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()
