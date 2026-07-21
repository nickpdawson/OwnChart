"""P2-1 / P1-11: admin-invariant guards + advisory-lock serialization.

Pure-function coverage for the last-admin guards that the advisory lock
serializes (revoke-admin, disable-admin, admin self-deletion), plus a check
that ``serialize_admin_mutation`` is a no-op off Postgres (so the test fakes /
SQLite never trip on it).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from ownchart.routes.admin import (
    _ADMIN_MUTATION_LOCK_KEY,
    ensure_active_admin_remains,
    ensure_admin_remains,
    ensure_admin_self_deletion_allowed,
    serialize_admin_mutation,
)
from ownchart.routes.auth import _BOOTSTRAP_ADVISORY_LOCK_KEY


# ---------------------------------------------------------------------------
# ensure_admin_remains (revoke-admin) — existing invariant, now serialized


def test_revoke_last_admin_blocked():
    with pytest.raises(HTTPException) as ei:
        ensure_admin_remains(target_is_admin=True, total_admin_count=1)
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "last_admin"


def test_revoke_non_last_admin_ok():
    ensure_admin_remains(target_is_admin=True, total_admin_count=2)  # no raise


# ---------------------------------------------------------------------------
# ensure_active_admin_remains (disable-admin)


def test_disable_last_enabled_admin_blocked():
    with pytest.raises(HTTPException) as ei:
        ensure_active_admin_remains(target_is_active_admin=True, active_admin_count=1)
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "last_active_admin"


def test_disable_admin_ok_when_another_enabled_admin_exists():
    ensure_active_admin_remains(target_is_active_admin=True, active_admin_count=2)


def test_disable_non_admin_never_blocked():
    ensure_active_admin_remains(target_is_active_admin=False, active_admin_count=1)


# ---------------------------------------------------------------------------
# ensure_admin_self_deletion_allowed — App Store 5.1.1(v) solo carve-out


def test_last_admin_with_other_users_blocked():
    with pytest.raises(HTTPException) as ei:
        ensure_admin_self_deletion_allowed(
            deleter_is_active_admin=True, other_active_admins=0, other_users=3,
        )
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "last_admin_transfer_required"


def test_solo_self_host_admin_can_always_delete():
    # No other users → deletion must be permitted (5.1.1(v)).
    ensure_admin_self_deletion_allowed(
        deleter_is_active_admin=True, other_active_admins=0, other_users=0,
    )


def test_admin_delete_ok_when_another_admin_remains():
    ensure_admin_self_deletion_allowed(
        deleter_is_active_admin=True, other_active_admins=1, other_users=5,
    )


def test_non_admin_delete_never_blocked():
    ensure_admin_self_deletion_allowed(
        deleter_is_active_admin=False, other_active_admins=0, other_users=5,
    )


# ---------------------------------------------------------------------------
# serialize_admin_mutation — no-op off Postgres, distinct lock key


def test_advisory_lock_key_distinct_from_bootstrap():
    assert _ADMIN_MUTATION_LOCK_KEY != _BOOTSTRAP_ADVISORY_LOCK_KEY


def test_serialize_is_noop_without_a_bind():
    # A fake session whose get_bind raises must be a clean no-op (never
    # touches .execute) — mirrors the test-fake / SQLite path.
    class _NoBind:
        def get_bind(self):
            raise RuntimeError("no bind in tests")

        async def execute(self, *a, **k):  # pragma: no cover - must not run
            raise AssertionError("execute should not be called off Postgres")

    asyncio.run(serialize_admin_mutation(_NoBind()))


def test_serialize_is_noop_on_sqlite_dialect():
    executed = {"n": 0}

    class _Sqlite:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

        async def execute(self, *a, **k):  # pragma: no cover - must not run
            executed["n"] += 1

    asyncio.run(serialize_admin_mutation(_Sqlite()))
    assert executed["n"] == 0
