from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path

from .io import read_text_preserving
from .models import (
    ControlFlowEdge,
    EdgeKind,
    Scene,
    ScriptFile,
    SourceRef,
    TextChannel,
    TextUnit,
    stable_id,
)


LABEL_RE = re.compile(r"^\s*label\s+([A-Za-z_][\w.]*)\s*(?:\([^)]*\))?\s*:\s*(?:#.*)?$")
FLOW_RE = re.compile(r"^\s*(call|jump)\s+(?:expression\s+)?([A-Za-z_][\w.]*)")
TAG_RE = re.compile(r"\{/?[A-Za-z][^{}]*\}")
PLACEHOLDER_RE = re.compile(r"\[[^\[\]\r\n]+\]|%(?:\([^)]+\))?[#0 +\-]?[0-9]*(?:\.[0-9]+)?[a-zA-Z]")
UI_STRING_RE = re.compile(r"(?<![A-Za-z0-9_])_\(\s*(?P<literal>(?:[rubfRUBF]*)(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'))\s*\)")
DIALOGUE_PREFIX_RE = re.compile(
    r"^(?:say\s+)?[A-Za-z_]\w*(?:\s+(?:@|-?[A-Za-z_]\w*))*$"
)

NON_SPEAKER_PREFIXES = frozenset({
    "$", "add", "at", "camera", "default", "define", "elif", "else", "for",
    "frame", "hide", "if", "image", "init", "key", "layeredimage", "pause",
    "play", "python", "queue", "scene", "screen", "show", "stop", "style",
    "text", "transform", "use", "vbox", "voice", "while", "window", "with",
})


def _decode_literal(literal: str) -> str | None:
    prefix_match = re.match(r"^[rubfRUBF]*", literal)
    prefix = prefix_match.group(0) if prefix_match else ""
    if "f" in prefix.casefold():
        literal = literal[len(prefix):]
    try:
        value = ast.literal_eval(literal)
    except (SyntaxError, ValueError):
        return None
    return value if isinstance(value, str) else None


def _quoted_segments(statement: str) -> list[tuple[int, int, str, str]]:
    results: list[tuple[int, int, str, str]] = []
    index = 0
    while index < len(statement):
        if statement[index] not in {'"', "'"}:
            index += 1
            continue
        quote = statement[index]
        start = index
        index += 1
        escaped = False
        while index < len(statement):
            char = statement[index]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                literal = statement[start:index + 1]
                decoded = _decode_literal(literal)
                if decoded is not None:
                    results.append((start, index + 1, literal, decoded))
                index += 1
                break
            index += 1
    return results


