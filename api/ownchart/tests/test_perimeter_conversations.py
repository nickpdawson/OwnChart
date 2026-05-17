"""Cross-record leak tests for /api/conversations.

Beta 1 M02 Slice 1, perimeter rollout Batch 5.

Conversations are the second-highest leakage surface (after Ask):
they create persistent rows that the LLM context, retrieval, and
the dossier-promotion path all feed off. The acceptance bar PM set:

  - List, detail, messages, patch, delete, candidates, suggest-topic
    propagate 403 on AuthContext failures.
  - save-as-topic gates to caregiver+ (creating a record-level Topic
    is record-shaping).
  - Conversation list filter narrows by Conversation.person_record_id,
    not just user_id. (Code-review check, since the SQL itself runs
    against a real DB — tests here verify the dependency is wired so
    the body never runs on denial.)
  - Cross-record conversation IDs return 404, not 403 or empty data.
    Existence must not be disclosed.
  - New Conversation / ConversationMessage / Topic / SensemakingJob /
    SensemakingCandidate rows stamp person_record_id from the active
    record. Verified by signature + (where reachable) by patching the
    helper to inspect the kwargs the route passes.
"""

from __future__ import annotations

import uuid
from typing import Callable
from unittest.mock import patch

import pytest

from ownchart.tests.conftest import authed_client, denied_client


def _id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Read endpoints — any active membership


READ_ENDPOINTS: list[tuple[str, str, Callable[[], str]]] = [
    ("providers", "GET", lambda: "/api/conversations/providers"),
    ("list", "GET", lambda: "/api/conversations"),
    ("detail", "GET", lambda: f"/api/conversations/{_id()}"),
    ("candidates", "GET",
     lambda: f"/api/conversations/{_id()}/candidates"),
]


@pytest.mark.parametrize("label,method,path_factory", READ_ENDPOINTS)
def test_conv_read_403_on_record_access_revoked(
    app_fixture, label, method, path_factory,
):
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.request(method, path_factory())
    assert r.status_code == 403, (
        f"{method} {label} returned {r.status_code} {r.text}"
    )
    assert r.json()["detail"]["code"] == "record_access_revoked"


@pytest.mark.parametrize("label,method,path_factory", READ_ENDPOINTS)
def test_conv_read_403_on_no_memberships(
    app_fixture, label, method, path_factory,
):
    c = denied_client(app_fixture, code="no_memberships")
    r = c.request(method, path_factory())
    assert r.status_code == 403, (
        f"{method} {label} returned {r.status_code} {r.text}"
    )
    assert r.json()["detail"]["code"] == "no_memberships"


# ---------------------------------------------------------------------------
# Write endpoints — most require any membership; save-as-topic gates caregiver+


WRITE_ENDPOINTS: list[tuple[str, str, Callable[[], str], dict]] = [
    ("create", "POST", lambda: "/api/conversations",
     {"json": {"kind": "ask"}}),
    ("messages", "POST", lambda: f"/api/conversations/{_id()}/messages",
     {"json": {"content": "hello"}}),
    ("patch", "PATCH", lambda: f"/api/conversations/{_id()}",
     {"json": {"starred": True}}),
    ("delete", "DELETE", lambda: f"/api/conversations/{_id()}", {}),
    ("suggest-topic", "POST",
     lambda: f"/api/conversations/{_id()}/suggest-topic", {}),
    ("save-as-topic", "POST",
     lambda: f"/api/conversations/{_id()}/save-as-topic",
     {"json": {"name": "Test"}}),
]


@pytest.mark.parametrize("label,method,path_factory,kwargs", WRITE_ENDPOINTS)
def test_conv_write_403_on_record_access_revoked(
    app_fixture, label, method, path_factory, kwargs,
):
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.request(method, path_factory(), **kwargs)
    assert r.status_code == 403, (
        f"{method} {label} returned {r.status_code} {r.text}"
    )
    assert r.json()["detail"]["code"] == "record_access_revoked"


