"""Migration 0054 shape test (Batch C — users.disabled account lockout).

Runs the migration against a fake ``op`` that records calls — no DB. Pins:

  - chains after 0053;
  - adds a ``disabled`` column to ``users``;
  - NOT NULL with a ``false`` server_default (existing rows backfill);
  - downgrade drops it.

Also pins the SQLAlchemy model side: ``disabled`` is a non-null bool
defaulting to false, so a freshly-constructed User is never "disabled".
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_MIG = (
    Path(__file__).resolve().parents[2]
    / "alembic" / "versions" / "0054_user_disabled.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("mig0054", _MIG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeOp:
    def __init__(self):
        self.added_columns = []
        self.dropped_columns = []

    def add_column(self, table, column):
        self.added_columns.append((table, column))

    def drop_column(self, table, name):
        self.dropped_columns.append((table, name))


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


def test_chains_after_0053():
    mod = _load()
    assert mod.down_revision == "0053_model_run_user_cost"
    assert mod.revision == "0054_user_disabled"


def test_upgrade_adds_disabled_column():
    fake = _run("upgrade")
    assert len(fake.added_columns) == 1
    table, col = fake.added_columns[0]
    assert table == "users"
    assert col.name == "disabled"
    assert col.nullable is False
    # server_default is a text('false') — stringifies to "false".
    assert "false" in str(col.server_default.arg).lower()


def test_downgrade_drops_disabled():
    fake = _run("downgrade")
    assert ("users", "disabled") in fake.dropped_columns


def test_model_disabled_defaults_false():
    from ownchart.models.user import User

    col = User.__table__.columns["disabled"]
    assert col.nullable is False
    # A constructed-but-unflushed User reads False via the Python default
    # only after flush; the important invariant is the column is non-null
    # with a false server_default so no existing row is ever "disabled".
    assert "false" in str(col.server_default.arg).lower()
