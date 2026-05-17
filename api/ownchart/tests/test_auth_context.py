"""AuthContext resolver tests (Beta 1 M02 Slice 1, batch 1).

Pure-function tests for `resolve_active_record_id` and the
header / session parsers. The DB-touching `get_auth_context`
dependency is exercised separately by integration tests; here we
pin the resolution algorithm and the PM A-5 error semantics
shape.

PM resolutions baked in:
  - A-5: revoked active record → 'denied' tag (caller maps to
    403 record_access_revoked). NEVER silent fall-through to next
    step.
  - A-5: zero memberships → resolver returns (None, 'none') →
    caller emits 403 no_memberships.
  - A-4: viewer is a valid role even though no viewer UI in M02.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from ownchart.core.auth_context import (
    HEADER_PERSON_RECORD,
    SESSION_KEY_ACTIVE_RECORD,
    _parse_record_id_from_header,
    _parse_session_active_record,
    resolve_active_record_id,
)


def _u() -> uuid.UUID:
    return uuid.uuid4()


# ---------------------------------------------------------------------------
# Header parsing


def test_header_parses_valid_uuid():
    target = _u()
    assert _parse_record_id_from_header(str(target)) == target


def test_header_returns_none_on_missing():
    assert _parse_record_id_from_header(None) is None
    assert _parse_record_id_from_header("") is None


def test_header_silently_ignores_malformed():
    """Server treats malformed header as 'no header'. Per the demo
    isolation pattern: don't leak validation rules."""
    assert _parse_record_id_from_header("not-a-uuid") is None
    assert _parse_record_id_from_header("12345") is None


def test_header_trims_whitespace():
    target = _u()
    assert _parse_record_id_from_header(f"  {target}  ") == target


# ---------------------------------------------------------------------------
# Session pin parsing


def test_session_pin_parses_valid_uuid_string():
    target = _u()
    payload = {SESSION_KEY_ACTIVE_RECORD: str(target)}
    assert _parse_session_active_record(payload) == target


def test_session_pin_returns_none_on_missing_key():
    assert _parse_session_active_record({}) is None
    assert _parse_session_active_record(None) is None
    assert _parse_session_active_record({"uid": "x"}) is None


def test_session_pin_returns_none_on_malformed():
    assert _parse_session_active_record({
        SESSION_KEY_ACTIVE_RECORD: "not-a-uuid",
    }) is None


# ---------------------------------------------------------------------------
# Resolution algorithm — the load-bearing piece


def test_header_wins_when_user_has_membership():
    """First step in the resolution: explicit header where the user
    has an active membership."""
    record_a = _u()
    record_b = _u()
    out, reason = resolve_active_record_id(
        header_record_id=record_b,
        session_pin=record_a,
        default_record_id=record_a,
        active_membership_record_ids=[record_a, record_b],
        user_explicitly_requested_record=True,
    )
    assert out == record_b
    assert reason == "header"


def test_header_to_revoked_record_returns_denied():
    """PM A-5: header points at a record the user does NOT have an
    active membership on. Resolver returns ('denied') so the
    caller emits 403 `record_access_revoked` — NEVER silent fall-
    through to the next step."""
    record_a = _u()
    revoked_record = _u()
    out, reason = resolve_active_record_id(
        header_record_id=revoked_record,
        session_pin=None,
        default_record_id=record_a,
        active_membership_record_ids=[record_a],
        user_explicitly_requested_record=True,
    )
    assert out is None
    assert reason == "denied"


def test_session_pin_to_revoked_record_also_denied():
    """Web session pin gets the same explicit-ask treatment as
    iOS header. If the pinned record was revoked, fail loud —
    don't silently switch to another record."""
    record_a = _u()
    revoked_record = _u()
    out, reason = resolve_active_record_id(
        header_record_id=None,
        session_pin=revoked_record,
        default_record_id=record_a,
        active_membership_record_ids=[record_a],
        user_explicitly_requested_record=True,
    )
    assert out is None
    assert reason == "denied"


