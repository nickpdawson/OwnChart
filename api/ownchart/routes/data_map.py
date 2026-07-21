"""GET /api/account/data-map — the iOS "Privacy & Storage map".

Powers the P0 trust surface from the 2026-07-20 review ("Privacy and
storage map"): a plain-language accounting of what OwnChart holds on
the *server* for the active person record, plus the instance's AI
posture and truthful retention statements.

Design constraints (Wave 3.1, backend half):

  - **Metadata only, no PHI.** Every value is a count, a byte total, a
    timestamp, or a label the caller already owns. No source text, no
    fact labels, no conversation content leaves this endpoint.
  - **Record-scoped + membership-authorized.** Reuses ``get_auth_context``
    so the counts always reflect the caller's active record and the
    membership re-check runs on every request. iOS pins the record with
    ``X-OwnChart-Person-Record``.
  - **Demo-safe.** In demo mode conversation/episode counts are scoped
    to the visitor's ``oc_demo_session`` cookie exactly like the list
    endpoints — a visitor never sees another visitor's tallies. Seeded
    synthetic sources/facts are shared and counted for everyone.
  - **No consent gate.** This is metadata, not an AI call, so no 412.

The on-*device* half of the map (pending-queue ownership, local file
protection, "Erase from this device") is the iOS agent's job; this
endpoint owns the on-server + with-provider columns only.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.functions import coalesce

from ..core.app_config import get_app_config
from ..core.auth_context import AuthContext, get_auth_context
from ..core.config import get_settings
from ..core.db import get_session
from ..core.demo_session import get_demo_session_id
from ..core.logger import get_logger
from ..llm.budget import (
    ceiling_from_setting,
    month_to_date_spend_usd,
    next_month_start_utc,
)
from ..llm.providers import resolve
from ..models.conversation import Conversation
from ..models.episode import Episode
from ..models.extracted_fact import ExtractedFact
from ..models.healthkit_cursor import HealthKitCursor
from ..models.source_document import SourceDocument
from ..models.user import User
from ..settings.registry import effective as setting_effective

router = APIRouter()
log = get_logger("ownchart.routes.data_map")


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without a DB)


# HealthKit wearable samples land as ExtractedFact rows with this
# extraction_method (see routes/healthkit_sync.py::_extraction_method_for).
# Clinical HK records flow through the FHIR lane and medications/symptoms
# through patient_self_report, so "HealthKit samples" here means the raw
# wearable metric rows the phone pushed — the ones the map is about.
HEALTHKIT_EXTRACTION_METHOD = "native_healthkit"


def preferred_provider_key(raw_setting: object) -> str | None:
    """Map the ``ai.default_provider`` setting to a ``resolve()`` arg.

    The setting choices are ``[anthropic, openai, gemini, local, auto]``.
    ``auto`` (and unset) → ``None`` so the registry picks the first
    configured provider. ``local`` → the registry's ``local_echo`` key.
    Everything else passes through verbatim. Pure — no DB, no import
    side effects.
    """
    if raw_setting is None:
        return None
    val = str(raw_setting).strip().lower()
    if val in ("", "auto"):
        return None
    if val == "local":
        return "local_echo"
    return val


def retention_statements(*, demo_mode: bool, instance_name: str) -> list[str]:
    """Truthful, config-derived retention strings for the map footer.

    OwnChart's posture is "the patient owns the record" — nothing
    auto-expires except transient demo-visitor state. These strings must
    match what an operator can verify today; never overpromise privacy.
    """
    lines = [
        (
            f"{instance_name} keeps your record for as long as you keep "
            "your account. Your health data is never auto-expired or "
            "sold."
        ),
        (
            "Deleting your account permanently removes the records you "
            "solely own — sources, extracted facts, conversations, and "
            "episodes — from this server. A record co-owned with someone "
            "else survives for them."
        ),
        (
            "AI conversations run only when you ask and only after you "
            "grant consent. Nothing is sent to a model provider before "
            "then."
        ),
    ]
    if demo_mode:
        lines.append(
            "This is a public demo on synthetic data. Do not enter real "
            "medical information. Chats and saved events you create here "
            "are purged 24 hours after they're written."
        )
    return lines


def evidence_bytes_on_disk(
    storage_uris: list[str], *, evidence_root: Path,
) -> int:
    """Sum the on-disk size of the given content-addressed blob files.

    Defensive like the deletion GC: only stats paths that resolve under
    the evidence vault root, treats a missing/unreadable file as 0, and
    never raises. Caller passes DISTINCT storage_uris (blobs are deduped
    by content hash, so two sources sharing a hash share one file — don't
    double-count).
    """
    try:
        root = evidence_root.resolve()
    except Exception:  # noqa: BLE001
        return 0
    total = 0
    for uri in storage_uris:
        try:
            path = Path(uri).resolve()
            if root not in path.parents:
                continue
            total += path.stat().st_size
        except Exception:  # noqa: BLE001
            # File gone, permission error, bad path — count as 0.
            continue
    return total


# ---------------------------------------------------------------------------
# Response shapes


class SourceKindCount(BaseModel):
    kind: str
    count: int
    last_activity: datetime | None


class SourcesBlock(BaseModel):
    total: int
    by_kind: list[SourceKindCount]
    last_activity: datetime | None
    evidence_bytes_on_disk: int


class CountBlock(BaseModel):
    total: int
    last_activity: datetime | None


class HealthKitBlock(BaseModel):
    sample_count: int
    last_sample_at: datetime | None
    device_cursor_count: int
    last_synced_at: datetime | None


class ServerBlock(BaseModel):
    sources: SourcesBlock
    facts: CountBlock
    conversations: CountBlock
    episodes: CountBlock
    healthkit: HealthKitBlock


class RecordBlock(BaseModel):
    id: str
    display_name: str
    is_self: bool
    role: str


class AiPostureBlock(BaseModel):
    provider_key: str
    provider_label: str
    provider_configured: bool
    model: str
    phi_consent_granted: bool
    privacy_mode: str
    monthly_ceiling_configured: bool
    monthly_ceiling_usd: float | None
    month_to_date_usd: float
    resets_at: datetime


class DataMapResponse(BaseModel):
    record: RecordBlock
    server: ServerBlock
    ai: AiPostureBlock
    retention: list[str]
    demo_mode: bool


# ---------------------------------------------------------------------------
# Route


@router.get("/data-map", response_model=DataMapResponse)
async def get_data_map(
    request: Request,
    ctx: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_session),
) -> DataMapResponse:
    """The on-server + with-provider privacy/storage map for the active
    record. Metadata only; membership-authorized via AuthContext;
    demo-session scoped where the data is visitor-writable."""
    settings = get_settings()
    cfg = get_app_config()
    demo_mode = settings.demo_mode
    record_id = ctx.active_record_id
    user = ctx.user

    server = await _collect_server_block(db, request, record_id, user)
    ai = await _collect_ai_posture(db, user, cfg)

    return DataMapResponse(
        record=RecordBlock(
            id=str(record_id),
            display_name=ctx.active_person_record.display_name,
            is_self=bool(ctx.active_person_record.is_self),
            role=ctx.active_role,
        ),
        server=server,
        ai=ai,
        retention=retention_statements(
            demo_mode=demo_mode, instance_name=cfg.instance.name,
        ),
        demo_mode=demo_mode,
    )


async def _collect_server_block(
    db: AsyncSession,
    request: Request,
    record_id: uuid.UUID,
    user: User,
) -> ServerBlock:
    # --- Sources: total + last activity + per-kind + on-disk bytes ---
    src_total, src_last = (
        await db.execute(
            select(
                func.count(SourceDocument.id),
                func.max(SourceDocument.acquired_at),
            ).where(SourceDocument.person_record_id == record_id)
        )
    ).one()

    kind_rows = (
        await db.execute(
            select(
                SourceDocument.source_type,
                func.count(SourceDocument.id),
                func.max(SourceDocument.acquired_at),
            )
            .where(SourceDocument.person_record_id == record_id)
            .group_by(SourceDocument.source_type)
            .order_by(func.count(SourceDocument.id).desc())
        )
    ).all()
    by_kind = [
        SourceKindCount(kind=k, count=int(c or 0), last_activity=la)
        for (k, c, la) in kind_rows
    ]

    # Distinct storage_uris — content-addressed blobs dedupe by hash,
    # so don't double-count a file two sources share.
    uri_rows = (
        await db.execute(
            select(SourceDocument.storage_uri)
            .where(SourceDocument.person_record_id == record_id)
            .distinct()
        )
    ).scalars().all()
    ev_bytes = evidence_bytes_on_disk(
        [u for u in uri_rows if u],
        evidence_root=get_settings().data_dir / "evidence",
    )

    sources = SourcesBlock(
        total=int(src_total or 0),
        by_kind=by_kind,
        last_activity=src_last,
        evidence_bytes_on_disk=ev_bytes,
    )

    # --- Facts (record-scoped; seeded/shared in demo) ---
    fact_total, fact_last = (
        await db.execute(
            select(
                func.count(ExtractedFact.id),
                func.max(
                    coalesce(
                        ExtractedFact.date_end,
                        ExtractedFact.date_start,
                        ExtractedFact.created_at,
                    )
                ),
            ).where(ExtractedFact.person_record_id == record_id)
        )
    ).one()
    facts = CountBlock(total=int(fact_total or 0), last_activity=fact_last)

    # --- Conversations (demo-session scoped, mirrors the list route) ---
    conversations = await _count_conversations(db, request, record_id, user)

    # --- Episodes (demo-session scoped, mirrors the list route) ---
    episodes = await _count_episodes(db, request, record_id)

    # --- HealthKit: record-scoped sample facts + user device cursors ---
    hk_count, hk_last = (
        await db.execute(
            select(
                func.count(ExtractedFact.id),
                func.max(
                    coalesce(
                        ExtractedFact.date_end,
                        ExtractedFact.date_start,
                        ExtractedFact.created_at,
                    )
                ),
            )
            .where(ExtractedFact.person_record_id == record_id)
            .where(
                ExtractedFact.extraction_method == HEALTHKIT_EXTRACTION_METHOD
            )
        )
    ).one()
    cur_count, cur_synced = (
        await db.execute(
            select(
                func.count(HealthKitCursor.id),
                func.max(HealthKitCursor.updated_at),
            ).where(HealthKitCursor.user_id == user.id)
        )
    ).one()
    healthkit = HealthKitBlock(
        sample_count=int(hk_count or 0),
        last_sample_at=hk_last,
        device_cursor_count=int(cur_count or 0),
        last_synced_at=cur_synced,
    )

    return ServerBlock(
        sources=sources,
        facts=facts,
        conversations=conversations,
        episodes=episodes,
        healthkit=healthkit,
    )


async def _count_conversations(
    db: AsyncSession,
    request: Request,
    record_id: uuid.UUID,
    user: User,
) -> CountBlock:
    """Count the caller's conversations about this record.

    In demo mode, scope to the visitor's ``oc_demo_session`` cookie —
    identical to ``GET /api/conversations``. A visitor with no cookie
    yet (first request) sees zero, never the shared pool.
    """
    if get_settings().demo_mode:
        sid = get_demo_session_id(request)
        if not sid:
            return CountBlock(total=0, last_activity=None)
    stmt = (
        select(
            func.count(Conversation.id),
            func.max(
                coalesce(
                    Conversation.last_message_at, Conversation.created_at
                )
            ),
        )
        .where(Conversation.user_id == user.id)
        .where(Conversation.person_record_id == record_id)
    )
    if get_settings().demo_mode:
        stmt = stmt.where(
            text("conversations.scope->>'demo_session_id' = :dsid")
            .bindparams(dsid=get_demo_session_id(request))
        )
    total, last = (await db.execute(stmt)).one()
    return CountBlock(total=int(total or 0), last_activity=last)


async def _count_episodes(
    db: AsyncSession,
    request: Request,
    record_id: uuid.UUID,
) -> CountBlock:
    """Count non-merged episodes on this record.

    In demo mode, hide other visitors' saved events exactly like
    ``GET /api/episodes``: seeded events (created_by != 'user') are
    shared; a visitor's own saves are gated by
    ``payload->>demo_session_id``.
    """
    stmt = (
        select(
            func.count(Episode.id),
            func.max(coalesce(Episode.date_start, Episode.created_at)),
        )
        .where(Episode.person_record_id == record_id)
        .where(Episode.merged_into_id.is_(None))
    )
    if get_settings().demo_mode:
        sid = get_demo_session_id(request)
        if sid:
            stmt = stmt.where(
                text(
                    "(episodes.created_by != 'user' "
                    "OR episodes.payload->>'demo_session_id' = :dsid)"
                ).bindparams(dsid=sid)
            )
        else:
            stmt = stmt.where(text("episodes.created_by != 'user'"))
    total, last = (await db.execute(stmt)).one()
    return CountBlock(total=int(total or 0), last_activity=last)


async def _collect_ai_posture(
    db: AsyncSession, user: User, cfg,
) -> AiPostureBlock:
    """The instance's AI posture for THIS caller.

    Provider/model reflect what would actually answer; consent + budget
    are the caller's own. Reuses ``llm/budget.py`` for the month-to-date
    spend + ceiling so the number never drifts from what enforcement
    actually uses.
    """
    raw_provider = await setting_effective(db, user, "ai.default_provider")
    provider = resolve(preferred_provider_key(raw_provider))
    privacy_mode = await setting_effective(db, user, "ai.privacy_mode")

    ceiling = ceiling_from_setting(
        await setting_effective(db, user, "ai.monthly_spend_ceiling_usd")
    )
    now = datetime.now(timezone.utc)
    spent = await month_to_date_spend_usd(db, user.id, now=now)

    return AiPostureBlock(
        provider_key=provider.key,
        provider_label=provider.label,
        provider_configured=provider.is_configured(),
        model=cfg.llm.default_model,
        phi_consent_granted=bool(user.phi_consent_granted),
        privacy_mode=str(privacy_mode),
        monthly_ceiling_configured=ceiling is not None,
        monthly_ceiling_usd=ceiling,
        month_to_date_usd=round(float(spent), 6),
        resets_at=next_month_start_utc(now),
    )
