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
    supports_json_parameter: bool
    reasoning_control: str
    accent: str
    mark: str
    description: str
    description_zh: str

    @property
    def base_url(self) -> str:
        return self.base_urls[0] if self.base_urls else ""

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
        supports_json_parameter=True,
        reasoning_control="effort",
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
        supports_json_parameter=True,
        reasoning_control="none",
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
        supports_json_parameter=False,
        reasoning_control="none",
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
        supports_json_parameter=True,
        reasoning_control="deepseek",
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
        supports_json_parameter=False,
        reasoning_control="none",
        accent="#F05A28",
        mark="M",
        description="MiniMax international or mainland China OpenAI-compatible API.",
        description_zh="MiniMax 国际站或中国大陆 OpenAI 兼容接口。",
    ),
    ProviderPreset(
        id="alibaba",
        name="Alibaba Cloud Model Studio",
        name_zh="阿里云百炼",
        category="official",
        base_urls=(
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        ),
        api_key_env="DASHSCOPE_API_KEY",
        supports_json_parameter=True,
        reasoning_control="toggle",
        accent="#FF6A00",
        mark="ALI",
        description="Alibaba Cloud Model Studio OpenAI-compatible API for mainland China or international accounts.",
        description_zh="阿里云百炼中国大陆或国际账号的 OpenAI 兼容接口。",
    ),
    ProviderPreset(
        id="zhipu",
        name="Zhipu AI",
        name_zh="智谱清言",
        category="official",
        base_urls=("https://open.bigmodel.cn/api/paas/v4",),
        api_key_env="ZHIPU_API_KEY",
        supports_json_parameter=True,
        reasoning_control="zhipu",
        accent="#2563EB",
        mark="ZP",
        description="Zhipu AI's official BigModel OpenAI-compatible API.",
        description_zh="智谱 BigModel 官方 OpenAI 兼容接口。",
    ),
    ProviderPreset(
        id="moonshot",
        name="Moonshot AI",
        name_zh="月之暗面",
        category="official",
        base_urls=("https://api.moonshot.cn/v1",),
        api_key_env="MOONSHOT_API_KEY",
        supports_json_parameter=True,
        reasoning_control="thinking",
        accent="#111827",
        mark="KIMI",
        description="Moonshot AI's official OpenAI-compatible API for Kimi models.",
        description_zh="月之暗面 Kimi 模型的官方 OpenAI 兼容接口。",
    ),
    ProviderPreset(
        id="siliconflow",
        name="SiliconFlow",
        name_zh="硅基流动",
        category="aggregator",
        base_urls=("https://api.siliconflow.cn/v1",),
        api_key_env="SILICONFLOW_API_KEY",
        supports_json_parameter=True,
        reasoning_control="budget",
        accent="#0F766E",
        mark="SF",
        description="SiliconFlow's model platform. Your live account catalog is loaded from the API.",
        description_zh="硅基流动模型平台；可用模型始终从当前账号实时获取。",
    ),
    ProviderPreset(
        id="openrouter",
        name="OpenRouter",
        name_zh="OpenRouter",
        category="aggregator",
        base_urls=("https://openrouter.ai/api/v1",),
        api_key_env="OPENROUTER_API_KEY",
        supports_json_parameter=False,
        reasoning_control="effort",
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
        supports_json_parameter=True,
        reasoning_control="effort",
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
