"""Invite token primitives — FU-MULTITENANT-ONBOARDING.

Pure-function tests for the token lifecycle:
  - `generate_invite_token` produces enough entropy and the
    expected url-safe shape.
  - `invite_lookup_prefix` is stable and small enough for an
    indexed DB lookup.
  - `hash_invite_token` + `verify_invite_token` round-trip.
  - Verifying a wrong token returns False (constant-time, no
    exception leak).
"""

from __future__ import annotations

from ownchart.core.security import (
    INVITE_LOOKUP_PREFIX_LEN,
    INVITE_TOKEN_BYTES,
    generate_invite_token,
    hash_invite_token,
    invite_lookup_prefix,
    verify_invite_token,
)


def test_generated_tokens_have_expected_entropy():
    """`secrets.token_urlsafe(32)` returns ~43 chars. We pin the
    range so a future bytes-bump doesn't quietly halve the entropy."""
    t = generate_invite_token()
    assert isinstance(t, str)
    # url-safe base64 of 32 bytes is 43 chars (no padding).
    assert len(t) >= 40
    assert INVITE_TOKEN_BYTES == 32


def test_generated_tokens_are_url_safe():
    """No '+' or '/' or '=' — should be drop-in for a URL path."""
    t = generate_invite_token()
    for ch in t:
        assert ch.isalnum() or ch in "-_"


def test_generated_tokens_are_unique():
    """Smoke: 1000 successive generations should have zero collisions
    at 256-bit entropy. (At true 256 bits the probability of any
    collision in N samples is N^2 / 2^257; for N=1000 it's
    ~5e-72 — astronomically unlikely.)"""
    seen = {generate_invite_token() for _ in range(1000)}
    assert len(seen) == 1000


def test_lookup_prefix_is_stable():
    """Same token in, same prefix out. The prefix is computed from
    the raw token chars; same input always yields same output."""
    t = generate_invite_token()
    assert invite_lookup_prefix(t) == invite_lookup_prefix(t)
    assert invite_lookup_prefix(t) == t[:INVITE_LOOKUP_PREFIX_LEN]


def test_lookup_prefix_length():
    t = generate_invite_token()
    assert len(invite_lookup_prefix(t)) == INVITE_LOOKUP_PREFIX_LEN


def test_hash_verify_round_trips():
    t = generate_invite_token()
    h = hash_invite_token(t)
    # argon2 hashes are long and start with $argon2id$.
    assert h.startswith("$argon2id$")
    assert verify_invite_token(t, h) is True


def test_verify_rejects_wrong_token():
    t1 = generate_invite_token()
    t2 = generate_invite_token()
    h1 = hash_invite_token(t1)
    assert verify_invite_token(t2, h1) is False


def test_verify_rejects_empty_token():
    """Belt-and-suspenders against a caller that fails to populate
    the body field — empty token never matches a real hash."""
    h = hash_invite_token(generate_invite_token())
    assert verify_invite_token("", h) is False


def test_hash_is_salted():
    """Two hashes of the same token differ — argon2 includes a per-call salt."""
    t = generate_invite_token()
    h1 = hash_invite_token(t)
    h2 = hash_invite_token(t)
    assert h1 != h2
    # Both still verify.
    assert verify_invite_token(t, h1)
    assert verify_invite_token(t, h2)
