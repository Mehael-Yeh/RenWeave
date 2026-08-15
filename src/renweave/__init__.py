"""RenWeave core package."""

from importlib.metadata import PackageNotFoundError, version as package_version

from .models import ProjectIndex, Scene, TextUnit
from .pipeline import PipelineStage, PipelineState, RenWeavePipeline
from .provider import ModelProfile
from .provider_presets import PROVIDER_PRESETS, ProviderPreset
from .runtime import CancellationToken
from .usage import TokenBudget

__all__ = [
    "ModelProfile",
    "CancellationToken",
    "PROVIDER_PRESETS",
    "PipelineStage",
    "PipelineState",
    "ProjectIndex",
    "ProviderPreset",
    "RenWeavePipeline",
    "Scene",
    "TextUnit",
    "TokenBudget",
]
try:
    __version__ = package_version("renweave")
except PackageNotFoundError:
    __version__ = "development"
