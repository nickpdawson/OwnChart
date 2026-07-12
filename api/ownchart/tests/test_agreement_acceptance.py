"""Batch A item 2 — Agreement-acceptance recording (GDPR Art. 9 evidence).

Three layers:

  1. Pure helpers — ``normalize_consent_flags`` / ``build_agreement_acceptance``.
  2. ``record_agreement_acceptance`` — records IFF a version is supplied,
     otherwise a no-op that keeps the default self-host flow unchanged.
  3. ``_compose_me_response`` exposure — the latest acceptance surfaces on
     ``/me`` when present and is null otherwise.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from ownchart.core.agreements import (
    build_agreement_acceptance,
    normalize_consent_flags,
    record_agreement_acceptance,
)
from ownchart.models.agreement_acceptance import AgreementAcceptance
from ownchart.models.user import User
from ownchart.routes.auth import _compose_me_response


# ---------------------------------------------------------------------------
# 1. Pure helpers


def test_normalize_consent_flags_passthrough_dict():
    flags = {"phi_processing": True, "ai_processing": False}
    assert normalize_consent_flags(flags) == flags


def test_normalize_consent_flags_none_and_non_dict_collapse_to_empty():
    assert normalize_consent_flags(None) == {}
    assert normalize_consent_flags(["a", "b"]) == {}
    assert normalize_consent_flags("checked") == {}


def test_normalize_consent_flags_coerces_non_string_keys():
    out = normalize_consent_flags({1: True, "x": False})
    assert out == {"1": True, "x": False}


def test_build_agreement_acceptance_sets_fields_and_strips_version():
    uid = uuid.uuid4()
    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    row = build_agreement_acceptance(
        user_id=uid,
        agreement_version="  tester-agreement-2026-07-01  ",
        consent_flags={"phi": True},
        context="register",
        now=now,
    )
    assert row.user_id == uid
    assert row.agreement_version == "tester-agreement-2026-07-01"
    assert row.consent_flags == {"phi": True}
    assert row.context == "register"
    assert row.accepted_at == now


# ---------------------------------------------------------------------------
# 2. record_agreement_acceptance — records iff version present


class _FakeSession:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)


async def test_record_adds_row_when_version_present():
    db = _FakeSession()
    uid = uuid.uuid4()
    row = await record_agreement_acceptance(
        db,
        user_id=uid,
        agreement_version="v1",
        consent_flags={"phi_processing": True},
        context="register",
    )
    assert isinstance(row, AgreementAcceptance)
    assert row in db.added
    assert row.agreement_version == "v1"
    assert row.context == "register"


async def test_record_is_noop_when_version_absent():
    """The default self-host register/accept path sends no agreement —
    it must record nothing and not break the transaction."""
    db = _FakeSession()
    for missing in (None, "", "   "):
        row = await record_agreement_acceptance(
            db,
            user_id=uuid.uuid4(),
            agreement_version=missing,
            consent_flags={"phi": True},
            context="register",
        )
        assert row is None
    assert db.added == []


# ---------------------------------------------------------------------------
# 3. /me exposure via the pure composer


def _user() -> User:
    return User(
        id=uuid.uuid4(),
        email="alice@example.com",
        password_hash="x",
        phi_consent_granted=True,
        is_instance_admin=False,
        default_person_record_id=None,
    )


def test_me_exposes_latest_agreement_when_present():
    now = datetime(2026, 7, 12, 10, 30, tzinfo=timezone.utc)
    latest = AgreementAcceptance(
        user_id=uuid.uuid4(),
        agreement_version="tester-agreement-2026-07-01",
        consent_flags={"phi": True},
        context="register",
        accepted_at=now,
    )
    out = _compose_me_response(
        user=_user(), memberships=[], active_record_id=None,
        latest_agreement=latest,
    )
    assert out.latest_agreement_version == "tester-agreement-2026-07-01"
    assert out.latest_agreement_accepted_at == now


def test_me_agreement_fields_null_when_never_accepted():
    out = _compose_me_response(
        user=_user(), memberships=[], active_record_id=None,
    )
    assert out.latest_agreement_version is None
    assert out.latest_agreement_accepted_at is None
