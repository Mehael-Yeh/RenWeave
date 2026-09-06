from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import re
import unicodedata

from .emitter import RenpyTranslationEmitter, normalize_renpy_language
from .io import read_text_preserving
from .models import ProjectIndex, TextChannel, TextUnit
from .parser import _quoted_segments
from .validation import PLACEHOLDER_RE, TAG_RE, TranslationValidator


TRANSLATE_HEADER_RE = re.compile(
    r"^\s*translate\s+([^\W\d]\w*)\s+(\w+)\s*:\s*$",
    re.UNICODE,
)


LANGUAGE_DISPLAY_ALIASES = {
    # Chinese, Japanese and Korean.
    "zh": "简体中文",
    "zh_hans": "简体中文",
    "zh_cn": "简体中文",
    "zh_sg": "简体中文",
    "zh_simplified": "简体中文",
    "simplified_chinese": "简体中文",
    "chinese": "简体中文",
    "mandarin": "简体中文",
    "schinese": "简体中文",
    "sc": "简体中文",
    "zh_hant": "繁體中文",
    "zh_tw": "繁體中文",
    "zh_hk": "繁體中文",
    "zh_mo": "繁體中文",
    "traditional_chinese": "繁體中文",
    "tchinese": "繁體中文",
    "tc": "繁體中文",
    "ja": "日本語",
    "jp": "日本語",
    "japanese": "日本語",
    "nihongo": "日本語",
    "ko": "한국어",
    "kr": "한국어",
    "korean": "한국어",
    # Germanic and Romance languages.
    "en": "English",
    "en_us": "English",
    "en_gb": "English",
    "english": "English",
    "de": "Deutsch",
    "de_de": "Deutsch",
    "german": "Deutsch",
    "deutsch": "Deutsch",
    "fr": "Français",
    "fr_fr": "Français",
    "french": "Français",
    "francais": "Français",
    "français": "Français",
    "es": "Español",
    "es_es": "Español",
    "spanish": "Español",
    "espanol": "Español",
    "it": "Italiano",
    "italian": "Italiano",
    "italiano": "Italiano",
    "pt": "Português",
    "pt_br": "Português",
    "pt_pt": "Português",
    "portuguese": "Português",
    "portugues": "Português",
    "nl": "Nederlands",
    "dutch": "Nederlands",
    "nederlands": "Nederlands",
    "af": "Afrikaans",
    "afrikaans": "Afrikaans",
    # Slavic and other European languages.
    "ru": "Русский",
    "ru_ru": "Русский",
    "russian": "Русский",
    "русский": "Русский",
    "uk": "Українська",
    "uk_ua": "Українська",
    "ukrainian": "Українська",
    "be": "Беларуская",
    "belarusian": "Беларуская",
    "bg": "Български",
    "bulgarian": "Български",
    "sr": "Српски",
    "serbian": "Српски",
    "hr": "Hrvatski",
    "croatian": "Hrvatski",
    "pl": "Polski",
    "polish": "Polski",
    "cs": "Čeština",
    "czech": "Čeština",
    "sk": "Slovenčina",
    "slovak": "Slovenčina",
    "sl": "Slovenščina",
    "slovenian": "Slovenščina",
    "hu": "Magyar",
    "hungarian": "Magyar",
    "ro": "Română",
    "romanian": "Română",
    "el": "Ελληνικά",
    "greek": "Ελληνικά",
    "tr": "Türkçe",
    "turkish": "Türkçe",
    "sv": "Svenska",
    "swedish": "Svenska",
    "da": "Dansk",
    "danish": "Dansk",
    "no": "Norsk",
    "nb": "Norsk",
    "norwegian": "Norsk",
    "fi": "Suomi",
    "finnish": "Suomi",
    "et": "Eesti",
    "estonian": "Eesti",
    "lv": "Latviešu",
    "latvian": "Latviešu",
    "lt": "Lietuvių",
    "lithuanian": "Lietuvių",
    "ca": "Català",
    "catalan": "Català",
    # Southeast Asian and South Asian languages.
    "id": "Bahasa Indonesia",
    "id_id": "Bahasa Indonesia",
    "indonesian": "Bahasa Indonesia",
    "bahasa_indonesia": "Bahasa Indonesia",
    "ms": "Bahasa Melayu",
    "malay": "Bahasa Melayu",
    "vi": "Tiếng Việt",
    "vietnamese": "Tiếng Việt",
    "th": "ไทย",
    "thai": "ไทย",
    "hi": "हिन्दी",
    "hindi": "हिन्दी",
    "bn": "বাংলা",
    "bengali": "বাংলা",
    "ta": "தமிழ்",
    "tamil": "தமிழ்",
    "te": "తెలుగు",
    "telugu": "తెలుగు",
    # Middle Eastern and African languages.
    "ar": "العربية",
    "arabic": "العربية",
    "fa": "فارسی",
    "persian": "فارسی",
    "farsi": "فارسی",
    "ur": "اردو",
    "urdu": "اردو",
    "he": "עברית",
    "hebrew": "עברית",
    "ka": "ქართული",
    "georgian": "ქართული",
    "hy": "Հայերեն",
    "armenian": "Հայերեն",
    "am": "አማርኛ",
    "amharic": "አማርኛ",
}

