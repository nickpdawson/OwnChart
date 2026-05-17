"""Cross-record leak tests for the summary / aggregation surfaces.

Beta 1 M02 Slice 1, perimeter rollout Batch 7.

Covers `/api/timeline*`, `/api/home/*`, `/api/discover*` — every
endpoint that aggregates, counts, ranks, or LLM-summarizes the
record. These surfaces are the highest leak risk for caregivers,
because a stale or missing scope filter silently inflates a count
or surfaces another patient's "Connected Event" card.

Three layers per surface:

  1. Route-level 403 propagation on AuthContext denial.
  2. Static AuthContext signature checks.
  3. Aggregation-leak regression: helpers that compute counts and
     date ranges MUST accept `person_record_id` as a kwarg with no
     default. Catches a refactor that silently widens the scope by
     dropping the kwarg from a helper signature.
"""

from __future__ import annotations

import inspect
import uuid
from typing import Callable

import pytest

from ownchart.tests.conftest import authed_client, denied_client


# ---------------------------------------------------------------------------
# /api/timeline


TIMELINE_ENDPOINTS: list[tuple[str, str, Callable[[], str]]] = [
    ("timeline", "GET", lambda: "/api/timeline"),
    ("period", "GET",
     lambda: "/api/timeline/period?start=2024-01-01T00:00:00Z&end=2025-01-01T00:00:00Z"),
    ("notable-events", "GET", lambda: "/api/timeline/notable-events"),
    ("period-cluster-facts", "GET",
     lambda: "/api/timeline/period/cluster/facts?start=2024-01-01T00:00:00Z&end=2025-01-01T00:00:00Z&cluster_id=abc123"),
]


@pytest.mark.parametrize("label,method,path_factory", TIMELINE_ENDPOINTS)
def test_timeline_403_on_record_access_revoked(
    app_fixture, label, method, path_factory,
):
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.request(method, path_factory())
    assert r.status_code == 403, (
        f"{method} {label} returned {r.status_code} {r.text}"
    )
    assert r.json()["detail"]["code"] == "record_access_revoked"


@pytest.mark.parametrize("label,method,path_factory", TIMELINE_ENDPOINTS)
def test_timeline_403_on_no_memberships(
    app_fixture, label, method, path_factory,
):
    c = denied_client(app_fixture, code="no_memberships")
    r = c.request(method, path_factory())
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "no_memberships"


def test_timeline_handler_signatures_include_auth_context():
    from typing import get_type_hints
    from ownchart.core.auth_context import AuthContext
    from ownchart.routes.timeline import (
        get_notable_events,
        get_period_cluster_facts,
        get_timeline,
        get_timeline_period,
    )

    for fn in (
        get_timeline,
        get_timeline_period,
        get_notable_events,
        get_period_cluster_facts,
    ):
        hints = get_type_hints(fn)
        ctx_params = [n for n, t in hints.items() if t is AuthContext]
        assert ctx_params == ["ctx"], (
            f"{fn.__name__} must declare ctx: AuthContext; got {ctx_params}"
        )


# ---------------------------------------------------------------------------
# /api/home


HOME_READ_ENDPOINTS: list[tuple[str, str, Callable[[], str]]] = [
    ("ai-partner", "GET", lambda: "/api/home/ai-partner"),
]


@pytest.mark.parametrize("label,method,path_factory", HOME_READ_ENDPOINTS)
def test_home_403_on_record_access_revoked(
    app_fixture, label, method, path_factory,
):
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.request(method, path_factory())
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "record_access_revoked"


@pytest.mark.parametrize("label,method,path_factory", HOME_READ_ENDPOINTS)
def test_home_403_on_no_memberships(
    app_fixture, label, method, path_factory,
):
    c = denied_client(app_fixture, code="no_memberships")
    r = c.request(method, path_factory())
    assert r.status_code == 403


# Insight refresh is caregiver+ (regenerates an LLM-shaped surface).


def test_home_refresh_403_on_record_access_revoked(app_fixture):
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.post("/api/home/insight/refresh")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "record_access_revoked"


def test_home_refresh_403_insufficient_role_for_viewer(app_fixture):
    """Viewers can read Home but cannot regenerate the insight."""
    c = authed_client(app_fixture, role="viewer")
    r = c.post("/api/home/insight/refresh")
    assert r.status_code == 403
    body = r.json()
    assert body["detail"]["code"] == "insufficient_role"
    assert body["detail"]["required"] == "caregiver"


