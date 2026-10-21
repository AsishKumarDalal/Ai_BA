"""Provider registry and resolution."""

from __future__ import annotations

from ..config import ModelConfig
from ..errors import ConfigError
from .base import Provider, ProviderRequest, ProviderResponse, ToolCall, Usage
from .anthropic import AnthropicProvider
from .openai_compat import OpenAICompatProvider

__all__ = [
    "AnthropicProvider",
    "OpenAICompatProvider",
    "Provider",
    "ProviderRequest",
    "ProviderResponse",
    "ToolCall",
    "Usage",
    "build_provider",
    "resolve_api_mode",
]


def resolve_api_mode(config: ModelConfig) -> str:
    """Resolve the wire format.

    Order: explicit ``api_mode`` -> provider name -> base-URL heuristic -> default.
    """
    if config.api_mode:
        mode = config.api_mode.lower()
        if mode in ("chat_completions", "anthropic_messages"):
            return mode
        raise ConfigError(f"unknown api_mode: {config.api_mode}")

    provider = (config.provider or "").lower()
    if provider in ("anthropic", "claude"):
        return "anthropic_messages"

    base_url = (config.base_url or "").lower()
    if "api.anthropic.com" in base_url:
        return "anthropic_messages"

    return "chat_completions"


def build_provider(config: ModelConfig, client=None) -> Provider:
    """Instantiate the provider for the given model configuration."""
    mode = resolve_api_mode(config)
    if mode == "anthropic_messages":
        return AnthropicProvider(config)
    return OpenAICompatProvider(config, client=client)
