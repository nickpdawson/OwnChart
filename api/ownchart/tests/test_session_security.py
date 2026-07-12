"""HIGH-5: session cookie expiry + revocation.

The session cookie is now signed with a TIMED serializer (so a captured
cookie expires) and carries a ``stv`` (session_token_version) field
(so bumping the user's version instantly revokes every live cookie).

Pure-function — no DB, no HTTP. The stv re-check itself lives in
``core/device_auth.py`` (needs a DB); here we pin the two contracts it
depends on: the serializer rejects expired/tampered tokens, and the
payload embeds ``stv`` exactly when the version is non-zero.
"""

from __future__ import annotations

import uuid

from ownchart.core.security import sign_session, unsign_session
from ownchart.routes.auth import _compose_session_payload


# ---------------------------------------------------------------------------
# Timed serializer


def test_sign_unsign_roundtrip():
    payload = {"uid": str(uuid.uuid4()), "stv": 2}
    token = sign_session(payload)
    assert unsign_session(token, max_age=3600) == payload


def test_expired_token_rejected():
    """A negative max_age makes any token older-than-(-1)s expired —
    deterministically exercises the SignatureExpired → None path."""
    token = sign_session({"uid": "abc"})
    assert unsign_session(token, max_age=-1) is None


def test_fresh_token_within_max_age_ok():
    token = sign_session({"uid": "abc"})
    assert unsign_session(token, max_age=3600) == {"uid": "abc"}


def test_tampered_token_rejected():
    assert unsign_session("not.a.valid.token") is None
    assert unsign_session("") is None


def test_default_max_age_is_settings_window():
    """Called with no max_age, unsign_session should still verify a
    fresh token (the default is the configured session window, which is
    days — well in the future for a just-signed token)."""
    token = sign_session({"uid": "abc"})
    assert unsign_session(token) == {"uid": "abc"}


# ---------------------------------------------------------------------------
# stv embedding — the revocation lever


def test_payload_omits_stv_when_version_zero():
    """Version-0 users keep the legacy {uid} shape; a missing stv is
    read as 0 on decode, so this is equivalent to stv=0 without the
    key. Keeps pre-fix cookies valid until the first bump."""
    p = _compose_session_payload(user_id="u1", session_token_version=0)
    assert "stv" not in p
    assert p["uid"] == "u1"


def test_payload_includes_stv_after_bump():
    p = _compose_session_payload(user_id="u1", session_token_version=3)
    assert p["stv"] == 3


def test_payload_default_is_version_zero():
    """Legacy call sites that don't pass a version get the {uid} shape."""
    p = _compose_session_payload(user_id="u1")
    assert "stv" not in p


def test_old_cookie_without_stv_reads_as_version_zero():
    """Decode-side contract: a cookie signed before the fix has no
    `stv`; `payload.get('stv', 0)` must equal a version-0 user's
    column so those sessions stay valid until expiry or a bump."""
    token = sign_session(_compose_session_payload(user_id="u1"))
    payload = unsign_session(token)
    assert payload is not None
    assert payload.get("stv", 0) == 0
