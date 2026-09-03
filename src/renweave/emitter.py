from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

from .io import atomic_write_bytes, atomic_write_json, atomic_write_text, read_text_preserving
from .models import ProjectIndex, TextChannel, TextUnit
from .parser import _quoted_segments


LANGUAGE_SEPARATOR_RE = re.compile(r"[^\w]+", re.UNICODE)
EXPLICIT_ID_RE = re.compile(r"(?:^|\s)id\s+([A-Za-z_]\w*)(?:\s|$)")
TRANSLATE_HEADER_RE = re.compile(
    r"^\s*translate\s+([^\W\d]\w*)\s+(\w+)\s*:\s*$",
    re.UNICODE,
)
LOCATION_COMMENT_RE = re.compile(r"^\s*#\s+.+:\d+\s*$")


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
    def build_fingerprint(
        cls,
        index: ProjectIndex,
        translations: dict[str, str],
        requested_language: str,
        *,
        existing_language_dir: str | Path | None = None,
        reused_unit_ids: set[str] | None = None,
    ) -> str:
        """Identify one immutable RPY build from every byte-affecting input."""
        digest = hashlib.sha256()
        digest.update(b"renweave-rpy-build-v4-single-terminal-strings-block\0")
        digest.update(index.project.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(requested_language.encode("utf-8"))
        digest.update(b"\0")
        for unit in index.text_units:
            digest.update(unit.id.encode("ascii"))
            digest.update(b"\0")
            digest.update(translations[unit.id].encode("utf-8"))
            digest.update(b"\0")
        for text_id in sorted(reused_unit_ids or ()):
            digest.update(b"reused\0")
            digest.update(text_id.encode("ascii"))
            digest.update(b"\0")
        if existing_language_dir:
            source_root = Path(existing_language_dir).expanduser().resolve()
            if source_root.is_dir():
                for source in sorted(
                    source_root.rglob("*.rpy"),
                    key=lambda item: item.as_posix().casefold(),
                ):
                    digest.update(source.relative_to(source_root).as_posix().encode("utf-8"))
                    digest.update(b"\0")
                    digest.update(hashlib.sha256(source.read_bytes()).digest())
        return digest.hexdigest()[:16]

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
        copied_paths: set[Path] = set()
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
                    copied_paths.add(destination.resolve())
        dialogue_by_file: dict[str, list[tuple[TextUnit, str]]] = {}
        string_rows: dict[str, tuple[str, TextUnit]] = {}
        strings_by_file: dict[str, list[tuple[str, str, TextUnit]]] = {}

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

        for source, (translated, unit) in string_rows.items():
            string_target = unit.location.relative_path if existing_language_dir else "strings.rpy"
            strings_by_file.setdefault(string_target, []).append(
                (source, translated, unit)
            )

        dialogue_identifiers = self.dialogue_identifiers(index)
        dialogue_order_by_file: dict[str, list[str]] = {}
        for unit in units:
            if unit.channel not in {TextChannel.DIALOGUE, TextChannel.NARRATION}:
                continue
            output_key = self._output_relative_path(
                unit.location.relative_path
            ).as_posix()
            dialogue_order_by_file.setdefault(output_key, []).append(
                dialogue_identifiers[unit.id]
            )
        dialogue_counts: dict[Path, int] = {}
        string_counts: dict[Path, int] = {}

        for relative_path, rows in dialogue_by_file.items():
            output_relative = self._output_relative_path(relative_path)
            destination = language_dir / output_relative
            content, encoding, newline, has_bom, existed = self._read_merge_target(
                destination,
                self._header(index, requested_language, language),
            )
            for unit, translated in rows:
                identifier = dialogue_identifiers[unit.id]
                content = self._upsert_dialogue_block(
                    content,
                    language,
                    identifier,
                    self._dialogue_block(language, identifier, unit, translated),
                    dialogue_order_by_file[output_relative.as_posix()],
                )
            content = self._normalize_strings_block_at_end(content, language)
            self._write_merge_target(destination, content, encoding, newline, has_bom)
            dialogue_counts[destination.resolve()] = len(rows)
            if existed:
                copied_paths.add(destination.resolve())

        for relative_path, rows in strings_by_file.items():
            destination = language_dir / self._output_relative_path(relative_path)
            content, encoding, newline, has_bom, existed = self._read_merge_target(
                destination,
                self._header(index, requested_language, language),
            )
            content = self._normalize_strings_block_at_end(content, language)
            for source, translated, unit in rows:
                content = self._upsert_string_entry(
                    content,
                    language,
                    source,
                    translated,
                    unit,
                )
            self._write_merge_target(destination, content, encoding, newline, has_bom)
            string_counts[destination.resolve()] = len(rows)
            if existed:
                copied_paths.add(destination.resolve())

        emitted = [
            self._file_record(
                root,
                path,
                dialogue_counts.get(path.resolve(), 0),
                string_counts.get(path.resolve(), 0),
                preserved=path.resolve() in copied_paths,
            )
            for path in sorted(
                language_dir.rglob("*.rpy"),
                key=lambda item: item.relative_to(language_dir).as_posix().casefold(),
            )
            if path.is_file() and not path.is_symlink()
        ]
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
            parts = parts[2:]
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

    @staticmethod
    def _read_merge_target(
        path: Path,
        default_header: str,
    ) -> tuple[str, str, str, bool, bool]:
        if not path.is_file():
            return default_header, "utf-8", "\n", False, False
        if path.is_symlink():
            raise ValueError(f"目标语言脚本是符号链接，拒绝修改：{path}")
        text, encoding, newline, has_bom = read_text_preserving(path)
        return text.replace("\r\n", "\n").replace("\r", "\n"), encoding, newline, has_bom, True

    @staticmethod
    def _write_merge_target(
        path: Path,
        text: str,
        encoding: str,
        newline: str,
        has_bom: bool,
    ) -> None:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        rendered = normalized if newline == "\n" else normalized.replace("\n", newline)
        try:
            payload = rendered.encode(encoding)
        except UnicodeEncodeError:
            encoding = "utf-8"
            payload = rendered.encode(encoding)
        if has_bom and encoding == "utf-8":
            payload = b"\xef\xbb\xbf" + payload
        atomic_write_bytes(path, payload)

    @staticmethod
    def _upsert_dialogue_block(
        text: str,
        language: str,
        identifier: str,
        block: str,
        expected_order: list[str],
    ) -> str:
        lines = text.splitlines()
        start = None
        end = None
        for index, line in enumerate(lines):
            match = TRANSLATE_HEADER_RE.fullmatch(line)
            if match and match.group(1) == language and match.group(2) == identifier:
                start = index
                for following in range(index + 1, len(lines)):
                    if TRANSLATE_HEADER_RE.fullmatch(lines[following]):
                        end = following
                        if following and LOCATION_COMMENT_RE.fullmatch(lines[following - 1]):
                            end -= 1
                        break
                if end is None:
                    end = len(lines)
                break
        replacement = block.rstrip("\n").splitlines()
        if start is not None and end is not None:
            if replacement and LOCATION_COMMENT_RE.fullmatch(replacement[0]):
                replacement = replacement[1:]
            merged = [*lines[:start], *replacement, "", *lines[end:]]
            return "\n".join(merged).rstrip("\n") + "\n"

        order = {translation_id: position for position, translation_id in enumerate(expected_order)}
        desired_position = order.get(identifier, len(order))
        insertion = len(lines)
        strings_header = None
        for index, line in enumerate(lines):
            match = TRANSLATE_HEADER_RE.fullmatch(line)
            if not match or match.group(1) != language:
                continue
            existing_identifier = match.group(2)
            if existing_identifier == "strings":
                strings_header = index
                break
            existing_position = order.get(existing_identifier)
            if existing_position is not None and existing_position > desired_position:
                insertion = index
                if index and LOCATION_COMMENT_RE.fullmatch(lines[index - 1]):
                    insertion -= 1
                break
        else:
            if strings_header is not None:
                insertion = strings_header

        if strings_header is not None and insertion == len(lines):
            insertion = strings_header
        replacement = block.rstrip("\n").splitlines()
        lines[insertion:insertion] = [*replacement, ""]
        return "\n".join(lines).rstrip("\n") + "\n"

    @staticmethod
    def _upsert_string_entry(
        text: str,
        language: str,
        source: str,
        translated: str,
        unit: TextUnit,
    ) -> str:
        lines = text.splitlines()
        block_start = None
        block_end = None
        for index, line in enumerate(lines):
            match = TRANSLATE_HEADER_RE.fullmatch(line)
            if match and match.group(1) == language and match.group(2) == "strings":
                block_start = index
                block_end = len(lines)
                for following in range(index + 1, len(lines)):
                    if TRANSLATE_HEADER_RE.fullmatch(lines[following]):
                        block_end = following
                        break
                break

        if block_start is not None and block_end is not None:
            for index in range(block_start + 1, block_end):
                stripped = lines[index].strip()
                if not stripped.startswith("old "):
                    continue
                segments = _quoted_segments(stripped[4:].lstrip())
                if not segments or segments[0][3] != source:
                    continue
                for following in range(index + 1, block_end):
                    candidate = lines[following].strip()
                    if candidate.startswith("new "):
                        indent = lines[following][:-len(lines[following].lstrip())]
                        lines[following] = f"{indent}new {quote_renpy_string(translated)}"
                        return "\n".join(lines).rstrip("\n") + "\n"
                    if candidate.startswith("old ") or TRANSLATE_HEADER_RE.fullmatch(lines[following]):
                        break

            entry = [
                f"    # {unit.location.relative_path}:{unit.location.line}",
                f"    old {quote_renpy_string(source)}",
                f"    new {quote_renpy_string(translated)}",
                "",
            ]
            insertion = block_end
            while insertion > block_start + 1 and not lines[insertion - 1].strip():
                insertion -= 1
            lines[insertion:insertion] = ["", *entry]
            return "\n".join(lines).rstrip("\n") + "\n"

        entry = (
            f"translate {language} strings:\n\n"
            f"    # {unit.location.relative_path}:{unit.location.line}\n"
            f"    old {quote_renpy_string(source)}\n"
            f"    new {quote_renpy_string(translated)}\n\n"
        )
        separator = (
            ""
            if not text or text.endswith("\n\n")
            else ("\n" if text.endswith("\n") else "\n\n")
        )
        return text + separator + entry

    @staticmethod
    def _normalize_strings_block_at_end(text: str, language: str) -> str:
        """Keep one strings block, after all dialogue blocks, without losing its rows."""
        lines = text.splitlines()
        headers = [
            index
            for index, line in enumerate(lines)
            if (
                (match := TRANSLATE_HEADER_RE.fullmatch(line))
                and match.group(1) == language
                and match.group(2) == "strings"
            )
        ]
        if not headers:
            return text

        ranges: list[tuple[int, int]] = []
        bodies: list[list[str]] = []
        for start in headers:
            end = len(lines)
            for following in range(start + 1, len(lines)):
                if TRANSLATE_HEADER_RE.fullmatch(lines[following]):
                    end = following
                    if following and LOCATION_COMMENT_RE.fullmatch(lines[following - 1]):
                        end -= 1
                    break
            body = lines[start + 1:end]
            while body and not body[0].strip():
                body.pop(0)
            while body and not body[-1].strip():
                body.pop()
            bodies.append(body)
            ranges.append((start, end))

        already_terminal = (
            len(ranges) == 1
            and all(not line.strip() for line in lines[ranges[0][1]:])
        )
        if already_terminal:
            return text

        retained = list(lines)
        for start, end in reversed(ranges):
            del retained[start:end]
        while retained and not retained[-1].strip():
            retained.pop()

        merged = [*retained, "", f"translate {language} strings:", ""]
        for body in bodies:
            if not body:
                continue
            merged.extend(body)
            merged.append("")
        return "\n".join(merged).rstrip("\n") + "\n"

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
