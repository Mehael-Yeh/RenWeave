from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

from .io import atomic_write_json, atomic_write_text
from .models import ProjectIndex, TextChannel, TextUnit
from .parser import _quoted_segments


LANGUAGE_SEPARATOR_RE = re.compile(r"[^\w]+", re.UNICODE)
EXPLICIT_ID_RE = re.compile(r"(?:^|\s)id\s+([A-Za-z_]\w*)(?:\s|$)")


def normalize_renpy_language(value: str) -> str:
    """Turn a user-facing language code or name into a Ren'Py identifier."""
    cleaned = LANGUAGE_SEPARATOR_RE.sub("_", value.strip().casefold()).strip("_")
    if not cleaned:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
        cleaned = f"lang_{digest}"
    if cleaned[0].isdigit():
        cleaned = f"lang_{cleaned}"
    if not cleaned.isidentifier():
        raise ValueError(f"无法生成合法的 Ren'Py 语言标识：{value!r}")
    return cleaned


def quote_renpy_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\a", "\\a")
        .replace("\b", "\\b")
        .replace("\f", "\\f")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
        .replace("\v", "\\v")
    )
    return f'"{escaped}"'


def encode_say_string(value: str) -> str:
    # Mirrors Ren'Py's translation identifier encoding, including preservation
    # of consecutive spaces inside say strings.
    escaped = value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
    escaped = re.sub(r"(?<= ) ", r"\\ ", escaped)
    return f'"{escaped}"'


@dataclass(slots=True, frozen=True)
class EmittedFile:
    relative_path: str
    sha256: str
    dialogue_blocks: int
    string_entries: int
    preserved: bool = False


@dataclass(slots=True)
class BuildManifest:
    schema_version: int
    requested_language: str
    renpy_language: str
    output_dir: str
    translated_units: int
    files: list[EmittedFile]
    archive_path: str = ""
    archive_sha256: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class TranslationConflict(ValueError):
    pass


