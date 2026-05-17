"""OAuth state-param signing tests (Beta 1 M02 Slice 1, PM A-3).

Pure-function round-trip + tamper-detection tests. SESSION_SECRET
is initialized from `infra/.env` at the configured default in
tests; no mocking required.
"""

from __future__ import annotations

import time
import uuid

import pytest

from ownchart.core.oauth_state import (
    DEFAULT_OAUTH_STATE_TTL_SECONDS,
    OAuthStateError,
    decode_oauth_state,
    generate_csrf_nonce,
    sign_oauth_state,
)


def test_round_trip_preserves_payload():
    """Sign then decode returns the same user_id + person_record_id +
    csrf_nonce + oauth_session_id."""
    user_id = uuid.uuid4()
    record_id = uuid.uuid4()
    sess_id = uuid.uuid4()
    nonce = "test-nonce-abc"
    token = sign_oauth_state(
        user_id=user_id,
        person_record_id=record_id,
        csrf_nonce=nonce,
        oauth_session_id=sess_id,
    )
    decoded = decode_oauth_state(token)
    assert decoded.user_id == user_id
    assert decoded.person_record_id == record_id
    assert decoded.csrf_nonce == nonce
    assert decoded.oauth_session_id == sess_id


def test_round_trip_without_session_id():
    """oauth_session_id is optional. When absent, decoded value is None."""
    token = sign_oauth_state(
        user_id=uuid.uuid4(),
        person_record_id=uuid.uuid4(),
        csrf_nonce="n",
    )
    decoded = decode_oauth_state(token)
    assert decoded.oauth_session_id is None


def test_default_csrf_nonce_is_generated():
    """When no nonce is supplied, a fresh random value is used."""
    token = sign_oauth_state(
        user_id=uuid.uuid4(),
        person_record_id=uuid.uuid4(),
    )
    decoded = decode_oauth_state(token)
    assert decoded.csrf_nonce
    assert len(decoded.csrf_nonce) >= 16


def test_distinct_signs_produce_distinct_tokens():
    """Different payloads (or different default nonces) produce
    different tokens. Sanity check the serializer isn't caching."""
    user_id = uuid.uuid4()
    record_id = uuid.uuid4()
    tokens = {
        sign_oauth_state(user_id=user_id, person_record_id=record_id)
        for _ in range(5)
    }
    # All 5 should be distinct because csrf_nonce defaults to random.
    assert len(tokens) == 5


def test_decode_rejects_empty_token():
    with pytest.raises(OAuthStateError, match="Missing state"):
        decode_oauth_state("")


def test_decode_rejects_bad_signature():
    """A token with the signature mangled must fail verification.
    itsdangerous tokens are `<payload>.<timestamp>.<signature>` — strip
    the signature suffix entirely to guarantee mismatch."""
    token = sign_oauth_state(
        user_id=uuid.uuid4(),
        person_record_id=uuid.uuid4(),
    )
    # Mangle by lopping off the last 6 chars (deep into the HMAC
    # signature). 6 is enough that no base64url padding can shift
    # the boundary back to the same signature.
    corrupted = token[:-6] + "ABCDEF"
    with pytest.raises(OAuthStateError, match="Bad state signature"):
        decode_oauth_state(corrupted)


def test_decode_rejects_unrelated_token():
    """A plausible-looking but unsigned string fails."""
    with pytest.raises(OAuthStateError, match="Bad state signature"):
        decode_oauth_state("notarealtokenbutlongenough.WhO9c3oP-Ux9")


def test_decode_rejects_expired_token():
    """`itsdangerous.URLSafeTimedSerializer.loads(max_age=...)`
    raises `SignatureExpired` for tokens older than the window.
    itsdangerous compares with integer-second granularity, so we
    sleep slightly longer than max_age + 1 to be unambiguous."""
    token = sign_oauth_state(
        user_id=uuid.uuid4(),
        person_record_id=uuid.uuid4(),
    )
    time.sleep(2.1)
    with pytest.raises(OAuthStateError, match="State expired"):
        decode_oauth_state(token, max_age_seconds=1)


def test_decode_accepts_within_ttl_window():
    """Tokens younger than max_age pass."""
    token = sign_oauth_state(
        user_id=uuid.uuid4(),
        person_record_id=uuid.uuid4(),
    )
    # 60 seconds is well within default 10 min.
    decoded = decode_oauth_state(token, max_age_seconds=60)
    assert decoded.user_id is not None


def test_generate_csrf_nonce_is_distinct():
    """No two nonces should collide."""
    seen = {generate_csrf_nonce() for _ in range(20)}
    assert len(seen) == 20


def test_default_ttl_is_ten_minutes():
    """Lock the default. If we change it, the test fails and forces
    a deliberate update."""
    assert DEFAULT_OAUTH_STATE_TTL_SECONDS == 600
