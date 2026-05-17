"""Cross-record leak tests for /api/episodes.

Beta 1 M02 Slice 1, perimeter rollout Batch 6.

Episodes are the canonical "named events" — Mom's hip surgery,
Dad's stroke, a child's broken arm. The named-memory contract:

  - Reads (list, recent, detail) require any active membership.
    Cross-record episode_id returns 404, not 403.
  - Writes (intelligence, patch, aliases, promote, attach,
    save-as-event, attach-conversation, merge, refresh) require
    caregiver+. Episodes shape Home / Timeline / Discover, so a
    viewer can read but not mutate.
  - Demo isolation (created_by='user' + payload.demo_session_id)
    layers UNDER the active-record filter — the visitor's demo
    cookie still gates, but record scope always applies first.
  - `related_conversations` on an Event detail must only return
    conversations whose `Conversation.person_record_id` matches
    the active record. Code-review check for the SQL WHERE.

Live SQL behavior verified at code-review of routes/episodes.py
(every `await db.get(Episode, ...)` guards with
`ep.person_record_id != ctx.active_record_id`, every list SELECT
filters by `Episode.person_record_id`).
"""

from __future__ import annotations

import uuid
from typing import Callable

import pytest

from ownchart.tests.conftest import authed_client, denied_client


def _id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Reads — any active membership


READ_ENDPOINTS: list[tuple[str, str, Callable[[], str]]] = [
    ("list", "GET", lambda: "/api/episodes"),
    ("recent", "GET", lambda: "/api/episodes/recent"),
    ("detail", "GET", lambda: f"/api/episodes/{_id()}"),
]


@pytest.mark.parametrize("label,method,path_factory", READ_ENDPOINTS)
def test_ep_read_403_on_record_access_revoked(
    app_fixture, label, method, path_factory,
):
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.request(method, path_factory())
    assert r.status_code == 403, (
        f"{method} {label} returned {r.status_code} {r.text}"
    )
    assert r.json()["detail"]["code"] == "record_access_revoked"


@pytest.mark.parametrize("label,method,path_factory", READ_ENDPOINTS)
def test_ep_read_403_on_no_memberships(
    app_fixture, label, method, path_factory,
):
    c = denied_client(app_fixture, code="no_memberships")
    r = c.request(method, path_factory())
    assert r.status_code == 403, (
        f"{method} {label} returned {r.status_code} {r.text}"
    )
    assert r.json()["detail"]["code"] == "no_memberships"


# ---------------------------------------------------------------------------
# Writes — all require caregiver+


WRITE_ENDPOINTS: list[tuple[str, str, Callable[[], str], dict]] = [
    ("intelligence", "POST", lambda: "/api/episodes/intelligence",
     {"json": {"natural_language": "test"}}),
    ("patch", "PATCH", lambda: f"/api/episodes/{_id()}",
     {"json": {"title": "x"}}),
    ("add-alias", "POST", lambda: f"/api/episodes/{_id()}/aliases",
     {"json": {"alias": "x"}}),
    ("remove-alias", "DELETE",
     lambda: f"/api/episodes/{_id()}/aliases/foo", {}),
    ("from-candidate", "POST",
     lambda: f"/api/episodes/from-candidate/{_id()}", {}),
    ("attach-candidate", "POST",
     lambda: f"/api/episodes/{_id()}/attach-candidate/{_id()}", {}),
    ("save-from-conv", "POST",
     lambda: f"/api/episodes/from-conversation/{_id()}",
     {"json": {"title": "x"}}),
    ("attach-conv", "POST",
     lambda: f"/api/episodes/{_id()}/attach-conversation",
     {"json": {"conversation_id": _id()}}),
    ("merge", "POST",
     lambda: f"/api/episodes/{_id()}/merge-into/{_id()}", {}),
    ("refresh", "POST",
     lambda: f"/api/episodes/{_id()}/refresh-intelligence", {}),
]


@pytest.mark.parametrize("label,method,path_factory,kwargs", WRITE_ENDPOINTS)
def test_ep_write_403_on_record_access_revoked(
    app_fixture, label, method, path_factory, kwargs,
):
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.request(method, path_factory(), **kwargs)
    assert r.status_code == 403, (
        f"{method} {label} returned {r.status_code} {r.text}"
    )
    assert r.json()["detail"]["code"] == "record_access_revoked"


