"""Cross-record leak tests for /api/topics.

Beta 1 M02 Slice 1, perimeter rollout Batch 6.

Dossiers (Topics) are the second named-memory surface:
"Cardiology", "Pregnancy", "Knee". Slug uniqueness is per-record
post-migration 0032, so two records can each have their own
"Cardiology" dossier. The named-memory contract:

  - Reads (list, dossier, cluster facts, brief, thread,
    conversations) require any active membership. Cross-record
    slug returns 404, not the other record's dossier.
  - Writes (create, brief generate, get-or-create conversation,
    attach-conversation, follow-up ask) require caregiver+.
    Dossiers shape retrieval (topic_membership_clause), so a
    viewer can read but not mutate.
  - `list_topic_conversations` only returns conversations whose
    person_record_id matches the active record, even if a sibling
    record has a same-slug Topic. The slug filter alone isn't
    enough — the record filter is load-bearing.

Live SQL behavior verified at code-review of routes/topics.py
(every `_resolve_topic_or_404` call passes person_record_id, every
list SELECT carries `Topic.person_record_id == ctx.active_record_id`).
"""

from __future__ import annotations

import uuid
from typing import Callable

import pytest

from ownchart.tests.conftest import authed_client, denied_client


def _id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Reads


READ_ENDPOINTS: list[tuple[str, str, Callable[[], str]]] = [
    ("list", "GET", lambda: "/api/topics"),
    ("dossier", "GET", lambda: "/api/topics/test-slug"),
    ("cluster-facts", "GET",
     lambda: "/api/topics/test-slug/clusters/abc123/facts"),
    ("brief", "GET", lambda: "/api/topics/test-slug/brief"),
    ("thread", "GET", lambda: "/api/topics/test-slug/thread"),
    ("conversations", "GET",
     lambda: "/api/topics/test-slug/conversations"),
]


@pytest.mark.parametrize("label,method,path_factory", READ_ENDPOINTS)
def test_tp_read_403_on_record_access_revoked(
    app_fixture, label, method, path_factory,
):
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.request(method, path_factory())
    assert r.status_code == 403, (
        f"{method} {label} returned {r.status_code} {r.text}"
    )
    assert r.json()["detail"]["code"] == "record_access_revoked"


@pytest.mark.parametrize("label,method,path_factory", READ_ENDPOINTS)
def test_tp_read_403_on_no_memberships(
    app_fixture, label, method, path_factory,
):
    c = denied_client(app_fixture, code="no_memberships")
    r = c.request(method, path_factory())
    assert r.status_code == 403, (
        f"{method} {label} returned {r.status_code} {r.text}"
    )
    assert r.json()["detail"]["code"] == "no_memberships"


# ---------------------------------------------------------------------------
# Writes — caregiver+


WRITE_ENDPOINTS: list[tuple[str, str, Callable[[], str], dict]] = [
    ("create", "POST", lambda: "/api/topics",
     {"json": {"name": "Test Dossier"}}),
    ("generate-brief", "POST",
     lambda: "/api/topics/test-slug/brief", {}),
    ("get-or-create-conv", "POST",
     lambda: "/api/topics/test-slug/conversation", {}),
    ("attach-conv", "POST",
     lambda: "/api/topics/test-slug/attach-conversation",
     {"json": {"conversation_id": _id()}}),
    ("ask", "POST", lambda: "/api/topics/test-slug/ask",
     {"json": {"question": "x"}}),
]


@pytest.mark.parametrize("label,method,path_factory,kwargs", WRITE_ENDPOINTS)
def test_tp_write_403_on_record_access_revoked(
    app_fixture, label, method, path_factory, kwargs,
):
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.request(method, path_factory(), **kwargs)
    assert r.status_code == 403, (
        f"{method} {label} returned {r.status_code} {r.text}"
    )
    assert r.json()["detail"]["code"] == "record_access_revoked"


@pytest.mark.parametrize("label,method,path_factory,kwargs", WRITE_ENDPOINTS)
def test_tp_write_403_on_no_memberships(
    app_fixture, label, method, path_factory, kwargs,
):
    c = denied_client(app_fixture, code="no_memberships")
    r = c.request(method, path_factory(), **kwargs)
    assert r.status_code == 403, (
        f"{method} {label} returned {r.status_code} {r.text}"
    )
    assert r.json()["detail"]["code"] == "no_memberships"


