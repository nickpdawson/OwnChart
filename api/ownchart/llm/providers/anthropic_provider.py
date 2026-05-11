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


def _get_client() -> AsyncAnthropic:
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
        client = _get_client()
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
            request_payload["tools"] = request.tools
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

        return LlmResponse(
            raw_text=raw_text,
            tool_input=tool_input,
            stop_reason=resp.stop_reason,
            usage={
                "input_tokens": resp.usage.input_tokens,
                "output_tokens": resp.usage.output_tokens,
                "latency_ms": latency_ms,
            },
            provider=self.key,
            model=request.model,
        )
