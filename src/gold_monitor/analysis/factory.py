"""Resolve model defaults and construct all analysis suppliers in one place."""

from ..config import Settings, settings as default_settings
from ..llm_config import (
    LLMConfig,
    LLMConfigManager,
    ModelProvider,
    get_llm_config_manager,
)
from .providers import AnthropicProvider, LLMProvider, MockLLMProvider, OpenAIProvider


def resolve_model(
    provider: ModelProvider | None, model: str | None = None
) -> str | None:
    if model:
        return model
    if provider is None:
        return None
    if provider.models:
        return provider.models[0]
    identity = f"{provider.name} {provider.base_url}".lower()
    for names, default in (
        (("deepseek",), "deepseek-v4-flash"),
        (("qwen", "通义", "dashscope"), "qwen-turbo"),
        (("moonshot", "kimi"), "moonshot-v1-8k"),
        (("zhipu", "glm"), "glm-4"),
    ):
        if any(name in identity for name in names):
            return default
    return None


def provider_from_config(
    config: LLMConfig, model: str | None, settings: Settings
) -> LLMProvider:
    active = config.get_active_provider()
    if active is None or active.id == "mock" or not active.api_key:
        return MockLLMProvider()
    selected = resolve_model(active, model or config.active_model)
    identity = f"{active.name} {active.base_url}".lower()
    if "anthropic" in identity or "claude" in identity:
        return AnthropicProvider(
            api_key=active.api_key,
            model=selected,
            settings=settings,
            base_url=active.base_url or None,
        )
    return OpenAIProvider(
        api_key=active.api_key,
        base_url=active.base_url,
        model=selected,
        settings=settings,
    )


def create_llm_provider(
    provider: str | None = None,
    *,
    manager: LLMConfigManager | None = None,
    settings: Settings | None = None,
    model: str | None = None,
) -> LLMProvider:
    """Explicit supplier wins; saved config then application defaults follow."""
    app_settings = settings or (manager.settings if manager else default_settings)
    if provider == "mock":
        return MockLLMProvider()
    if provider == "anthropic":
        return AnthropicProvider(settings=app_settings, model=model)
    if provider == "openai":
        return OpenAIProvider(settings=app_settings, model=model)
    if provider is not None:
        raise ValueError(f"不支持的 LLM 提供商: {provider}")
    config_manager = manager or (
        LLMConfigManager(settings=settings)
        if settings is not None
        else get_llm_config_manager()
    )
    app_settings = settings or config_manager.settings
    return provider_from_config(config_manager.reload_config(), model, app_settings)
