"""P1-8b: API-tier security headers.

The web tier owns the browser-facing headers (HSTS/CSP/etc). Here we pin the
API-side hardening: every /api response carries `X-Content-Type-Options:
nosniff` and `Cache-Control: private, no-store` so PHI behind /api is never
retained by a shared cache — including on error responses — while non-/api
probes are left alone.
"""

from __future__ import annotations

from ownchart.tests.conftest import client  # noqa: F401  (pytest fixture)


def test_api_error_response_carries_security_headers(client):
    # A validation error still flows out through the header middleware.
    r = client.post("/api/auth/login", json={})
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("cache-control") == "private, no-store"


def test_non_api_path_not_forced_no_store(client):
    # /healthz is not under /api — the API cache guard must not touch it.
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.headers.get("cache-control") != "private, no-store"