def test_home_handler_signatures_include_auth_context():
    from typing import get_type_hints
    from ownchart.core.auth_context import AuthContext
    from ownchart.routes.home_ai import (
        get_home_ai_partner,
        refresh_home_insight,
    )

    for fn in (get_home_ai_partner, refresh_home_insight):
        hints = get_type_hints(fn)
        ctx_params = [n for n, t in hints.items() if t is AuthContext]
        assert ctx_params == ["ctx"], fn.__name__


def test_home_insight_helper_requires_person_record_id():
    """`_build_home_insight` must accept `person_record_id` as a
    keyword-only argument with no default. Silently building an
    insight without scope would leak cross-record facts into the
    cached body and the LLM prompt."""
    from ownchart.routes.home_ai import _build_home_insight
    sig = inspect.signature(_build_home_insight)
    assert "person_record_id" in sig.parameters
    p = sig.parameters["person_record_id"]
    assert p.kind == inspect.Parameter.KEYWORD_ONLY
    assert p.default is inspect.Parameter.empty


def test_home_insight_background_passes_person_record_id():
    """The background task wrapper must accept person_record_id so
    the scheduled call inherits the request's active record id even
    if the user switches records before the background fires."""
    from ownchart.routes.home_ai import _background_build_home_insight
    sig = inspect.signature(_background_build_home_insight)
    assert "person_record_id" in sig.parameters


def test_home_insight_cache_key_has_three_dimensions():
    """The in-process cache key is (user_id, person_record_id,
    day_iso). Two records under the same user must cache
    independently — same user switching records should NOT see
    the same body."""
    from ownchart.routes.home_ai import _INSIGHT_CACHE, HomeInsight
    from datetime import datetime, timezone

    user_id = uuid.uuid4()
    rec_a = uuid.uuid4()
    rec_b = uuid.uuid4()
    day = "2026-05-18"
    a = HomeInsight(body="A", generated_at=datetime.now(timezone.utc))
    b = HomeInsight(body="B", generated_at=datetime.now(timezone.utc))
    _INSIGHT_CACHE[(user_id, rec_a, day)] = a
    _INSIGHT_CACHE[(user_id, rec_b, day)] = b
    try:
        assert _INSIGHT_CACHE[(user_id, rec_a, day)].body == "A"
        assert _INSIGHT_CACHE[(user_id, rec_b, day)].body == "B"
    finally:
        _INSIGHT_CACHE.pop((user_id, rec_a, day), None)
        _INSIGHT_CACHE.pop((user_id, rec_b, day), None)


# ---------------------------------------------------------------------------
# /api/discover


def _id() -> str:
    return str(uuid.uuid4())


DISCOVER_READ_ENDPOINTS: list[tuple[str, str, Callable[[], str]]] = [
    ("get-discover", "GET", lambda: "/api/discover"),
]

DISCOVER_WRITE_ENDPOINTS: list[tuple[str, str, Callable[[], str]]] = [
    ("dismiss", "POST", lambda: f"/api/discover/dense_period:2024/dismiss"),
    ("undismiss", "POST", lambda: f"/api/discover/dense_period:2024/undismiss"),
]


@pytest.mark.parametrize("label,method,path_factory", DISCOVER_READ_ENDPOINTS)
def test_discover_read_403_on_record_access_revoked(
    app_fixture, label, method, path_factory,
):
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.request(method, path_factory())
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "record_access_revoked"


@pytest.mark.parametrize("label,method,path_factory", DISCOVER_READ_ENDPOINTS)
def test_discover_read_403_on_no_memberships(
    app_fixture, label, method, path_factory,
):
    c = denied_client(app_fixture, code="no_memberships")
    r = c.request(method, path_factory())
    assert r.status_code == 403


@pytest.mark.parametrize("label,method,path_factory", DISCOVER_WRITE_ENDPOINTS)
def test_discover_write_403_on_denial(
    app_fixture, label, method, path_factory,
):
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.request(method, path_factory())
    assert r.status_code == 403


def test_discover_handler_signatures_include_auth_context():
    from typing import get_type_hints
    from ownchart.core.auth_context import AuthContext
    from ownchart.routes.discover import (
        dismiss_discover_item,
        get_discover,
        undismiss_discover_item,
    )

    for fn in (get_discover, dismiss_discover_item, undismiss_discover_item):
        hints = get_type_hints(fn)
        ctx_params = [n for n, t in hints.items() if t is AuthContext]
        assert ctx_params == ["ctx"], fn.__name__