LANGUAGE_DISPLAY_NAMES = tuple(dict.fromkeys(LANGUAGE_DISPLAY_ALIASES.values()))


TRADITIONAL_MARKERS = set("體繁體臺灣國語學習說話這個與為後發現門開關車裡麼會對於從現時實產業").union(
    "電腦檔案網頁廣東話聽見轉譯選擇畫面"
)


LANGUAGE_WORD_HINTS = {
    "English": {"the", "and", "this", "that", "with", "you", "for", "not", "are", "from"},
    "Deutsch": {"der", "die", "das", "und", "ist", "ein", "eine", "nicht", "mit", "für", "auf"},
    "Français": {"les", "des", "une", "est", "que", "dans", "pour", "avec", "pas", "sont", "français"},
    "Español": {"los", "las", "una", "que", "para", "con", "del", "está", "están", "español"},
    "Italiano": {"gli", "una", "che", "per", "con", "non", "sono", "questo", "italiano"},
    "Português": {"uma", "que", "para", "com", "não", "dos", "das", "está", "português"},
    "Nederlands": {"het", "een", "van", "voor", "met", "niet", "zijn", "naar", "dutch"},
    "Bahasa Indonesia": {"yang", "dan", "untuk", "dengan", "tidak", "ini", "itu", "dari", "bahasa"},
    "Bahasa Melayu": {"yang", "dan", "untuk", "dengan", "tidak", "ini", "itu", "daripada", "melayu"},
    "Türkçe": {"bir", "ve", "için", "ile", "değil", "bu", "şu", "türkçe"},
    "Polski": {"jest", "nie", "dla", "oraz", "się", "tego", "polski"},
    "Română": {"este", "pentru", "care", "nu", "și", "din", "română"},
    "Svenska": {"och", "det", "att", "för", "inte", "med", "svenska"},
    "Dansk": {"og", "det", "at", "for", "ikke", "med", "dansk"},
    "Norsk": {"og", "det", "for", "ikke", "med", "som", "norsk"},
    "Suomi": {"ja", "että", "on", "ei", "kanssa", "suomi"},
    "Tiếng Việt": {"và", "của", "cho", "không", "trong", "tiếng", "việt"},
}


@dataclass(slots=True, frozen=True)
class ExistingLanguageSummary:
    language: str
    script_files: int
    compiled_files: int
    total_bytes: int
    display_name: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class ExistingTranslationInventory:
    language: str
    language_dir: str
    files_scanned: int
    total_units: int
    reusable_units: int
    missing_units: int
    invalid_units: int
    source_fallback_units: int
    complete_scenes: int
    partial_scenes: int
    missing_scenes: int
    translations_by_scene: dict[str, dict[str, str]] = field(repr=False)
    model_units: int = 0
    workspace_reused_units: int = 0
    exact_source_reused_units: int = 0
    normalized_source_reused_units: int = 0
    total_existing_records: int = 0
    duplicate_source_conflict_groups: int = 0
    duplicate_source_conflict_units: int = 0
    pending_units: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    @property
    def has_existing_language(self) -> bool:
        return bool(self.language_dir)

    @property
    def complete(self) -> bool:
        return self.total_units > 0 and self.reusable_units == self.total_units

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload.pop("translations_by_scene", None)
        payload["has_existing_language"] = self.has_existing_language
        payload["complete"] = self.complete
        return payload


