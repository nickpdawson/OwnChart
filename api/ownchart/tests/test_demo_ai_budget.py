"""G-1: per-visitor / per-IP daily budget on the demo AI assistant.

G-1 enables Ask + Episode Intelligence on the SHARED public demo
account so App Store reviewers can exercise the assistant. Because that
points anonymous public-demo traffic at a real, billed LLM,
``enforce_demo_ai_budget`` caps cost-bearing calls per visitor session
AND per source IP per rolling day. These tests pin:

  - No-op when demo_mode is off (self-hosted / personal instances are
    never throttled by this — even with a cap of 0).
  - The per-session cap trips the Nth+1 call with a friendly 429.
  - A second visitor session has its OWN budget (one visitor can't burn
    another's).
  - The per-IP dimension trips independently (a cookie-cycling curl
    loop from one IP is still bounded).

Pure-function + in-process backend — no Redis, no HTTP, no LLM. The
prod path uses the same limiter with a Redis backend; the throttle
logic is identical (see test_rate_limit.py).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from ownchart.core import rate_limit as rl
from ownchart.core.demo_session import DEMO_SESSION_COOKIE
from ownchart.core.rate_limit import RateLimiter, enforce_demo_ai_budget


def _fake_request(*, session_id: str | None = None, ip: str = "203.0.113.5"):
    """Minimal Request stand-in: cookies + headers + client peer.

    ``get_demo_session_id`` reads ``cookies``; ``client_ip`` reads the
    ``x-forwarded-for`` header then falls back to ``client.host``.
    """
    cookies: dict[str, str] = {}
    if session_id is not None:
        cookies[DEMO_SESSION_COOKIE] = session_id
    return SimpleNamespace(
        cookies=cookies,
        headers={"x-forwarded-for": ip},
        client=SimpleNamespace(host=ip),
    )


def _fake_settings(
    *,
    demo_mode: bool = True,
    per_session: int = 3,
    per_ip: int = 100,
    window: int = 86_400,
):
    return SimpleNamespace(
        demo_mode=demo_mode,
        demo_ai_daily_cap_per_session=per_session,
        demo_ai_daily_cap_per_ip=per_ip,
        demo_ai_window_seconds=window,
    )


def _fresh_limiter() -> RateLimiter:
    lim = RateLimiter()
    # Force the in-process backend so the test never touches Redis.
    lim._redis_disabled = True
    return lim


async def _run(settings, limiter, request):
    with patch.object(rl, "get_settings", lambda: settings), \
         patch.object(rl, "get_rate_limiter", lambda: limiter):
        await enforce_demo_ai_budget(request)


# ---------------------------------------------------------------------------
# 1. No-op outside demo mode


async def test_budget_noop_outside_demo_mode():
    settings = _fake_settings(demo_mode=False, per_session=0, per_ip=0)
    limiter = _fresh_limiter()
    req = _fake_request(session_id="visitor-a")
    # Even with a cap of 0, a non-demo instance is never throttled here.
    for _ in range(5):
        await _run(settings, limiter, req)  # must not raise


# ---------------------------------------------------------------------------
# 2. Per-session cap trips the Nth+1 call


async def test_per_session_cap_trips_after_n():
    settings = _fake_settings(per_session=3, per_ip=100)
    limiter = _fresh_limiter()
    req = _fake_request(session_id="visitor-a")

    # First 3 allowed.
    for _ in range(3):
        await _run(settings, limiter, req)

    # 4th is over the per-session cap.
    with pytest.raises(HTTPException) as ei:
        await _run(settings, limiter, req)
    exc = ei.value
    assert exc.status_code == 429
    assert exc.detail["code"] == "demo_ai_daily_limit"
    assert "demo" in exc.detail["message"].lower()
    assert int(exc.headers["Retry-After"]) >= 1


# ---------------------------------------------------------------------------
# 3. A second visitor session has its own budget


async def test_separate_sessions_have_independent_budgets():
    settings = _fake_settings(per_session=2, per_ip=100)
    limiter = _fresh_limiter()

    a = _fake_request(session_id="visitor-a", ip="203.0.113.5")
    b = _fake_request(session_id="visitor-b", ip="203.0.113.5")

    # Visitor A burns their budget.
    await _run(settings, limiter, a)
    await _run(settings, limiter, a)
    with pytest.raises(HTTPException):
        await _run(settings, limiter, a)

    # Visitor B (same IP, different cookie) still has a fresh budget.
    # (Per-IP cap is high enough that A's calls don't starve B.)
    await _run(settings, limiter, b)
    await _run(settings, limiter, b)
    with pytest.raises(HTTPException):
        await _run(settings, limiter, b)


# ---------------------------------------------------------------------------
# 4. Per-IP dimension bounds a cookie-cycling curl loop


async def test_per_ip_cap_bounds_cookie_cycling():
    # High per-session cap so the SESSION dimension never trips; a fresh
    # cookie every call would otherwise sail past a session-only limit.
    settings = _fake_settings(per_session=1000, per_ip=2)
    limiter = _fresh_limiter()

    # Each call rotates the cookie but keeps the same source IP.
    await _run(settings, limiter, _fake_request(session_id="c1", ip="198.51.100.9"))
    await _run(settings, limiter, _fake_request(session_id="c2", ip="198.51.100.9"))
    with pytest.raises(HTTPException) as ei:
        await _run(settings, limiter, _fake_request(session_id="c3", ip="198.51.100.9"))
    assert ei.value.detail["code"] == "demo_ai_daily_limit"


# ---------------------------------------------------------------------------
# 5. No cookie yet → bounded "nosession" bucket (not a bypass)


async def test_no_cookie_collapses_to_bounded_bucket():
    settings = _fake_settings(per_session=2, per_ip=100)
    limiter = _fresh_limiter()
    req = _fake_request(session_id=None)  # first-request visitor, no cookie

    await _run(settings, limiter, req)
    await _run(settings, limiter, req)
    with pytest.raises(HTTPException):
        await _run(settings, limiter, req)
