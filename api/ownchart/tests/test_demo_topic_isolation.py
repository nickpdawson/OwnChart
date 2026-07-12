"""MED-7 / HIGH-8: demo per-visitor isolation for Topics and the Home
insight cache.

Root cause: the demo account is shared and single-record, so
``person_record_id`` scope alone can't isolate visitors. Topics created
by a visitor's save-as-topic now carry ``scope.demo_session_id``, and
every Topic reader gates on it; the Home insight cache key now includes
the visitor id.

Pure-function — no DB, no HTTP.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import patch

from ownchart.core import demo_session as ds
from ownchart.routes.home_ai import _insight_cache_key


def _req(cookies=None):
    return SimpleNamespace(cookies=cookies or {})


def _demo_on():
    return patch.object(ds, "get_settings", lambda: SimpleNamespace(demo_mode=True))


def _demo_off():
    return patch.object(ds, "get_settings", lambda: SimpleNamespace(demo_mode=False))


# ---------------------------------------------------------------------------
# demo_scope_visible — Topic detail gating


def test_scope_visible_seeded_topic_visible_to_all():
    req = _req({ds.DEMO_SESSION_COOKIE: "visitor-A"})
    with _demo_on():
        # Seeded Topic (scope NULL) is visible to any visitor.
        assert ds.demo_scope_visible(None, req) is True
        assert ds.demo_scope_visible({}, req) is True


def test_scope_visible_own_topic():
    req = _req({ds.DEMO_SESSION_COOKIE: "visitor-A"})
    with _demo_on():
        assert ds.demo_scope_visible({"demo_session_id": "visitor-A"}, req) is True


def test_scope_visible_other_visitor_topic_hidden():
    req = _req({ds.DEMO_SESSION_COOKIE: "visitor-B"})
    with _demo_on():
        assert ds.demo_scope_visible({"demo_session_id": "visitor-A"}, req) is False


def test_scope_visible_noop_outside_demo():
    req = _req()
    with _demo_off():
        # Non-demo: every Topic is visible regardless of scope.
        assert ds.demo_scope_visible({"demo_session_id": "x"}, req) is True


# ---------------------------------------------------------------------------
# demo_seeded_or_owned_clause — SQL reader filter


def test_clause_none_outside_demo():
    req = _req({ds.DEMO_SESSION_COOKIE: "v1"})
    with _demo_off():
        assert ds.demo_seeded_or_owned_clause(
            req, table="topics", json_col="scope",
        ) is None


def test_clause_with_cookie_allows_seeded_or_owned():
    req = _req({ds.DEMO_SESSION_COOKIE: "v1"})
    with _demo_on():
        clause = ds.demo_seeded_or_owned_clause(
            req, table="topics", json_col="scope",
        )
    sql = str(clause)
    assert "topics.scope" in sql
    assert "IS NULL OR" in sql  # seeded rows still visible
    assert clause.compile().params.get("dsid") == "v1"


def test_clause_without_cookie_only_seeded():
    req = _req()  # no cookie yet
    with _demo_on():
        clause = ds.demo_seeded_or_owned_clause(
            req, table="topics", json_col="scope",
        )
    sql = str(clause)
    assert "IS NULL" in sql
    assert "OR" not in sql  # only seeded rows; no visitor branch


def test_clause_for_sid_helper_matches():
    # Background contexts pass a raw sid instead of a Request.
    with_sid = str(ds.demo_topic_clause_for_sid("v9"))
    without = str(ds.demo_topic_clause_for_sid(None))
    assert "IS NULL OR" in with_sid
    assert "OR" not in without


# ---------------------------------------------------------------------------
# Home insight cache key — per-visitor partitioning


def test_insight_key_distinct_per_visitor():
    u, r = uuid.uuid4(), uuid.uuid4()
    a = _insight_cache_key(u, r, "2026-07-11", "visitor-A")
    b = _insight_cache_key(u, r, "2026-07-11", "visitor-B")
    assert a != b


def test_insight_key_same_visitor_same_key():
    u, r = uuid.uuid4(), uuid.uuid4()
    a = _insight_cache_key(u, r, "2026-07-11", "visitor-A")
    b = _insight_cache_key(u, r, "2026-07-11", "visitor-A")
    assert a == b


def test_insight_key_none_is_base_shape():
    u, r = uuid.uuid4(), uuid.uuid4()
    k = _insight_cache_key(u, r, "2026-07-11")
    assert k == (u, r, "2026-07-11", None)
