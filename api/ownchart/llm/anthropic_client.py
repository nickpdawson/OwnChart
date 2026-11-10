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
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.crypto import TokenCryptoError, decrypt_str
from ..core.logger import get_logger
from ..models.llm_provider_credential import LlmProviderCredential
from ..models.model_run import ModelRun
from ..models.user import User
from .budget import enforce_monthly_ceiling
from .egress import authorize_egress
from .pricing import estimate_cost_usd
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


async def _user_credential_providers(db: AsyncSession, user: User) -> frozenset[str]:
    """Providers the user holds a live api_key credential for."""
    rows = (await db.execute(
        select(LlmProviderCredential.provider)
        .where(LlmProviderCredential.user_id == user.id)
        .where(LlmProviderCredential.auth_kind == "api_key")
        .where(LlmProviderCredential.revoked_at.is_(None))
    )).scalars().all()
    return frozenset(str(r) for r in rows)


# Provider exception text is not PHI-free: a 400 from a vendor can echo
# the request, which carried record excerpts. Persist and log only the
# exception class, an HTTP status when present, and a coarse hint; the
# raw text goes to the debug log only.
_ERROR_HINTS: tuple[tuple[str, str], ...] = (
    ("rate", "rate_limited"),
    ("overload", "overloaded"),
    ("timeout", "timeout"),
    ("timed out", "timeout"),
    ("authentication", "authentication"),
    ("invalid_api_key", "authentication"),
    ("api key", "authentication"),
    ("permission", "permission"),
    ("not_found", "not_found"),
    ("context", "context_length"),
    ("too long", "context_length"),
    ("invalid_request", "invalid_request"),
    ("connect", "connection"),
)


def _safe_error(e: BaseException) -> str:
    parts = [type(e).__name__]
    status_code = getattr(e, "status_code", None)
    if isinstance(status_code, int):
        parts.append(f"http_{status_code}")
    msg = str(e).lower()
    for needle, hint in _ERROR_HINTS:
        if needle in msg:
            parts.append(hint)
            break
    return " ".join(parts)