@pytest.mark.parametrize("label,method,path_factory,kwargs", WRITE_ENDPOINTS)
def test_ep_write_403_on_no_memberships(
    app_fixture, label, method, path_factory, kwargs,
):
    c = denied_client(app_fixture, code="no_memberships")
    r = c.request(method, path_factory(), **kwargs)
    assert r.status_code == 403, (
        f"{method} {label} returned {r.status_code} {r.text}"
    )
    assert r.json()["detail"]["code"] == "no_memberships"


@pytest.mark.parametrize("label,method,path_factory,kwargs", WRITE_ENDPOINTS)
def test_ep_write_403_insufficient_role_for_viewer(
    app_fixture, label, method, path_factory, kwargs,
):
    """Every Event write requires caregiver+. Viewers see Events
    on the record but cannot rename, alias, promote, merge, or
    refresh them."""
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
# Static signature check


def test_ep_handler_signatures_include_auth_context():
    from typing import get_type_hints
    from ownchart.core.auth_context import AuthContext
    from ownchart.routes.episodes import (
        add_episode_alias_route,
        attach_candidate_to_episode_route,
        attach_conversation_to_episode_route,
        get_episode_route,
        list_episodes_route,
        list_recent_episodes_route,
        merge_episodes_route,
        patch_episode_route,
        promote_candidate_route,
        refresh_episode_intelligence_route,
        remove_episode_alias_route,
        run_intelligence_route,
        save_conversation_as_event_route,
    )

    handlers = (
        run_intelligence_route,
        list_episodes_route,
        list_recent_episodes_route,
        patch_episode_route,
        add_episode_alias_route,
        remove_episode_alias_route,
        get_episode_route,
        promote_candidate_route,
        attach_candidate_to_episode_route,
        save_conversation_as_event_route,
        attach_conversation_to_episode_route,
        merge_episodes_route,
        refresh_episode_intelligence_route,
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


# ---------------------------------------------------------------------------
# Role-gate identity — every write must use require_role("caregiver")


def test_ep_writes_use_caregiver_role_gate():
    """Inspect each write handler's `ctx` Depends to ensure
    require_role('caregiver') is wired. Catches a future refactor
    that accidentally downgrades a write to get_auth_context."""
    import inspect
    from fastapi.params import Depends as DependsParam
    from ownchart.routes.episodes import (
        add_episode_alias_route,
        attach_candidate_to_episode_route,
        attach_conversation_to_episode_route,
        merge_episodes_route,
        patch_episode_route,
        promote_candidate_route,
        refresh_episode_intelligence_route,
        remove_episode_alias_route,
        run_intelligence_route,
        save_conversation_as_event_route,
    )

    write_handlers = (
        run_intelligence_route,
        patch_episode_route,
        add_episode_alias_route,
        remove_episode_alias_route,
        promote_candidate_route,
        attach_candidate_to_episode_route,
        save_conversation_as_event_route,
        attach_conversation_to_episode_route,
        merge_episodes_route,
        refresh_episode_intelligence_route,
    )
    for fn in write_handlers:
        sig = inspect.signature(fn)
        ctx_param = sig.parameters["ctx"]
        assert isinstance(ctx_param.default, DependsParam), fn.__name__
        dep_fn = ctx_param.default.dependency
        # require_role returns a closure named "_dep"; get_auth_context
        # has __name__ == "get_auth_context".
        assert dep_fn.__name__ == "_dep", (
            f"{fn.__name__} must use require_role('caregiver'); "
            f"got dep {dep_fn.__name__}"
        )
        required = None
        for cell in dep_fn.__closure__ or ():
            v = cell.cell_contents
            if isinstance(v, str) and v in ("viewer", "member", "caregiver", "owner"):
                required = v
                break
        assert required == "caregiver", (
            f"{fn.__name__} must gate at caregiver, got {required}"
        )


# ---------------------------------------------------------------------------
# Model regression guards


def test_episode_model_carries_person_record_id():
    from ownchart.models.episode import Episode
    assert "person_record_id" in Episode.__table__.columns


# ---------------------------------------------------------------------------
# Body never runs on denial


def test_ep_list_does_not_run_on_denial(app_fixture):
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.get("/api/episodes")
    assert r.status_code == 403


def test_ep_patch_does_not_run_on_denial(app_fixture):
    c = denied_client(app_fixture, code="no_memberships")
    r = c.patch(f"/api/episodes/{_id()}", json={"title": "x"})
    assert r.status_code == 403
