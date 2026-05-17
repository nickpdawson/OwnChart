"""Auto Export bearer-auth helper tests (Beta 1 M02 Slice 1, PM A-2).

Pure-function tests for the token-hash + bearer-parse helpers. The
DB-backed `authenticate_auto_export_push` flow is exercised by
integration tests separately.
"""

from __future__ import annotations

import hashlib

from ownchart.core.auto_export_auth import (
    generate_token,
    hash_token,
    parse_bearer_header,
    verify_token_hash,
)


# ---------------------------------------------------------------------------
# Token hashing


def test_hash_token_is_sha256_hex():
    """Stable algorithm so a token issued in one process verifies
    in another (worker / api / web). 64 hex chars."""
    out = hash_token("hello")
    expected = hashlib.sha256(b"hello").hexdigest()
    assert out == expected
    assert len(out) == 64


def test_hash_token_distinct_for_distinct_input():
    assert hash_token("abc") != hash_token("xyz")


def test_hash_token_deterministic():
    """Same input → same output across calls."""
    for _ in range(3):
        assert hash_token("repeatable") == hash_token("repeatable")


# ---------------------------------------------------------------------------
# Token verification (constant-time wrapper)


def test_verify_token_hash_matches_correct_raw():
    raw = "secret-token-value"
    stored = hash_token(raw)
    assert verify_token_hash(raw, stored) is True


def test_verify_token_hash_rejects_wrong_raw():
    stored = hash_token("real-token")
    assert verify_token_hash("not-the-real-token", stored) is False


def test_verify_token_hash_rejects_empty_inputs():
    """Defensive: empty raw or empty stored short-circuits to False
    without touching the comparison loop."""
    assert verify_token_hash("", "any") is False
    assert verify_token_hash("any", "") is False
    assert verify_token_hash("", "") is False


def test_verify_token_hash_rejects_when_stored_is_not_a_hash():
    """If the stored value is corrupted (not a valid sha256), it
    can't match any real token. Still constant-time."""
    assert verify_token_hash("anything", "not-actually-a-hash") is False


# ---------------------------------------------------------------------------
# Bearer header parsing


def test_parse_bearer_returns_token():
    assert parse_bearer_header("Bearer abc123") == "abc123"


def test_parse_bearer_handles_case_insensitive_scheme():
    """Per RFC, the auth scheme is case-insensitive."""
    assert parse_bearer_header("BEARER abc123") == "abc123"
    assert parse_bearer_header("bearer abc123") == "abc123"
    assert parse_bearer_header("BeArEr abc123") == "abc123"


def test_parse_bearer_trims_whitespace_in_token():
    """Trim leading/trailing whitespace on the token portion. Some
    clients add a space."""
    assert parse_bearer_header("Bearer   spaced-token  ") == "spaced-token"


def test_parse_bearer_returns_none_on_missing():
    assert parse_bearer_header(None) is None
    assert parse_bearer_header("") is None


def test_parse_bearer_returns_none_on_wrong_scheme():
    """Basic auth and other schemes are not Auto Export's path."""
    assert parse_bearer_header("Basic dXNlcjpwYXNz") is None
    assert parse_bearer_header("Token abc123") is None


def test_parse_bearer_returns_none_on_no_token_value():
    """`Bearer` with no token after it is malformed."""
    assert parse_bearer_header("Bearer") is None
    assert parse_bearer_header("Bearer ") is None


# ---------------------------------------------------------------------------
# Token generation


def test_generate_token_is_long_enough():
    """48 bytes of entropy → 64 char base64url string."""
    tok = generate_token()
    assert len(tok) >= 60
    # No padding chars in base64url.
    assert "=" not in tok
    # No whitespace.
    assert " " not in tok


def test_generate_token_is_random():
    """No two calls should produce the same token."""
    seen = {generate_token() for _ in range(10)}
    assert len(seen) == 10