@pytest.mark.parametrize("label,method,path_factory,kwargs", WRITE_ENDPOINTS)
def test_conv_write_403_on_no_memberships(
    app_fixture, label, method, path_factory, kwargs,
):
    c = denied_client(app_fixture, code="no_memberships")
    r = c.request(method, path_factory(), **kwargs)
    assert r.status_code == 403, (
        f"{method} {label} returned {r.status_code} {r.text}"
    )
    assert r.json()["detail"]["code"] == "no_memberships"


# ---------------------------------------------------------------------------
# Save-as-topic gates caregiver+. Other writes allow viewer.


def test_save_as_topic_403_insufficient_role_for_viewer(app_fixture):
    """A viewer passes get_auth_context but require_role('caregiver')
    rejects with insufficient_role. Creating a Topic is record-
    shaping; viewers must not be able to do it."""
    c = authed_client(app_fixture, role="viewer")
    r = c.post(
        f"/api/conversations/{_id()}/save-as-topic",
        json={"name": "Test"},
    )
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["detail"]["code"] == "insufficient_role"
    assert body["detail"]["required"] == "caregiver"
    assert body["detail"]["actual"] == "viewer"


# Note: we don't test "viewer can write to messages/patch/delete/
# suggest-topic" here because those handlers reach the DB before any
# role-gate signal is observable, and the test stack has no DB. The
# static signature check below verifies they declare AuthContext (not
# a stronger gate); save-as-topic is the only caregiver+ write and is
# covered by the test above.


# ---------------------------------------------------------------------------
# Static signature check


