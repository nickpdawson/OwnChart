"""POST /api/auth/set-active-record helpers (Beta 1 Section B).

Pure-function tests for the web record switcher:
  - `_parse_target_record_id` — best-effort UUID parse, treats
    malformed input as None (route maps to 404).
  - `_classify_switch_target` — three-way verdict for the
    switcher (ok / revoked / not_found) given the two queries the
    route has already executed.
  - `_compose_session_payload` — pins the cookie payload shape
    so the switcher updates `active_record_id` without breaking
    legacy `{uid}` sessions.

The DB-loading half (`_load_active_memberships` + the
`Membership` existence query) is a one-liner SQL whose user-id
filter is visible by code review; the load-bearing logic is
the classifier + payload composer.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from ownchart.core.auth_context import SESSION_KEY_ACTIVE_RECORD
from ownchart.models.membership import Membership
from ownchart.models.person_record import PersonRecord
from ownchart.routes.auth import (
    _classify_switch_target,
    _compose_session_payload,
    _parse_target_record_id,
)


def _u() -> uuid.UUID:
    return uuid.uuid4()


def _record(*, record_id: uuid.UUID, name: str = "Test") -> PersonRecord:
    return PersonRecord(
        id=record_id,
        display_name=name,
        is_self=False,
        created_by_user_id=_u(),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _membership(*, user_id: uuid.UUID, record_id: uuid.UUID) -> Membership:
    return Membership(
        id=_u(),
        user_id=user_id,
        person_record_id=record_id,
        role="caregiver",
        created_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# _parse_target_record_id


def test_parse_valid_uuid():
    rid = uuid.uuid4()
    assert _parse_target_record_id(str(rid)) == rid


def test_parse_uuid_with_whitespace():
    rid = uuid.uuid4()
    assert _parse_target_record_id(f"  {rid}  ") == rid


def test_parse_garbage_returns_none():
    assert _parse_target_record_id("not-a-uuid") is None


def test_parse_empty_returns_none():
    assert _parse_target_record_id("") is None


def test_parse_non_string_inputs_return_none():
    # Pydantic should reject before we get here, but be defensive.
    assert _parse_target_record_id(None) is None  # type: ignore[arg-type]
    assert _parse_target_record_id(123) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _classify_switch_target


def test_classify_ok_when_target_in_active_set():
    """Happy path: the target id appears in the user's active
    memberships → switch is allowed."""
    uid = _u()
    own_id, mom_id = _u(), _u()
    own = _record(record_id=own_id, name="Me")
    mom = _record(record_id=mom_id, name="Mom")
    memberships = [
        (_membership(user_id=uid, record_id=own_id), own),
        (_membership(user_id=uid, record_id=mom_id), mom),
    ]
    assert _classify_switch_target(
        target_id=mom_id,
        active_memberships=memberships,
        has_any_membership_row=True,
    ) == "ok"


def test_classify_revoked_when_no_active_but_row_exists():
    """User had a membership; it was revoked. The active-memberships
    join filtered it out, but the existence query found the
    (revoked) row → 403 record_access_revoked."""
    uid = _u()
    own_id, mom_id = _u(), _u()
    own = _record(record_id=own_id, name="Me")
    # Active set only contains own record; mom membership was revoked.
    memberships = [
        (_membership(user_id=uid, record_id=own_id), own),
    ]
    assert _classify_switch_target(
        target_id=mom_id,
        active_memberships=memberships,
        has_any_membership_row=True,  # revoked row found
    ) == "revoked"


def test_classify_not_found_when_no_row_at_all():
    """User has memberships on other records but never had access
    to the target → 404. Same shape returned whether the record
    doesn't exist or belongs only to other users — avoids leaking
    record existence to non-members."""
    uid = _u()
    own_id, mystery_id = _u(), _u()
    own = _record(record_id=own_id, name="Me")
    memberships = [
        (_membership(user_id=uid, record_id=own_id), own),
    ]
    assert _classify_switch_target(
        target_id=mystery_id,
        active_memberships=memberships,
        has_any_membership_row=False,
    ) == "not_found"


def test_classify_revoked_takes_priority_over_active_lookup_miss():
    """If a membership row exists for the target but isn't in the
    active set (e.g. revoked, or its record is disconnected), the
    response distinguishes it from a never-existed target."""
    uid = _u()
    target_id = _u()
    # No active memberships at all.
    assert _classify_switch_target(
        target_id=target_id,
        active_memberships=[],
        has_any_membership_row=True,
    ) == "revoked"


def test_classify_not_found_when_user_has_no_memberships_at_all():
    """Edge case: a user with zero memberships POSTs to switch.
    Without an existing row on the target they get 404."""
    target_id = _u()
    assert _classify_switch_target(
        target_id=target_id,
        active_memberships=[],
        has_any_membership_row=False,
    ) == "not_found"


def test_classify_cross_record_leak_guard():
    """The classifier consults ONLY the `active_memberships` list
    it was passed. The caller's responsibility is to filter that
    list by user_id; the classifier does not re-query or fabricate.

    This pins that the verdict cannot depend on records the user
    has no membership on — a structural cross-record-leak guard."""
    uid = _u()
    own_id = _u()
    other_user_record_id = _u()
    own = _record(record_id=own_id, name="Me")
    # Only the user's own record is in the active set.
    memberships = [
        (_membership(user_id=uid, record_id=own_id), own),
    ]
    # Asking to switch to someone else's record → not_found, even
    # though that record exists somewhere in the system.
    assert _classify_switch_target(
        target_id=other_user_record_id,
        active_memberships=memberships,
        has_any_membership_row=False,
    ) == "not_found"


# ---------------------------------------------------------------------------
# _compose_session_payload


def test_session_payload_uid_only_when_no_active_record():
    """Legacy shape — login/register/logout flows. Used to keep
    pre-M02 iOS device-pairing sessions backward compatible."""
    uid = str(_u())
    p = _compose_session_payload(user_id=uid)
    assert p == {"uid": uid}


def test_session_payload_includes_active_record_when_set():
    """Switcher path — cookie pins both uid AND active_record_id
    so subsequent requests resolve to the chosen record via the
    session step of `resolve_active_record_id`."""
    uid = str(_u())
    rid = str(_u())
    p = _compose_session_payload(user_id=uid, active_record_id=rid)
    assert p == {"uid": uid, SESSION_KEY_ACTIVE_RECORD: rid}


def test_session_payload_omits_active_record_when_none():
    """Passing None for active_record_id matches the legacy shape.
    Belt-and-suspenders — the route never calls it that way, but
    if `_set_session_cookie` is ever called without the kwarg the
    cookie payload stays clean."""
    uid = str(_u())
    p = _compose_session_payload(user_id=uid, active_record_id=None)
    assert SESSION_KEY_ACTIVE_RECORD not in p
    assert p["uid"] == uid