def discover_existing_languages(target: str | Path) -> list[ExistingLanguageSummary]:
    """Return lightweight language-folder summaries without analyzing game scripts."""
    from .discovery import ProjectDiscovery

    project = ProjectDiscovery().discover(target)
    tl_dir = Path(project.game_dir) / "tl"
    if not tl_dir.is_dir():
        return []
    result = []
    for language_dir in sorted(
        (item for item in tl_dir.iterdir() if item.is_dir()),
        key=lambda item: item.name.casefold(),
    ):
        scripts = [item for item in language_dir.rglob("*.rpy") if item.is_file()]
        compiled = [item for item in language_dir.rglob("*.rpyc") if item.is_file()]
        sample = _sample_translation_text(scripts[:3])
        result.append(ExistingLanguageSummary(
            language=language_dir.name,
            script_files=len(scripts),
            compiled_files=len(compiled),
            total_bytes=sum(item.stat().st_size for item in [*scripts, *compiled]),
            display_name=language_display_name(language_dir.name, sample),
        ))
    return result


def language_display_name(language: str, sample_text: str = "") -> str:
    """Return a user-facing language name using the folder and a small sample."""
    normalized = re.sub(r"[^\w]+", "_", language.strip().casefold()).strip("_")
    aliased = LANGUAGE_DISPLAY_ALIASES.get(normalized)
    if aliased is not None:
        return aliased
    detected = _detect_language_from_sample(sample_text)
    if detected is not None:
        return detected
    return language


def _detect_language_from_sample(sample_text: str) -> str | None:
    """Infer a language from script markers and a conservative word score."""
    if not sample_text.strip():
        return None
    text = sample_text.casefold()
    counts = {
        "han": sum("\u3400" <= char <= "\u9fff" for char in text),
        "kana": sum("\u3040" <= char <= "\u30ff" for char in text),
        "hangul": sum("\uac00" <= char <= "\ud7af" for char in text),
        "cyrillic": sum("\u0400" <= char <= "\u052f" for char in text),
        "arabic": sum("\u0600" <= char <= "\u06ff" for char in text),
        "hebrew": sum("\u0590" <= char <= "\u05ff" for char in text),
        "devanagari": sum("\u0900" <= char <= "\u097f" for char in text),
        "bengali": sum("\u0980" <= char <= "\u09ff" for char in text),
        "tamil": sum("\u0b80" <= char <= "\u0bff" for char in text),
        "telugu": sum("\u0c00" <= char <= "\u0c7f" for char in text),
        "thai": sum("\u0e00" <= char <= "\u0e7f" for char in text),
        "greek": sum("\u0370" <= char <= "\u03ff" for char in text),
        "georgian": sum("\u10a0" <= char <= "\u10ff" for char in text),
        "armenian": sum("\u0530" <= char <= "\u058f" for char in text),
        "ethiopic": sum("\u1200" <= char <= "\u137f" for char in text),
    }
    if counts["kana"]:
        return "日本語"
    if counts["hangul"]:
        return "한국어"
    if counts["han"]:
        traditional = sum(char in TRADITIONAL_MARKERS for char in text)
        return "繁體中文" if traditional >= 2 else "简体中文"
    if counts["cyrillic"]:
        if any(char in text for char in "іїєґ"):
            return "Українська"
        if any(char in text for char in "ђјљњћџ"):
            return "Српски"
        if any(char in text for char in "ъьъ") and "ы" not in text:
            return "Български"
        return "Русский"
    if counts["arabic"]:
        if any(char in text for char in "پچژگ"):
            return "فارسی"
        if any(char in text for char in "ٹڈڑںے"):
            return "اردو"
        return "العربية"
    if counts["hebrew"]:
        return "עברית"
    script_names = (
        ("devanagari", "हिन्दी"),
        ("bengali", "বাংলা"),
        ("tamil", "தமிழ்"),
        ("telugu", "తెలుగు"),
        ("thai", "ไทย"),
        ("greek", "Ελληνικά"),
        ("georgian", "ქართული"),
        ("armenian", "Հայերեն"),
        ("ethiopic", "አማርኛ"),
    )
    for script, display_name in script_names:
        if counts[script]:
            return display_name

    words = set(re.findall(r"[\wÀ-ÿ]+", text, re.UNICODE))
    scores = {
        name: len(words.intersection(hints))
        for name, hints in LANGUAGE_WORD_HINTS.items()
    }
    best_name, best_score = max(scores.items(), key=lambda item: item[1])
    if best_score < 2:
        return None
    tied = [name for name, score in scores.items() if score == best_score]
    if len(tied) > 1:
        return None
    return best_name


