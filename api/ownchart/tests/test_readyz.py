"""P1-7: honest /readyz readiness contract.

Pure-function tests for ``overall_readiness`` — the decision core that maps a
component-state dict to (ready, http_status). No DB, no Redis, no FS. The live
probes (``_check_db_and_migrations`` etc.) run against real infra in the
deploy smoke; here we pin the fail-open/fail-closed policy that governs which
components can and cannot take an instance out of rotation.
"""

from __future__ import annotations

from ownchart.routes.health import _CRITICAL_COMPONENTS, overall_readiness


def _all_ok() -> dict[str, str]:
    return {
        "db": "ok",
        "migrations": "ok",
        "storage": "ok",
        "redis": "ok",
        "worker": "ok",
    }


def test_all_ok_is_ready_200():
    ready, code = overall_readiness(_all_ok())
    assert ready is True
    assert code == 200


def test_db_degraded_is_503():
    c = _all_ok() | {"db": "degraded"}
    assert overall_readiness(c) == (False, 503)


def test_migrations_behind_is_503():
    c = _all_ok() | {"migrations": "degraded"}
    assert overall_readiness(c) == (False, 503)


def test_storage_unwritable_is_503():
    c = _all_ok() | {"storage": "degraded"}
    assert overall_readiness(c) == (False, 503)


def test_redis_degraded_still_ready_200():
    # Async lane — Redis down alone must NOT pull the node from rotation.
    c = _all_ok() | {"redis": "degraded"}
    assert overall_readiness(c) == (True, 200)


def test_worker_degraded_still_ready_200():
    # Async lane — a stale worker heartbeat is reported, not fatal.
    c = _all_ok() | {"worker": "degraded"}
    assert overall_readiness(c) == (True, 200)


def test_missing_critical_component_fails_closed():
    # An incomplete map (a probe that never ran) must fail readiness, not
    # accidentally pass because the key is absent.
    assert overall_readiness({}) == (False, 503)
    assert overall_readiness({"db": "ok", "storage": "ok"}) == (False, 503)


def test_critical_set_is_the_synchronous_lanes():
    # Guard against someone quietly moving redis/worker into the critical
    # set (which would make an async-lane blip take the instance down).
    assert set(_CRITICAL_COMPONENTS) == {"db", "migrations", "storage"}
