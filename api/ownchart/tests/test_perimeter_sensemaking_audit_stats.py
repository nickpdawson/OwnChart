"""Cross-record leak tests for sensemaking / audit / stats.

Beta 1 M02 Slice 1, perimeter rollout Batch 9 (final).

Three surfaces:

  - **sensemaking** (`/api/sensemaking*`, `/api/sources/{id}/sensemake`,
    `/api/sources/{id}/candidates`, `/api/review/*`): writes that
    produce SensemakingJob + SensemakingCandidate + AuditEvent rows.
    Caregiver+ on writes, any-membership on reads, every insert
    stamps person_record_id (via helpers in llm/medication_triage.py,
    llm/provider_triage.py, llm/sensemaking.py).

  - **audit** (`/api/audit/model-runs*`): ModelRun is the SYSTEM
    audit catalog. By design (M02 Slice 1 Batch 9) it stays
    **admin-global** per PM's allowlist for "instance/admin/system
    audit views may remain admin/global where appropriate."
    Per-user audit needs are met by per-turn ConversationCitation,
    BriefMessage.citations, and the Ask response shape.

  - **stats** (`/api/stats`): dashboard aggregation. Every count,
    fact-state group-by, topic list, and recent-source query
    filters by ctx.active_record_id. Pre-M02 this endpoint had
    NO user scoping; this batch closes a pre-existing leak in
    addition to the M02 perimeter.

Also includes a cross-batch regression: every SQLAlchemy model
that ANY perimeter SELECT references via `Model.person_record_id`
must declare the column. The Batch 9 audit discovered that
ExtractedFact / SourceDocument / EvidenceAnchor were missing
the column even though Batches 2–8 referenced them — every
perimeter SELECT would have AttributeError'd at request time in
production. This test pins the regression so the gap can't recur.
"""

from __future__ import annotations

import inspect
import uuid
from typing import Callable

import pytest

from ownchart.tests.conftest import authed_client, denied_client


def _id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Sensemaking


# NB: the sensemaking router is mounted at /api (not /api/sensemaking)
# in main.py — its handler decorators carry the full path segments
# (/review/..., /sources/..., /sensemaking/...).
SM_READ_ENDPOINTS: list[tuple[str, str, Callable[[], str]]] = [
    ("pattern-stats", "GET", lambda: "/api/review/pattern-stats"),
    ("get-job", "GET",
     lambda: f"/api/sensemaking/jobs/{_id()}"),
    ("list-source-candidates", "GET",
     lambda: f"/api/sources/{_id()}/candidates"),
]

SM_WRITE_ENDPOINTS: list[tuple[str, str, Callable[[], str], dict]] = [
    ("medication-triage", "POST",
     lambda: "/api/review/medication-patterns", {}),
    ("provider-triage", "POST",
     lambda: "/api/review/provider-patterns", {}),
    ("source-sensemake", "POST",
     lambda: f"/api/sources/{_id()}/sensemake", {}),
    ("patch-disposition", "PATCH",
     lambda: f"/api/sensemaking/candidates/{_id()}",
     {"json": {"disposition": "accepted"}}),
]


@pytest.mark.parametrize("label,method,path_factory", SM_READ_ENDPOINTS)
def test_sm_read_403_on_record_access_revoked(
    app_fixture, label, method, path_factory,
):
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.request(method, path_factory())
    assert r.status_code == 403, (
        f"{method} {label} returned {r.status_code} {r.text}"
    )
    assert r.json()["detail"]["code"] == "record_access_revoked"


@pytest.mark.parametrize("label,method,path_factory", SM_READ_ENDPOINTS)
def test_sm_read_403_on_no_memberships(
    app_fixture, label, method, path_factory,
):
    c = denied_client(app_fixture, code="no_memberships")
    r = c.request(method, path_factory())
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "no_memberships"


