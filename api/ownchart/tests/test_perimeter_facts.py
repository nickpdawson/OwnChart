"""Cross-record leak tests for /api/facts endpoints.

Beta 1 M02 Slice 1, perimeter rollout Batch 3.

Pairs with `test_perimeter_sources_{read,write}.py`. The contract:

  - Reads (`GET /api/facts`, `GET /api/facts/{id}`,
    `GET /api/facts/{id}/context`) require any active membership.
    Cross-record reads 404 (we do not disclose existence of facts
    outside the active record).

  - Writes (`PATCH /api/facts/{id}`, `PATCH /api/facts/{id}/significance`,
    `POST /api/facts/bulk`, `POST /api/facts/relabel-backfill`,
    `POST /api/facts/significance-backfill`) require role >=
    caregiver. Viewers are denied with `insufficient_role`.

What this pins (without a live DB):

  1. Every facts handler propagates the PM-A-5 AuthContext errors
     (`record_access_revoked`, `no_memberships`) as 403 with the
     correct code.
  2. Write handlers also propagate `insufficient_role` when the
     active role is viewer.
  3. Each handler declares an `AuthContext` parameter so FastAPI
     runs the dep chain.

Live SQL-filter behavior — "list_facts WHERE
person_record_id = ctx.active_record_id" and bulk_correct's
silent filter on cross-record ids — is verifiable by code review
of routes/facts.py (look for the explicit `.where(person_record_id
== ctx.active_record_id)` clauses) and exercised once a real
Postgres test fixture lands.
"""

from __future__ import annotations

import uuid
from typing import Callable

import pytest

from ownchart.tests.conftest import authed_client, denied_client


def _id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# /api/facts read endpoints


READ_ENDPOINTS: list[tuple[str, str, Callable[[], str]]] = [
    ("list", "GET", lambda: "/api/facts"),
    ("get", "GET", lambda: f"/api/facts/{_id()}"),
    ("context", "GET", lambda: f"/api/facts/{_id()}/context"),
]


@pytest.mark.parametrize("label,method,path_factory", READ_ENDPOINTS)
def test_facts_read_403_on_record_access_revoked(
    app_fixture, label, method, path_factory,
):
    """Reads propagate record_access_revoked from AuthContext."""
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.request(method, path_factory())
    assert r.status_code == 403, (
        f"{method} {label} returned {r.status_code} {r.text}"
    )
    assert r.json()["detail"]["code"] == "record_access_revoked"


@pytest.mark.parametrize("label,method,path_factory", READ_ENDPOINTS)
def test_facts_read_403_on_no_memberships(
    app_fixture, label, method, path_factory,
):
    c = denied_client(app_fixture, code="no_memberships")
    r = c.request(method, path_factory())
    assert r.status_code == 403, (
        f"{method} {label} returned {r.status_code} {r.text}"
    )
    assert r.json()["detail"]["code"] == "no_memberships"


# ---------------------------------------------------------------------------
# /api/facts write endpoints

WRITE_ENDPOINTS: list[tuple[str, str, Callable[[], str], dict]] = [
    # PATCH /{id} — JSON body
    ("correct", "PATCH", lambda: f"/api/facts/{_id()}",
     {"json": {"assertion_type": "confirm"}}),
    # PATCH /{id}/significance
    ("significance", "PATCH", lambda: f"/api/facts/{_id()}/significance",
     {"json": {"significance": "major_event"}}),
    # POST /bulk
    ("bulk", "POST", lambda: "/api/facts/bulk",
     {"json": {"fact_ids": [_id()], "assertion_type": "confirm"}}),
    # POST /relabel-backfill (no body)
    ("relabel-backfill", "POST", lambda: "/api/facts/relabel-backfill", {}),
    # POST /significance-backfill (no body)
    ("significance-backfill", "POST",
     lambda: "/api/facts/significance-backfill", {}),
]


@pytest.mark.parametrize("label,method,path_factory,kwargs", WRITE_ENDPOINTS)
def test_facts_write_403_on_record_access_revoked(
    app_fixture, label, method, path_factory, kwargs,
):
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.request(method, path_factory(), **kwargs)
    assert r.status_code == 403, (
        f"{method} {label} returned {r.status_code} {r.text}"
    )
    assert r.json()["detail"]["code"] == "record_access_revoked"


@pytest.mark.parametrize("label,method,path_factory,kwargs", WRITE_ENDPOINTS)
def test_facts_write_403_on_no_memberships(
    app_fixture, label, method, path_factory, kwargs,
):
    c = denied_client(app_fixture, code="no_memberships")
    r = c.request(method, path_factory(), **kwargs)
    assert r.status_code == 403, (
        f"{method} {label} returned {r.status_code} {r.text}"
    )
    assert r.json()["detail"]["code"] == "no_memberships"


@pytest.mark.parametrize("label,method,path_factory,kwargs", WRITE_ENDPOINTS)
def test_facts_write_403_insufficient_role_for_viewer(
    app_fixture, label, method, path_factory, kwargs,
):
    """Viewer passes get_auth_context but require_role("caregiver")
    rejects with insufficient_role. Reads should still work for a
    viewer — only writes are gated above member."""
    c = authed_client(app_fixture, role="viewer")
    r = c.request(method, path_factory(), **kwargs)
    assert r.status_code == 403, (
        f"{method} {label} returned {r.status_code} {r.text}"
    )
    body = r.json()
    assert body["detail"]["code"] == "insufficient_role", body
    assert body["detail"]["required"] == "caregiver"
    assert body["detail"]["actual"] == "viewer"


# ---------------------------------------------------------------------------
# Body never runs on denial — structural guarantee


def test_facts_list_handler_does_not_run_on_denial(app_fixture):
    """If FastAPI ran the body, the SQL would touch
    person_record_id, which the un-migrated test DB doesn't have.
    A clean 403 proves the dep tripped first."""
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.get("/api/facts")
    assert r.status_code == 403


def test_facts_correct_handler_does_not_run_on_denial(app_fixture):
    c = denied_client(app_fixture, code="no_memberships")
    r = c.patch(
        f"/api/facts/{_id()}",
        json={"assertion_type": "confirm"},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Static signature check


def test_facts_handler_signatures_include_auth_context():
    from typing import get_type_hints

    from ownchart.core.auth_context import AuthContext
    from ownchart.routes.facts import (
        bulk_correct_facts,
        correct_fact,
        get_fact,
        get_fact_context,
        list_facts,
        relabel_backfill,
        set_fact_significance,
        significance_backfill,
    )

    handlers = (
        list_facts,
        get_fact,
        get_fact_context,
        correct_fact,
        set_fact_significance,
        bulk_correct_facts,
        relabel_backfill,
        significance_backfill,
    )
    for fn in handlers:
        hints = get_type_hints(fn)
        ctx_params = [
            name for name, hint in hints.items()
            if hint is AuthContext
        ]
        assert len(ctx_params) == 1, (
            f"{fn.__name__} must declare exactly one "
            f"`AuthContext` parameter; got {ctx_params}."
        )
