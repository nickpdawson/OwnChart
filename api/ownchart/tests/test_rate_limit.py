"""HIGH-7: auth rate limiter.

The Lapland tenant launch gate. These tests pin the pure-function
window math and prove the in-process backend actually throttles the
Nth+1 attempt inside a window and resets on the next window.

Pure-function + in-process backend — no Redis, no HTTP. The prod path
uses the same limiter with a Redis backend; the throttle logic is
identical.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from ownchart.core import rate_limit as rl
from ownchart.core.rate_limit import (
    RateLimiter,
    counter_key,
    retry_after_seconds,
    window_start,
)


# ---------------------------------------------------------------------------
# Pure window math


def test_window_start_floors_to_window():
    assert window_start(1000.0, 300) == 900
    assert window_start(1200.0, 300) == 1200
    assert window_start(1499.9, 300) == 1200


def test_retry_after_is_time_until_window_rolls():
    # now sits 1s into a 300s window → 299s until reset.
    assert retry_after_seconds(901.0, 300) == 299
    # exactly at a boundary → a full window remains.
    assert retry_after_seconds(900.0, 300) == 300


def test_retry_after_never_zero():
    assert retry_after_seconds(1199.9999, 300) >= 1


def test_counter_key_stable_within_window_changes_across():
    k1 = counter_key("login", "ip:1.2.3.4", 905.0, 300)
    k2 = counter_key("login", "ip:1.2.3.4", 1100.0, 300)  # same window (900)
    k3 = counter_key("login", "ip:1.2.3.4", 1250.0, 300)  # next window (1200)
    assert k1 == k2
    assert k1 != k3


# ---------------------------------------------------------------------------
# In-process backend throttling (Redis disabled)


async def _fresh_limiter() -> RateLimiter:
    lim = RateLimiter()
    # Force the in-process backend so the test never touches Redis.
    lim._redis_disabled = True
    return lim


async def test_nth_plus_one_attempt_is_throttled():
    lim = await _fresh_limiter()
    now = 10_000.0
    verdicts = []
    for _ in range(6):
        verdicts.append(await lim.check(
            bucket="login", identifier="ip:9.9.9.9",
            max_attempts=5, window_seconds=300, now=now,
        ))
    # First 5 allowed, the 6th throttled with a Retry-After.
    assert all(v.allowed for v in verdicts[:5])
    assert verdicts[5].allowed is False
    assert verdicts[5].retry_after > 0


async def test_window_reset_allows_again():
    lim = await _fresh_limiter()
    for _ in range(5):
        await lim.check(
            bucket="login", identifier="ip:8.8.8.8",
            max_attempts=5, window_seconds=300, now=10_000.0,
        )
    blocked = await lim.check(
        bucket="login", identifier="ip:8.8.8.8",
        max_attempts=5, window_seconds=300, now=10_000.0,
    )
    assert blocked.allowed is False
    # Advance into the next window — counter resets.
    ok = await lim.check(
        bucket="login", identifier="ip:8.8.8.8",
        max_attempts=5, window_seconds=300, now=10_400.0,
    )
    assert ok.allowed is True


async def test_check_all_blocks_when_any_dimension_over():
    lim = await _fresh_limiter()
    now = 20_000.0
    # Burn the per-email dimension to its limit while the IP stays fresh.
    for _ in range(5):
        await lim.check(
            bucket="login", identifier="id:victim@example.com",
            max_attempts=5, window_seconds=300, now=now,
        )
    v = await lim.check_all(
        bucket="login",
        identifiers=["ip:brand.new", "id:victim@example.com"],
        max_attempts=5, window_seconds=300, now=now,
    )
    assert v.allowed is False


async def test_separate_buckets_do_not_share_counters():
    lim = await _fresh_limiter()
    now = 30_000.0
    for _ in range(5):
        await lim.check(
            bucket="login", identifier="ip:5.5.5.5",
            max_attempts=5, window_seconds=300, now=now,
        )
    # Different bucket (device_pair) starts fresh for the same IP.
    v = await lim.check(
        bucket="device_pair", identifier="ip:5.5.5.5",
        max_attempts=5, window_seconds=300, now=now,
    )
    assert v.allowed is True


# ---------------------------------------------------------------------------
# enforce_rate_limit: disabled = no-op; over-limit = 429 + Retry-After


async def test_enforce_noop_when_disabled():
    req = SimpleNamespace(headers={}, client=SimpleNamespace(host="1.1.1.1"))
    fake_settings = SimpleNamespace(rate_limit_enabled=False)
    with patch.object(rl, "get_settings", lambda: fake_settings):
        # Should simply return without raising even with a tiny limit.
        await rl.enforce_rate_limit(
            req, bucket="login", max_attempts=0, window_seconds=300,
        )


def test_client_ip_prefers_forwarded_for():
    req = SimpleNamespace(
        headers={"x-forwarded-for": "203.0.113.7, 10.0.0.1"},
        client=SimpleNamespace(host="10.0.0.1"),
    )
    assert rl.client_ip(req) == "203.0.113.7"


def test_client_ip_falls_back_to_peer():
    req = SimpleNamespace(headers={}, client=SimpleNamespace(host="198.51.100.9"))
    assert rl.client_ip(req) == "198.51.100.9"
