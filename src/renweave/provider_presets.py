from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProviderPreset:
    id: str
    name: str
    name_zh: str
    category: str
    base_urls: tuple[str, ...]
    api_key_env: str
    default_models: tuple[str, ...]
    supports_json_parameter: bool
    accent: str
    mark: str
    description: str
    description_zh: str

    @property
    def base_url(self) -> str:
        return self.base_urls[0] if self.base_urls else ""

    @property
    def default_model(self) -> str:
        return self.default_models[0] if self.default_models else ""

    def display_name(self, locale: str) -> str:
        return self.name_zh if locale == "zh" else self.name

    def localized_description(self, locale: str) -> str:
        return self.description_zh if locale == "zh" else self.description


PROVIDER_PRESETS: tuple[ProviderPreset, ...] = (
    ProviderPreset(
        id="openai",
        name="OpenAI",
        name_zh="OpenAI",
        category="official",
        base_urls=("https://api.openai.com/v1",),
        api_key_env="OPENAI_API_KEY",
        default_models=(),
        supports_json_parameter=True,
        accent="#10A37F",
        mark="OA",
        description="Official OpenAI API. Models are loaded from your account.",
        description_zh="OpenAI 官方接口，将从你的账户获取可用模型。",
    ),
    ProviderPreset(
        id="google",
        name="Google Gemini",
        name_zh="Google Gemini",
        category="official",
        base_urls=("https://generativelanguage.googleapis.com/v1beta/openai",),
        api_key_env="GEMINI_API_KEY",
        default_models=(),
        supports_json_parameter=True,
        accent="#4285F4",
        mark="G",
        description="Gemini through Google's official OpenAI compatibility endpoint.",
        description_zh="通过 Google 官方 OpenAI 兼容接口调用 Gemini。",
    ),
    ProviderPreset(
        id="anthropic",
        name="Anthropic",
        name_zh="Anthropic",
        category="official",
        base_urls=("https://api.anthropic.com/v1",),
        api_key_env="ANTHROPIC_API_KEY",
        default_models=(),
        supports_json_parameter=False,
        accent="#D97757",
        mark="A",
        description="Claude through Anthropic's OpenAI SDK compatibility layer.",
        description_zh="通过 Anthropic 的 OpenAI SDK 兼容层调用 Claude。",
    ),
    ProviderPreset(
        id="deepseek",
        name="DeepSeek",
        name_zh="DeepSeek",
        category="official",
        base_urls=("https://api.deepseek.com", "https://api.deepseek.com/v1"),
        api_key_env="DEEPSEEK_API_KEY",
        default_models=("deepseek-v4-flash", "deepseek-v4-pro"),
        supports_json_parameter=True,
        accent="#4D6BFE",
        mark="DS",
        description="Official DeepSeek OpenAI-compatible API.",
        description_zh="DeepSeek 官方 OpenAI 兼容接口。",
    ),
    ProviderPreset(
        id="minimax",
        name="MiniMax",
        name_zh="MiniMax",
        category="official",
        base_urls=("https://api.minimax.io/v1", "https://api.minimaxi.com/v1"),
        api_key_env="MINIMAX_API_KEY",
        default_models=("MiniMax-M2.7", "MiniMax-M2.7-highspeed", "MiniMax-M2.5"),
        supports_json_parameter=False,
        accent="#F05A28",
        mark="M",
        description="MiniMax international or mainland China OpenAI-compatible API.",
        description_zh="MiniMax 国际站或中国大陆 OpenAI 兼容接口。",
    ),
    ProviderPreset(
        id="openrouter",
        name="OpenRouter",
        name_zh="OpenRouter",
        category="aggregator",
        base_urls=("https://openrouter.ai/api/v1",),
        api_key_env="OPENROUTER_API_KEY",
        default_models=(),
        supports_json_parameter=False,
        accent="#6D5EF8",
        mark="OR",
        description="One API for models from multiple vendors. The catalog may be large.",
        description_zh="通过一个接口接入多家厂商模型；模型列表可能较长。",
    ),
    ProviderPreset(
        id="custom",
        name="Custom endpoint",
        name_zh="自定义接口",
        category="custom",
        base_urls=("http://127.0.0.1:8000/v1",),
        api_key_env="RENWEAVE_API_KEY",
        default_models=(),
        supports_json_parameter=True,
        accent="#64748B",
        mark="API",
        description="Any third-party or local OpenAI-compatible endpoint.",
        description_zh="任意第三方或本地 OpenAI 兼容接口。",
    ),
)

PROVIDER_PRESETS_BY_ID = {preset.id: preset for preset in PROVIDER_PRESETS}


def get_provider_preset(preset_id: str) -> ProviderPreset:
    try:
        return PROVIDER_PRESETS_BY_ID[preset_id]
    except KeyError as exc:
        raise ValueError(f"Unknown provider preset: {preset_id}") from exc
