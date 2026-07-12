"""Redis-backed fixed-window rate limiter for credential endpoints.

HIGH-7 (Lapland tenant launch gate). Before this, ``/api/auth/login``,
``/api/auth/device/pair``, ``/api/auth/register`` and the unauthenticated
``/api/invitations/preview`` had NO throttle — a bot could grind
passwords or enumerate invite tokens at line rate. This module adds a
small fixed-window limiter:

  - **Backend:** Redis (shared with arq via ``settings.redis_url``) so
    the counter holds across api worker processes. When Redis is
    unreachable we fall back to an in-process counter — correct for
    single-process dev, best-effort in prod (the fallback is logged).
  - **Windowing:** fixed window. ``key`` is
    ``ratelimit:{bucket}:{identifier}:{window_start}``; we ``INCR`` and,
    on the first hit of a window, ``EXPIRE`` it. The Nth+1 hit inside a
    window is rejected.
  - **Dimensions:** every protected endpoint throttles per client IP.
    Endpoints with a natural subject (login/pair/register → email) ALSO
    throttle per identifier so one IP can't lock out one account and
    one account can't be sprayed from many IPs undetected.

The route layer calls :func:`enforce_rate_limit`, which raises a 429
with a ``Retry-After`` header when any dimension is over its limit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from ..core.config import get_settings
from ..core.logger import get_logger

log = get_logger("ownchart.core.rate_limit")


# ---------------------------------------------------------------------------
# Pure-function core (testable without Redis or FastAPI)


def window_start(now: float, window_seconds: int) -> int:
    """Start (unix seconds) of the fixed window containing ``now``."""
    return int(now // window_seconds) * window_seconds


def retry_after_seconds(now: float, window_seconds: int) -> int:
    """Seconds until the current window rolls over. Always >= 1."""
    resets_at = window_start(now, window_seconds) + window_seconds
    return max(1, int(resets_at - now + 0.999))


def counter_key(bucket: str, identifier: str, now: float, window_seconds: int) -> str:
    """The Redis / in-process key for one (bucket, identifier, window)."""
    return f"ratelimit:{bucket}:{identifier}:{window_start(now, window_seconds)}"


@dataclass(frozen=True)
class RateVerdict:
    allowed: bool
    retry_after: int  # seconds; 0 when allowed


# ---------------------------------------------------------------------------
# Backends


class _InProcessBackend:
    """Fixed-window counter in a plain dict. Single-process only.

    Used as the dev default and the fallback when Redis is down. Prunes
    keys whose window has fully elapsed so the dict can't grow without
    bound under sustained traffic.
    """

    def __init__(self) -> None:
        # key -> (count, window_expiry_epoch)
        self._hits: dict[str, tuple[int, float]] = {}

    async def hit(self, key: str, window_seconds: int, now: float) -> int:
        self._prune(now)
        count, expiry = self._hits.get(key, (0, now + window_seconds))
        count += 1
        self._hits[key] = (count, expiry)
        return count

    def _prune(self, now: float) -> None:
        expired = [k for k, (_, exp) in self._hits.items() if exp <= now]
        for k in expired:
            self._hits.pop(k, None)


class _RedisBackend:
    """Fixed-window counter in Redis via INCR + EXPIRE."""

    def __init__(self, client) -> None:  # redis.asyncio.Redis
        self._client = client

    async def hit(self, key: str, window_seconds: int, now: float) -> int:
        # INCR returns the post-increment value. On the first hit of a
        # window (value == 1) set the TTL so the key self-cleans.
        count = await self._client.incr(key)
        if count == 1:
            await self._client.expire(key, window_seconds)
        return int(count)


# ---------------------------------------------------------------------------
# Limiter


class RateLimiter:
    """Fixed-window limiter with a Redis backend + in-process fallback.

    ``check`` increments the counter for a single (bucket, identifier)
    pair and returns a :class:`RateVerdict`. ``check_all`` runs several
    dimensions and returns the most-restrictive verdict — the IP is
    over OR the identifier is over ⇒ blocked.
    """

    def __init__(self) -> None:
        self._inproc = _InProcessBackend()
        self._redis_backend: _RedisBackend | None = None
        self._redis_disabled = False

    async def _backend(self):
        s = get_settings()
        if self._redis_disabled:
            return self._inproc
        if self._redis_backend is not None:
            return self._redis_backend
        try:
            import redis.asyncio as aioredis

            client = aioredis.from_url(
                s.redis_url, encoding="utf-8", decode_responses=True,
            )
            # Cheap liveness probe so a dead Redis degrades immediately
            # to the in-process fallback instead of erroring per request.
            await client.ping()
            self._redis_backend = _RedisBackend(client)
            return self._redis_backend
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "rate_limit_redis_unavailable",
                error=f"{type(exc).__name__}: {exc}",
            )
            self._redis_disabled = True
            return self._inproc

    async def check(
        self,
        *,
        bucket: str,
        identifier: str,
        max_attempts: int,
        window_seconds: int,
        now: float | None = None,
    ) -> RateVerdict:
        now = time.time() if now is None else now
        backend = await self._backend()
        key = counter_key(bucket, identifier, now, window_seconds)
        try:
            count = await backend.hit(key, window_seconds, now)
        except Exception as exc:  # noqa: BLE001
            # A backend error must never take down the endpoint it
            # protects — fail OPEN but loud. (A hard-fail-closed limiter
            # would be a DoS lever of its own.)
            log.warning(
                "rate_limit_backend_error",
                bucket=bucket, error=f"{type(exc).__name__}: {exc}",
            )
            return RateVerdict(allowed=True, retry_after=0)
        if count > max_attempts:
            return RateVerdict(
                allowed=False,
                retry_after=retry_after_seconds(now, window_seconds),
            )
        return RateVerdict(allowed=True, retry_after=0)

    async def check_all(
        self,
        *,
        bucket: str,
        identifiers: list[str],
        max_attempts: int,
        window_seconds: int,
        now: float | None = None,
    ) -> RateVerdict:
        now = time.time() if now is None else now
        worst = RateVerdict(allowed=True, retry_after=0)
        for ident in identifiers:
            v = await self.check(
                bucket=bucket,
                identifier=ident,
                max_attempts=max_attempts,
                window_seconds=window_seconds,
                now=now,
            )
            if not v.allowed and v.retry_after > worst.retry_after:
                worst = v
        return worst


# Module-level singleton — one limiter per api process, sharing the
# Redis pool and the in-process fallback dict across all endpoints.
_limiter = RateLimiter()


def get_rate_limiter() -> RateLimiter:
    return _limiter


def client_ip(request: Request) -> str:
    """Best-effort client IP.

    Behind the demo/tenant reverse proxy the socket peer is the proxy,
    so honor ``X-Forwarded-For``'s first hop when present. Falls back to
    the socket peer. Never raises — an unknown IP collapses to a shared
    ``"unknown"`` bucket rather than bypassing the limit.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first[:64]
    if request.client and request.client.host:
        return request.client.host[:64]
    return "unknown"


async def enforce_rate_limit(
    request: Request,
    *,
    bucket: str,
    max_attempts: int,
    window_seconds: int,
    identifier: str | None = None,
) -> None:
    """Throttle a request per client IP (+ optional identifier).

    No-op when ``settings.rate_limit_enabled`` is False. Raises
    ``429`` with a ``Retry-After`` header when either dimension is over
    its window limit.
    """
    s = get_settings()
    if not s.rate_limit_enabled:
        return
    identifiers = [f"ip:{client_ip(request)}"]
    if identifier:
        identifiers.append(f"id:{identifier.strip().lower()[:200]}")
    verdict = await get_rate_limiter().check_all(
        bucket=bucket,
        identifiers=identifiers,
        max_attempts=max_attempts,
        window_seconds=window_seconds,
    )
    if not verdict.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "rate_limited",
                "message": (
                    "Too many attempts. Wait a moment and try again."
                ),
            },
            headers={"Retry-After": str(verdict.retry_after)},
        )
