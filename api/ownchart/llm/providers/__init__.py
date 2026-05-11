"""Multi-provider LLM abstraction (docs/10).

OwnChart no longer assumes a single LLM vendor. This package provides:

  - `LlmProvider` ABC — common interface every provider implements.
  - `LlmRequest` / `LlmResponse` dataclasses — shared shape.
  - Per-provider modules: anthropic, openai, local_echo (testing).
  - `resolve(...)` — picks the provider+credential to call for a given
    user, prompt, and override.

Callers should NOT import provider modules directly. Use the registry
returned by `resolve()` so the provider is chosen based on user
settings + admin defaults + per-prompt overrides.
"""

from .base import (
    LlmProvider,
    LlmProviderError,
    LlmRequest,
    LlmResponse,
    ProviderUnavailable,
)
from .registry import available_providers, get_provider, resolve

__all__ = [
    "LlmProvider",
    "LlmProviderError",
    "LlmRequest",
    "LlmResponse",
    "ProviderUnavailable",
    "available_providers",
    "get_provider",
    "resolve",
]
