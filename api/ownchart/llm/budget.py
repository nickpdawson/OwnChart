"""Per-user monthly LLM spend ceiling enforcement.

Batch B. ``model_runs`` now carries ``user_id`` + ``estimated_cost_usd``,
so a user's month-to-date external LLM spend is a single SUM. The
``ai.monthly_spend_ceiling_usd`` setting (already in the registry) was
unenforced; this module is the enforcement.

Semantics
---------
- The ceiling is the per-user setting ``ai.monthly_spend_ceiling_usd``.
  ``None``/unset **or** ``<= 0`` means *unlimited* — the enforcement is
  opt-in, so a self-hoster (or a BYOK Lapland tenant) who never sets a
  cap is never blocked. Setting a positive number makes the cap real
  for that user, BYOK or not.
- Spend is summed over the CURRENT calendar month in UTC (``[first of
  month 00:00Z, now]``) from ``estimated_cost_usd``. Last month's spend
  never counts against this month.
- Enforcement happens at the single billed-call choke point
  (``llm/anthropic_client.py``), alongside BYOK/consent resolution, so
  no caller can bypass it. The free ``local_echo`` / no-key path is
  never checked and never blocked.

Over budget → ``MonthlyBudgetExceeded`` (an HTTP 402 subclass with a
structured ``code``), so it flows through the existing HTTP exception
handler for route callers and is caught by workers' broad handlers.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.model_run import ModelRun
from ..models.user import User
from ..settings.registry import effective as setting_effective

BUDGET_EXCEEDED_CODE = "monthly_ai_budget_exceeded"


class MonthlyBudgetExceeded(HTTPException):
    """Raised before a billed LLM call when the user is at/over their cap.

    402 Payment Required — the request is well-formed and authorized, but
    the user's self-imposed monthly AI budget is exhausted.
    """

    def __init__(self, *, ceiling_usd: float, spent_usd: float, resets_at: datetime) -> None:
        self.ceiling_usd = ceiling_usd
        self.spent_usd = spent_usd
        self.resets_at = resets_at
        super().__init__(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "code": BUDGET_EXCEEDED_CODE,
                "message": (
                    f"Monthly AI budget reached: ${spent_usd:.2f} of "
                    f"${ceiling_usd:.2f} spent this month. New AI requests "
                    f"resume when the budget resets on "
                    f"{resets_at.date().isoformat()}. Raise or clear the "
                    f"limit in Settings → Privacy & AI to continue now."
                ),
                "ceiling_usd": round(ceiling_usd, 4),
                "spent_usd": round(spent_usd, 4),
                "resets_at": resets_at.isoformat(),
            },
        )


def ceiling_from_setting(value: object) -> float | None:
    """Interpret the raw ``ai.monthly_spend_ceiling_usd`` value.

    Returns the positive USD cap, or ``None`` for *unlimited* (unset,
    non-numeric, or ``<= 0``). Pure — no DB.
    """
    if value is None:
        return None
    try:
        cap = float(value)
    except (TypeError, ValueError):
        return None
    if cap <= 0:
        return None
    return cap


def month_start_utc(now: datetime) -> datetime:
    """First instant of ``now``'s calendar month, in UTC."""
    now = now.astimezone(timezone.utc)
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc)


def next_month_start_utc(now: datetime) -> datetime:
    """First instant of the month AFTER ``now`` (when the budget resets)."""
    now = now.astimezone(timezone.utc)
    if now.month == 12:
        return datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    return datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)


async def month_to_date_spend_usd(
    db: AsyncSession, user_id: uuid.UUID, *, now: datetime | None = None,
) -> float:
    """Sum ``estimated_cost_usd`` for a user across the current UTC month."""
    now = now or datetime.now(timezone.utc)
    start = month_start_utc(now)
    total = (await db.execute(
        select(func.coalesce(func.sum(ModelRun.estimated_cost_usd), 0))
        .where(ModelRun.user_id == user_id)
        .where(ModelRun.created_at >= start)
    )).scalar_one()
    return float(total or 0.0)


async def month_to_date_spend_by_user(
    db: AsyncSession, *, now: datetime | None = None,
) -> dict[uuid.UUID, float]:
    """Sum ``estimated_cost_usd`` per user across the current UTC month.

    The cross-user counterpart to ``month_to_date_spend_usd`` — one
    grouped query instead of N per-user sums, for the instance-admin
    usage view (Batch C). Rows whose ``user_id`` nulled out on account
    erasure are skipped (a deleted user has no admin row to attribute
    to). Returns a ``{user_id: usd}`` map; users with zero spend are
    simply absent.
    """
    now = now or datetime.now(timezone.utc)
    start = month_start_utc(now)
    rows = (await db.execute(
        select(
            ModelRun.user_id,
            func.coalesce(func.sum(ModelRun.estimated_cost_usd), 0),
        )
        .where(ModelRun.created_at >= start)
        .where(ModelRun.user_id.is_not(None))
        .group_by(ModelRun.user_id)
    )).all()
    return {uid: float(total or 0.0) for uid, total in rows}


async def enforce_monthly_ceiling(
    db: AsyncSession, user: User, *, now: datetime | None = None,
) -> None:
    """Refuse a billed LLM call when the user is at/over their monthly cap.

    No-op when the user has no ceiling set (unlimited). Call this only on
    the BILLED path — never for ``local_echo``.
    """
    if user is None:
        return
    raw = await setting_effective(db, user, "ai.monthly_spend_ceiling_usd")
    ceiling = ceiling_from_setting(raw)
    if ceiling is None:
        return  # unlimited
    now = now or datetime.now(timezone.utc)
    spent = await month_to_date_spend_usd(db, user.id, now=now)
    if spent >= ceiling:
        raise MonthlyBudgetExceeded(
            ceiling_usd=ceiling,
            spent_usd=spent,
            resets_at=next_month_start_utc(now),
        )
