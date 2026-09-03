from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from pathlib import Path

from .discovery import ProjectDiscovery
from .models import ProjectIndex


@dataclass(frozen=True, slots=True)
class TokenBudget:
    source_characters: int
    source_token_equivalent: int
    estimated_input_low: int
    estimated_input_high: int
    estimated_output_low: int
    estimated_output_high: int
    estimated_total_low: int
    estimated_total_high: int
    scene_count: int
    script_count: int
    confidence: str
    basis: str

    def to_dict(self) -> dict:
        return asdict(self)


def estimate_index_tokens(
    index: ProjectIndex,
    unit_ids: set[str] | None = None,
) -> TokenBudget:
    selected = (
        index.text_units
        if unit_ids is None
        else [unit for unit in index.text_units if unit.id in unit_ids]
    )
    characters = sum(len(unit.source) for unit in selected)
    scene_ids = {unit.scene_id for unit in selected}
    script_paths = {unit.location.relative_path for unit in selected}
    return _budget(
        characters,
        scenes=len(scene_ids),
        scripts=len(index.files) if unit_ids is None else len(script_paths),
        confidence="medium",
        basis=(
            "indexed_translatable_text"
            if unit_ids is None
            else "indexed_pending_translation_text"
        ),
    )


def estimate_project_tokens(target: str | Path) -> TokenBudget:
    """Fast, read-only preflight estimate; compiled-only games deliberately use a wide range."""
    project = ProjectDiscovery().discover(target)
    game_dir = Path(project.game_dir)
    characters = 0
    for relative in project.source_scripts:
        path = game_dir / relative
        characters += len(path.read_text(encoding="utf-8", errors="replace"))
    compiled_bytes = sum((game_dir / relative).stat().st_size for relative in project.compiled_scripts)
    script_count = len(project.source_scripts) + len(project.compiled_scripts)
    if characters:
        proxy_characters = characters + compiled_bytes
        confidence = "medium" if not compiled_bytes and not project.archives else "low"
        basis = "loose_source_and_compiled_proxy" if compiled_bytes else "loose_source_text"
    else:
        proxy_characters = compiled_bytes
        confidence = "low"
        basis = "compiled_or_archived_proxy"
    if proxy_characters <= 0 and project.archives:
        # Archive indexes are intentionally not unpacked in the review screen.
        proxy_characters = 80_000 * len(project.archives)
    scene_proxy = max(1, script_count * 4 + len(project.archives) * 20)
    return _budget(
        proxy_characters,
        scenes=scene_proxy,
        scripts=script_count + len(project.archives),
        confidence=confidence,
        basis=basis,
    )


def _budget(
    characters: int,
    *,
    scenes: int,
    scripts: int,
    confidence: str,
    basis: str,
) -> TokenBudget:
    if characters <= 0:
        return TokenBudget(
            source_characters=0,
            source_token_equivalent=0,
            estimated_input_low=0,
            estimated_input_high=0,
            estimated_output_low=0,
            estimated_output_high=0,
            estimated_total_low=0,
            estimated_total_high=0,
            scene_count=max(0, scenes),
            script_count=max(0, scripts),
            confidence=confidence,
            basis=basis,
        )
    source_tokens = max(1, math.ceil(max(0, characters) / 4))
    # The range includes narrative synthesis, contextual scene prompts, target output,
    # selective repairs, and risk-only refinement. Provider retries are excluded.
    input_low = max(1_000, math.ceil(source_tokens * 2.0 + scenes * 180))
    input_high = max(input_low, math.ceil(source_tokens * 5.5 + scenes * 900))
    output_low = max(500, math.ceil(source_tokens * 0.7))
    output_high = max(output_low, math.ceil(source_tokens * 1.8 + scenes * 120))
    return TokenBudget(
        source_characters=max(0, characters),
        source_token_equivalent=source_tokens,
        estimated_input_low=input_low,
        estimated_input_high=input_high,
        estimated_output_low=output_low,
        estimated_output_high=output_high,
        estimated_total_low=input_low + output_low,
        estimated_total_high=input_high + output_high,
        scene_count=max(0, scenes),
        script_count=max(0, scripts),
        confidence=confidence,
        basis=basis,
    )
