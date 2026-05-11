"""Trivial echo provider for tests + local dev.

Returns the rendered user prompt as the assistant message. Useful
when you want to exercise the full conversation/citations plumbing
without spending tokens. Not for production.
"""

from __future__ import annotations

from .base import LlmProvider, LlmRequest, LlmResponse


class LocalEchoProvider(LlmProvider):
    key = "local_echo"
    label = "Local echo (testing)"
    default_capabilities = {
        "vision": False,
        "long_context": True,
        "structured_outputs": False,
        "local": True,
    }

    def is_configured(self) -> bool:
        return True

    async def call(self, request: LlmRequest) -> LlmResponse:
        # Find the last user text content and echo it back.
        echoed = ""
        for m in reversed(request.messages):
            c = m.get("content")
            if isinstance(c, str):
                echoed = c
                break
            if isinstance(c, list):
                for block in c:
                    if block.get("type") == "text":
                        echoed = block.get("text", "")
                        break
                if echoed:
                    break
        return LlmResponse(
            raw_text=f"[local_echo] {echoed[:512]}",
            tool_input=None,
            stop_reason="end_turn",
            usage={"input_tokens": 0, "output_tokens": 0, "latency_ms": 0},
            provider=self.key,
            model=request.model,
        )