def test_session_pin_used_when_user_has_membership():
    """Web: no header, session pin matches an active membership."""
    record_a = _u()
    record_b = _u()
    out, reason = resolve_active_record_id(
        header_record_id=None,
        session_pin=record_b,
        default_record_id=record_a,
        active_membership_record_ids=[record_a, record_b],
        user_explicitly_requested_record=True,
    )
    assert out == record_b
    assert reason == "session"


def test_default_record_used_when_no_header_or_session():
    """Third step: users.default_person_record_id. The user didn't
    ask for any specific record — fall back to their seeded default."""
    default_id = _u()
    other_id = _u()
    out, reason = resolve_active_record_id(
        header_record_id=None,
        session_pin=None,
        default_record_id=default_id,
        active_membership_record_ids=[default_id, other_id],
        user_explicitly_requested_record=False,
    )
    assert out == default_id
    assert reason == "default"


def test_default_skipped_when_no_membership():
    """Default is a soft fallback — if the user lost membership on
    their default record, skip to first-membership rather than
    deny. Distinct from explicit-ask behavior because the user
    didn't *ask* for the default; the server chose it."""
    default_id = _u()
    other_id = _u()
    out, reason = resolve_active_record_id(
        header_record_id=None,
        session_pin=None,
        default_record_id=default_id,
        active_membership_record_ids=[other_id],
        user_explicitly_requested_record=False,
    )
    assert out == other_id
    assert reason == "first_membership"


def test_first_membership_used_when_no_default():
    """Fresh user (no default) with at least one membership: pick
    the first by deterministic ordering."""
    first = _u()
    second = _u()
    out, reason = resolve_active_record_id(
        header_record_id=None,
        session_pin=None,
        default_record_id=None,
        active_membership_record_ids=[first, second],
        user_explicitly_requested_record=False,
    )
    assert out == first
    assert reason == "first_membership"


def test_zero_memberships_returns_none():
    """User has no active memberships. Caller emits 403
    `no_memberships`."""
    out, reason = resolve_active_record_id(
        header_record_id=None,
        session_pin=None,
        default_record_id=None,
        active_membership_record_ids=[],
        user_explicitly_requested_record=False,
    )
    assert out is None
    assert reason == "none"


def test_zero_memberships_with_header_still_no_memberships():
    """If the user has zero memberships AT ALL, the no_memberships
    error wins regardless of what they sent. The header points at
    a record they don't have, which COULD be 'denied' — but zero
    memberships is the more user-actionable error (their session is
    still valid; they just need to be added to a record)."""
    target = _u()
    out, reason = resolve_active_record_id(
        header_record_id=target,
        session_pin=None,
        default_record_id=None,
        active_membership_record_ids=[],
        user_explicitly_requested_record=True,
    )
    # The resolver returns 'denied' because the user explicitly
    # asked for a record they have no membership on. The DB-level
    # `get_auth_context` short-circuits BEFORE calling the resolver
    # when memberships are empty — emitting `no_memberships`. The
    # resolver itself doesn't have that signal here, so it returns
    # what's consistent with explicit-ask semantics. Caller handles.
    assert out is None
    assert reason == "denied"


def test_resolver_is_deterministic():
    """Same inputs → same output. No clock-dependence, no random
    shuffling."""
    target = _u()
    other = _u()
    for _ in range(5):
        out, reason = resolve_active_record_id(
            header_record_id=None,
            session_pin=None,
            default_record_id=None,
            active_membership_record_ids=[target, other],
            user_explicitly_requested_record=False,
        )
        assert out == target
        assert reason == "first_membership"


# ---------------------------------------------------------------------------
# Constants


def test_header_name_is_canonical_form():
    """Header name is lowercase (HTTP headers are case-insensitive
    but Starlette `request.headers.get` normalizes to lowercase)."""
    assert HEADER_PERSON_RECORD == "x-ownchart-person-record"


def test_session_key_constant():
    assert SESSION_KEY_ACTIVE_RECORD == "active_record_id"
