"""G-2: account deletion (App Store 5.1.1(v)).

Two layers:

  1. Pure-function tests for ``plan_record_dispositions`` — the cascade
     classifier that decides which PersonRecords are purged vs which
     survive (co-owned) and get a provenance transfer. No DB.

  2. Route-contract tests for ``DELETE /api/account`` — the password
     gate (wrong password → 403, right password → 204) and that the
     session cookie is cleared on success. The DB cascade itself
     (``delete_user_account``) is patched here; its live behavior is
     exercised against Postgres in the deploy smoke.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import patch

from ownchart.core.account_deletion import (
    RecordDisposition,
    plan_record_dispositions,
)
from ownchart.tests.conftest import authed_client

# ---------------------------------------------------------------------------
# 1. plan_record_dispositions — pure classifier


def test_solely_owned_record_is_purged():
    u = uuid.uuid4()
    rec = uuid.uuid4()
    plan = plan_record_dispositions(
        deleting_user_id=u,
        candidate_records={rec},
        active_owners_by_record={rec: {u}},
        created_by_by_record={rec: u},
    )
    assert plan.purge == [rec]
    assert plan.survive == []
    assert plan.reassign_created_by == {}


def test_co_owned_record_survives_without_reassign_when_not_creator():
    u = uuid.uuid4()
    other = uuid.uuid4()
    rec = uuid.uuid4()
    # Another user is an active owner; the record's creator is that other
    # user, so no provenance transfer is needed.
    plan = plan_record_dispositions(
        deleting_user_id=u,
        candidate_records={rec},
        active_owners_by_record={rec: {u, other}},
        created_by_by_record={rec: other},
    )
    assert plan.survive == [rec]
    assert plan.purge == []
    assert plan.reassign_created_by == {}


def test_co_owned_record_survives_and_reassigns_creator():
    u = uuid.uuid4()
    other = uuid.uuid4()
    rec = uuid.uuid4()
    # The deleting user CREATED the record but a co-owner remains — the
    # record must survive AND creation provenance must transfer so the
    # created_by RESTRICT FK doesn't block the user delete.
    plan = plan_record_dispositions(
        deleting_user_id=u,
        candidate_records={rec},
        active_owners_by_record={rec: {u, other}},
        created_by_by_record={rec: u},
    )
    assert plan.survive == [rec]
    assert plan.purge == []
    assert plan.reassign_created_by == {rec: other}


def test_reassign_target_is_deterministic():
    u = uuid.uuid4()
    o1 = uuid.UUID("00000000-0000-0000-0000-000000000001")
    o2 = uuid.UUID("00000000-0000-0000-0000-000000000002")
    rec = uuid.uuid4()
    plan = plan_record_dispositions(
        deleting_user_id=u,
        candidate_records={rec},
        active_owners_by_record={rec: {u, o2, o1}},
        created_by_by_record={rec: u},
    )
    # Lowest uuid-string wins — stable across runs.
    assert plan.reassign_created_by == {rec: o1}


def test_creator_with_no_other_owner_is_purged():
    u = uuid.uuid4()
    rec = uuid.uuid4()
    # User created it, holds no membership, and no one else owns it →
    # candidate via created_by, no other owner → purge.
    plan = plan_record_dispositions(
        deleting_user_id=u,
        candidate_records={rec},
        active_owners_by_record={rec: set()},
        created_by_by_record={rec: u},
    )
    assert plan.purge == [rec]
    assert plan.survive == []


def test_mixed_fleet_partitions_cleanly():
    u = uuid.uuid4()
    other = uuid.uuid4()
    solo = uuid.uuid4()
    shared = uuid.uuid4()
    plan = plan_record_dispositions(
        deleting_user_id=u,
        candidate_records={solo, shared},
        active_owners_by_record={solo: {u}, shared: {u, other}},
        created_by_by_record={solo: u, shared: u},
    )
    assert plan.purge == [solo]
    assert plan.survive == [shared]
    assert plan.reassign_created_by == {shared: other}


def test_empty_candidate_set_is_noop_plan():
    plan = plan_record_dispositions(
        deleting_user_id=uuid.uuid4(),
        candidate_records=set(),
        active_owners_by_record={},
        created_by_by_record={},
    )
    assert plan == RecordDisposition(purge=[], survive=[], reassign_created_by={})


# ---------------------------------------------------------------------------
# 2. DELETE /api/account — password gate + cookie clear


def _override_session(app):
    """Override get_session so the route test never touches Postgres
    (delete_user_account is patched, so the session is unused)."""
    from ownchart.core.db import get_session

    async def _fake_session():
        yield SimpleNamespace()

    app.dependency_overrides[get_session] = _fake_session


def test_delete_account_wrong_password_403(app_fixture):
    _override_session(app_fixture)
    called = {"n": 0}

    async def _fake_delete(db, user):
        called["n"] += 1
        return {}

    with patch("ownchart.routes.account.verify_password", lambda p, h: False), \
         patch("ownchart.routes.account.delete_user_account", new=_fake_delete):
        c = authed_client(app_fixture, role="owner")
        r = c.request("DELETE", "/api/account",
                      json={"current_password": "wrong"})
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "invalid_current_password"
    # The destructive cascade must NOT run on a failed password check.
    assert called["n"] == 0


def test_delete_account_correct_password_204_and_clears_cookie(app_fixture):
    _override_session(app_fixture)
    seen = {}

    async def _fake_delete(db, user):
        seen["user_id"] = user.id
        return {"purged_person_records": 1, "survived_person_records": 0,
                "reassigned_created_by": 0}

    uid = uuid.uuid4()
    with patch("ownchart.routes.account.verify_password", lambda p, h: True), \
         patch("ownchart.routes.account.delete_user_account", new=_fake_delete):
        c = authed_client(app_fixture, user_id=uid, role="owner")
        r = c.request("DELETE", "/api/account",
                      json={"current_password": "correct-horse"})
    assert r.status_code == 204, r.text
    # The cascade ran for the calling user.
    assert seen.get("user_id") == uid
    # The session cookie is cleared so the orphaned session doesn't linger.
    set_cookie = r.headers.get("set-cookie", "")
    assert "ownchart_session" in set_cookie
