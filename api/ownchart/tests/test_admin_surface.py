"""Instance-admin surface (Batch C).

Three layers:

  1. Pure guards + shape composers — no DB, no HTTP.
  2. Perimeter — every admin route 403s for a non-admin and does NOT
     403 for an admin (reaches the body).
  3. Route contracts with a queued fake session — list/detail shapes,
     the can't-disable-self and last-admin guards, disable actually
     bumps session_token_version, and the cross-user usage view returns
     every user.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from ownchart.models.agreement_acceptance import AgreementAcceptance
from ownchart.models.audit_event import AuditEvent
from ownchart.models.invitation import Invitation
from ownchart.models.membership import Membership
from ownchart.models.model_run import ModelRun
from ownchart.models.person_record import PersonRecord
from ownchart.models.user import User
from ownchart.routes import admin as admin_mod
from ownchart.routes.admin import (
    build_invite_out,
    build_user_row,
    ensure_admin_remains,
    ensure_not_self,
    summarize_runs,
)
from ownchart.tests.conftest import authed_client

NOW = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 1. Pure guards


def test_ensure_not_self_raises_on_self():
    uid = uuid.uuid4()
    with pytest.raises(Exception) as exc:
        ensure_not_self(uid, uid, action="disable")
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "cannot_target_self"


def test_ensure_not_self_ok_for_other():
    ensure_not_self(uuid.uuid4(), uuid.uuid4(), action="disable")  # no raise


def test_ensure_admin_remains_blocks_last_admin():
    with pytest.raises(Exception) as exc:
        ensure_admin_remains(target_is_admin=True, total_admin_count=1)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "last_admin"


def test_ensure_admin_remains_ok_with_two_admins():
    ensure_admin_remains(target_is_admin=True, total_admin_count=2)  # no raise


def test_ensure_admin_remains_ignores_non_admin_target():
    # Revoking admin from someone who isn't an admin never reaches here in
    # the route, but the guard is a no-op regardless.
    ensure_admin_remains(target_is_admin=False, total_admin_count=1)  # no raise


# ---------------------------------------------------------------------------
# 1b. Pure shape composers


def _user(**kw) -> User:
    base = dict(
        id=uuid.uuid4(),
        email="a@example.com",
        password_hash="x",
        phi_consent_granted=True,
        is_instance_admin=False,
        session_token_version=0,
        disabled=False,
        email_verified=True,
        created_at=NOW,
    )
    base.update(kw)
    return User(**base)


def test_build_user_row_shape():
    u = _user(email="nick@example.com", is_instance_admin=True)
    agr = AgreementAcceptance(
        id=uuid.uuid4(), user_id=u.id,
        agreement_version="tester-2026-07-01", consent_flags={},
        context="register", accepted_at=NOW,
    )
    row = build_user_row(u, membership_count=3, latest_agreement=agr, month_spend=1.23456789)
    assert row.email == "nick@example.com"
    assert row.is_instance_admin is True
    assert row.disabled is False
    assert row.membership_count == 3
    assert row.latest_agreement_version == "tester-2026-07-01"
    assert row.latest_agreement_accepted_at == NOW
    # rounded to 6 dp
    assert row.month_to_date_spend_usd == round(1.23456789, 6)


def test_build_user_row_no_agreement():
    row = build_user_row(_user(), membership_count=0, latest_agreement=None, month_spend=0.0)
    assert row.latest_agreement_version is None
    assert row.latest_agreement_accepted_at is None


def _run(model: str, purpose: str, stored) -> ModelRun:
    return ModelRun(
        id=uuid.uuid4(), provider="anthropic", model=model, purpose=purpose,
        input_source_ids=[], prompt_version="p@1", consent_state=True,
        estimated_cost_usd=stored, usage={},
    )


def test_summarize_runs_totals_and_breakdown():
    runs = [
        _run("claude-opus-4-7", "ask", 0.10),
        _run("claude-opus-4-7", "ask", 0.20),
        _run("claude-opus-4-7", "episode_intelligence", 0.05),
    ]
    total, per_model, per_purpose = summarize_runs(runs)
    assert total == round(0.35, 6)
    assert len(per_model) == 1
    assert per_model[0].key == "claude-opus-4-7"
    assert per_model[0].runs == 3
    # per-purpose sorted by cost desc → 'ask' (0.30) before EI (0.05)
    assert [p.key for p in per_purpose] == ["ask", "episode_intelligence"]
    assert per_purpose[0].estimated_usd_cost == round(0.30, 6)


def test_build_invite_out_status_classification():
    def _inv(**kw):
        base = dict(
            id=uuid.uuid4(), invited_email="b@example.com",
            target_person_record_id=None, create_new_record=True,
            proposed_record_name=None, role="owner", token_hash="x",
            token_lookup_prefix="abcd1234",
            expires_at=NOW + timedelta(days=1),
            created_by_user_id=uuid.uuid4(), created_at=NOW,
            accepted_at=None, accepted_by_user_id=None, revoked_at=None,
        )
        base.update(kw)
        return Invitation(**base)

    assert build_invite_out(_inv(), now=NOW).status == "active"
    assert build_invite_out(_inv(accepted_at=NOW), now=NOW).status == "accepted"
    assert build_invite_out(_inv(revoked_at=NOW), now=NOW).status == "revoked"
    assert build_invite_out(
        _inv(expires_at=NOW - timedelta(days=1)), now=NOW,
    ).status == "expired"


# ---------------------------------------------------------------------------
# Fake session (queued results + get map)


class _R:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalar_one(self):
        return self._rows[0] if self._rows else 0

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _QueueSession:
    def __init__(self, results=None, get_map=None):
        self._q = list(results or [])
        self._get = get_map or {}
        self.added = []
        self.commits = 0

    async def execute(self, *a, **k):
        return self._q.pop(0) if self._q else _R([])

    async def get(self, model, pk):
        return self._get.get(pk)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        return None


ADMIN_ID = uuid.uuid4()


def _admin_client(app, session):
    from ownchart.core.db import get_session

    async def _fake():
        yield session

    app.dependency_overrides[get_session] = _fake
    return authed_client(app, user_id=ADMIN_ID, is_instance_admin=True)


def _nonadmin_client(app, session):
    from ownchart.core.db import get_session

    async def _fake():
        yield session

    app.dependency_overrides[get_session] = _fake
    return authed_client(app, is_instance_admin=False)


# ---------------------------------------------------------------------------
# 2. Perimeter — 403 for non-admins across the whole surface


_ADMIN_ROUTES = [
    ("GET", "/api/admin/users"),
    ("GET", f"/api/admin/users/{uuid.uuid4()}"),
    ("POST", f"/api/admin/users/{uuid.uuid4()}/disable"),
    ("POST", f"/api/admin/users/{uuid.uuid4()}/enable"),
    ("POST", f"/api/admin/users/{uuid.uuid4()}/make-admin"),
    ("POST", f"/api/admin/users/{uuid.uuid4()}/revoke-admin"),
    ("GET", "/api/admin/invitations"),
    ("POST", f"/api/admin/invitations/{uuid.uuid4()}/revoke"),
    ("GET", "/api/admin/usage"),
    ("GET", "/api/admin/overview"),
]


@pytest.mark.parametrize("method,path", _ADMIN_ROUTES)
def test_admin_routes_403_for_non_admin(app_fixture, method, path):
    c = _nonadmin_client(app_fixture, _QueueSession())
    r = c.request(method, path)
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "instance_admin_required"


# ---------------------------------------------------------------------------
# 3. Route contracts (admin)


def test_list_users_shape(app_fixture):
    u1 = _user(email="z@example.com")
    u2 = _user(email="a@example.com", is_instance_admin=True)
    agr = AgreementAcceptance(
        id=uuid.uuid4(), user_id=u2.id, agreement_version="t-1",
        consent_flags={}, context="register", accepted_at=NOW,
    )
    session = _QueueSession(results=[
        _R([2]),                        # total count
        _R([u1, u2]),                   # users page
        _R([(u1.id, 4.5)]),             # spend group rows
        _R([(u1.id, 2)]),               # membership counts
        _R([agr]),                      # agreements
    ])
    c = _admin_client(app_fixture, session)
    r = c.get("/api/admin/users")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2
    assert len(body["users"]) == 2
    by_email = {u["email"]: u for u in body["users"]}
    assert by_email["z@example.com"]["month_to_date_spend_usd"] == 4.5
    assert by_email["z@example.com"]["membership_count"] == 2
    assert by_email["a@example.com"]["is_instance_admin"] is True
    assert by_email["a@example.com"]["latest_agreement_version"] == "t-1"
    # a user with no spend/agreement rows defaults cleanly
    assert by_email["a@example.com"]["month_to_date_spend_usd"] == 0.0


def test_get_user_detail_shape(app_fixture):
    target = _user(email="mom@example.com")
    rec = PersonRecord(
        id=uuid.uuid4(), display_name="Mom", is_self=False,
        created_by_user_id=uuid.uuid4(), created_at=NOW, updated_at=NOW,
    )
    mem = Membership(
        id=uuid.uuid4(), user_id=target.id, person_record_id=rec.id,
        role="caregiver", created_at=NOW, revoked_at=None,
    )
    session = _QueueSession(
        get_map={target.id: target},
        results=[
            _R([(mem, rec)]),           # memberships joined to records
            _R([1]),                    # device token count
            _R([_run("claude-opus-4-7", "ask", 0.25)]),  # runs this month
            _R([]),                     # latest agreement (none)
        ],
    )
    c = _admin_client(app_fixture, session)
    r = c.get(f"/api/admin/users/{target.id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["email"] == "mom@example.com"
    assert body["device_token_count"] == 1
    assert body["membership_count"] == 1
    assert len(body["memberships"]) == 1
    assert body["memberships"][0]["display_name"] == "Mom"
    assert body["memberships"][0]["role"] == "caregiver"
    assert body["memberships"][0]["revoked"] is False
    assert body["per_model"][0]["key"] == "claude-opus-4-7"
    assert body["month_to_date_spend_usd"] == 0.25


def test_get_user_detail_404_for_unknown(app_fixture):
    session = _QueueSession(get_map={})
    c = _admin_client(app_fixture, session)
    r = c.get(f"/api/admin/users/{uuid.uuid4()}")
    assert r.status_code == 404


def test_disable_user_bumps_session_token_version(app_fixture):
    target = _user(email="t@example.com", disabled=False, session_token_version=3)
    session = _QueueSession(get_map={target.id: target})
    c = _admin_client(app_fixture, session)
    r = c.post(f"/api/admin/users/{target.id}/disable")
    assert r.status_code == 200, r.text
    assert r.json()["disabled"] is True
    assert target.disabled is True
    # stv bumped → all live cookies die
    assert target.session_token_version == 4
    assert session.commits == 1
    assert any(
        isinstance(o, AuditEvent) and o.event_type == "user_disabled"
        for o in session.added
    )


def test_cannot_disable_self(app_fixture):
    # The admin's own id is ADMIN_ID (see _admin_client).
    admin_user = _user(id=ADMIN_ID, is_instance_admin=True)
    session = _QueueSession(get_map={ADMIN_ID: admin_user})
    c = _admin_client(app_fixture, session)
    r = c.post(f"/api/admin/users/{ADMIN_ID}/disable")
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "cannot_target_self"
    assert session.commits == 0


def test_enable_user_does_not_bump_stv(app_fixture):
    target = _user(email="t@example.com", disabled=True, session_token_version=9)
    session = _QueueSession(get_map={target.id: target})
    c = _admin_client(app_fixture, session)
    r = c.post(f"/api/admin/users/{target.id}/enable")
    assert r.status_code == 200, r.text
    assert r.json()["disabled"] is False
    assert target.disabled is False
    assert target.session_token_version == 9  # unchanged
    assert session.commits == 1


def test_disable_is_idempotent_no_stv_churn(app_fixture):
    target = _user(disabled=True, session_token_version=2)
    session = _QueueSession(get_map={target.id: target})
    c = _admin_client(app_fixture, session)
    r = c.post(f"/api/admin/users/{target.id}/disable")
    assert r.status_code == 200
    assert target.session_token_version == 2  # already disabled → no bump
    assert session.commits == 0


def test_make_admin_grants(app_fixture):
    target = _user(is_instance_admin=False)
    session = _QueueSession(get_map={target.id: target})
    c = _admin_client(app_fixture, session)
    r = c.post(f"/api/admin/users/{target.id}/make-admin")
    assert r.status_code == 200, r.text
    assert r.json()["is_instance_admin"] is True
    assert target.is_instance_admin is True
    assert session.commits == 1


def test_revoke_admin_blocked_for_last_admin(app_fixture):
    target = _user(is_instance_admin=True)
    session = _QueueSession(
        get_map={target.id: target},
        results=[_R([1])],  # _admin_count → 1
    )
    c = _admin_client(app_fixture, session)
    r = c.post(f"/api/admin/users/{target.id}/revoke-admin")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "last_admin"
    assert target.is_instance_admin is True  # untouched
    assert session.commits == 0


def test_revoke_admin_ok_when_two_admins(app_fixture):
    target = _user(is_instance_admin=True)
    session = _QueueSession(
        get_map={target.id: target},
        results=[_R([2])],  # _admin_count → 2
    )
    c = _admin_client(app_fixture, session)
    r = c.post(f"/api/admin/users/{target.id}/revoke-admin")
    assert r.status_code == 200, r.text
    assert r.json()["is_instance_admin"] is False
    assert target.is_instance_admin is False
    assert session.commits == 1


def test_revoke_admin_noop_when_not_admin(app_fixture):
    target = _user(is_instance_admin=False)
    session = _QueueSession(get_map={target.id: target})
    c = _admin_client(app_fixture, session)
    r = c.post(f"/api/admin/users/{target.id}/revoke-admin")
    assert r.status_code == 200
    assert r.json()["is_instance_admin"] is False
    assert session.commits == 0


def test_instance_usage_returns_all_users(app_fixture):
    a = _user(email="a@example.com")
    b = _user(email="b@example.com")
    session = _QueueSession(results=[
        _R([(a.id, 3.0)]),   # spend group — only a spent
        _R([a, b]),          # all users
    ])
    c = _admin_client(app_fixture, session)
    r = c.get("/api/admin/usage")
    assert r.status_code == 200, r.text
    body = r.json()
    emails = {u["email"] for u in body["per_user"]}
    assert emails == {"a@example.com", "b@example.com"}
    assert body["total_estimated_usd_cost"] == 3.0
    # zero-spend user still present at 0.0
    spend = {u["email"]: u["month_to_date_spend_usd"] for u in body["per_user"]}
    assert spend["b@example.com"] == 0.0
    assert spend["a@example.com"] == 3.0


def test_list_invitations_admin_sees_all(app_fixture):
    inv = Invitation(
        id=uuid.uuid4(), invited_email="c@example.com",
        target_person_record_id=None, create_new_record=True,
        proposed_record_name=None, role="owner", token_hash="x",
        token_lookup_prefix="abcd1234", expires_at=NOW + timedelta(days=1),
        created_by_user_id=uuid.uuid4(), created_at=NOW,
    )
    session = _QueueSession(results=[_R([inv])])
    c = _admin_client(app_fixture, session)
    r = c.get("/api/admin/invitations")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 1
    assert body[0]["invited_email"] == "c@example.com"


def test_revoke_invitation_sets_revoked(app_fixture):
    inv = Invitation(
        id=uuid.uuid4(), invited_email="c@example.com",
        target_person_record_id=None, create_new_record=True,
        proposed_record_name=None, role="owner", token_hash="x",
        token_lookup_prefix="abcd1234", expires_at=NOW + timedelta(days=1),
        created_by_user_id=uuid.uuid4(), created_at=NOW,
        accepted_at=None, revoked_at=None,
    )
    session = _QueueSession(get_map={inv.id: inv})
    c = _admin_client(app_fixture, session)
    r = c.post(f"/api/admin/invitations/{inv.id}/revoke")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "revoked"
    assert inv.revoked_at is not None
    assert session.commits == 1


def test_revoke_accepted_invitation_409(app_fixture):
    inv = Invitation(
        id=uuid.uuid4(), invited_email="c@example.com",
        target_person_record_id=None, create_new_record=True,
        proposed_record_name=None, role="owner", token_hash="x",
        token_lookup_prefix="abcd1234", expires_at=NOW + timedelta(days=1),
        created_by_user_id=uuid.uuid4(), created_at=NOW,
        accepted_at=NOW, revoked_at=None,
    )
    session = _QueueSession(get_map={inv.id: inv})
    c = _admin_client(app_fixture, session)
    r = c.post(f"/api/admin/invitations/{inv.id}/revoke")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "invitation_already_accepted"


def test_overview_counts(app_fixture):
    session = _QueueSession(results=[
        _R([10]),                     # user_count
        _R([2]),                      # admin_count
        _R([1]),                      # disabled_count
        _R([7]),                      # record_count
        _R([9]),                      # active_membership_count
        _R([3]),                      # pending_invitation_count
        _R([(uuid.uuid4(), 5.0)]),    # spend group
    ])
    c = _admin_client(app_fixture, session)
    r = c.get("/api/admin/overview")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user_count"] == 10
    assert body["admin_count"] == 2
    assert body["disabled_user_count"] == 1
    assert body["record_count"] == 7
    assert body["active_membership_count"] == 9
    assert body["pending_invitation_count"] == 3
    assert body["month_to_date_spend_usd"] == 5.0
