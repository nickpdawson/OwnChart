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


# Setting values that mean "no preference". The `ai.default_provider`
# setting stores "auto"; older callers pass None.
_NO_PREFERENCE: frozenset[str] = frozenset({"", "auto"})


def resolve(
    preferred: str | None = None,
    *,
    extra_configured: frozenset[str] = frozenset(),
) -> LlmProvider:
    """Pick the provider to call.

    `extra_configured` names providers the acting user holds their own
    API key for (BYOK). A provider counts as configured when the
    deployment has a key OR the user does; before 2026-11-10 only the
    deployment key counted, so Settings could show a user-only key as
    configured while resolution went elsewhere.

    An explicit `preferred` is binding: if it is unknown (`local`,
    `gemini`, a typo) or known but unconfigured, raise ProviderUnavailable.
    The 2026-11-10 audit showed `local` resolving to Anthropic through the
    old fall-through, which turned a privacy choice into a cloud call.

    With no preference (None or "auto"):
      1. anthropic if configured.
      2. openai if configured.
      3. local_echo (development stub, so the stack stays exercisable
         without a vendor credential).
    """
    def _configured(key: str, p: LlmProvider) -> bool:
        return p.is_configured() or key in extra_configured

    if preferred and preferred not in _NO_PREFERENCE:
        p = _PROVIDERS.get(preferred)
        if p is None:
            raise ProviderUnavailable(
                f"AI provider {preferred!r} is not available on this OwnChart "
                "instance. Choose a configured provider in Settings."
            )
        if not _configured(preferred, p):
            raise ProviderUnavailable(
                f"{p.label} is selected but has no API key configured. Add one "
                "in Settings or choose another provider."
            )
        return p
    for key in _FALLBACK_ORDER:
        p = _PROVIDERS.get(key)
        if p and _configured(key, p):
            return p
    # Last resort: echo provider so the rest of the stack stays
    # exercisable in environments without any vendor credential.
    return _PROVIDERS["local_echo"]
