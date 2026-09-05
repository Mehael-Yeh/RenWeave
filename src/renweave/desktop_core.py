"""UI-toolkit-neutral desktop services shared by the Qt frontend."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .pipeline import PipelineStage, PipelineState, RenWeavePipeline
from .provider import ModelProfile
from .runtime import CancellationToken


def _user_home_fallback() -> Path:
    candidates = [
        os.environ.get("USERPROFILE"),
        (
            f"{os.environ.get('HOMEDRIVE', '')}{os.environ.get('HOMEPATH', '')}"
            if os.environ.get("HOMEDRIVE") and os.environ.get("HOMEPATH")
            else None
        ),
    ]
    for candidate in candidates:
        if candidate:
            return Path(candidate)
    try:
        return Path.home()
    except (OSError, RuntimeError):
        return Path(tempfile.gettempdir())


def default_desktop_settings_path() -> Path:
    if os.name == "nt":
        configured = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        base = Path(configured) if configured else _user_home_fallback() / "AppData" / "Roaming"
    else:
        configured = os.environ.get("XDG_CONFIG_HOME")
        base = Path(configured) if configured else _user_home_fallback() / ".config"
    return base / "RenWeave" / "settings.json"


@dataclass(slots=True)
class TranslationRequest:
    project: str
    workspace: str
    provider: str
    source_language: str
    target_language: str
    api_key: str = ""
    generate_rpa: bool = True
    install: bool = False
    overwrite_existing: bool = False
    renpy_sdk: str = ""
    require_engine_validation: bool = False
    knowledge_consent: str = "auto"

    def validate(self) -> None:
        if not Path(self.project).expanduser().exists():
            raise ValueError("Select an existing Ren'Py game directory or executable")
        if not self.workspace.strip():
            raise ValueError("Select a workspace directory")
        if not Path(self.provider).expanduser().is_file():
            raise ValueError("Select a valid provider configuration JSON file")
        if not self.target_language.strip():
            raise ValueError("Enter a target language name or code")
        if self.renpy_sdk and not Path(self.renpy_sdk).expanduser().exists():
            raise ValueError("The selected Ren'Py SDK path does not exist")


def execute_translation(
    request: TranslationRequest,
    *,
    pipeline_factory: Callable[[str], RenWeavePipeline] = RenWeavePipeline,
    cancel_token: CancellationToken | None = None,
    progress_callback: Callable[[PipelineState], None] | None = None,
) -> PipelineState:
    request.validate()
    profile = ModelProfile.load(request.provider)
    if request.api_key:
        profile.api_key = request.api_key
    profile.validate()
    pipeline = pipeline_factory(request.workspace)
    try:
        state = pipeline.translate(
            request.project,
            request.source_language.strip() or "auto",
            request.target_language.strip(),
            profile,
            install=request.install,
            overwrite_existing=request.overwrite_existing,
            generate_rpa=request.generate_rpa,
            renpy_sdk_path=request.renpy_sdk or None,
            require_engine_validation=request.require_engine_validation,
            knowledge_consent=request.knowledge_consent,
            cancel_token=cancel_token,
            progress_callback=progress_callback,
        )
    except BaseException as exc:
        pipeline.logger.exception("run_failed", exc)
        raise
    if state.stage == PipelineStage.PAUSED:
        return state
    if state.stage != "complete" or state.failed_scene_ids:
        detail = state.error or f"{len(state.failed_scene_ids)} scenes were not completed"
        raise RuntimeError(detail)
    return state


def execute_blank_translation(
    project: str,
    workspace: str,
    source_language: str,
    target_language: str,
    *,
    pipeline_factory: Callable[[str], RenWeavePipeline] = RenWeavePipeline,
    cancel_token: CancellationToken | None = None,
    progress_callback: Callable[[PipelineState], None] | None = None,
) -> PipelineState:
    if not Path(project).expanduser().exists():
        raise ValueError("Select an existing Ren'Py game directory or executable")
    if not workspace.strip():
        raise ValueError("Select a workspace directory")
    if not target_language.strip():
        raise ValueError("Enter a target language name or code")
    pipeline = pipeline_factory(workspace)
    state = pipeline.extract_blank_translation(
        project,
        source_language.strip() or "auto",
        target_language.strip(),
        cancel_token=cancel_token,
        progress_callback=progress_callback,
    )
    if state.stage == PipelineStage.PAUSED:
        return state
    if state.stage != PipelineStage.COMPLETE:
        raise RuntimeError(state.error or "Blank translation extraction did not complete")
    return state


__all__ = [
    "TranslationRequest",
    "default_desktop_settings_path",
    "execute_blank_translation",
    "execute_translation",
    "_user_home_fallback",
]
