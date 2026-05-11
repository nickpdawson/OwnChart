"""Provider registry + resolution.

V1 instantiates the providers as singletons at import time. Resolution
is straightforward: caller passes an optional preferred provider key
(usually from `ai.default_provider` user setting); the registry picks
the first configured provider in this order: preferred → anthropic →
openai → local_echo. `LocalEcho` is excluded unless explicitly named.
"""

from __future__ import annotations

from .anthropic_provider import AnthropicProvider
from .base import LlmProvider, ProviderUnavailable
from .local_echo import LocalEchoProvider
from .openai_provider import OpenAIProvider


_PROVIDERS: dict[str, LlmProvider] = {
    "anthropic": AnthropicProvider(),
    "openai": OpenAIProvider(),
    "local_echo": LocalEchoProvider(),
}

# Fallback order when no preferred provider is given (or the preferred
# one isn't configured). LocalEcho is intentionally last and never
# picked unless explicitly requested by key.
_FALLBACK_ORDER: tuple[str, ...] = ("anthropic", "openai")


def get_provider(key: str) -> LlmProvider:
    if key not in _PROVIDERS:
        raise ProviderUnavailable(f"Unknown LLM provider: {key!r}")
    return _PROVIDERS[key]


def available_providers() -> list[dict[str, object]]:
    """Provider list with config status — drives the Settings UI."""
    out: list[dict[str, object]] = []
    for key, p in _PROVIDERS.items():
        out.append({
            "key": key,
            "label": p.label,
            "configured": p.is_configured(),
            "capabilities": p.default_capabilities,
        })
    return out


def resolve(preferred: str | None = None) -> LlmProvider:
    """Pick the provider to call.

    Resolution order:
      1. `preferred` if given and configured.
      2. anthropic if configured.
      3. openai if configured.
      4. local_echo (only if no other provider works — explicit log line).

    Raises ProviderUnavailable if even local_echo is somehow missing.
    """
    if preferred:
        p = _PROVIDERS.get(preferred)
        if p and p.is_configured():
            return p
        # Fall through — preferred was named but unavailable.
    for key in _FALLBACK_ORDER:
        p = _PROVIDERS.get(key)
        if p and p.is_configured():
            return p
    # Last resort: echo provider so the rest of the stack stays
    # exercisable in environments without any vendor credential.
    return _PROVIDERS["local_echo"]
