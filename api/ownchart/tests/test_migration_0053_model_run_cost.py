"""Migration 0053 shape test (Batch B — model_run cost attribution).

Runs the migration against a fake ``op`` that records calls — no DB. Pins:

  - chains after 0052;
  - adds ``user_id`` / ``person_record_id`` / ``estimated_cost_usd``;
  - both FKs are ON DELETE SET NULL (audit rows survive erasure);
  - index on ``(user_id, created_at)`` for month-to-date sums;
  - downgrade removes all of it.

Also pins the SQLAlchemy model side: the FK ondelete is SET NULL and the
columns are nullable, so account/record deletion nulls the FK rather than
cascading the (PHI-free) audit row away.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_MIG = (
    Path(__file__).resolve().parents[2]
    / "alembic" / "versions" / "0053_model_run_user_cost.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("mig0053", _MIG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeOp:
    def __init__(self):
        self.added_columns = []
        self.dropped_columns = []
        self.fks = []
        self.created_indexes = []
        self.dropped_indexes = []
        self.dropped_constraints = []

    def add_column(self, table, column):
        self.added_columns.append((table, column.name, column))

    def drop_column(self, table, name):
        self.dropped_columns.append((table, name))

    def create_foreign_key(self, name, source, referent, local_cols, remote_cols, ondelete=None):
        self.fks.append({
            "name": name, "source": source, "referent": referent,
            "local_cols": local_cols, "remote_cols": remote_cols,
            "ondelete": ondelete,
        })

    def create_index(self, name, table, cols, unique=False):
        self.created_indexes.append({"name": name, "table": table, "cols": cols})

    def drop_index(self, name, table_name=None):
        self.dropped_indexes.append({"name": name, "table_name": table_name})

    def drop_constraint(self, name, table, type_=None):
        self.dropped_constraints.append({"name": name, "table": table, "type": type_})


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


def test_chains_after_0052():
    mod = _load()
    assert mod.down_revision == "0052_agreement_acceptances"
    assert mod.revision == "0053_model_run_user_cost"


def test_upgrade_adds_columns():
    fake = _run("upgrade")
    added = {name for _, name, _ in fake.added_columns}
    assert {"user_id", "person_record_id", "estimated_cost_usd"} <= added
    assert all(t == "model_runs" for t, _, _ in fake.added_columns)


def test_upgrade_fks_are_set_null():
    fake = _run("upgrade")
    by_col = {fk["local_cols"][0]: fk for fk in fake.fks}
    assert by_col["user_id"]["referent"] == "users"
    assert by_col["user_id"]["ondelete"] == "SET NULL"
    assert by_col["person_record_id"]["referent"] == "person_records"
    assert by_col["person_record_id"]["ondelete"] == "SET NULL"


def test_upgrade_indexes_user_created():
    fake = _run("upgrade")
    idx = [i for i in fake.created_indexes if i["name"] == "ix_model_runs_user_created"]
    assert len(idx) == 1
    assert idx[0]["cols"] == ["user_id", "created_at"]


def test_downgrade_removes_everything():
    fake = _run("downgrade")
    dropped_cols = {name for _, name in fake.dropped_columns}
    assert {"user_id", "person_record_id", "estimated_cost_usd"} <= dropped_cols
    assert any(i["name"] == "ix_model_runs_user_created" for i in fake.dropped_indexes)


def test_model_fk_ondelete_set_null():
    """The SQLAlchemy model mirrors the migration: deleting a user/record
    SET-NULLs the FK on the (PHI-free) audit row rather than removing it."""
    from ownchart.models.model_run import ModelRun

    cols = ModelRun.__table__.columns
    assert cols["user_id"].nullable is True
    assert cols["person_record_id"].nullable is True
    assert cols["estimated_cost_usd"].nullable is True

    fk_ondelete = {}
    for fk in ModelRun.__table__.foreign_keys:
        fk_ondelete[fk.parent.name] = fk.ondelete
    assert fk_ondelete.get("user_id") == "SET NULL"
    assert fk_ondelete.get("person_record_id") == "SET NULL"
