"""Email/SMTP workstream — invite delivery.

The create-invitation route must, when SMTP is configured, email the invite
AND still return the raw ``invite_url`` (the manual-copy fallback). A send
failure must NOT fail invite creation.

Uses the perimeter-test pattern: auth is overridden to an instance admin
(which skips the record-ownership query), and ``get_session`` is a fake so
the route's add/commit/get run without Postgres.
"""

from __future__ import annotations

import uuid

from ownchart.routes import invitations as inv
from ownchart.tests.conftest import _synthetic_record, authed_client


class _FakeSession:
    def __init__(self, get_map=None):
        self._get_map = get_map or {}
        self.added = []
        self.commits = 0

    async def execute(self, *a, **k):  # not reached on the admin path
        raise AssertionError("unexpected execute() on admin existing_record path")

    async def get(self, model, pk):
        return self._get_map.get(pk)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        pass


def _wire(app, record_id, session):
    from ownchart.core.db import get_session

    async def _fake():
        yield session

    app.dependency_overrides[get_session] = _fake


def _body(record_id):
    return {
        "invited_email": "Invitee@Example.com",
        "target_kind": "existing_record",
        "target_person_record_id": str(record_id),
        "role": "viewer",
        "expiry_preset": "7d",
    }


def test_invite_creation_sends_email_and_returns_url(app_fixture, monkeypatch):
    rid = uuid.uuid4()
    record = _synthetic_record(record_id=rid, display_name="Dad")
    session = _FakeSession(get_map={rid: record})
    _wire(app_fixture, rid, session)

    sent = []

    async def _fake_send(*, to, invite_url, role, expiry_note):
        sent.append((to, invite_url, role))
        return True

    monkeypatch.setattr(inv, "is_smtp_configured", lambda: True)
    monkeypatch.setattr(inv, "send_invitation_email", _fake_send)

    client = authed_client(app_fixture, is_instance_admin=True)
    r = client.post("/api/invitations", json=_body(rid))
    assert r.status_code == 201, r.text
    payload = r.json()
    # URL still returned for the manual-copy fallback.
    assert "invite_url" in payload and payload["invite_url"].endswith(
        payload["invite_url"].split("/invite/")[-1]
    )
    assert "/invite/" in payload["invite_url"]
    # Email dispatched to the normalized (lowercased) address.
    assert len(sent) == 1
    to, url, role = sent[0]
    assert to == "invitee@example.com"
    assert url == payload["invite_url"]
    assert role == "viewer"


def test_invite_send_failure_does_not_fail_creation(app_fixture, monkeypatch):
    rid = uuid.uuid4()
    record = _synthetic_record(record_id=rid, display_name="Mom")
    session = _FakeSession(get_map={rid: record})
    _wire(app_fixture, rid, session)

    async def _boom(*, to, invite_url, role, expiry_note):
        raise RuntimeError("smtp exploded")

    monkeypatch.setattr(inv, "is_smtp_configured", lambda: True)
    monkeypatch.setattr(inv, "send_invitation_email", _boom)

    client = authed_client(app_fixture, is_instance_admin=True)
    r = client.post("/api/invitations", json=_body(rid))
    # Creation still succeeds; the invite row was committed.
    assert r.status_code == 201, r.text
    assert "/invite/" in r.json()["invite_url"]
    assert session.commits == 1


def test_invite_no_email_when_smtp_unconfigured(app_fixture, monkeypatch):
    rid = uuid.uuid4()
    record = _synthetic_record(record_id=rid, display_name="Kid")
    session = _FakeSession(get_map={rid: record})
    _wire(app_fixture, rid, session)

    called = {"n": 0}

    async def _fake_send(*, to, invite_url, role, expiry_note):
        called["n"] += 1
        return True

    monkeypatch.setattr(inv, "is_smtp_configured", lambda: False)
    monkeypatch.setattr(inv, "send_invitation_email", _fake_send)

    client = authed_client(app_fixture, is_instance_admin=True)
    r = client.post("/api/invitations", json=_body(rid))
    assert r.status_code == 201, r.text
    # No send attempted; URL still present for manual copy.
    assert called["n"] == 0
    assert "/invite/" in r.json()["invite_url"]