@pytest.mark.parametrize("label,method,path_factory,kwargs", SM_WRITE_ENDPOINTS)
def test_sm_write_403_on_record_access_revoked(
    app_fixture, label, method, path_factory, kwargs,
):
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.request(method, path_factory(), **kwargs)
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "record_access_revoked"


@pytest.mark.parametrize("label,method,path_factory,kwargs", SM_WRITE_ENDPOINTS)
def test_sm_write_403_insufficient_role_for_viewer(
    app_fixture, label, method, path_factory, kwargs,
):
    """Sensemaking writes produce SensemakingJob + Candidate +
    AuditEvent rows that shape every Review Inbox surface.
    Viewers must not be able to trigger them."""
    c = authed_client(app_fixture, role="viewer")
    r = c.request(method, path_factory(), **kwargs)
    assert r.status_code == 403
    body = r.json()
    assert body["detail"]["code"] == "insufficient_role"
    assert body["detail"]["required"] == "caregiver"


def test_sm_handler_signatures_include_auth_context():
    from typing import get_type_hints
    from ownchart.core.auth_context import AuthContext
    from ownchart.routes.sensemaking import (
        get_sensemaking_job,
        list_source_candidates,
        patch_candidate_disposition,
        pattern_suppression_stats,
        run_medication_pattern_triage,
        run_provider_pattern_triage,
        run_source_sensemake,
    )

    for fn in (
        run_medication_pattern_triage,
        run_provider_pattern_triage,
        pattern_suppression_stats,
        run_source_sensemake,
        get_sensemaking_job,
        list_source_candidates,
        patch_candidate_disposition,
    ):
        hints = get_type_hints(fn)
        ctx_params = [n for n, t in hints.items() if t is AuthContext]
        assert ctx_params == ["ctx"], (
            f"{fn.__name__} must declare ctx: AuthContext; got {ctx_params}"
        )


def test_sm_writes_use_caregiver_role_gate():
    """Inspect each write handler's `ctx` Depends to ensure
    require_role('caregiver') is wired. Same identity check used
    in earlier batches."""
    from fastapi.params import Depends as DependsParam
    from ownchart.routes.sensemaking import (
        patch_candidate_disposition,
        run_medication_pattern_triage,
        run_provider_pattern_triage,
        run_source_sensemake,
    )

    for fn in (
        run_medication_pattern_triage,
        run_provider_pattern_triage,
        run_source_sensemake,
        patch_candidate_disposition,
    ):
        sig = inspect.signature(fn)
        ctx_param = sig.parameters["ctx"]
        assert isinstance(ctx_param.default, DependsParam), fn.__name__
        dep = ctx_param.default.dependency
        assert dep.__name__ == "_dep", (
            f"{fn.__name__} must use require_role; got {dep.__name__}"
        )
        required = None
        for cell in dep.__closure__ or ():
            v = cell.cell_contents
            if isinstance(v, str) and v in ("viewer", "member", "caregiver", "owner"):
                required = v
                break
        assert required == "caregiver", (
            f"{fn.__name__} must gate at caregiver, got {required}"
        )


def test_sm_triage_helpers_accept_person_record_id():
    """Helpers in llm/medication_triage.py, llm/provider_triage.py,
    llm/sensemaking.py must accept person_record_id as a keyword-
    only arg. Defaults to None for backward compat with in-process
    callers; the route layer is responsible for always passing it."""
    from ownchart.llm.medication_triage import triage_medication_patterns
    from ownchart.llm.provider_triage import triage_provider_patterns
    from ownchart.llm.sensemaking import summarize_source

    for helper in (
        triage_medication_patterns,
        triage_provider_patterns,
        summarize_source,
    ):
        sig = inspect.signature(helper)
        assert "person_record_id" in sig.parameters, helper.__name__


def test_sm_pattern_stats_filters_by_record():
    """Source-level: pattern_suppression_stats must AND in
    SensemakingCandidate.person_record_id so a caregiver looking at
    Mom's stats doesn't see Dad's accepted-pattern count."""
    from ownchart.routes.sensemaking import pattern_suppression_stats
    src = inspect.getsource(pattern_suppression_stats)
    assert "SensemakingCandidate.person_record_id == ctx.active_record_id" in src