class RenpyTranslationEmitter:
    """Emits validated translations as standard Ren'Py translation scripts."""

    @classmethod
    def dialogue_identifiers(cls, index: ProjectIndex) -> dict[str, str]:
        """Return the standard Ren'Py translation id for every say unit.

        The same mapping is also used when importing an existing ``game/tl``
        language, so an already translated block can be matched without
        relying on source line numbers.
        """
        units = index.text_units
        explicit_ids = {
            match.group(1)
            for unit in units
            if unit.channel in {TextChannel.DIALOGUE, TextChannel.NARRATION}
            for match in [EXPLICIT_ID_RE.search(cls._suffix_after_literal(unit))]
            if match is not None
        }
        scene_labels: dict[str, str] = {}
        primary_label = ""
        previous_file = ""
        for scene in index.scenes:
            if scene.relative_path != previous_file:
                primary_label = ""
                previous_file = scene.relative_path
            if scene.label.startswith("_") and primary_label:
                scene_labels[scene.id] = primary_label
            else:
                primary_label = "" if scene.label == "__preamble__" else scene.label
                scene_labels[scene.id] = primary_label

        emitter = cls()
        identifiers: set[str] = set()
        result: dict[str, str] = {}
        for unit in units:
            if unit.channel not in {TextChannel.DIALOGUE, TextChannel.NARRATION}:
                continue
            result[unit.id] = emitter._translation_identifier(
                unit,
                scene_labels.get(unit.scene_id, ""),
                identifiers,
                explicit_ids,
            )
        return result

    def emit(
        self,
        index: ProjectIndex,
        translations: dict[str, str],
        requested_language: str,
        output_root: str | Path,
        *,
        existing_language_dir: str | Path | None = None,
        reused_unit_ids: set[str] | None = None,
    ) -> BuildManifest:
        language = normalize_renpy_language(requested_language)
        root = Path(output_root).expanduser().resolve()
        language_dir = root / "game" / "tl" / language
        language_dir.mkdir(parents=True, exist_ok=True)
        reused = set(reused_unit_ids or ())
        preserved: list[EmittedFile] = []
        if existing_language_dir:
            source_language_dir = Path(existing_language_dir).expanduser().resolve()
            if source_language_dir.is_dir() and source_language_dir != language_dir:
                for source in sorted(
                    source_language_dir.rglob("*.rpy"),
                    key=lambda item: item.as_posix().casefold(),
                ):
                    if source.is_symlink():
                        raise ValueError(f"现有语言包包含符号链接，拒绝复制：{source}")
                    relative = source.relative_to(source_language_dir)
                    destination = language_dir / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
                    preserved.append(self._file_record(root, destination, 0, 0, preserved=True))
        dialogue_by_file: dict[str, list[tuple[TextUnit, str]]] = {}
        string_rows: dict[str, tuple[str, TextUnit]] = {}

        units = index.text_units
        missing = [unit.id for unit in units if unit.id not in translations]
        if missing:
            raise ValueError(f"构建缺少 {len(missing)} 条已验证译文，首项：{missing[0]}")

        for unit in units:
            if unit.id in reused:
                continue
            translated = translations[unit.id]
            if unit.channel in {TextChannel.DIALOGUE, TextChannel.NARRATION}:
                dialogue_by_file.setdefault(unit.location.relative_path, []).append((unit, translated))
                continue
            previous = string_rows.get(unit.source)
            if previous is not None and previous[0] != translated:
                raise TranslationConflict(
                    "Ren'Py 字符串翻译要求同一原文只有一个译文；"
                    f"{unit.source!r} 在 {previous[1].location.relative_path}:"
                    f"{previous[1].location.line} 与 {unit.location.relative_path}:"
                    f"{unit.location.line} 的译文冲突"
                )
            string_rows.setdefault(unit.source, (translated, unit))

        dialogue_identifiers = self.dialogue_identifiers(index)
        emitted: list[EmittedFile] = []

        for relative_path, rows in dialogue_by_file.items():
            blocks = []
            for unit, translated in rows:
                identifier = dialogue_identifiers[unit.id]
                blocks.append(self._dialogue_block(language, identifier, unit, translated))
            output_relative = self._output_relative_path(relative_path)
            destination = (
                language_dir / "_renweave_incremental" / "dialogue" / output_relative
                if existing_language_dir
                else language_dir / output_relative
            )
            content = self._header(index, requested_language, language) + "".join(blocks)
            atomic_write_text(destination, content)
            emitted.append(self._file_record(root, destination, len(rows), 0))

        if string_rows:
            destination = language_dir / (
                "_renweave_incremental/strings.rpy" if existing_language_dir else "strings.rpy"
            )
            body = [self._header(index, requested_language, language), f"translate {language} strings:\n\n"]
            for source, (translated, unit) in string_rows.items():
                body.append(f"    # {unit.location.relative_path}:{unit.location.line}\n")
                body.append(f"    old {quote_renpy_string(source)}\n")
                body.append(f"    new {quote_renpy_string(translated)}\n\n")
            atomic_write_text(destination, "".join(body))
            emitted.append(self._file_record(root, destination, 0, len(string_rows)))

        emitted = [*preserved, *emitted]
        expected_files = {(root / item.relative_path).resolve() for item in emitted}
        for stale in language_dir.rglob("*.rpy"):
            if stale.resolve() in expected_files:
                continue
            if stale.read_bytes().startswith(b"# Generated by RenWeave."):
                stale.unlink()

        manifest = BuildManifest(
            schema_version=2,
            requested_language=requested_language,
            renpy_language=language,
            output_dir=str(language_dir),
            translated_units=len(units),
            files=emitted,
        )
        atomic_write_json(root / "build.json", manifest.to_dict())
        return manifest

    @staticmethod
    def _header(index: ProjectIndex, requested_language: str, language: str) -> str:
        project_name = RenpyTranslationEmitter._comment_text(index.project.name)
        requested = RenpyTranslationEmitter._comment_text(requested_language)
        return (
            "# Generated by RenWeave. Do not edit source game scripts.\n"
            f"# Project: {project_name}\n"
            f"# Requested language: {requested}; Ren'Py language: {language}\n\n"
        )

    @staticmethod
    def _output_relative_path(relative_path: str) -> Path:
        candidate = Path(relative_path.replace("\\", "/"))
        parts = candidate.parts
        if candidate.is_absolute() or ".." in parts:
            raise ValueError(f"不安全的源脚本路径：{relative_path}")
        if len(parts) >= 3 and parts[0] == "_archives" and parts[1].isdigit():
            parts = ("_archives", parts[1], *parts[2:])
        result = Path(*parts)
        return result.with_suffix(".rpy")

    @staticmethod
    def _comment_text(value: str) -> str:
        return value.replace("\r", " ").replace("\n", " ")

    @staticmethod
    def _file_record(
        root: Path,
        path: Path,
        dialogue: int,
        strings: int,
        *,
        preserved: bool = False,
    ) -> EmittedFile:
        payload = path.read_bytes()
        return EmittedFile(
            relative_path=path.relative_to(root).as_posix(),
            sha256=hashlib.sha256(payload).hexdigest(),
            dialogue_blocks=dialogue,
            string_entries=strings,
            preserved=preserved,
        )

    def _translation_identifier(
        self,
        unit: TextUnit,
        scene_label: str,
        identifiers: set[str],
        explicit_ids: set[str],
    ) -> str:
        suffix = self._suffix_after_literal(unit)
        explicit = EXPLICIT_ID_RE.search(suffix)
        canonical = self._canonical_say(unit)
        digest = hashlib.md5((canonical + "\r\n").encode("utf-8")).hexdigest()[:8]
        base = f"{scene_label.replace('.', '_')}_{digest}" if scene_label else digest
        if explicit is not None:
            identifier = explicit.group(1)
            if identifier in identifiers:
                raise TranslationConflict(f"显式 Ren'Py 翻译 ID 重复：{identifier}")
        else:
            identifier = base
            ordinal = 0
            while identifier in identifiers or identifier in explicit_ids:
                ordinal += 1
                identifier = f"{base}_{ordinal}"
        identifiers.add(identifier)
        return identifier

    @staticmethod
    def _suffix_after_literal(unit: TextUnit) -> str:
        segments = _quoted_segments(unit.raw_statement.strip())
        if unit.literal_ordinal >= len(segments):
            return ""
        return unit.raw_statement.strip()[segments[unit.literal_ordinal][1]:].strip()

    @staticmethod
    def _canonical_say(unit: TextUnit) -> str:
        statement = unit.raw_statement.strip()
        segments = _quoted_segments(statement)
        if unit.literal_ordinal >= len(segments):
            raise ValueError(
                f"无法定位 {unit.location.relative_path}:{unit.location.line} 的对话字符串"
            )
        pieces = []
        cursor = 0
        for start, end, _literal, decoded in segments:
            pieces.append(statement[cursor:start])
            pieces.append(encode_say_string(decoded))
            cursor = end
        pieces.append(statement[cursor:])
        canonical = re.sub(r"\s+", " ", "".join(pieces)).strip()
        if canonical.startswith("say "):
            canonical = canonical[4:].lstrip()
        return canonical

    @staticmethod
    def _dialogue_block(
        language: str,
        identifier: str,
        unit: TextUnit,
        translated: str,
    ) -> str:
        statement = unit.raw_statement.strip()
        segments = _quoted_segments(statement)
        if unit.literal_ordinal >= len(segments):
            raise ValueError(
                f"无法写回 {unit.location.relative_path}:{unit.location.line} 的对话字符串"
            )
        start, end, _literal, _decoded = segments[unit.literal_ordinal]
        translated_statement = statement[:start] + encode_say_string(translated) + statement[end:]
        source_comment = statement.replace("\n", " ")
        return (
            f"# {unit.location.relative_path}:{unit.location.line}\n"
            f"translate {language} {identifier}:\n\n"
            f"    # {source_comment}\n"
            f"    {translated_statement}\n\n"
        )
