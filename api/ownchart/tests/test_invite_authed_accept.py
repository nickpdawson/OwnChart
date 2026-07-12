"""Batch A item 1 — POST /api/invitations/accept (authenticated join).

Two layers, matching the classifier-plus-route-contract pattern:

  1. Pure ``classify_authed_accept`` — the existing-user branch decision.
  2. Route contracts with ``_consume_invite`` patched (its own verification
     is already pinned by test_invite_accept_classifier) and the DB session
     faked: authed accept creates the membership + consumes the invite;
     double-accept / already-member / create-new-record / terminal-token /
     email-mismatch are each handled.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status

from ownchart.models.invitation import Invitation
from ownchart.models.membership import Membership
from ownchart.models.person_record import PersonRecord
from ownchart.routes import invitations as inv
from ownchart.routes.invitations import classify_authed_accept
from ownchart.tests.conftest import authed_client


# ---------------------------------------------------------------------------
# 1. Pure classifier


def test_classify_ok_for_existing_record_non_member():
    assert classify_authed_accept(
        create_new_record=False, already_active_member=False,
    ) == "ok"


def test_classify_new_record_invite_is_wrong_shape_for_existing_user():
    assert classify_authed_accept(
        create_new_record=True, already_active_member=False,
    ) == "new_record_for_existing_user"


def test_classify_already_member_conflicts():
    assert classify_authed_accept(
        create_new_record=False, already_active_member=True,
    ) == "already_member"


def test_classify_new_record_takes_priority_over_member_flag():
    # A create-new-record invite is rejected regardless of the (irrelevant)
    # membership flag — there's no target record to be a member of.
    assert classify_authed_accept(
        create_new_record=True, already_active_member=True,
    ) == "new_record_for_existing_user"


# ---------------------------------------------------------------------------
# 2. Route contract — fakes


class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, get_map=None, execute_rows=None):
        self._get_map = get_map or {}
        self._execute_rows = execute_rows or []
        self.added = []
        self.commits = 0

    async def execute(self, *a, **k):
        return _FakeResult(self._execute_rows)

    async def get(self, model, pk):
        return self._get_map.get(pk)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


def _override_session(app, session):
    from ownchart.core.db import get_session

    async def _fake():
        yield session

    app.dependency_overrides[get_session] = _fake


def _existing_record_invite(*, target_id, role="caregiver", creator=None):
    now = datetime.now(timezone.utc)
    return Invitation(
        id=uuid.uuid4(),
        invited_email="b@example.com",
        target_person_record_id=target_id,
        create_new_record=False,
        proposed_record_name=None,
        role=role,
        token_hash="x",
        token_lookup_prefix="abcdef12",
        expires_at=now + timedelta(days=1),
        created_by_user_id=creator or uuid.uuid4(),
        created_at=now,
        accepted_at=None,
        accepted_by_user_id=None,
        revoked_at=None,
    )


def _record(rid, name="Mom"):
    return PersonRecord(
        id=rid,
        display_name=name,
        is_self=False,
        created_by_user_id=uuid.uuid4(),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _patch_consume(monkeypatch, invite=None, raises=None):
    async def _fake(db, *, raw_token, invitee_email):
        if raises is not None:
            raise raises
        return invite

    monkeypatch.setattr(inv, "_consume_invite", _fake)


# ---------------------------------------------------------------------------
# Happy path


def test_authed_accept_creates_membership_and_consumes_invite(app_fixture, monkeypatch):
    uid = uuid.uuid4()
    rid = uuid.uuid4()
    invite = _existing_record_invite(target_id=rid, role="caregiver")
    _patch_consume(monkeypatch, invite=invite)
    session = _FakeSession(get_map={rid: _record(rid, "Mom")}, execute_rows=[])
    _override_session(app_fixture, session)

    c = authed_client(app_fixture, user_id=uid, role="owner")
    r = c.post("/api/invitations/accept", json={"token": "a-real-looking-token"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {
        "person_record_id": str(rid),
        "role": "caregiver",
        "display_name": "Mom",
    }
    # Invite consumed atomically.
    assert invite.accepted_at is not None
    assert invite.accepted_by_user_id == uid
    assert session.commits == 1
    # A membership on the target record for THIS user, at the invite's role.
    mems = [o for o in session.added if isinstance(o, Membership)]
    assert len(mems) == 1
    assert mems[0].user_id == uid
    assert mems[0].person_record_id == rid
    assert mems[0].role == "caregiver"


def test_authed_accept_records_agreement_when_supplied(app_fixture, monkeypatch):
    from ownchart.models.agreement_acceptance import AgreementAcceptance

    uid = uuid.uuid4()
    rid = uuid.uuid4()
    _patch_consume(monkeypatch, invite=_existing_record_invite(target_id=rid))
    session = _FakeSession(get_map={rid: _record(rid)}, execute_rows=[])
    _override_session(app_fixture, session)

    c = authed_client(app_fixture, user_id=uid, role="owner")
    r = c.post(
        "/api/invitations/accept",
        json={
            "token": "a-real-looking-token",
            "agreement_version": "tester-agreement-2026-07-01",
            "consent_flags": {"phi_processing": True},
        },
    )
    assert r.status_code == 200, r.text
    accepts = [o for o in session.added if isinstance(o, AgreementAcceptance)]
    assert len(accepts) == 1
    assert accepts[0].context == "invite_accept"
    assert accepts[0].agreement_version == "tester-agreement-2026-07-01"


# ---------------------------------------------------------------------------
# Guards


def test_authed_accept_already_member_409_and_does_not_consume(app_fixture, monkeypatch):
    uid = uuid.uuid4()
    rid = uuid.uuid4()
    invite = _existing_record_invite(target_id=rid)
    _patch_consume(monkeypatch, invite=invite)
    # execute returns an existing membership row → caller is already a member.
    existing = Membership(
        id=uuid.uuid4(), user_id=uid, person_record_id=rid, role="viewer",
        created_at=datetime.now(timezone.utc),
    )
    session = _FakeSession(get_map={rid: _record(rid)}, execute_rows=[existing])
    _override_session(app_fixture, session)

    c = authed_client(app_fixture, user_id=uid, role="owner")
    r = c.post("/api/invitations/accept", json={"token": "a-real-looking-token"})

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "already_a_member"
    # Invite NOT burned; nothing committed.
    assert invite.accepted_at is None
    assert session.commits == 0


def test_authed_accept_create_new_record_invite_rejected_400(app_fixture, monkeypatch):
    uid = uuid.uuid4()
    now = datetime.now(timezone.utc)
    invite = Invitation(
        id=uuid.uuid4(),
        invited_email="b@example.com",
        target_person_record_id=None,
        create_new_record=True,
        proposed_record_name="New Person",
        role="owner",
        token_hash="x",
        token_lookup_prefix="abcdef12",
        expires_at=now + timedelta(days=1),
        created_by_user_id=uuid.uuid4(),
        created_at=now,
    )
    _patch_consume(monkeypatch, invite=invite)
    session = _FakeSession()
    _override_session(app_fixture, session)

    c = authed_client(app_fixture, user_id=uid, role="owner")
    r = c.post("/api/invitations/accept", json={"token": "a-real-looking-token"})

    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "invite_creates_new_record"
    # A genuine new user can still use the invite — not consumed.
    assert invite.accepted_at is None
    assert session.commits == 0


def test_authed_accept_terminal_token_propagates_410(app_fixture, monkeypatch):
    """Consumed/expired/revoked/unknown token → 410 from _consume_invite.
    Covers the double-accept case: the second accept sees accepted_at set,
    _consume_invite classifies it 'accepted', and the route returns 410."""
    _patch_consume(monkeypatch, raises=HTTPException(
        status_code=status.HTTP_410_GONE,
        detail={"code": "invitation_unavailable", "message": "gone"},
    ))
    session = _FakeSession()
    _override_session(app_fixture, session)

    c = authed_client(app_fixture, user_id=uuid.uuid4(), role="owner")
    r = c.post("/api/invitations/accept", json={"token": "used-token"})
    assert r.status_code == 410, r.text
    assert r.json()["detail"]["code"] == "invitation_unavailable"
    assert session.commits == 0


def test_authed_accept_email_mismatch_propagates_403(app_fixture, monkeypatch):
    _patch_consume(monkeypatch, raises=HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": "invite_email_mismatch", "message": "nope"},
    ))
    session = _FakeSession()
    _override_session(app_fixture, session)

    c = authed_client(app_fixture, user_id=uuid.uuid4(), role="owner")
    r = c.post("/api/invitations/accept", json={"token": "someone-elses-invite"})
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "invite_email_mismatch"
    assert session.commits == 0


def test_authed_accept_requires_authentication(app_fixture):
    from fastapi.testclient import TestClient

    from ownchart.core.device_auth import get_user_from_device_token_or_session

    async def _raise_401():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized",
        )

    app_fixture.dependency_overrides[
        get_user_from_device_token_or_session
    ] = _raise_401
    c = TestClient(app_fixture)
    r = c.post("/api/invitations/accept", json={"token": "whatever"})
    assert r.status_code == 401