def test_sm_list_source_candidates_filters_by_record():
    from ownchart.routes.sensemaking import list_source_candidates
    src = inspect.getsource(list_source_candidates)
    assert "SensemakingCandidate.person_record_id == ctx.active_record_id" in src


# ---------------------------------------------------------------------------
# Audit — admin-global by design


def test_audit_list_403_on_record_access_revoked(app_fixture):
    """Even at the AuthContext layer the audit list refuses non-
    members. (After they pass AuthContext they STILL get 403
    instance_admin_required if they're not admin — see below.)"""
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.get("/api/audit/model-runs")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "record_access_revoked"


def test_audit_get_403_on_record_access_revoked(app_fixture):
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.get(f"/api/audit/model-runs/{_id()}")
    assert r.status_code == 403


def test_audit_list_403_for_non_admin(app_fixture):
    """A member who is NOT an instance admin gets 403
    instance_admin_required. The audit catalog is intentionally
    admin-global per PM's allowlist — per-user audit needs are
    met by per-turn citations on each thread."""
    c = authed_client(app_fixture, role="owner", is_instance_admin=False)
    r = c.get("/api/audit/model-runs")
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["detail"]["code"] == "instance_admin_required"


def test_audit_get_403_for_non_admin(app_fixture):
    fake_id = _id()
    c = authed_client(app_fixture, role="owner", is_instance_admin=False)
    r = c.get(f"/api/audit/model-runs/{fake_id}")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "instance_admin_required"


def test_audit_handlers_signatures_include_auth_context():
    from typing import get_type_hints
    from ownchart.core.auth_context import AuthContext
    from ownchart.routes.audit import get_model_run, list_model_runs

    for fn in (list_model_runs, get_model_run):
        hints = get_type_hints(fn)
        ctx_params = [n for n, t in hints.items() if t is AuthContext]
        assert ctx_params == ["ctx"], fn.__name__


def test_audit_handlers_call_admin_helper():
    """Source-level: both handlers must invoke
    _require_instance_admin(ctx) as their first body line. If a
    refactor demotes this surface to any-membership, this test
    fires."""
    from ownchart.routes.audit import get_model_run, list_model_runs

    for fn in (list_model_runs, get_model_run):
        src = inspect.getsource(fn)
        assert "_require_instance_admin(ctx)" in src, fn.__name__


def test_audit_admin_helper_emits_instance_admin_required_code():
    """The helper's 403 detail.code must be `instance_admin_required`
    so iOS / web can distinguish 'not a member' (record_access_revoked)
    from 'not an admin' (instance_admin_required) and route the user
    to the right recovery UI."""
    from ownchart.routes.audit import _require_instance_admin
    src = inspect.getsource(_require_instance_admin)
    assert "instance_admin_required" in src


# ---------------------------------------------------------------------------
# Stats — dashboard aggregation


# NB: stats router is mounted at /api/dashboard (the route lives in
# stats.py but the URL is /api/dashboard per main.py).


def test_stats_403_on_record_access_revoked(app_fixture):
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.get("/api/dashboard")
    assert r.status_code == 403


def test_stats_403_on_no_memberships(app_fixture):
    c = denied_client(app_fixture, code="no_memberships")
    r = c.get("/api/dashboard")
    assert r.status_code == 403


def test_stats_handler_signature_includes_auth_context():
    from typing import get_type_hints
    from ownchart.core.auth_context import AuthContext
    from ownchart.routes.stats import get_dashboard_stats
    hints = get_type_hints(get_dashboard_stats)
    ctx_params = [n for n, t in hints.items() if t is AuthContext]
    assert ctx_params == ["ctx"]


