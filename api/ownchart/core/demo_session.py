"""Per-visitor session isolation for the shared demo account.

Problem: ``demo@ownchart.me`` is a shared login. Every visitor signs
in as the same user, so a conversation written by visitor A is
visible to visitor B in ``GET /api/conversations`` and via the
detail endpoint. Even though our patient record is synthetic and
read-only, the Ask box accepts free-form text the visitor types,
which could include their own medical situation.

PM's fix (preferred option 1, 2026-05-16): in demo mode, every
visitor gets an ``oc_demo_session`` cookie carrying a random UUID.
Conversations created by that visitor stamp the UUID into
``Conversation.scope.demo_session_id``. List + detail endpoints
filter by that UUID, so prior visitors' chats simply don't appear.

This module owns:
  - The middleware that issues the cookie on every demo-mode response
    that doesn't already carry one.
  - The dependency / helper for reading the current visitor's UUID
    out of the request.

The cookie is httpOnly + SameSite=Lax. It carries no PHI — just an
opaque UUID. A second visitor in the same browser still gets a fresh
cookie if the first session expired (24h).
"""

from __future__ import annotations

import uuid
from typing import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from .config import get_settings


# Cookie name. Distinct from the session cookie (which authenticates
# the demo user); this one identifies the *visitor* inside that
# shared account.
DEMO_SESSION_COOKIE = "oc_demo_session"

# 24 hours. Long enough that a visitor can come back the same day
# and see their own chat; short enough that the leakage window if
# we ever miss a filter is bounded.
DEMO_SESSION_MAX_AGE_SECONDS = 60 * 60 * 24


def _new_session_id() -> str:
    return uuid.uuid4().hex


def get_demo_session_id(request: Request) -> str | None:
    """Read the visitor's session id from the request cookie.

    Returns None if the cookie isn't set. Callers that need a value
    even on the first request should use ``ensure_demo_session_id``
    via the middleware path (the middleware writes the cookie on the
    response so subsequent requests have it).
    """
    raw = request.cookies.get(DEMO_SESSION_COOKIE)
    if not raw:
        return None
    # Defensive: cap at 64 chars even though we always write 32-hex.
    # If the cookie was tampered with, we'd rather return a bounded
    # string than risk a giant value flowing into JSONB.
    return raw[:64]


def apply_demo_session_scope(
    scope: dict | None,
    request: Request,
) -> dict:
    """Stamp the per-visitor demo session id onto a Conversation /
    Episode scope dict.

    No-op outside demo mode. Returns a new dict; never mutates the
    input. Use at row-create time so listing/detail filters can match.
    """
    base = dict(scope or {})
    if not get_settings().demo_mode:
        return base
    sid = get_demo_session_id(request)
    if sid:
        base["demo_session_id"] = sid
    return base


def demo_session_matches(scope: dict | None, request: Request) -> bool:
    """Return True if this request's cookie matches the demo session
    id stamped on the row, OR if the row has no demo_session_id (i.e.
    it was seeded / pre-demo / created outside the visitor flow).

    Outside demo mode always True — this is a demo-only gate.

    Use to defend conversation / episode detail endpoints in demo
    mode: if False, return 404 (don't disclose the resource exists).
    """
    if not get_settings().demo_mode:
        return True
    sid_on_row = (scope or {}).get("demo_session_id") if scope else None
    if sid_on_row is None:
        # Seeded resource — visible to every visitor.
        return True
    return sid_on_row == get_demo_session_id(request)


class DemoSessionMiddleware(BaseHTTPMiddleware):
    """Issue the per-visitor cookie on every demo-mode response.

    The middleware runs after the route handler. Reading the cookie
    inside the handler uses ``get_demo_session_id`` against the
    request; on the *first* request from a visitor that returns None,
    so the handler must defensively treat 'no cookie' as 'no scoping
    match' (which falls back to empty list on the list path — the
    visitor will see their own chat starting from the *next* request).

    Outside demo mode this middleware is a no-op fast path.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        s = get_settings()
        if not s.demo_mode:
            return await call_next(request)
        existing = request.cookies.get(DEMO_SESSION_COOKIE)
        response = await call_next(request)
        if not existing:
            response.set_cookie(
                key=DEMO_SESSION_COOKIE,
                value=_new_session_id(),
                max_age=DEMO_SESSION_MAX_AGE_SECONDS,
                httponly=True,
                samesite="lax",
                # Secure tied to env=prod via the same heuristic the
                # session cookie uses. Behind the public demo's
                # reverse proxy this is always HTTPS, but local dev
                # over HTTP would lose the cookie if we hard-set
                # Secure=True.
                secure=s.env == "prod",
                path="/",
            )
        return response
