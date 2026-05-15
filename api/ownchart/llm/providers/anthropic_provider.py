"""Anthropic provider — wraps the existing AsyncAnthropic SDK."""

from __future__ import annotations

import time
from typing import Any

from anthropic import AsyncAnthropic

from ...core.config import get_settings
from ...core.logger import get_logger
from .base import LlmProvider, LlmRequest, LlmResponse, ProviderUnavailable

log = get_logger("ownchart.llm.anthropic")
_settings = get_settings()
_client: AsyncAnthropic | None = None


def _get_client(api_key_override: str | None = None) -> AsyncAnthropic:
    # BYOK path: build a one-shot client with the user's decrypted key.
    # Don't cache — the override is per-call and the singleton must
    # stay bound to the deployment default key.
    if api_key_override:
        return AsyncAnthropic(api_key=api_key_override)
    global _client
    if _client is None:
        if not _settings.anthropic_api_key:
            raise ProviderUnavailable(
                "OWNCHART_ANTHROPIC_API_KEY is not set; cannot reach Anthropic.",
            )
        _client = AsyncAnthropic(
            api_key=_settings.anthropic_api_key.get_secret_value(),
        )
    return _client


class AnthropicProvider(LlmProvider):
    key = "anthropic"
    label = "Anthropic Claude"
    default_capabilities = {
        "vision": True,
        "long_context": True,
        "structured_outputs": True,
        "local": False,
    }

    def is_configured(self) -> bool:
        return bool(_settings.anthropic_api_key)

    async def call(self, request: LlmRequest) -> LlmResponse:
        client = _get_client(request.api_key_override)
        request_payload: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "system": [
                {
                    "type": "text",
                    "text": request.system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": request.messages,
        }
        if request.tools:
            # Stamp cache_control on the LAST tool so Anthropic
            # caches everything up to and including the tool schema.
            # EI ships a ~2k-token tool definition that's identical
            # across calls; without this, every call paid full
            # input price for the schema. 2026-05-14 cost audit
            # showed cache_read at $0.35 vs input_no_cache at $6.62 —
            # caching was barely being used. This is the fix.
            tools = [dict(t) for t in request.tools]
            if tools:
                last = dict(tools[-1])
                last["cache_control"] = {"type": "ephemeral"}
                tools[-1] = last
            request_payload["tools"] = tools
            if request.tool_choice:
                request_payload["tool_choice"] = {
                    "type": "tool",
                    "name": request.tool_choice,
                }

        started = time.monotonic()
        resp = await client.messages.create(**request_payload)
        latency_ms = int((time.monotonic() - started) * 1000)

        tool_input: dict[str, Any] | None = None
        raw_text: str | None = None
        for block in resp.content:
            if block.type == "tool_use" and (
                request.tool_choice is None or block.name == request.tool_choice
            ):
                tool_input = dict(block.input)
            elif block.type == "text":
                raw_text = (raw_text or "") + block.text

        # Surface cache hits/misses in usage so we can audit cost
        # impact from the model_runs table. Anthropic's response has
        # cache_read_input_tokens + cache_creation_input_tokens; both
        # default to 0 when caching isn't in play.
        usage_out: dict[str, Any] = {
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
            "latency_ms": latency_ms,
        }
        cache_read = getattr(resp.usage, "cache_read_input_tokens", None)
        cache_create = getattr(resp.usage, "cache_creation_input_tokens", None)
        if cache_read is not None:
            usage_out["cache_read_input_tokens"] = cache_read
        if cache_create is not None:
            usage_out["cache_creation_input_tokens"] = cache_create

        return LlmResponse(
            raw_text=raw_text,
            tool_input=tool_input,
            stop_reason=resp.stop_reason,
            usage=usage_out,
            provider=self.key,
            model=request.model,
        )