def _sample_translation_text(paths: list[Path]) -> str:
    """Read a few translation files for a lightweight language sanity check."""
    chunks: list[str] = []
    for path in paths:
        try:
            text, _encoding, _newline, _bom = read_text_preserving(path)
        except (OSError, UnicodeError):
            continue
        new_values = re.findall(r"^\s*new\s+(.+)$", text, re.MULTILINE)
        if new_values:
            chunks.append("\n".join(new_values)[:12000])
            continue
        quoted_values = [match[3] for match in _quoted_segments(text) if match[3].strip()]
        chunks.append("\n".join(quoted_values)[:12000] if quoted_values else text[:12000])
    return "\n".join(chunks)


class ExistingTranslationScanner:
    """Match standard Ren'Py language blocks to the current source index."""

    def scan(self, index: ProjectIndex, requested_language: str) -> ExistingTranslationInventory:
        language = self._resolve_language(index, requested_language)
        language_dir = Path(index.project.game_dir) / "tl" / language
        if not language_dir.is_dir():
            return ExistingTranslationInventory(
                language=language,
                language_dir="",
                files_scanned=0,
                total_units=len(index.text_units),
                reusable_units=0,
                missing_units=len(index.text_units),
                invalid_units=0,
                source_fallback_units=0,
                complete_scenes=0,
                partial_scenes=0,
                missing_scenes=sum(bool(scene.text_units) for scene in index.scenes),
                translations_by_scene={},
            )

        blocks: dict[str, list[str]] = {}
        string_rows: dict[str, str] = {}
        statement_rows: dict[tuple[str, str, int], list[str]] = {}
        source_rows: dict[tuple[str, str, int], list[str]] = {}
        exact_source_rows: dict[str, list[tuple[str, str]]] = {}
        issues: list[str] = []
        files = sorted(language_dir.rglob("*.rpy"), key=lambda item: item.as_posix().casefold())
        for path in files:
            try:
                text, _encoding, _newline, _bom = read_text_preserving(path)
                self._parse_file(
                    text.splitlines(),
                    blocks,
                    string_rows,
                    statement_rows,
                    source_rows,
                    exact_source_rows,
                    issues,
                    path,
                    language_dir,
                )
            except (OSError, UnicodeError, ValueError) as exc:
                issues.append(f"{path.relative_to(language_dir).as_posix()}: {exc}")

        dialogue_ids = RenpyTranslationEmitter.dialogue_identifiers(index)
        translations_by_scene: dict[str, dict[str, str]] = {}
        invalid_ids: set[str] = set()
        source_fallback_ids: set[str] = set()
        exact_source_fallback_ids: set[str] = set()
        normalized_source_fallback_ids: set[str] = set()
        pending_units: list[dict] = []
        statement_offsets: dict[tuple[str, str, int], int] = {}
        source_offsets: dict[tuple[str, str, int], int] = {}
        for scene in index.scenes:
            scene_translations: dict[str, str] = {}
            for unit in scene.text_units:
                translated, match_kind = self._translation_for(
                    unit,
                    dialogue_ids,
                    blocks,
                    string_rows,
                    statement_rows,
                    statement_offsets,
                    source_rows,
                    source_offsets,
                )
                structural_issue = self._structural_issue(unit, translated) if translated is not None else ""
                if translated is None or structural_issue:
                    exact_translation = self._unique_exact_source_translation(unit, exact_source_rows)
                    if exact_translation is not None:
                        translated = exact_translation
                        structural_issue = ""
                        match_kind = "exact_source"
                if translated is None or structural_issue:
                    normalized_translation = self._unique_normalized_source_translation(
                        unit, exact_source_rows
                    )
                    if normalized_translation is not None:
                        translated = normalized_translation
                        structural_issue = ""
                        match_kind = "normalized_source"
                if translated is None and self._language_neutral(unit.source):
                    translated = unit.source
                    source_fallback_ids.add(unit.id)
                    match_kind = "language_neutral"
                if translated is None:
                    pending_units.append(self._pending_unit(
                        unit, "missing", "旧语言包中没有找到可安全复用的译文"
                    ))
                    continue
                if structural_issue:
                    invalid_ids.add(unit.id)
                    issues.append(
                        f"{unit.location.relative_path}:{unit.location.line}: "
                        f"{structural_issue} ({unit.id})"
                    )
                    pending_units.append(self._pending_unit(unit, "invalid", structural_issue))
                    continue
                scene_translations[unit.id] = translated
                if match_kind == "exact_source":
                    exact_source_fallback_ids.add(unit.id)
                elif match_kind == "normalized_source":
                    normalized_source_fallback_ids.add(unit.id)
            if scene_translations:
                translations_by_scene[scene.id] = scene_translations

        validator = TranslationValidator()
        complete_scenes = 0
        partial_scenes = 0
        missing_scenes = 0
        for scene in index.scenes:
            if not scene.text_units:
                continue
            translations = translations_by_scene.get(scene.id, {})
            if validator.validate_scene(index, scene.id, translations).passed:
                complete_scenes += 1
            elif translations:
                partial_scenes += 1
            else:
                missing_scenes += 1
        reusable_units = sum(len(items) for items in translations_by_scene.values())
        total_units = len(index.text_units)
        conflicts = self._source_conflicts(exact_source_rows)
        return ExistingTranslationInventory(
            language=language,
            language_dir=str(language_dir),
            files_scanned=len(files),
            total_units=total_units,
            reusable_units=reusable_units,
            missing_units=max(0, total_units - reusable_units - len(invalid_ids)),
            invalid_units=len(invalid_ids),
            source_fallback_units=len(source_fallback_ids),
            complete_scenes=complete_scenes,
            partial_scenes=partial_scenes,
            missing_scenes=missing_scenes,
            translations_by_scene=translations_by_scene,
            model_units=max(0, total_units - reusable_units),
            exact_source_reused_units=len(exact_source_fallback_ids),
            normalized_source_reused_units=len(normalized_source_fallback_ids),
            total_existing_records=sum(len(items) for items in exact_source_rows.values()),
            duplicate_source_conflict_groups=len(conflicts),
            duplicate_source_conflict_units=sum(item["occurrences"] for item in conflicts),
            pending_units=pending_units[:500],
            conflicts=conflicts[:100],
            issues=issues[:100],
        )

    @staticmethod
    def _resolve_language(index: ProjectIndex, requested_language: str) -> str:
        normalized = normalize_renpy_language(requested_language)
        by_casefold = {item.casefold(): item for item in index.project.translation_languages}
        return by_casefold.get(normalized.casefold(), normalized)

    @staticmethod
    def _parse_file(
        lines: list[str],
        blocks: dict[str, list[str]],
        string_rows: dict[str, str],
        statement_rows: dict[tuple[str, str, int], list[str]],
        source_rows: dict[tuple[str, str, int], list[str]],
        exact_source_rows: dict[str, list[tuple[str, str]]],
        issues: list[str],
        path: Path,
        language_dir: Path,
    ) -> None:
        index = 0
        while index < len(lines):
            header = TRANSLATE_HEADER_RE.match(lines[index])
            if header is None:
                index += 1
                continue
            identifier = header.group(2)
            start = index + 1
            index = start
            while index < len(lines) and TRANSLATE_HEADER_RE.match(lines[index]) is None:
                index += 1
            body = lines[start:index]
            if identifier == "strings":
                ExistingTranslationScanner._parse_strings(
                    body, string_rows, exact_source_rows, issues, path, language_dir
                )
            elif identifier not in blocks:
                blocks[identifier] = body
            ExistingTranslationScanner._parse_statement_rows(
                body,
                statement_rows,
                source_rows,
                exact_source_rows,
                path.relative_to(language_dir).as_posix(),
            )

    @staticmethod
    def _parse_statement_rows(
        lines: list[str],
        statement_rows: dict[tuple[str, str, int], list[str]],
        source_rows: dict[tuple[str, str, int], list[str]],
        exact_source_rows: dict[str, list[tuple[str, str]]],
        relative_path: str,
    ) -> None:
        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped.startswith("#"):
                continue
            source_statement = stripped[1:].strip()
            source_segments = _quoted_segments(source_statement)
            if not source_segments:
                continue
            translated_statement = ""
            for candidate in lines[index + 1:]:
                rendered = candidate.strip()
                if not rendered or rendered.startswith("#"):
                    continue
                if _quoted_segments(rendered):
                    translated_statement = rendered
                break
            translated_segments = _quoted_segments(translated_statement)
            if not translated_segments:
                continue
            for ordinal in range(min(len(source_segments), len(translated_segments))):
                key = (
                    ExistingTranslationScanner._canonical_relative(relative_path),
                    ExistingTranslationScanner._statement_key(source_statement),
                    ordinal,
                )
                statement_rows.setdefault(key, []).append(translated_segments[ordinal][3])
                source_key = (
                    ExistingTranslationScanner._canonical_relative(relative_path),
                    source_segments[ordinal][3],
                    ordinal,
                )
                source_rows.setdefault(source_key, []).append(translated_segments[ordinal][3])
                exact_source_rows.setdefault(source_segments[ordinal][3], []).append((
                    translated_segments[ordinal][3],
                    f"{relative_path}:{index + 1}",
                ))
            break

    @staticmethod
    def _parse_strings(
        lines: list[str],
        string_rows: dict[str, str],
        exact_source_rows: dict[str, list[tuple[str, str]]],
        issues: list[str],
        path: Path,
        language_dir: Path,
    ) -> None:
        pending_source: str | None = None
        for line_number, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith("old "):
                segments = _quoted_segments(stripped[4:].strip())
                pending_source = segments[0][3] if segments else None
            elif stripped.startswith("new ") and pending_source is not None:
                segments = _quoted_segments(stripped[4:].strip())
                if not segments:
                    pending_source = None
                    continue
                translated = segments[0][3]
                previous = string_rows.get(pending_source)
                if previous is not None and previous != translated:
                    issues.append(f"{path.name}:{line_number}: duplicate old string has different translations")
                else:
                    string_rows[pending_source] = translated
                exact_source_rows.setdefault(pending_source, []).append((
                    translated,
                    f"{path.relative_to(language_dir).as_posix()}:{line_number}",
                ))
                pending_source = None

    @staticmethod
    def _translation_for(
        unit: TextUnit,
        dialogue_ids: dict[str, str],
        blocks: dict[str, list[str]],
        string_rows: dict[str, str],
        statement_rows: dict[tuple[str, str, int], list[str]],
        statement_offsets: dict[tuple[str, str, int], int],
        source_rows: dict[tuple[str, str, int], list[str]],
        source_offsets: dict[tuple[str, str, int], int],
    ) -> tuple[str | None, str]:
        if unit.channel not in {TextChannel.DIALOGUE, TextChannel.NARRATION}:
            translated = string_rows.get(unit.source)
            return translated, "strings" if translated is not None else ""
        key = (
            ExistingTranslationScanner._canonical_relative(unit.location.relative_path),
            ExistingTranslationScanner._statement_key(unit.raw_statement),
            unit.literal_ordinal,
        )
        occurrence = statement_offsets.get(key, 0)
        statement_offsets[key] = occurrence + 1
        source_key = (
            ExistingTranslationScanner._canonical_relative(unit.location.relative_path),
            unit.source,
            unit.literal_ordinal,
        )
        source_occurrence = source_offsets.get(source_key, 0)
        source_offsets[source_key] = source_occurrence + 1
        statement_candidates = statement_rows.get(key, [])
        if occurrence < len(statement_candidates):
            return statement_candidates[occurrence], "statement"
        source_candidates = source_rows.get(source_key, [])
        if source_occurrence < len(source_candidates):
            return source_candidates[source_occurrence], "file_source"
        body = blocks.get(dialogue_ids.get(unit.id, ""))
        if body is None:
            return ExistingTranslationScanner._fallback_translation(
                key,
                occurrence,
                source_key,
                source_occurrence,
                statement_rows,
                source_rows,
            ), "fallback"
        for line in body:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            segments = _quoted_segments(stripped)
            if unit.literal_ordinal < len(segments):
                return segments[unit.literal_ordinal][3], "block_id"
        return ExistingTranslationScanner._fallback_translation(
            key,
            occurrence,
            source_key,
            source_occurrence,
            statement_rows,
            source_rows,
        ), "fallback"

    @staticmethod
    def _unique_exact_source_translation(
        unit: TextUnit,
        exact_source_rows: dict[str, list[tuple[str, str]]],
    ) -> str | None:
        valid = {
            translated
            for translated, _location in exact_source_rows.get(unit.source, [])
            if not ExistingTranslationScanner._structural_issue(unit, translated)
        }
        return next(iter(valid)) if len(valid) == 1 else None

    @staticmethod
    def _unique_normalized_source_translation(
        unit: TextUnit,
        exact_source_rows: dict[str, list[tuple[str, str]]],
    ) -> str | None:
        """Reuse only uniquely translated sources differing by harmless typography.

        This deliberately does not perform fuzzy or semantic matching. It accepts
        Unicode-width, whitespace, letter-case, and comma-only differences while
        requiring Ren'Py placeholders and text tags to remain identical.
        """
        key = ExistingTranslationScanner._normalized_source_key(unit.source)
        valid: set[str] = set()
        for old_source, rows in exact_source_rows.items():
            if old_source == unit.source:
                continue
            if ExistingTranslationScanner._normalized_source_key(old_source) != key:
                continue
            if sorted(TAG_RE.findall(old_source)) != sorted(TAG_RE.findall(unit.source)):
                continue
            if sorted(PLACEHOLDER_RE.findall(old_source)) != sorted(PLACEHOLDER_RE.findall(unit.source)):
                continue
            valid.update(
                translated
                for translated, _location in rows
                if not ExistingTranslationScanner._structural_issue(unit, translated)
            )
            if len(valid) > 1:
                return None
        return next(iter(valid)) if len(valid) == 1 else None

    @staticmethod
    def _normalized_source_key(source: str) -> str:
        normalized = unicodedata.normalize("NFKC", source)
        normalized = re.sub(r"\s+", " ", normalized).strip().casefold()
        return normalized.translate(str.maketrans("", "", ",，､﹐﹑"))

    @staticmethod
    def _pending_unit(unit: TextUnit, reason: str, detail: str) -> dict:
        return {
            "text_id": unit.id,
            "source": unit.source,
            "file": unit.location.relative_path,
            "line": unit.location.line,
            "channel": str(unit.channel),
            "reason": reason,
            "detail": detail,
        }

    @staticmethod
    def _source_conflicts(exact_source_rows: dict[str, list[tuple[str, str]]]) -> list[dict]:
        conflicts: list[dict] = []
        for source, rows in exact_source_rows.items():
            translations = sorted({translated for translated, _location in rows})
            if len(translations) <= 1:
                continue
            conflicts.append({
                "source": source,
                "translations": translations[:20],
                "locations": [location for _translated, location in rows[:50]],
                "occurrences": len(rows),
            })
        conflicts.sort(key=lambda item: (-item["occurrences"], item["source"]))
        return conflicts

    @staticmethod
    def _fallback_translation(
        statement_key: tuple[str, str, int],
        statement_occurrence: int,
        source_key: tuple[str, str, int],
        source_occurrence: int,
        statement_rows: dict[tuple[str, str, int], list[str]],
        source_rows: dict[tuple[str, str, int], list[str]],
    ) -> str | None:
        candidates = statement_rows.get(statement_key, [])
        if statement_occurrence < len(candidates):
            return candidates[statement_occurrence]
        source_candidates = source_rows.get(source_key, [])
        return source_candidates[source_occurrence] if source_occurrence < len(source_candidates) else None

    @staticmethod
    def _canonical_relative(value: str) -> str:
        normalized = value.replace("\\", "/").strip("/")
        normalized = re.sub(r"^_archives/\d+/", "", normalized)
        return normalized.casefold()

    @staticmethod
    def _statement_key(value: str) -> str:
        return re.sub(r"\s+", " ", value.strip())

    @staticmethod
    def _structurally_valid(unit: TextUnit, translated: str) -> bool:
        return not ExistingTranslationScanner._structural_issue(unit, translated)

    @staticmethod
    def _structural_issue(unit: TextUnit, translated: str) -> str:
        if unit.source.strip() and not translated.strip():
            return "empty existing translation"
        if sorted(TAG_RE.findall(unit.source)) != sorted(TAG_RE.findall(translated)):
            return "Ren'Py text tags differ from the source"
        if sorted(PLACEHOLDER_RE.findall(unit.source)) != sorted(PLACEHOLDER_RE.findall(translated)):
            return "variables or placeholders differ from the source"
        return ""

    @staticmethod
    def _language_neutral(source: str) -> bool:
        visible = TAG_RE.sub("", source)
        visible = PLACEHOLDER_RE.sub("", visible)
        return not any(character.isalnum() for character in visible)
