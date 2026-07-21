"""Route tests for GET /api/account/data-map (Wave 3.1 — Privacy & Storage map).

Covers: membership-authz perimeter (403 propagation), record-scoping,
demo-session isolation of visitor-writable counts, the response shape,
and the pure helpers. Same fake-session idiom as
test_invite_preview_post.py — the route's DB queries are served by a
statement-routing fake so no Postgres is needed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from ownchart.routes.data_map import (
    evidence_bytes_on_disk,
    preferred_provider_key,
    retention_statements,
)
from ownchart.tests.conftest import authed_client, denied_client

_NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Statement-routing fake session


class _Scalars:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


class _Result:
    def __init__(self, *, one=None, all_=None, scalar=None, scalars=None):
        self._one = one
        self._all = all_ if all_ is not None else []
        self._scalar = scalar
        self._scalars = scalars if scalars is not None else []

    def one(self):
        return self._one

    def all(self):
        return list(self._all)

    def scalar(self):
        return self._scalar

    def scalar_one(self):
        return self._scalar

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return _Scalars(self._scalars)


class _FakeSession:
    """Routes each execute() to a canned result by table/keyword.

    Records every statement string so tests can assert record-scoping.
    """

    def __init__(self, canned: dict):
        self.canned = canned
        self.executed: list[str] = []

    async def execute(self, statement, *a, **k):
        s = str(statement)
        self.executed.append(s)
        # Order matters: most specific keyword first.
        if "user_settings" in s:
            return _Result(scalar=None)  # → registry default
        if "model_runs" in s:
            return _Result(scalar=0)  # month-to-date spend sum
        if "storage_uri" in s:
            return _Result(scalars=self.canned.get("uris", []))
        if "GROUP BY" in s:
            return _Result(all_=self.canned.get("kind_rows", []))
        if "source_documents" in s:
            return _Result(one=self.canned.get("sources", (0, None)))
        if "extraction_method" in s:
            return _Result(one=self.canned.get("hk_facts", (0, None)))
        if "extracted_facts" in s:
            return _Result(one=self.canned.get("facts", (0, None)))
        if "healthkit_sync_cursors" in s:
            return _Result(one=self.canned.get("cursors", (0, None)))
        if "conversations" in s:
            return _Result(one=self.canned.get("conversations", (0, None)))
        if "episodes" in s:
            return _Result(one=self.canned.get("episodes", (0, None)))
        raise AssertionError(f"unrouted statement: {s[:120]}")

    async def get(self, *a, **k):
        return None


def _override_session(app, session):
    from ownchart.core.db import get_session

    async def _fake():
        yield session

    app.dependency_overrides[get_session] = _fake


def _canned(**over):
    base = {
        "sources": (5, _NOW),
        "kind_rows": [("fax_pdf", 3, _NOW), ("ccda_xml", 2, _NOW)],
        "uris": ["/data/evidence/ab/cd/does-not-exist.pdf"],
        "facts": (42, _NOW),
        "hk_facts": (17, _NOW),
        "cursors": (2, _NOW),
        "conversations": (4, _NOW),
        "episodes": (1, _NOW),
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Pure helpers


def test_preferred_provider_key_maps_setting():
    assert preferred_provider_key("auto") is None
    assert preferred_provider_key(None) is None
    assert preferred_provider_key("") is None
    assert preferred_provider_key("anthropic") == "anthropic"
    assert preferred_provider_key("OpenAI") == "openai"
    assert preferred_provider_key("local") == "local_echo"


def test_retention_statements_truthful():
    plain = retention_statements(demo_mode=False, instance_name="OwnChart")
    assert plain and all(isinstance(s, str) for s in plain)
    # No demo purge line outside demo mode.
    assert not any("purged" in s for s in plain)
    demo = retention_statements(demo_mode=True, instance_name="OwnChart")
    # Demo adds a truthful synthetic-data + 24h purge line, never a
    # "nothing persists" overpromise.
    assert any("synthetic" in s and "24 hours" in s for s in demo)
    assert not any("nothing persists" in s.lower() for s in demo)


def test_evidence_bytes_on_disk_is_fail_safe(tmp_path):
    # Missing files and paths outside the vault root → counted as 0,
    # never raise.
    root = tmp_path / "evidence"
    root.mkdir()
    blob = root / "ab" / "cd"
    blob.mkdir(parents=True)
    real = blob / "x.pdf"
    real.write_bytes(b"1234567890")  # 10 bytes
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"nope")

    total = evidence_bytes_on_disk(
        [str(real), str(outside), "/does/not/exist"],
        evidence_root=root,
    )
    assert total == 10  # only the in-vault, existing file counts


# ---------------------------------------------------------------------------
# Perimeter: AuthContext 403 propagates


def test_data_map_propagates_record_access_revoked(app_fixture):
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.get("/api/account/data-map")
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "record_access_revoked"


def test_data_map_propagates_no_memberships(app_fixture):
    c = denied_client(app_fixture, code="no_memberships")
    r = c.get("/api/account/data-map")
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "no_memberships"


# ---------------------------------------------------------------------------
# Happy path shape + record scoping


def test_data_map_happy_shape_and_record_scoping(app_fixture):
    uid = uuid.uuid4()
    rid = uuid.uuid4()
    sess = _FakeSession(_canned())
    _override_session(app_fixture, sess)
    c = authed_client(
        app_fixture, user_id=uid, active_record_id=rid, role="owner",
    )

    r = c.get("/api/account/data-map")
    assert r.status_code == 200, r.text
    body = r.json()

    # Record block reflects the active AuthContext.
    assert body["record"]["id"] == str(rid)
    assert body["record"]["role"] == "owner"

    # Server counts reflect the canned aggregates.
    assert body["server"]["sources"]["total"] == 5
    assert {k["kind"] for k in body["server"]["sources"]["by_kind"]} == {
        "fax_pdf", "ccda_xml",
    }
    # Non-existent blob path → 0 bytes, no crash.
    assert body["server"]["sources"]["evidence_bytes_on_disk"] == 0
    assert body["server"]["facts"]["total"] == 42
    assert body["server"]["conversations"]["total"] == 4
    assert body["server"]["episodes"]["total"] == 1
    assert body["server"]["healthkit"]["sample_count"] == 17

    # AI posture: model is the config default; consent from the user;
    # unset ceiling → not configured; spend defaults to 0.
    ai = body["ai"]
    assert ai["provider_key"]
    assert ai["model"] == "claude-opus-4-7"
    assert ai["phi_consent_granted"] is True
    assert ai["privacy_mode"] == "selected_evidence"
    assert ai["monthly_ceiling_configured"] is False
    assert ai["monthly_ceiling_usd"] is None
    assert ai["month_to_date_usd"] == 0.0

    assert body["retention"]
    assert body["demo_mode"] is False

    # Record scoping: the source/fact aggregates filter on the active
    # record id — assert the id shows up bound in the scoped queries.
    scoped = [s for s in sess.executed if "person_record_id" in s]
    assert scoped, "expected person_record_id-scoped queries"


# ---------------------------------------------------------------------------
# Demo isolation


def test_data_map_demo_scopes_conversations_and_episodes(app_fixture, monkeypatch):
    from ownchart.core.config import get_settings

    monkeypatch.setattr(get_settings(), "demo_mode", True)

    sess = _FakeSession(_canned())
    _override_session(app_fixture, sess)
    c = authed_client(app_fixture)

    # Visitor WITH a cookie → conversation/episode counts are session
    # scoped (the SQL carries the demo_session_id filter).
    r = c.get(
        "/api/account/data-map",
        headers={"Cookie": "oc_demo_session=visitor-abc"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["demo_mode"] is True

    conv_sql = [s for s in sess.executed if "conversations" in s]
    assert conv_sql and any("demo_session_id" in s for s in conv_sql)
    ep_sql = [s for s in sess.executed if "episodes" in s]
    assert ep_sql and any("demo_session_id" in s for s in ep_sql)


def test_data_map_demo_without_cookie_zeroes_conversations(app_fixture, monkeypatch):
    from ownchart.core.config import get_settings

    monkeypatch.setattr(get_settings(), "demo_mode", True)

    sess = _FakeSession(_canned())
    _override_session(app_fixture, sess)
    c = authed_client(app_fixture)

    # No oc_demo_session cookie → the visitor's own chats aren't
    # resolvable yet, so conversations count is 0 and no shared-pool
    # conversation query runs.
    r = c.get("/api/account/data-map")
    assert r.status_code == 200, r.text
    assert r.json()["server"]["conversations"]["total"] == 0
    assert not [s for s in sess.executed if "conversations" in s]
