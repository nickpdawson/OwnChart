"""Provider-neutral LLM request/response shape + base class."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any


class LlmProviderError(RuntimeError):
    pass


class ProviderUnavailable(LlmProviderError):
    """Raised when a provider can't be reached at all (no credentials,
    no endpoint, etc.). Callers can decide whether to fall back."""


@dataclass
class LlmRequest:
    """Provider-neutral call shape.

    `messages` follows the role/content shape every modern provider
    accepts. Tool definitions and a forced tool name (for structured
    output) are optional.

    The provider implementation translates these into its own SDK
    format.

    `api_key_override` lets a caller hand a per-user, decrypted API key
    to the provider for this single call. Used by the BYOK path so the
    user's own Anthropic / OpenAI key is billed instead of the
    deployment default. Plaintext only lives in memory for the call.
    """

    model: str
    system: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] = field(default_factory=list)
    tool_choice: str | None = None
    max_tokens: int = 4096
    api_key_override: str | None = None


@dataclass
class LlmResponse:
    raw_text: str | None
    tool_input: dict[str, Any] | None
    stop_reason: str | None
    usage: dict[str, Any]
    # The provider key the response came from ("anthropic" / "openai").
    provider: str
    # Echo of the model actually called — may differ from request.model
    # if the provider remapped a friendly alias.
    model: str


class LlmProvider(abc.ABC):
    """One vendor implementation.

    Subclasses are registered in `providers/registry.py`. The base
    class enforces the cross-provider contract: every call carries a
    rendered prompt, every response carries token usage.
    """

    #: short stable key — also the value used in user settings, audit
    #: rows, and conversation_messages.provider.
    key: str
    #: human label for the UI ("Anthropic Claude").
    label: str
    #: short capability advert; merged into the credential row's
    #: capabilities dict so the Settings UI can show "vision",
    #: "long context", etc.
    default_capabilities: dict[str, Any] = {}

    @abc.abstractmethod
    async def call(self, request: LlmRequest) -> LlmResponse:
        ...

    @abc.abstractmethod
    def is_configured(self) -> bool:
        """Return True iff this provider has a credential it can use.

        `resolve()` filters available providers by this.
        """
