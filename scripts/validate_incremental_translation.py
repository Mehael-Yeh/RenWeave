from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
import tempfile
from pathlib import Path

from renweave.emitter import RenpyTranslationEmitter
from renweave.existing_translations import ExistingTranslationScanner
from renweave.models import ProjectIndex


def _load_index(workspace: Path) -> ProjectIndex:
    path = workspace / "project-index.json"
    if not path.is_file():
        raise FileNotFoundError(f"Run `renweave analyze` first; missing {path}")
    return ProjectIndex.from_dict(json.loads(path.read_text(encoding="utf-8")))


def _isolated_index(index: ProjectIndex, game_dir: Path) -> ProjectIndex:
    isolated = copy.deepcopy(index)
    isolated.project.game_dir = str(game_dir)
    return isolated


def _copy_language(index: ProjectIndex, language: str, destination: Path) -> Path:
    source = Path(index.project.game_dir) / "tl" / language
    target = destination / "game" / "tl" / language
    if not source.is_dir():
        raise FileNotFoundError(f"Existing language directory not found: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    return target


def _translation_block(path: Path, language: str, identifier: str) -> re.Match[str] | None:
    text = path.read_text(encoding="utf-8-sig")
    pattern = re.compile(
        rf"(?ms)^translate\s+{re.escape(language)}\s+{re.escape(identifier)}\s*:\s*$.*?"
        rf"(?=^translate\s+{re.escape(language)}\s+|\Z)"
    )
    return pattern.search(text)


def _find_block(language_dir: Path, language: str, identifier: str) -> tuple[Path, re.Match[str]]:
    for path in sorted(language_dir.rglob("*.rpy")):
        match = _translation_block(path, language, identifier)
        if match is not None:
            return path, match
    raise LookupError(f"Could not locate translation block {identifier}")


def _delete_one_block(index: ProjectIndex, language_dir: Path, language: str) -> str:
    identifiers = RenpyTranslationEmitter.dialogue_identifiers(index)
    for unit in index.text_units:
        identifier = identifiers.get(unit.id)
        if not identifier:
            continue
        try:
            path, match = _find_block(language_dir, language, identifier)
        except LookupError:
            continue
        text = path.read_text(encoding="utf-8-sig")
        path.write_text(text[: match.start()] + text[match.end() :], encoding="utf-8")
        return unit.id
    raise LookupError("No reusable dialogue block was available for the deletion test")


def _damage_one_placeholder(index: ProjectIndex, language_dir: Path, language: str) -> str:
    identifiers = RenpyTranslationEmitter.dialogue_identifiers(index)
    for unit in index.text_units:
        identifier = identifiers.get(unit.id)
        if not identifier or not unit.placeholders:
            continue
        try:
            path, match = _find_block(language_dir, language, identifier)
        except LookupError:
            continue
        block = match.group(0)
        placeholder = next((item for item in unit.placeholders if item in block), "")
        if not placeholder:
            continue
        placeholder_at = block.rfind(placeholder)
        if placeholder_at < 0:
            continue
        damaged = (
            block[:placeholder_at]
            + "BROKEN_VARIABLE"
            + block[placeholder_at + len(placeholder) :]
        )
        text = path.read_text(encoding="utf-8-sig")
        path.write_text(text[: match.start()] + damaged + text[match.end() :], encoding="utf-8")
        return unit.id
    raise LookupError("No reusable placeholder-bearing dialogue block was available for the damage test")


def _summary(inventory) -> dict[str, int | bool]:
    return {
        "reusable_units": inventory.reusable_units,
        "missing_units": inventory.missing_units,
        "invalid_units": inventory.invalid_units,
        "source_fallback_units": inventory.source_fallback_units,
        "complete": inventory.complete,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate existing, deleted, and damaged Ren'Py translations without changing the game."
    )
    parser.add_argument("project", type=Path)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--language", default="zh_hans")
    args = parser.parse_args()

    index = _load_index(args.workspace.resolve())
    scanner = ExistingTranslationScanner()
    baseline = scanner.scan(index, args.language)

    with tempfile.TemporaryDirectory(prefix="renweave-incremental-qa-") as temp_name:
        temp = Path(temp_name)

        deleted_root = temp / "deleted"
        deleted_language = _copy_language(index, args.language, deleted_root)
        deleted_id = _delete_one_block(index, deleted_language, args.language)
        deleted = scanner.scan(_isolated_index(index, deleted_root / "game"), args.language)

        damaged_root = temp / "damaged"
        damaged_language = _copy_language(index, args.language, damaged_root)
        damaged_id = _damage_one_placeholder(index, damaged_language, args.language)
        damaged = scanner.scan(_isolated_index(index, damaged_root / "game"), args.language)

    if deleted.reusable_units >= baseline.reusable_units:
        raise AssertionError("Deleting a translation block did not reduce reusable work")
    if damaged.invalid_units <= baseline.invalid_units:
        raise AssertionError("Damaging a placeholder was not reported as invalid")

    print(
        json.dumps(
            {
                "project": str(args.project.resolve()),
                "language": args.language,
                "baseline": _summary(baseline),
                "deleted_translation": {"text_unit": deleted_id, **_summary(deleted)},
                "damaged_translation": {"text_unit": damaged_id, **_summary(damaged)},
                "original_game_modified": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
