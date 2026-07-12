"""Batch B — /api/llm-providers/usage is scoped to the caller (MED-5).

Before this batch the endpoint returned EVERY user's model_runs to any
authed user. These tests pin:

  - both the row query and the month-to-date scope query filter on
    ``ModelRun.user_id == <caller>.id`` (the caller's own id, bound);
  - the response carries only the caller's scripted rows;
  - the month-to-date scope block computes total / ceiling / remaining /
    over_budget / per-model breakdown correctly.

The row-level exclusion of a second user's runs is enforced by that
WHERE clause in Postgres; the test runner has no Postgres, so we pin the
clause + its bound caller id structurally (the pattern used across the
perimeter suite).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from ownchart.routes.llm_providers import get_usage


class _Result:
    def __init__(self, *, rows=(), scalar=None):
        self._rows = list(rows)
        self._scalar = scalar

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def scalar_one_or_none(self):
        return self._scalar


class _CapturingSession:
    def __init__(self, results):
        self._results = list(results)
        self.executed = []

    async def execute(self, stmt, *a, **k):
        self.executed.append(stmt)
        return self._results.pop(0)


def _run_row(*, model="claude-haiku-4-5", cost=0.80):
    return SimpleNamespace(
        id=uuid.uuid4(),
        created_at=datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc),
        provider="anthropic",
        model=model,
        purpose="ask_query",
        prompt_version="ask_query@1",
        usage={"input_tokens": 1_000_000, "output_tokens": 0},
        error=None,
        estimated_cost_usd=cost,
    )


async def _call(user, session):
    return await get_usage(
        date_from=None, date_to=None, provider=None, model=None,
        purpose=None, billed_to=None, limit=500, format="json",
        user=user, db=session,
    )


async def test_usage_filters_on_caller_id():
    user = SimpleNamespace(id=uuid.uuid4())
    caller_rows = [_run_row(), _run_row()]
    session = _CapturingSession([
        _Result(rows=caller_rows),                       # main row query
        _Result(rows=caller_rows),                       # month-to-date query
        _Result(scalar=SimpleNamespace(value=25)),       # ceiling setting
    ])

    resp = await _call(user, session)

    # Only the caller's rows come back.
    assert {r.id for r in resp.rows} == {str(r.id) for r in caller_rows}
    assert resp.aggregate.total_runs == 2

    # MED-5: both data queries filter on the caller's own user_id.
    main_stmt, mtd_stmt = session.executed[0], session.executed[1]
    assert "model_runs.user_id" in str(main_stmt)
    assert "model_runs.user_id" in str(mtd_stmt)
    # The bound value is THIS caller's id (not some other user's).
    assert user.id in main_stmt.compile().params.values()
    assert user.id in mtd_stmt.compile().params.values()


async def test_scope_totals_ceiling_and_remaining():
    user = SimpleNamespace(id=uuid.uuid4())
    rows = [_run_row(cost=0.80), _run_row(cost=0.80)]
    session = _CapturingSession([
        _Result(rows=rows),
        _Result(rows=rows),
        _Result(scalar=SimpleNamespace(value=25)),       # $25 ceiling
    ])

    resp = await _call(user, session)
    scope = resp.scope
    assert scope.total_estimated_usd_cost == 1.60
    assert scope.ceiling_usd == 25.0
    assert scope.remaining_usd == 23.40
    assert scope.over_budget is False
    # Per-model breakdown.
    hk = [i for i in scope.per_model if i.key == "claude-haiku-4-5"]
    assert len(hk) == 1 and hk[0].runs == 2
    assert hk[0].estimated_usd_cost == 1.60
    # Per-purpose breakdown.
    assert any(i.key == "ask_query" and i.runs == 2 for i in scope.per_purpose)


async def test_scope_over_budget_flag_and_unlimited():
    user = SimpleNamespace(id=uuid.uuid4())
    rows = [_run_row(cost=0.80), _run_row(cost=0.80)]

    # Ceiling $1 < $1.60 spent → over budget, remaining clamped to 0.
    over = _CapturingSession([
        _Result(rows=rows), _Result(rows=rows),
        _Result(scalar=SimpleNamespace(value=1)),
    ])
    r1 = await _call(user, over)
    assert r1.scope.over_budget is True
    assert r1.scope.remaining_usd == 0.0

    # No ceiling row → unlimited: ceiling/remaining null, never over.
    unlimited = _CapturingSession([
        _Result(rows=rows), _Result(rows=rows),
        _Result(scalar=None),
    ])
    r2 = await _call(user, unlimited)
    assert r2.scope.ceiling_usd is None
    assert r2.scope.remaining_usd is None
    assert r2.scope.over_budget is False
