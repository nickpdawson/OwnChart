"""Public LLM call entrypoint — dispatches through the provider registry.

Historically this module owned the Anthropic SDK wrapper directly.
After the docs/10 multi-provider refactor (2026-05-11 PM), the
Anthropic specifics moved to `providers/anthropic_provider.py` and
this file is a thin orchestration layer:

  1. Resolve the provider (preferred → fallback chain).
  2. Build the provider-neutral `LlmRequest`.
  3. Call the provider; catch errors.
  4. Write the `ModelRun` audit row regardless of outcome.

Public signature (`call_with_tool`) is unchanged so existing callers
(relabel, sensemaking, dossier_brief, ask) keep working. The new
`provider` kwarg lets a caller pin to a specific provider; without
it the registry picks the default.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.logger import get_logger
from ..models.model_run import ModelRun
from ..models.user import User
from .prompts import Prompt
from .providers import (
    LlmRequest,
    ProviderUnavailable,
    resolve as resolve_provider,
)

log = get_logger("ownchart.llm")


@dataclass
class LlmCallResult:
    model_run_id: uuid.UUID
    tool_input: dict[str, Any] | None
    raw_text: str | None
    stop_reason: str | None
    usage: dict[str, Any]
    error: str | None
    # New 2026-05-11: provider/model actually used. Lets callers
    # persist these on conversation_messages without re-deriving.
    provider: str | None = None
    model: str | None = None


def _hash_payload(p: Any) -> str:
    return hashlib.sha256(json.dumps(p, sort_keys=True, default=str).encode()).hexdigest()


async def call_with_tool(
    db: AsyncSession,
    user: User,
    prompt: Prompt,
    *,
    user_vars: dict[str, Any],
    purpose: str,
    input_source_ids: list[uuid.UUID] | None = None,
    tool_name: str | None = None,
    image_b64: str | None = None,
    image_media_type: str | None = None,
    max_tokens: int = 4096,
    provider: str | None = None,
) -> LlmCallResult:
    """Call an LLM with optional tool-use enforcement and one image input.

    `tool_name` (when set) MUST match a tool defined in the prompt YAML;
    the provider will force structured emission via that tool.

    `provider` (optional) pins to a specific provider key. Defaults to
    the registry's resolution chain (anthropic → openai → local_echo).
    Per-user provider preference (from `ai.default_provider` setting)
    should be passed here by the caller — we don't peek at settings
    inside this module to keep it pure.
    """
    rendered_user = prompt.render(**user_vars)

    content: list[dict[str, Any]] = []
    if image_b64 and image_media_type:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": image_media_type,
                    "data": image_b64,
                },
            }
        )
    content.append({"type": "text", "text": rendered_user})

    request = LlmRequest(
        model=prompt.model,
        system=prompt.system,
        messages=[{"role": "user", "content": content}],
        tools=prompt.tools,
        tool_choice=tool_name,
        max_tokens=max_tokens,
    )
    input_hash = _hash_payload({
        "model": request.model, "system": request.system,
        "messages": request.messages, "tools": request.tools,
        "tool_choice": request.tool_choice,
    })
    run_id = uuid.uuid4()
    selected = resolve_provider(provider)

    err: str | None = None
    tool_input: dict[str, Any] | None = None
    raw_text: str | None = None
    stop_reason: str | None = None
    usage: dict[str, Any] = {}
    output_hash = None
    started = time.monotonic()
    try:
        resp = await selected.call(request)
        tool_input = resp.tool_input
        raw_text = resp.raw_text
        stop_reason = resp.stop_reason
        usage = dict(resp.usage or {})
        if stop_reason is not None:
            usage["stop_reason"] = stop_reason
        output_hash = _hash_payload({"tool_input": tool_input, "raw_text": raw_text})
    except ProviderUnavailable as e:
        err = f"ProviderUnavailable: {e}"
        usage = {"latency_ms": int((time.monotonic() - started) * 1000)}
        log.warning("llm_provider_unavailable", purpose=purpose,
                    provider=selected.key, error=err)
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
        usage = {"latency_ms": int((time.monotonic() - started) * 1000)}
        log.warning("llm_call_failed", purpose=purpose,
                    provider=selected.key, error=err)

    run = ModelRun(
        id=run_id,
        provider=selected.key,
        model=prompt.model,
        purpose=purpose,
        input_source_ids=list(input_source_ids or []),
        input_hash=input_hash,
        output_hash=output_hash,
        prompt_version=prompt.version_tag,
        consent_state=user.phi_consent_granted,
        usage=usage,
        error=err,
    )
    db.add(run)
    await db.commit()

    return LlmCallResult(
        model_run_id=run.id,
        tool_input=tool_input,
        raw_text=raw_text,
        stop_reason=stop_reason,
        usage=usage,
        error=err,
        provider=selected.key,
        model=prompt.model,
    )
