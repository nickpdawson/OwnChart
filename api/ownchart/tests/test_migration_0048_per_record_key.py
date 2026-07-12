"""Migration 0048 shape test (CRIT-1 — per-record client_sample_key).

Verifies the migration drops the GLOBAL single-column unique index and
creates a COMPOSITE `(person_record_id, client_sample_key)` partial
unique index, so two records posting the same aggregate/medication key
no longer collide. Runs the migration against a fake `op` that records
calls — no DB required.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_MIG = (
    Path(__file__).resolve().parents[2]
    / "alembic" / "versions" / "0048_client_sample_key_per_record.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("mig0048", _MIG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeOp:
    def __init__(self):
        self.created = []
        self.dropped = []

    def create_index(self, name, table, cols, unique=False, postgresql_where=None):
        self.created.append(
            {
                "name": name,
                "table": table,
                "cols": cols,
                "unique": unique,
                "where": str(postgresql_where) if postgresql_where is not None else None,
            }
        )

    def drop_index(self, name, table_name=None):
        self.dropped.append({"name": name, "table_name": table_name})


def _run(fn_name):
    mod = _load()
    fake = _FakeOp()
    orig = mod.op
    mod.op = fake
    try:
        getattr(mod, fn_name)()
    finally:
        mod.op = orig
    return fake


def test_chains_after_0047():
    mod = _load()
    assert mod.down_revision == "0047_date_provenance_documentation_date"
    assert mod.revision == "0048_client_sample_key_per_record"


def test_upgrade_swaps_global_for_composite_partial_unique():
    fake = _run("upgrade")
    # Old global single-column index dropped.
    assert any(
        d["name"] == "ix_extracted_facts_client_sample_key" for d in fake.dropped
    )
    # New composite partial unique index created.
    comp = [
        c for c in fake.created
        if c["name"] == "ix_extracted_facts_record_client_sample_key"
    ]
    assert len(comp) == 1
    idx = comp[0]
    assert idx["cols"] == ["person_record_id", "client_sample_key"]
    assert idx["unique"] is True
    assert "client_sample_key IS NOT NULL" in (idx["where"] or "")


def test_downgrade_restores_global_index():
    fake = _run("downgrade")
    assert any(
        d["name"] == "ix_extracted_facts_record_client_sample_key"
        for d in fake.dropped
    )
    restored = [
        c for c in fake.created
        if c["name"] == "ix_extracted_facts_client_sample_key"
    ]
    assert len(restored) == 1
    assert restored[0]["cols"] == ["client_sample_key"]