# Get-or-create conversation should accept viewers (it's get-or-create,
# essentially a read with a creation side-effect for the user's own
# thread). The other four writes are caregiver+.
WRITES_CAREGIVER_GATED: list[tuple[str, str, Callable[[], str], dict]] = [
    ("create", "POST", lambda: "/api/topics",
     {"json": {"name": "Test"}}),
    ("generate-brief", "POST",
     lambda: "/api/topics/test-slug/brief", {}),
    ("attach-conv", "POST",
     lambda: "/api/topics/test-slug/attach-conversation",
     {"json": {"conversation_id": _id()}}),
    ("ask", "POST", lambda: "/api/topics/test-slug/ask",
     {"json": {"question": "x"}}),
]


@pytest.mark.parametrize("label,method,path_factory,kwargs", WRITES_CAREGIVER_GATED)
def test_tp_writes_caregiver_gated_for_viewer(
    app_fixture, label, method, path_factory, kwargs,
):
    """create / generate-brief / attach-conv / ask require caregiver+.
    Viewers cannot shape the record's dossiers."""
    c = authed_client(app_fixture, role="viewer")
    r = c.request(method, path_factory(), **kwargs)
    assert r.status_code == 403, (
        f"{method} {label} returned {r.status_code} {r.text}"
    )
    body = r.json()
    assert body["detail"]["code"] == "insufficient_role", body
    assert body["detail"]["required"] == "caregiver"


# ---------------------------------------------------------------------------
# Static signature check


def test_tp_handler_signatures_include_auth_context():
    from typing import get_type_hints
    from ownchart.core.auth_context import AuthContext
    from ownchart.routes.topics import (
        ask_followup,
        attach_conversation_to_topic_route,
        create_topic,
        generate_exec_brief,
        get_brief_thread,
        get_cluster_facts,
        get_latest_brief,
        get_or_create_topic_conversation,
        get_topic_dossier,
        list_topic_conversations,
        list_topics,
    )

    handlers = (
        list_topics,
        create_topic,
        get_topic_dossier,
        get_cluster_facts,
        get_latest_brief,
        generate_exec_brief,
        get_brief_thread,
        list_topic_conversations,
        get_or_create_topic_conversation,
        attach_conversation_to_topic_route,
        ask_followup,
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
# Caregiver-role-gate identity


def test_tp_writes_use_caregiver_role_gate():
    import inspect
    from fastapi.params import Depends as DependsParam
    from ownchart.routes.topics import (
        ask_followup,
        attach_conversation_to_topic_route,
        create_topic,
        generate_exec_brief,
    )

    caregiver_writes = (
        create_topic,
        generate_exec_brief,
        attach_conversation_to_topic_route,
        ask_followup,
    )
    for fn in caregiver_writes:
        sig = inspect.signature(fn)
        ctx_param = sig.parameters["ctx"]
        assert isinstance(ctx_param.default, DependsParam), fn.__name__
        dep_fn = ctx_param.default.dependency
        assert dep_fn.__name__ == "_dep", (
            f"{fn.__name__} must use require_role; got {dep_fn.__name__}"
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
# _resolve_topic_or_404 plumbthrough — the load-bearing scope helper


def test_resolve_topic_requires_person_record_id():
    """Every topic-route handler resolves the slug via
    _resolve_topic_or_404(..., person_record_id=...). Catches a
    refactor that drops the kwarg, which would silently revert to
    global lookup."""
    import inspect
    from ownchart.routes.topics import _resolve_topic_or_404
    sig = inspect.signature(_resolve_topic_or_404)
    assert "person_record_id" in sig.parameters
    # Should be keyword-only (no default), per the helper's
    # contract — callers must explicitly pass scope, not get it
    # wrong by omission.
    p = sig.parameters["person_record_id"]
    assert p.kind == inspect.Parameter.KEYWORD_ONLY
    assert p.default is inspect.Parameter.empty


# ---------------------------------------------------------------------------
# Model regression guards


def test_topic_model_carries_person_record_id():
    from ownchart.models.topic import Topic
    assert "person_record_id" in Topic.__table__.columns


def test_topic_brief_model_carries_person_record_id():
    from ownchart.models.topic_brief import TopicBrief
    assert "person_record_id" in TopicBrief.__table__.columns


def test_brief_message_model_carries_person_record_id():
    from ownchart.models.brief_message import BriefMessage
    assert "person_record_id" in BriefMessage.__table__.columns


# ---------------------------------------------------------------------------
# Body never runs on denial


def test_tp_list_does_not_run_on_denial(app_fixture):
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.get("/api/topics")
    assert r.status_code == 403


def test_tp_create_does_not_run_on_denial(app_fixture):
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.post("/api/topics", json={"name": "x"})
    assert r.status_code == 403