class RenpyParser:
    def parse_file(
        self,
        path: Path,
        game_dir: Path,
        *,
        relative_prefix: str = "",
    ) -> tuple[ScriptFile, list[Scene]]:
        text, encoding, newline, has_bom = read_text_preserving(path)
        relative = path.relative_to(game_dir).as_posix()
        if relative_prefix:
            relative = f"{relative_prefix.strip('/')}/{relative}"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines = text.splitlines()
        scenes: list[Scene] = []
        current = self._new_scene(relative, "__preamble__", 1)
        scenes.append(current)

        for line_number, raw in enumerate(lines, start=1):
            label_match = LABEL_RE.match(raw)
            if label_match:
                current.end_line = max(current.start_line, line_number - 1)
                current = self._new_scene(relative, label_match.group(1), line_number)
                scenes.append(current)
                continue

            flow_match = FLOW_RE.match(raw)
            if flow_match:
                current.edges.append(ControlFlowEdge(
                    source_scene_id=current.id,
                    target_label=flow_match.group(2),
                    kind=EdgeKind(flow_match.group(1)),
                    location=SourceRef(relative, line_number),
                ))

            parsed_units = self._parse_text_units(raw, relative, line_number, current)
            if current.label == "__preamble__":
                # Label-free source is predominantly init, Python, screen, image,
                # ATL, and metadata declarations. Quoted values in those blocks
                # are not say/menu statements. Explicit _(...) UI strings remain
                # eligible because Ren'Py intentionally marks those for string
                # translation even outside a label.
                parsed_units = [
                    unit
                    for unit in parsed_units
                    if unit.channel in {TextChannel.UI, TextChannel.TRANSLATE_STRING}
                ]
            for unit in parsed_units:
                current.text_units.append(unit)

        current.end_line = max(current.start_line, len(lines))
        scenes = [scene for scene in scenes if scene.label != "__preamble__" or scene.text_units or scene.edges]
        self._add_fallthrough_edges(scenes)
        script = ScriptFile(
            relative_path=relative,
            sha256=digest,
            encoding=encoding,
            newline=newline,
            has_bom=has_bom,
            scene_ids=[scene.id for scene in scenes],
        )
        return script, scenes

    @staticmethod
    def _new_scene(relative: str, label: str, line: int) -> Scene:
        return Scene(
            id=stable_id(relative, label, prefix="scene_"),
            label=label,
            relative_path=relative,
            start_line=line,
            end_line=line,
        )

    def _parse_text_units(
        self,
        raw: str,
        relative: str,
        line_number: int,
        scene: Scene,
    ) -> list[TextUnit]:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            return []

        segments = _quoted_segments(stripped)
        if not segments:
            return self._parse_ui_strings(stripped, relative, line_number, scene)

        literal_ordinal = 0
        start, end, _literal, source = segments[0]
        before = stripped[:start].strip()
        after = stripped[end:].strip()
        ui_units = self._parse_ui_strings(stripped, relative, line_number, scene)
        if ui_units and (
            before.endswith("_(")
            or (
                before
                and before.split(maxsplit=1)[0].casefold() in NON_SPEAKER_PREFIXES
            )
        ):
            return ui_units
        channel: TextChannel
        speaker = ""
        attributes: tuple[str, ...] = ()
        condition = ""

        between_first_literals = (
            stripped[segments[0][1]:segments[1][0]].strip()
            if len(segments) >= 2
            else ""
        )
        if before == "" and len(segments) >= 2 and not between_first_literals:
            # Ren'Py accepts a string expression as the speaker, for example
            # ``"Eileen" "Hello."``. The second literal is the dialogue.
            speaker = source
            literal_ordinal = 1
            start, end, _literal, source = segments[1]
            after = stripped[end:].strip()
            channel = TextChannel.DIALOGUE
        elif after.startswith(("if ", "(")) or after.endswith(":"):
            # Menu entries may use standard ``if condition:`` syntax or
            # project-defined argument syntax such as ``(condition, icon):``.
            # They must be emitted through Ren'Py's old/new string table, not
            # copied as statements into a dialogue translation block.
            channel = TextChannel.MENU
            condition = after.removesuffix(":").strip()
            if condition.startswith("if "):
                condition = condition[3:].strip()
        elif before == "":
            channel = TextChannel.NARRATION
        else:
            # A Ren'Py say statement begins with a character identifier and
            # optional text attributes. Do not treat arbitrary Python/style
            # expressions that happen to contain a string as dialogue. This
            # conservative boundary is important because emitted translation
            # blocks may only contain translatable Ren'Py statements.
            if DIALOGUE_PREFIX_RE.fullmatch(before) is None:
                return self._parse_ui_strings(stripped, relative, line_number, scene)
            parts = before.split()
            if parts and parts[0] == "say" and len(parts) > 1:
                parts = parts[1:]
            if not parts or parts[0].casefold() in NON_SPEAKER_PREFIXES or before.endswith("="):
                return self._parse_ui_strings(stripped, relative, line_number, scene)
            channel = TextChannel.DIALOGUE
            speaker = parts[0]
            attributes = tuple(parts[1:])

        return [self._unit(
            source=source,
            channel=channel,
            speaker=speaker,
            attributes=attributes,
            condition=condition,
            raw=raw,
            relative=relative,
            line_number=line_number,
            scene=scene,
            ordinal=0,
            literal_ordinal=literal_ordinal,
        )]

    def _parse_ui_strings(
        self,
        stripped: str,
        relative: str,
        line_number: int,
        scene: Scene,
    ) -> list[TextUnit]:
        results = []
        segments = _quoted_segments(stripped)
        for ordinal, match in enumerate(UI_STRING_RE.finditer(stripped)):
            # A narration or dialogue literal may itself mention Ren'Py source,
            # such as ``"Use _('key') here."``. Do not mistake a call-shaped
            # substring inside a quoted literal for an executable UI string.
            if any(start <= match.start() < end for start, end, _literal, _source in segments):
                continue
            source = _decode_literal(match.group("literal"))
            if source is None:
                continue
            literal_ordinal = next(
                (index for index, segment in enumerate(segments) if segment[0] == match.start("literal")),
                0,
            )
            results.append(self._unit(
                source=source,
                channel=TextChannel.UI,
                speaker="",
                attributes=(),
                condition="",
                raw=stripped,
                relative=relative,
                line_number=line_number,
                scene=scene,
                ordinal=ordinal,
                literal_ordinal=literal_ordinal,
            ))
        return results

    @staticmethod
    def _unit(
        *,
        source: str,
        channel: TextChannel,
        speaker: str,
        attributes: tuple[str, ...],
        condition: str,
        raw: str,
        relative: str,
        line_number: int,
        scene: Scene,
        ordinal: int,
        literal_ordinal: int = 0,
    ) -> TextUnit:
        return TextUnit(
            id=stable_id(relative, line_number, ordinal, source, prefix="text_"),
            channel=channel,
            source=source,
            location=SourceRef(relative, line_number),
            scene_id=scene.id,
            speaker=speaker,
            attributes=attributes,
            condition=condition,
            raw_statement=raw,
            tags=tuple(TAG_RE.findall(source)),
            placeholders=tuple(PLACEHOLDER_RE.findall(source)),
            literal_ordinal=literal_ordinal,
        )

    @staticmethod
    def _add_fallthrough_edges(scenes: list[Scene]) -> None:
        for current, following in zip(scenes, scenes[1:]):
            if current.relative_path != following.relative_path:
                continue
            if current.edges and current.edges[-1].kind == EdgeKind.JUMP:
                continue
            current.edges.append(ControlFlowEdge(
                source_scene_id=current.id,
                target_label=following.label,
                kind=EdgeKind.FALLTHROUGH,
                location=SourceRef(current.relative_path, current.end_line),
            ))