def test_stats_aggregations_all_filter_by_record():
    """Source-level aggregation-leak regression. PM Batch 7
    pattern, applied to stats — every SELECT in
    get_dashboard_stats must carry the record-scope clause."""
    from ownchart.routes.stats import get_dashboard_stats
    src = inspect.getsource(get_dashboard_stats)
    # Source count + facts-by-state + topics + recent_sources = 4
    # SELECTs. Each one needs the filter; we expect at least 4
    # mentions of the clause.
    count = src.count("person_record_id == ctx.active_record_id")
    assert count >= 4, (
        f"get_dashboard_stats missing record-scope filters; "
        f"only {count} mentions in source"
    )


# ---------------------------------------------------------------------------
# Cross-batch regression: model column declarations
#
# Discovered during Batch 9: ExtractedFact / SourceDocument /
# EvidenceAnchor were missing the `person_record_id` column
# declaration even though Batches 2–8 referenced it in every
# perimeter SELECT. The denied_client perimeter tests didn't catch
# it because they 403 before SQL compiles, so the AttributeError
# only surfaces in production. This regression test pins every
# perimeter-touched model so the gap can't recur.


_PERIMETER_MODELS_REQUIRED = [
    # Discovered missing in Batch 9 audit — load-bearing for every
    # source / fact / anchor SELECT across Batches 2–8.
    ("ownchart.models.extracted_fact", "ExtractedFact"),
    ("ownchart.models.source_document", "SourceDocument"),
    ("ownchart.models.evidence_anchor", "EvidenceAnchor"),
    # Already declared in earlier batches; included for regression
    # coverage so a future model edit can't quietly remove the
    # column.
    ("ownchart.models.topic", "Topic"),
    ("ownchart.models.episode", "Episode"),
    ("ownchart.models.conversation", "Conversation"),
    ("ownchart.models.conversation", "ConversationMessage"),
    ("ownchart.models.sensemaking_job", "SensemakingJob"),
    ("ownchart.models.sensemaking_candidate", "SensemakingCandidate"),
    ("ownchart.models.audit_event", "AuditEvent"),
    ("ownchart.models.brief_message", "BriefMessage"),
    ("ownchart.models.provider_connection", "ProviderConnection"),
    ("ownchart.models.oauth_session", "OAuthSession"),
    ("ownchart.models.topic_brief", "TopicBrief"),
]


@pytest.mark.parametrize("module_path,class_name", _PERIMETER_MODELS_REQUIRED)
def test_perimeter_models_declare_person_record_id(module_path, class_name):
    """Every model referenced by a `.where(Model.person_record_id == ...)`
    clause in any perimeter route MUST declare the column. Without
    the SQLAlchemy column, the WHERE clause raises AttributeError
    at request time — the perimeter tests don't catch it because
    they 403 before SQL compiles.

    Originally discovered: Batch 9 (subagent A's verification
    flagged ExtractedFact missing the column). All three were
    backfilled in Batch 9. This test pins the regression."""
    import importlib
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    assert hasattr(cls, "person_record_id"), (
        f"{class_name} must declare a person_record_id mapped_column; "
        "every perimeter SELECT that filters on it would otherwise "
        "AttributeError at request time"
    )
    # Also verify the column lands on the actual SQLAlchemy table —
    # `hasattr` alone could be satisfied by a non-column attribute.
    assert "person_record_id" in cls.__table__.columns, (
        f"{class_name}.person_record_id must be a mapped column"
    )


def test_perimeter_select_compiles_for_critical_models():
    """End-to-end smoke: build the exact SELECT shape every
    perimeter route uses, ensure it compiles. Catches the
    AttributeError class of bug at suite-load time."""
    from sqlalchemy import select
    from ownchart.models.extracted_fact import ExtractedFact
    from ownchart.models.source_document import SourceDocument
    from ownchart.models.evidence_anchor import EvidenceAnchor

    rec = uuid.uuid4()
    for model in (ExtractedFact, SourceDocument, EvidenceAnchor):
        stmt = select(model).where(model.person_record_id == rec)
        # str() forces SQL compile.
        rendered = str(stmt.compile())
        assert "person_record_id" in rendered, model.__name__