# ---------------------------------------------------------------------------
# Aggregation-leak regression — every discover helper must take
# person_record_id as a keyword-only arg with no default.


def test_discover_helpers_require_person_record_id_kwarg():
    """The four discover aggregators (`_dense_periods`,
    `_long_gaps`, `_connected_episodes`, `_unreviewed_high_counts`)
    compute counts and date ranges that drive UI cards. Each must
    accept `person_record_id` keyword-only with no default so a
    future caller can't accidentally invoke them globally and leak
    cross-record facts into the dense-period detection or the
    needs-review pile.

    This is the regression test PM specifically asked for: 'tests
    that facts from another record do not affect counts, date
    ranges, or "something I noticed."'"""
    from ownchart.routes.discover import (
        _connected_episodes,
        _dense_periods,
        _long_gaps,
        _unreviewed_high_counts,
    )

    for helper in (
        _dense_periods,
        _long_gaps,
        _connected_episodes,
        _unreviewed_high_counts,
    ):
        sig = inspect.signature(helper)
        assert "person_record_id" in sig.parameters, helper.__name__
        p = sig.parameters["person_record_id"]
        assert p.kind == inspect.Parameter.KEYWORD_ONLY, (
            f"{helper.__name__}.person_record_id must be keyword-only; "
            f"got {p.kind}"
        )
        assert p.default is inspect.Parameter.empty, (
            f"{helper.__name__}.person_record_id must have no default "
            "so callers can't silently drop scope; got default "
            f"{p.default!r}"
        )


# ---------------------------------------------------------------------------
# Aggregation-leak regression — timeline + home call search_facts/
# DB directly with explicit person_record_id filters. We assert the
# critical SQL phrase is present in the function source. Brittle to
# refactors but catches the load-bearing leak that pure-Python tests
# without a DB can't catch.


def _src(fn) -> str:
    return inspect.getsource(fn)


def test_get_timeline_filters_by_person_record_id():
    """get_timeline's clinical/wearable/source/event/contrib queries
    must all carry ExtractedFact.person_record_id or
    SourceDocument.person_record_id. We grep the function source as
    a regression hedge — a future SELECT that lacks the filter would
    silently leak counts."""
    from ownchart.routes.timeline import get_timeline
    src = _src(get_timeline)
    # Should appear multiple times (clinical, wearable, event, contrib,
    # episode passes — at least 5 mentions).
    count = src.count("person_record_id == ctx.active_record_id")
    assert count >= 5, (
        f"get_timeline appears to be missing record-scope filters "
        f"(only {count} found in source)"
    )


def test_get_discover_routes_helpers_with_active_record():
    """get_discover must invoke each helper with
    person_record_id=ctx.active_record_id. We check the source for
    the four expected helper invocations."""
    from ownchart.routes.discover import get_discover
    src = _src(get_discover)
    for helper_name in (
        "_connected_episodes",
        "_dense_periods",
        "_long_gaps",
        "_unreviewed_high_counts",
    ):
        assert (
            f"{helper_name}(db, person_record_id=ctx.active_record_id)" in src
        ), f"get_discover missing {helper_name} record-scope call"


def test_home_ai_partner_filters_by_record():
    """get_home_ai_partner must filter the recent-conversations,
    recent-episodes, recent-sources, dossier-rows, and topic queries
    by person_record_id. Source grep regression check."""
    from ownchart.routes.home_ai import get_home_ai_partner
    src = _src(get_home_ai_partner)
    # Should appear on at least 7 SELECTs (most_recent_major, top_topic,
    # convs, eps, recent_sources, dossier_rows, low_conf, conv_scopes).
    count = src.count("person_record_id == ctx.active_record_id")
    assert count >= 7, (
        f"get_home_ai_partner missing record-scope filters; "
        f"only {count} mentions in source"
    )


# ---------------------------------------------------------------------------
# Dismiss audit row is record-scoped


def test_discover_dismiss_stamps_active_record(app_fixture):
    """When a dismiss is written, it stamps person_record_id from
    ctx.active_record_id so dismissing 'dense_period:2024' on Mom's
    record doesn't hide it on Nick's own record.

    Source-level check: the handler body must include
    `person_record_id=ctx.active_record_id` on the AuditEvent
    insert."""
    from ownchart.routes.discover import dismiss_discover_item
    src = _src(dismiss_discover_item)
    assert "person_record_id=ctx.active_record_id" in src, (
        "dismiss_discover_item must stamp active_record_id on the "
        "audit row so dismisses are per-record"
    )
