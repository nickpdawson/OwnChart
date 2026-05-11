"""OpenAI provider — stub-but-callable when OPENAI_API_KEY is set.

V1 supports text + tool-use. Vision and Whisper integrations land
later. If the `openai` SDK isn't installed (it isn't in V1), the
provider reports unconfigured and the registry falls back to
Anthropic. The shape is here so admin / user BYOK rows in
`llm_provider_credentials` can target it on day one.
"""

from __future__ import annotations

import os
import time
from typing import Any

from ...core.logger import get_logger
from .base import LlmProvider, LlmRequest, LlmResponse, ProviderUnavailable

log = get_logger("ownchart.llm.openai")


def _api_key() -> str | None:
    # Read from env at call time so the deploy can add the key
    # without a restart. Future: read from llm_provider_credentials.
    return os.environ.get("OWNCHART_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")


class OpenAIProvider(LlmProvider):
    key = "openai"
    label = "OpenAI"
    default_capabilities = {
        "vision": True,
        "long_context": True,
        "structured_outputs": True,
        "local": False,
    }

    def is_configured(self) -> bool:
        if not _api_key():
            return False
        try:
            import openai  # noqa: F401
        except ImportError:
            return False
        return True

    async def call(self, request: LlmRequest) -> LlmResponse:
        if not self.is_configured():
            raise ProviderUnavailable(
                "OpenAI provider not configured: missing OWNCHART_OPENAI_API_KEY "
                "or the `openai` package is not installed."
            )
        # Late import keeps the package optional.
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=_api_key())
        messages = [{"role": "system", "content": request.system}, *_translate_messages(request.messages)]
        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "max_tokens": request.max_tokens,
        }
        if request.tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {}),
                    },
                }
                for t in request.tools
            ]
            if request.tool_choice:
                kwargs["tool_choice"] = {
                    "type": "function",
                    "function": {"name": request.tool_choice},
                }

        started = time.monotonic()
        resp = await client.chat.completions.create(**kwargs)
        latency_ms = int((time.monotonic() - started) * 1000)

        choice = resp.choices[0]
        tool_input: dict[str, Any] | None = None
        raw_text: str | None = choice.message.content or None
        if choice.message.tool_calls:
            tc = choice.message.tool_calls[0]
            try:
                import json
                tool_input = json.loads(tc.function.arguments)
            except Exception:  # noqa: BLE001
                tool_input = None
        usage = getattr(resp, "usage", None)
        usage_dict = {
            "input_tokens": getattr(usage, "prompt_tokens", None),
            "output_tokens": getattr(usage, "completion_tokens", None),
            "latency_ms": latency_ms,
        }
        return LlmResponse(
            raw_text=raw_text,
            tool_input=tool_input,
            stop_reason=choice.finish_reason,
            usage=usage_dict,
            provider=self.key,
            model=request.model,
        )


def _translate_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic-shape messages → OpenAI-shape.

    Anthropic accepts `content` as a list of blocks (text/image/tool_use).
    OpenAI expects either a string or a list of {"type": "text", "text"}
    or {"type": "image_url", ...} blocks. For V1 we only handle text +
    image blocks since that's what the existing prompts produce.
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            out.append({"role": m["role"], "content": c})
            continue
        if isinstance(c, list):
            new_blocks = []
            for block in c:
                btype = block.get("type")
                if btype == "text":
                    new_blocks.append({"type": "text", "text": block["text"]})
                elif btype == "image":
                    src = block.get("source", {})
                    if src.get("type") == "base64":
                        new_blocks.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{src['media_type']};base64,{src['data']}"
                            },
                        })
            out.append({"role": m["role"], "content": new_blocks})
    return out
