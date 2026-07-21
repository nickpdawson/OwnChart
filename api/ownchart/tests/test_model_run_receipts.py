"""Route tests for GET /api/model-runs/receipts (Wave 3.1 — Data Route receipt).

Covers: membership-authz perimeter, STRICT user-scoping (the MED-5
lesson), demo-session isolation of the shared demo account, the receipt
shape, the `limit` cap, and the pure `receipt_status` helper. Same
fake-session idiom as test_invite_preview_post.py.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from ownchart.models.model_run import ModelRun
from ownchart.routes.model_runs import receipt_status
from ownchart.tests.conftest import authed_client, denied_client

_NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


class _Scalars:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


class _Result:
    def __init__(self, scalars):
        self._scalars = scalars

    def scalars(self):
        return _Scalars(self._scalars)


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows
        self.executed: list[str] = []

    async def execute(self, statement, *a, **k):
        self.executed.append(str(statement))
        return _Result(self._rows)

    async def get(self, *a, **k):
        return None


def _override_session(app, session):
    from ownchart.core.db import get_session

    async def _fake():
        yield session

    app.dependency_overrides[get_session] = _fake


def _run(
    *,
    purpose="ask",
    provider="anthropic",
    model="claude-opus-4-7",
    prompt_version="ask@1",
    cost=Decimal("0.0123"),
    person_record_id=None,
    error=None,
) -> ModelRun:
    r = ModelRun(
        id=uuid.uuid4(),
        provider=provider,
        model=model,
        purpose=purpose,
        prompt_version=prompt_version,
        estimated_cost_usd=cost,
        person_record_id=person_record_id,
        consent_state=True,
        input_source_ids=[],
        error=error,
    )
    r.created_at = _NOW
    return r


# ---------------------------------------------------------------------------
# Pure helper


def test_receipt_status():
    assert receipt_status(None) == "ok"
    assert receipt_status("") == "ok"
    assert receipt_status("RateLimit: 429") == "error"


# ---------------------------------------------------------------------------
# Perimeter


def test_receipts_propagates_403(app_fixture):
    c = denied_client(app_fixture, code="no_memberships")
    r = c.get("/api/model-runs/receipts")
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "no_memberships"


# ---------------------------------------------------------------------------
# Happy path shape + user scoping


def test_receipts_happy_shape_and_user_scoping(app_fixture):
    uid = uuid.uuid4()
    rid = uuid.uuid4()
    rows = [
        _run(purpose="ask", person_record_id=rid),
        _run(purpose="extract_fax_vision", cost=None, error="boom"),
    ]
    sess = _FakeSession(rows)
    _override_session(app_fixture, sess)
    c = authed_client(app_fixture, user_id=uid, active_record_id=rid)

    r = c.get("/api/model-runs/receipts?limit=10")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    assert body["limit"] == 10

    first = body["receipts"][0]
    assert set(first) == {
        "id", "created_at", "purpose", "provider", "model",
        "prompt_version", "estimated_cost_usd", "person_record_id",
        "status",
    }
    assert first["purpose"] == "ask"
    assert first["estimated_cost_usd"] == 0.0123
    assert first["person_record_id"] == str(rid)
    assert first["status"] == "ok"

    # Unpriced/errored run: null cost + error status, no raw error leaked.
    second = body["receipts"][1]
    assert second["estimated_cost_usd"] is None
    assert second["status"] == "error"
    assert "boom" not in r.text

    # MED-5: the query is strictly scoped to the calling user.
    assert sess.executed
    assert "model_runs.user_id" in sess.executed[0]


def test_receipts_limit_is_capped(app_fixture):
    sess = _FakeSession([])
    _override_session(app_fixture, sess)
    c = authed_client(app_fixture)
    # ge=1, le=200 — over the cap is a 422.
    assert c.get("/api/model-runs/receipts?limit=0").status_code == 422
    assert c.get("/api/model-runs/receipts?limit=500").status_code == 422
    assert c.get("/api/model-runs/receipts?limit=200").status_code == 200


# ---------------------------------------------------------------------------
# Demo isolation


def test_receipts_demo_without_cookie_is_empty(app_fixture, monkeypatch):
    from ownchart.core.config import get_settings

    monkeypatch.setattr(get_settings(), "demo_mode", True)
    sess = _FakeSession([_run()])
    _override_session(app_fixture, sess)
    c = authed_client(app_fixture)

    # No visitor cookie → never touch the shared demo user's runs.
    r = c.get("/api/model-runs/receipts")
    assert r.status_code == 200, r.text
    assert r.json()["receipts"] == []
    assert not sess.executed  # short-circuited before any query


def test_receipts_demo_with_cookie_scopes_to_visitor(app_fixture, monkeypatch):
    from ownchart.core.config import get_settings

    monkeypatch.setattr(get_settings(), "demo_mode", True)
    sess = _FakeSession([_run()])
    _override_session(app_fixture, sess)
    c = authed_client(app_fixture)

    r = c.get(
        "/api/model-runs/receipts",
        headers={"Cookie": "oc_demo_session=visitor-xyz"},
    )
    assert r.status_code == 200, r.text
    # The query narrows to this visitor's own conversations.
    assert sess.executed
    assert "demo_session_id" in sess.executed[0]
    assert "conversations" in sess.executed[0]