def test_conv_handler_signatures_include_auth_context():
    from typing import get_type_hints

    from ownchart.core.auth_context import AuthContext
    from ownchart.routes.conversations import (
        create_conversation_route,
        delete_conversation_route,
        get_conversation_route,
        list_conversation_candidates,
        list_conversations,
        list_providers,
        patch_conversation_route,
        post_message_route,
        save_as_topic_route,
        suggest_topic_route,
    )

    handlers = (
        list_providers,
        create_conversation_route,
        list_conversations,
        get_conversation_route,
        post_message_route,
        patch_conversation_route,
        delete_conversation_route,
        list_conversation_candidates,
        suggest_topic_route,
        save_as_topic_route,
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


def test_save_as_topic_dep_is_caregiver_role_gate():
    """save-as-topic creates a record-level Topic; require_role
    must be wired so viewers cannot. Inspect the Depends() default."""
    import inspect
    from fastapi.params import Depends as DependsParam
    from ownchart.routes.conversations import save_as_topic_route

    sig = inspect.signature(save_as_topic_route)
    ctx_param = sig.parameters["ctx"]
    assert isinstance(ctx_param.default, DependsParam)
    dep_fn = ctx_param.default.dependency
    # require_role returns a closure with __name__ == "_dep". Its
    # closure cell carries the required role.
    assert dep_fn.__name__ == "_dep", (
        f"save-as-topic must use require_role(...) for caregiver+ "
        f"gating; dep is {dep_fn.__name__}"
    )
    # Walk the closure to find the required role.
    required_role = None
    for cell in dep_fn.__closure__ or ():
        v = cell.cell_contents
        if isinstance(v, str) and v in ("viewer", "member", "caregiver", "owner"):
            required_role = v
            break
    assert required_role == "caregiver", (
        f"save-as-topic should require caregiver, got {required_role}"
    )


# ---------------------------------------------------------------------------
# Helper signatures — the underlying create_conversation /
# seed_episode_intelligence_conversation / search-scoping plumb-through
# tests. If the route stops passing person_record_id into a helper that
# inserts rows, the rows go in NULL and the audit/scoping breaks.


def test_create_conversation_helper_accepts_person_record_id():
    import inspect
    from ownchart.llm.conversations import create_conversation
    sig = inspect.signature(create_conversation)
    assert "person_record_id" in sig.parameters, (
        "create_conversation must accept person_record_id kwarg"
    )
    assert sig.parameters["person_record_id"].default is None


def test_seed_ei_conversation_accepts_person_record_id():
    import inspect
    from ownchart.llm.episode_intelligence import (
        seed_episode_intelligence_conversation,
    )
    sig = inspect.signature(seed_episode_intelligence_conversation)
    assert "person_record_id" in sig.parameters
    assert sig.parameters["person_record_id"].default is None


def test_run_ei_background_accepts_person_record_id():
    import inspect
    from ownchart.llm.episode_intelligence import (
        run_episode_intelligence_in_background,
    )
    sig = inspect.signature(run_episode_intelligence_in_background)
    assert "person_record_id" in sig.parameters
    assert sig.parameters["person_record_id"].default is None


def test_gather_evidence_accepts_person_record_id():
    import inspect
    from ownchart.llm.conversations import _gather_evidence
    sig = inspect.signature(_gather_evidence)
    assert "person_record_id" in sig.parameters


def test_facts_for_topic_accepts_person_record_id():
    import inspect
    from ownchart.retrieval.topics import facts_for_topic
    sig = inspect.signature(facts_for_topic)
    assert "person_record_id" in sig.parameters


# ---------------------------------------------------------------------------
# Plumbthrough: route passes ctx.active_record_id to create_conversation
#
# This is the load-bearing line on the synchronous-create path. If
# the route stops passing it, every new "ask"-kind conversation lands
# with person_record_id=NULL and the list-filter silently hides them.


def test_create_conversation_route_passes_active_record_id(app_fixture):
    seen: dict = {}

    async def _fake_create_conversation(db, user, **kwargs):
        seen.update(kwargs)
        # Return a fake Conversation row that has an id + scope.
        class _Conv:
            id = uuid.uuid4()
            person_record_id = kwargs.get("person_record_id")
            user_id = user.id
            title = "test"
            kind = kwargs.get("kind", "ask")
            scope = kwargs.get("scope") or {"type": "whole_record"}
            provider = None
            model = None
            privacy_mode = None
            starred = False
            archived = False
            last_message_at = None
            from datetime import datetime, timezone as _tz
            created_at = datetime.now(_tz.utc)
        return _Conv()

    active_record = uuid.uuid4()
    c = authed_client(
        app_fixture, active_record_id=active_record, role="owner",
    )
    with patch(
        "ownchart.routes.conversations.create_conversation",
        new=_fake_create_conversation,
    ):
        r = c.post(
            "/api/conversations",
            json={"kind": "ask"},
        )
    # The response can be 201 or 500 depending on the rest of the
    # stack; what we care about is that we *got into the create
    # call with the right kwarg*.
    assert seen.get("person_record_id") == active_record, (
        f"create_conversation must receive ctx.active_record_id; "
        f"got {seen}"
    )


# ---------------------------------------------------------------------------
# Body never runs on denial — the SQL would touch person_record_id which
# the test DB doesn't have.


def test_list_handler_does_not_run_on_denial(app_fixture):
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.get("/api/conversations")
    assert r.status_code == 403


def test_create_handler_does_not_run_on_denial(app_fixture):
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.post("/api/conversations", json={"kind": "ask"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Conversation model column registered (regression guard)


def test_conversation_models_carry_person_record_id():
    """The SQLAlchemy model must declare person_record_id so the
    route can stamp it on insert. If a refactor accidentally drops
    the column from the model, the route would silently insert with
    a default of None on a NOT NULL column."""
    from ownchart.models.conversation import (
        Conversation, ConversationMessage,
    )
    assert "person_record_id" in Conversation.__table__.columns
    assert "person_record_id" in ConversationMessage.__table__.columns


def test_sensemaking_models_carry_person_record_id():
    """SensemakingJob + SensemakingCandidate also must register the
    column so the EI background path doesn't insert with NULL."""
    from ownchart.models.sensemaking_job import SensemakingJob
    from ownchart.models.sensemaking_candidate import SensemakingCandidate
    assert "person_record_id" in SensemakingJob.__table__.columns
    assert "person_record_id" in SensemakingCandidate.__table__.columns


def test_episode_model_carries_person_record_id():
    from ownchart.models.episode import Episode
    assert "person_record_id" in Episode.__table__.columns


def test_audit_event_model_carries_person_record_id():
    from ownchart.models.audit_event import AuditEvent
    assert "person_record_id" in AuditEvent.__table__.columns
