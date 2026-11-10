"""Public demo boundary (audit 2026-11-10, item 2).

  - In demo mode, writes to sources / connectors / facts / topics /
    settings / llm-providers are refused even when the old
    OWNCHART_DEMO_ALLOW_INGEST flag is set — the escape hatch is gone.
  - The conversation / episode / consent allowlist still works.
  - The demo persona is created non-admin and demoted on startup if it
    somehow became an instance admin.
  - Source sensemaking's topic lookup is scoped to the source's record.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ownchart import main as m
from ownchart.core import demo_seed
from ownchart.llm import sensemaking


@pytest.fixture
def demo_client(monkeypatch):
    monkeypatch.setattr(m.settings, "demo_mode", True)
    monkeypatch.setattr(m.settings, "demo_allow_ingest", True, raising=False)
    return TestClient(m.app)


@pytest.mark.parametrize("path", [
    "/api/sources/pdf",
    "/api/connectors/epic/connect",
    "/api/facts/abc/correct",
    "/api/topics",
    "/api/settings/ai.privacy_mode",
    "/api/llm-providers/credentials",
    "/api/healthkit/sync",
    "/api/auto-export/ingest",
])
def test_demo_refuses_ingest_writes_even_with_legacy_flag(demo_client, path):
    r = demo_client.post(path, json={})
    assert r.status_code == 403, (path, r.status_code, r.text[:200])
    assert "Demo mode" in r.json()["detail"]


def test_demo_allowlist_still_passes_through(demo_client):
    # Reaches the real route (auth fails → 401), proving the middleware
    # did not 403 it.
    r = demo_client.post("/api/conversations", json={})
    assert r.status_code != 403


def test_demo_reads_are_not_blocked(demo_client):
    r = demo_client.get("/api/sources")
    assert r.status_code != 403


def test_no_ingest_prefix_list_remains():
    assert not hasattr(m, "_DEMO_INGEST_ALLOWED_PREFIXES")


# ---------------------------------------------------------------------------
# Demo persona


class _Result:
    def __init__(self, v):
        self._v = v

    def scalar_one_or_none(self):
        return self._v


class _DB:
    def __init__(self, existing):
        self.existing = existing
        self.added = []
        self.commits = 0

    async def execute(self, *a, **k):
        return _Result(self.existing)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


def _demo_settings(monkeypatch):
    s = SimpleNamespace(
        demo_mode=True,
        demo_user_email="demo@ownchart.me",
        demo_user_password=SimpleNamespace(get_secret_value=lambda: "pw"),
    )
    monkeypatch.setattr(demo_seed, "get_settings", lambda: s)
    monkeypatch.setattr(demo_seed, "hash_password", lambda p: "hashed")


async def test_new_demo_user_is_not_instance_admin(monkeypatch):
    _demo_settings(monkeypatch)
    db = _DB(existing=None)
    assert await demo_seed.seed_demo_user_if_needed(db) is True
    user = db.added[0]
    assert user.is_instance_admin is False
    assert user.phi_consent_granted is True


async def test_existing_admin_demo_user_is_demoted(monkeypatch):
    _demo_settings(monkeypatch)
    existing = SimpleNamespace(is_instance_admin=True, email="demo@ownchart.me")
    db = _DB(existing=existing)
    assert await demo_seed.seed_demo_user_if_needed(db) is False
    assert existing.is_instance_admin is False
    assert db.commits == 1


async def test_existing_non_admin_demo_user_untouched(monkeypatch):
    _demo_settings(monkeypatch)
    existing = SimpleNamespace(is_instance_admin=False, email="demo@ownchart.me")
    db = _DB(existing=existing)
    await demo_seed.seed_demo_user_if_needed(db)
    assert db.commits == 0


# ---------------------------------------------------------------------------
# Sensemaking topic scope


def test_sensemaking_topic_lookup_is_record_scoped():
    rid = uuid.uuid4()
    stmt = sensemaking.topics_for_record_stmt(rid)
    sql = str(stmt.compile(compile_kwargs={"literal_binds": False}))
    assert "topics.person_record_id" in sql
    assert stmt.compile().params[next(iter(stmt.compile().params))] == rid
