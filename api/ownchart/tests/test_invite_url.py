"""`_compute_invite_url` — FU-MULTITENANT-ONBOARDING.

The invite URL is the load-bearing artifact: the owner copies it
once at creation time and the invitee opens it. We pin two
properties:

  1. When OWNCHART_PUBLIC_BASE_URL is configured, the invite URL
     uses it regardless of the request origin. This is the
     production posture: the api is behind a reverse proxy and
     the request scheme/host may be the internal docker network.

  2. When OWNCHART_PUBLIC_BASE_URL is empty, the URL falls back
     to the request origin. This is the dev / first-boot posture
     before the operator configures their public URL.
"""

from __future__ import annotations

from types import SimpleNamespace

from ownchart.routes.invitations import _compute_invite_url


def _request_with_base(base: str):
    return SimpleNamespace(base_url=base)


def test_uses_configured_public_base_when_set():
    req = _request_with_base("http://api:8000/")
    url = _compute_invite_url(
        req, "TOK123", public_base_url="https://ownchart.example.com",
    )
    assert url == "https://ownchart.example.com/invite/TOK123"


def test_strips_trailing_slash_on_public_base():
    req = _request_with_base("http://api:8000/")
    url = _compute_invite_url(
        req, "TOK", public_base_url="https://ownchart.example.com/",
    )
    assert url == "https://ownchart.example.com/invite/TOK"


def test_falls_back_to_request_origin_when_no_public_base():
    req = _request_with_base("https://localhost:3000/")
    url = _compute_invite_url(req, "TOK", public_base_url=None)
    assert url == "https://localhost:3000/invite/TOK"


def test_falls_back_when_public_base_empty_string():
    req = _request_with_base("https://localhost:3000/")
    url = _compute_invite_url(req, "TOK", public_base_url="")
    assert url == "https://localhost:3000/invite/TOK"


def test_url_path_is_invite_route():
    """The invite landing route is /invite/{token} per the FU spec.
    Pinned here so a future rename of the web route doesn't drift
    the URL silently."""
    req = _request_with_base("https://ownchart.example.com")
    url = _compute_invite_url(
        req, "abc", public_base_url="https://ownchart.example.com",
    )
    assert "/invite/abc" in url
