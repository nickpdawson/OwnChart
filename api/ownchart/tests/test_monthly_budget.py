"""Batch B — per-user monthly LLM spend ceiling enforcement.

``llm/budget.py``. Scripted-session tests (the pattern in test_blob_gc):
no Postgres. Pins:

  - ceiling interpretation: unset / 0 / negative → unlimited; positive → cap;
  - month window helpers (first-of-month UTC, reset date, Dec rollover);
  - enforcement: under budget proceeds; at/over budget raises the coded
    402; unset ceiling never blocks; the sum is scoped to the current
    UTC month so last month's spend doesn't count.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from ownchart.llm.budget import (
    BUDGET_EXCEEDED_CODE,
    MonthlyBudgetExceeded,
    ceiling_from_setting,
    enforce_monthly_ceiling,
    month_start_utc,
    month_to_date_spend_usd,
    next_month_start_utc,
)

# ---------------------------------------------------------------------------
# Scripted session


class _Result:
    def __init__(self, *, scalar=None):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar

    def scalar_one(self):
        return self._scalar


class _ScriptedSession:
    def __init__(self, results):
        self._results = list(results)
        self.executed = []

    async def execute(self, stmt, *a, **k):
        self.executed.append(stmt)
        return self._results.pop(0)


def _user():
    return SimpleNamespace(id=uuid.uuid4(), phi_consent_granted=True)


# ---------------------------------------------------------------------------
# 1. ceiling_from_setting


def test_ceiling_unlimited_cases():
    assert ceiling_from_setting(None) is None
    assert ceiling_from_setting(0) is None
    assert ceiling_from_setting(-5) is None
    assert ceiling_from_setting("nonsense") is None


def test_ceiling_positive_cases():
    assert ceiling_from_setting(25) == 25.0
    assert ceiling_from_setting("10") == 10.0
    assert ceiling_from_setting(3.5) == 3.5


# ---------------------------------------------------------------------------
# 2. month window helpers


def test_month_start_and_reset():
    now = datetime(2026, 7, 12, 14, 30, tzinfo=timezone.utc)
    assert month_start_utc(now) == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert next_month_start_utc(now) == datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_reset_rolls_over_december():
    now = datetime(2026, 12, 20, tzinfo=timezone.utc)
    assert next_month_start_utc(now) == datetime(2027, 1, 1, tzinfo=timezone.utc)


def test_month_start_normalizes_non_utc():
    from datetime import timedelta
    tz = timezone(timedelta(hours=-5))
    # 2026-07-01 02:00 -05:00 == 2026-07-01 07:00 UTC → still July.
    now = datetime(2026, 7, 1, 2, 0, tzinfo=tz)
    assert month_start_utc(now) == datetime(2026, 7, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 3. month_to_date_spend_usd


async def test_month_to_date_spend_returns_float():
    session = _ScriptedSession([_Result(scalar=12.5)])
    total = await month_to_date_spend_usd(session, uuid.uuid4())
    assert total == 12.5


async def test_month_to_date_spend_none_is_zero():
    session = _ScriptedSession([_Result(scalar=None)])
    total = await month_to_date_spend_usd(session, uuid.uuid4())
    assert total == 0.0


# ---------------------------------------------------------------------------
# 4. enforce_monthly_ceiling


async def test_unset_ceiling_never_blocks():
    # setting_effective → no UserSetting row → registry default (0) →
    # unlimited. Only the setting query runs; the sum is never issued.
    session = _ScriptedSession([_Result(scalar=None)])
    await enforce_monthly_ceiling(session, _user())  # must not raise
    assert len(session.executed) == 1  # summing was skipped


async def test_under_budget_proceeds():
    session = _ScriptedSession([
        _Result(scalar=SimpleNamespace(value=25)),   # ceiling = $25
        _Result(scalar=10.0),                         # spent $10
    ])
    await enforce_monthly_ceiling(session, _user())  # must not raise


async def test_over_budget_raises_coded_402():
    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    session = _ScriptedSession([
        _Result(scalar=SimpleNamespace(value=25)),   # ceiling = $25
        _Result(scalar=30.0),                         # spent $30
    ])
    with pytest.raises(MonthlyBudgetExceeded) as ei:
        await enforce_monthly_ceiling(session, _user(), now=now)
    exc = ei.value
    assert exc.status_code == 402
    assert exc.detail["code"] == BUDGET_EXCEEDED_CODE
    assert exc.detail["ceiling_usd"] == 25.0
    assert exc.detail["spent_usd"] == 30.0
    # Resets on the 1st of the following month.
    assert exc.detail["resets_at"].startswith("2026-08-01")


async def test_at_budget_exactly_is_refused():
    session = _ScriptedSession([
        _Result(scalar=SimpleNamespace(value=25)),
        _Result(scalar=25.0),                         # spent == ceiling
    ])
    with pytest.raises(MonthlyBudgetExceeded):
        await enforce_monthly_ceiling(session, _user())


async def test_zero_ceiling_is_unlimited():
    # A stored value of 0 means "no cap" — never blocked, sum not run.
    session = _ScriptedSession([_Result(scalar=SimpleNamespace(value=0))])
    await enforce_monthly_ceiling(session, _user())
    assert len(session.executed) == 1