async def _resolve_user_api_key(
    db: AsyncSession, user: User, provider_key: str,
) -> tuple[str | None, uuid.UUID | None]:
    """Look up the user's non-revoked api_key credential for `provider_key`.

    Returns (plaintext_key, credential_id) when one exists and decrypts
    cleanly. Returns (None, None) when the user has no row, has only a
    revoked row, or the row's secret can't be decrypted (logged + audit
    suppressed; caller falls back to the deployment default key).
    """
    row = (await db.execute(
        select(LlmProviderCredential)
        .where(LlmProviderCredential.user_id == user.id)
        .where(LlmProviderCredential.provider == provider_key)
        .where(LlmProviderCredential.auth_kind == "api_key")
        .where(LlmProviderCredential.revoked_at.is_(None))
        .order_by(LlmProviderCredential.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    if row is None or row.encrypted_secret is None:
        return None, None
    try:
        plaintext = decrypt_str(row.encrypted_secret)
    except TokenCryptoError as e:
        log.warning("byok_decrypt_failed",
                    provider=provider_key, credential_id=str(row.id),
                    error=str(e))
        return None, None
    if not plaintext:
        return None, None
    return plaintext, row.id


async def call_with_tool(
    db: AsyncSession,
    user: User,
    prompt: Prompt,
    *,
    user_vars: dict[str, Any],
    purpose: str,
    input_source_ids: list[uuid.UUID] | None = None,
    person_record_id: uuid.UUID | None = None,
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

    `person_record_id` (optional) is the active record at call time. It
    is stamped on the ModelRun for attribution; pass it where a record
    is in scope (route ctx / worker job / source row). Runs with no
    record (system/none) leave it null.

    Before any BILLED call, the acting user's monthly spend ceiling is
    enforced (`ai.monthly_spend_ceiling_usd`); over budget raises
    `MonthlyBudgetExceeded` (402). The free local_echo path is never
    checked. Every completed run is stamped with the acting `user_id`,
    `person_record_id`, and an `estimated_cost_usd` derived from usage.
    """
    # Policy first, before anything is rendered or resolved: disabled
    # account, no PHI consent, AI turned off, or no active membership on
    # the record in scope all stop here. Raises LlmEgressDenied. This is
    # the single egress boundary; callers may pre-check for a friendlier
    # UI but must not rely on their own checks.
    await authorize_egress(
        db, user, person_record_id=person_record_id, purpose=purpose,
    )

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

    # Fail-closed: an explicit provider that is unknown or unconfigured
    # raises ProviderUnavailable here instead of quietly selecting a
    # different recipient. Only `auto`/None walks the fallback chain.
    # A user's own key (BYOK) makes a provider configured for them even
    # when the deployment has no key, so retry resolution with the set
    # of providers they hold credentials for before giving up.
    try:
        selected = resolve_provider(provider)
    except ProviderUnavailable:
        byok_keys = await _user_credential_providers(db, user)
        if not byok_keys:
            raise
        selected = resolve_provider(provider, extra_configured=byok_keys)
    # `local_echo` is the free, no-external-call fallback. Every other
    # provider makes a real, billed request — that's the line the
    # monthly ceiling and cost stamping care about.
    billed = selected.key != "local_echo"

    # Monthly ceiling (Batch B). Enforced here — the single choke point
    # every LLM call funnels through, alongside BYOK resolution below —
    # so no route or worker can bypass it. Only the billed path is
    # checked; the free local_echo path is never blocked. Over budget
    # raises MonthlyBudgetExceeded (402) BEFORE any request leaves the
    # host, so no ModelRun and no spend are recorded for the refusal.
    if billed:
        await enforce_monthly_ceiling(db, user)

    # BYOK: prefer the user's stored, non-revoked api_key credential
    # for this provider. Falls back to the deployment default key
    # (env var) when the user has no row. Decryption only at call time,
    # plaintext never persisted.
    user_key, user_credential_id = await _resolve_user_api_key(
        db, user, selected.key,
    )

    request = LlmRequest(
        model=prompt.model,
        system=prompt.system,
        messages=[{"role": "user", "content": content}],
        tools=prompt.tools,
        tool_choice=tool_name,
        max_tokens=max_tokens,
        api_key_override=user_key,
    )
    input_hash = _hash_payload({
        "model": request.model, "system": request.system,
        "messages": request.messages, "tools": request.tools,
        "tool_choice": request.tool_choice,
    })
    run_id = uuid.uuid4()

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
        err = _safe_error(e)
        usage = {"latency_ms": int((time.monotonic() - started) * 1000)}
        log.warning("llm_provider_unavailable", purpose=purpose,
                    provider=selected.key, error=err)
        log.debug("llm_error_detail", purpose=purpose, detail=str(e)[:500])
    except Exception as e:  # noqa: BLE001
        err = _safe_error(e)
        usage = {"latency_ms": int((time.monotonic() - started) * 1000)}
        log.warning("llm_call_failed", purpose=purpose,
                    provider=selected.key, error=err)
        log.debug("llm_error_detail", purpose=purpose, detail=str(e)[:500])

    # Receipt: exactly which prompt text and which recipient. The
    # version tag alone cannot prove the text; the file hash can.
    usage["prompt_sha256"] = getattr(prompt, "content_sha256", None)
    usage["prompt_file"] = getattr(prompt, "source_file", None)
    usage["provider_key"] = selected.key
    usage["requested_model"] = prompt.model

    # Stamp which credential was billed so cost can be attributed
    # per-user without exposing the key. Audit-only; doesn't change
    # behavior.
    if user_credential_id is not None:
        usage["billed_credential_id"] = str(user_credential_id)
        usage["billed_to"] = "user_byok"
    elif billed:
        usage["billed_to"] = "deployment_default"
    else:
        usage["billed_to"] = "local_echo"

    # Cost attribution (Batch B). The free local_echo path is $0 even
    # though prompt.model is a priced model id — only real external
    # calls count toward the ceiling and /usage totals.
    cost = estimate_cost_usd(prompt.model, usage) if billed else 0.0

    run = ModelRun(
        id=run_id,
        provider=selected.key,
        model=prompt.model,
        purpose=purpose,
        user_id=user.id if user is not None else None,
        person_record_id=person_record_id,
        estimated_cost_usd=cost,
        input_source_ids=list(input_source_ids or []),
        input_hash=input_hash,
        output_hash=output_hash,
        prompt_version=prompt.version_tag,
        consent_state=user.phi_consent_granted,
        usage=usage,
        error=err,
    )
    db.add(run)
    # Touch last_used_at when a BYOK credential was actually used
    # (regardless of success — even a failed call indicates the key
    # was attempted, which is what Settings UI shows the user).
    if user_credential_id is not None:
        cred = await db.get(LlmProviderCredential, user_credential_id)
        if cred is not None and cred.revoked_at is None:
            cred.last_used_at = datetime.now(timezone.utc)
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
