"""Cross-record leak tests for the external-ingest surfaces.

Beta 1 M02 Slice 1, perimeter rollout Batch 8.

External ingest is the highest-stakes write surface because data
comes IN from outside the trust boundary. Three lanes, each with
a distinct binding mechanism:

  - **HealthKit native sync** (`/api/healthkit/sync`): iOS sends
    `X-OwnChart-Person-Record` header. `require_role("caregiver")`
    resolves it via AuthContext. Every SourceDocument /
    EvidenceAnchor / ExtractedFact stamps the active record id.

  - **Auto Export REST push** (`/api/auto-export/push`):
    `authenticate_auto_export_push` resolves either a per-(user,
    record) token or the legacy env token. Legacy env token is
    valid ONLY when the instance has exactly one active person
    record; multi-record instances must use per-record tokens.

  - **OAuth connectors** (`/api/connectors/...`): start_connect
    signs `person_record_id` into the OAuth state. Callback decodes
    the SIGNED state and binds the resulting ProviderConnection to
    that value, not to whatever active record the user has switched
    to mid-flow. Sync verifies the connection's bound record matches
    the active record (cross-record sync → 404).

Live database verification is out of scope (no test DB); these
tests cover the structural perimeter — route dependency wiring,
helper signatures, role gates, signed state round-trips, and
source-level filter checks.
"""

from __future__ import annotations

import inspect
import uuid
from typing import Callable
from unittest.mock import patch

import pytest

from ownchart.tests.conftest import authed_client, denied_client


# ---------------------------------------------------------------------------
# HealthKit


def test_hk_sync_403_on_record_access_revoked(app_fixture):
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.post("/api/healthkit/sync", json={
        "device_id": "x", "identifier": "HKQuantityTypeIdentifierHeartRate",
        "strategy": "daily_aggregate", "samples": [],
    })
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "record_access_revoked"


def test_hk_sync_403_on_no_memberships(app_fixture):
    c = denied_client(app_fixture, code="no_memberships")
    r = c.post("/api/healthkit/sync", json={
        "device_id": "x", "identifier": "HKQuantityTypeIdentifierHeartRate",
        "strategy": "daily_aggregate", "samples": [],
    })
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "no_memberships"


def test_hk_sync_403_insufficient_role_for_viewer(app_fixture):
    """HK sync is a write — viewers must not be able to push."""
    c = authed_client(app_fixture, role="viewer")
    r = c.post("/api/healthkit/sync", json={
        "device_id": "x", "identifier": "HKQuantityTypeIdentifierHeartRate",
        "strategy": "daily_aggregate", "samples": [],
    })
    assert r.status_code == 403
    body = r.json()
    assert body["detail"]["code"] == "insufficient_role"
    assert body["detail"]["required"] == "caregiver"


def test_hk_sync_uses_require_role_caregiver():
    """The /sync handler must use require_role('caregiver').
    Reads (capabilities, cursors) stay on the device-token dep —
    they don't write record data."""
    import inspect
    from fastapi.params import Depends as DependsParam
    from ownchart.routes.healthkit_sync import sync_healthkit

    sig = inspect.signature(sync_healthkit)
    ctx_param = sig.parameters["ctx"]
    assert isinstance(ctx_param.default, DependsParam)
    dep = ctx_param.default.dependency
    assert dep.__name__ == "_dep", dep.__name__
    found = None
    for cell in dep.__closure__ or ():
        v = cell.cell_contents
        if isinstance(v, str) and v in ("viewer", "member", "caregiver", "owner"):
            found = v
            break
    assert found == "caregiver"


def test_hk_upsert_source_requires_person_record_id():
    """The per-day source helper must accept person_record_id
    keyword-only with no default — a refactor that drops the
    kwarg would silently collapse Mom's and Dad's day-sources
    into one row."""
    from ownchart.routes.healthkit_sync import _upsert_source_for_day
    sig = inspect.signature(_upsert_source_for_day)
    assert "person_record_id" in sig.parameters
    p = sig.parameters["person_record_id"]
    assert p.kind == inspect.Parameter.KEYWORD_ONLY
    assert p.default is inspect.Parameter.empty


def test_hk_sync_source_select_filters_by_record():
    """Source-level check: `_upsert_source_for_day`'s SELECT must
    filter by person_record_id so the (user, record) lookup is
    distinct from (user, anything-else). Catches a refactor that
    drops the filter and silently re-uses Mom's row for Dad's
    push."""
    from ownchart.routes.healthkit_sync import _upsert_source_for_day
    src = inspect.getsource(_upsert_source_for_day)
    assert "SourceDocument.person_record_id == person_record_id" in src


def test_hk_sync_stamps_record_id_on_inserts():
    """Source-level: every Source/Anchor/Fact insert in
    _sync_healthkit_inner must include person_record_id from ctx."""
    from ownchart.routes.healthkit_sync import _sync_healthkit_inner
    src = inspect.getsource(_sync_healthkit_inner)
    # Should appear at least twice — once on the ExtractedFact
    # insert and once on the EvidenceAnchor insert.
    count = src.count("ctx.active_record_id")
    assert count >= 2, (
        f"_sync_healthkit_inner missing record stamping (only "
        f"{count} mentions in source)"
    )


# ---------------------------------------------------------------------------
# Auto Export


def test_auto_export_config_403_on_denial(app_fixture):
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.get("/api/auto-export/config")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "record_access_revoked"


def test_auto_export_config_signature_includes_auth_context():
    from typing import get_type_hints
    from ownchart.core.auth_context import AuthContext
    from ownchart.routes.auto_export import get_push_config
    hints = get_type_hints(get_push_config)
    assert AuthContext in hints.values()


def test_auto_export_push_uses_token_auth_not_session(app_fixture):
    """The /push endpoint must NOT depend on AuthContext or
    get_current_user. It uses bearer token auth via
    `authenticate_auto_export_push` so iOS Auto Export (with no
    session cookie) can post. The destination record comes from
    the token, NOT from any caller-controlled header."""
    from ownchart.routes.auto_export import push_auto_export
    sig = inspect.signature(push_auto_export)
    # The handler's only deps should be Request + Authorization header.
    param_names = list(sig.parameters.keys())
    assert "request" in param_names
    assert "authorization" in param_names
    # No ctx / no user param.
    assert "ctx" not in param_names
    assert "user" not in param_names


def test_auto_export_push_body_calls_authenticate_helper():
    """Source-level: the handler must invoke
    `authenticate_auto_export_push` and stamp
    `auth_result.person_record_id` on the SourceDocument. Catches
    a regression that bypasses the helper and uses
    `_resolve_owner` (the old single-tenant fallback)."""
    from ownchart.routes.auto_export import push_auto_export
    src = inspect.getsource(push_auto_export)
    assert "authenticate_auto_export_push" in src
    assert "person_record_id=auth_result.person_record_id" in src


# NB: live "401 on missing/invalid token" tests are covered by the
# unit suite for `authenticate_auto_export_push` (test_auto_export_auth.py
# from Slice 1 Batch 1). Re-running them through the full HTTP
# stack here requires a real DB session, which the perimeter test
# fixtures don't provide. The route-side guarantee is asserted via
# `test_auto_export_push_body_calls_authenticate_helper` above.


def test_auto_export_authenticates_before_writing_body():
    """Source-level: the handler must call
    authenticate_auto_export_push BEFORE storage.write_blob. A
    bogus push must not be allowed to write anything to disk
    before we know the destination record."""
    from ownchart.routes.auto_export import push_auto_export
    src = inspect.getsource(push_auto_export)
    auth_idx = src.find("authenticate_auto_export_push")
    write_idx = src.find("storage.write_blob")
    assert auth_idx > 0
    assert write_idx > 0
    assert auth_idx < write_idx, (
        "authenticate_auto_export_push must run BEFORE "
        "storage.write_blob — unauthenticated bodies must not "
        "touch the evidence vault"
    )


def test_legacy_env_token_refused_on_multi_record_instance():
    """PM A-2: the env-fallback path raises 503 when the instance
    has multiple person_records. We can't reach the live DB here,
    but we verify the helper contains the multi-record gate."""
    from ownchart.core.auto_export_auth import authenticate_auto_export_push
    src = inspect.getsource(authenticate_auto_export_push)
    # Must check the record count and refuse when > 1.
    assert "record_count > 1" in src
    assert "503" in src or "SERVICE_UNAVAILABLE" in src
    # Must mention the env-token name in the error so operators
    # can grep for it.
    assert "OWNCHART_AUTO_EXPORT_TOKEN" in src


def test_auto_export_helper_returns_token_id_for_audit():
    """The helper's result must carry token_id so audits can
    distinguish which per-record token authorized a push (and the
    operator can revoke it surgically)."""
    from ownchart.core.auto_export_auth import AutoExportAuthResult
    # The dataclass has token_id field.
    fields = {f.name for f in AutoExportAuthResult.__dataclass_fields__.values()}
    assert "token_id" in fields
    assert "person_record_id" in fields
    assert "auth_method" in fields


# ---------------------------------------------------------------------------
# Connectors — list / directory / create / delete


def test_connectors_list_403_on_denial(app_fixture):
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.get("/api/connectors")
    assert r.status_code == 403


def test_connectors_directory_403_on_denial(app_fixture):
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.get("/api/connectors/directory/search?vendor=epic")
    assert r.status_code == 403


def test_connectors_create_403_on_denial(app_fixture):
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.post("/api/connectors", json={
        "name": "x", "fhir_base": "https://x", "ehr_vendor": "epic",
    })
    assert r.status_code == 403


def test_connectors_create_403_insufficient_role_for_viewer(app_fixture):
    """Creating a connector is caregiver+ — it adds an EHR target
    every record on the instance can connect to."""
    c = authed_client(app_fixture, role="viewer")
    r = c.post("/api/connectors", json={
        "name": "x", "fhir_base": "https://x", "ehr_vendor": "epic",
    })
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "insufficient_role"


def test_connectors_delete_403_insufficient_role_for_viewer(app_fixture):
    c = authed_client(app_fixture, role="viewer")
    r = c.delete("/api/connectors/test-slug")
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Connectors — OAuth start / callback


def test_connectors_start_403_on_denial(app_fixture):
    c = denied_client(app_fixture, code="record_access_revoked")
    r = c.post("/api/connectors/test/connect")
    assert r.status_code == 403


def test_connectors_start_403_insufficient_role_for_viewer(app_fixture):
    """Starting OAuth is caregiver+ — it produces ingest data on
    the active record. Viewers must not be able to start a flow
    that writes to the record."""
    c = authed_client(app_fixture, role="viewer")
    r = c.post("/api/connectors/test/connect")
    assert r.status_code == 403


def test_connectors_start_signs_state_with_active_record():
    """Source-level: start_connect must call sign_oauth_state with
    person_record_id=ctx.active_record_id. A refactor that signs
    something else (or doesn't sign at all) would let the callback
    re-bind the grant to whatever record the user has switched to."""
    from ownchart.routes.connectors import start_connect
    src = inspect.getsource(start_connect)
    assert "sign_oauth_state" in src
    assert "person_record_id=ctx.active_record_id" in src


def test_connectors_callback_does_not_depend_on_auth_context():
    """PM A-3: the callback MUST NOT depend on AuthContext (which
    would fall back to active record). It depends on the bare
    user dep and decodes the SIGNED state for the binding."""
    from typing import get_type_hints
    from ownchart.core.auth_context import AuthContext
    from ownchart.routes.connectors import oauth_callback
    hints = get_type_hints(oauth_callback)
    assert AuthContext not in hints.values(), (
        "oauth_callback must NOT depend on AuthContext — the "
        "binding comes from the signed state, not the active "
        "record at callback time"
    )


def test_connectors_callback_decodes_signed_state():
    """Source-level: the callback must invoke decode_oauth_state
    and bind the resulting ProviderConnection to
    payload.person_record_id, not ctx.active_record_id."""
    from ownchart.routes.connectors import oauth_callback
    src = inspect.getsource(oauth_callback)
    assert "decode_oauth_state(state)" in src
    assert "payload.user_id != user.id" in src
    assert "payload.person_record_id" in src
    # The binding line — connection.person_record_id is set from
    # the signed value, NOT from any active-record source.
    assert "bound_person_record_id = payload.person_record_id" in src


def test_connectors_callback_refuses_mismatched_user(app_fixture):
    """If the SIGNED state's user_id != current session user, the
    callback returns 403. Source-level check (we can't easily
    forge a signed state to round-trip via TestClient without
    duplicating the helper)."""
    from ownchart.routes.connectors import oauth_callback
    src = inspect.getsource(oauth_callback)
    assert "payload.user_id != user.id" in src
    # Must raise 403 (not 400) for the mismatch.
    assert "HTTP_403_FORBIDDEN" in src


def test_connectors_callback_db_row_record_binding_check():
    """Defense-in-depth: the callback also verifies the
    OAuthSession DB row's person_record_id matches what was
    signed. Catches a tampered or replayed state where the signer
    secret leaked but the DB row is intact."""
    from ownchart.routes.connectors import oauth_callback
    src = inspect.getsource(oauth_callback)
    assert "sess.person_record_id is not None" in src
    assert "sess.person_record_id != payload.person_record_id" in src


# ---------------------------------------------------------------------------
# Connectors — sync / disconnect


def test_connectors_sync_403_insufficient_role_for_viewer(app_fixture):
    """Sync produces sources + facts; caregiver+ required."""
    fake_id = str(uuid.uuid4())
    c = authed_client(app_fixture, role="viewer")
    r = c.post(f"/api/connectors/{fake_id}/sync")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "insufficient_role"


def test_connectors_sync_binds_to_connection_record():
    """Source-level: sync uses `conn.person_record_id` (the
    BOUND record from OAuth callback time) as the destination,
    not `ctx.active_record_id`. The active record is only used as
    a 404 gate."""
    from ownchart.routes.connectors import sync_connection
    src = inspect.getsource(sync_connection)
    # Must compute dest_record_id from the connection's bound value.
    assert "dest_record_id = conn.person_record_id" in src
    # Must use dest_record_id (not ctx.active_record_id) for the
    # source/anchor/fact inserts.
    assert "person_record_id=dest_record_id" in src
    # And there must be a 404 guard for cross-record sync attempts.
    assert "conn.person_record_id != ctx.active_record_id" in src


def test_connectors_disconnect_403_insufficient_role_for_viewer(app_fixture):
    fake_id = str(uuid.uuid4())
    c = authed_client(app_fixture, role="viewer")
    r = c.post(f"/api/connectors/{fake_id}/disconnect")
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Static signature check — all connector handlers


def test_connector_handler_signatures_include_auth_context():
    from typing import get_type_hints
    from ownchart.core.auth_context import AuthContext
    from ownchart.routes.connectors import (
        create_connector,
        delete_connector,
        directory_search,
        disconnect,
        list_connectors,
        start_connect,
        sync_connection,
    )

    # NB: oauth_callback is EXCLUDED — it depends on the bare user
    # dep, not AuthContext (see test above).
    for fn in (
        list_connectors,
        directory_search,
        create_connector,
        delete_connector,
        start_connect,
        sync_connection,
        disconnect,
    ):
        hints = get_type_hints(fn)
        ctx_params = [n for n, t in hints.items() if t is AuthContext]
        assert len(ctx_params) == 1, fn.__name__


def test_provider_connection_model_carries_person_record_id():
    """ProviderConnection.person_record_id is the destination
    record for OAuth-derived data. Without this column the OAuth
    binding can't be persisted."""
    from ownchart.models.provider_connection import ProviderConnection
    assert "person_record_id" in ProviderConnection.__table__.columns


def test_oauth_session_model_carries_person_record_id():
    """OAuthSession.person_record_id stores the intended record at
    start_connect time; the callback verifies it matches the
    signed state."""
    from ownchart.models.oauth_session import OAuthSession
    assert "person_record_id" in OAuthSession.__table__.columns


# ---------------------------------------------------------------------------
# OAuth state round-trip (signed -> decoded -> verified)


def test_oauth_state_round_trip_carries_person_record_id():
    """A signed state token decodes back to the same user_id +
    person_record_id. This is the contract start_connect/oauth_callback
    rely on."""
    from ownchart.core.oauth_state import sign_oauth_state, decode_oauth_state

    user_id = uuid.uuid4()
    record_id = uuid.uuid4()
    session_id = uuid.uuid4()
    token = sign_oauth_state(
        user_id=user_id,
        person_record_id=record_id,
        oauth_session_id=session_id,
    )
    payload = decode_oauth_state(token)
    assert payload.user_id == user_id
    assert payload.person_record_id == record_id
    assert payload.oauth_session_id == session_id


def test_oauth_state_bad_signature_rejected():
    """Tampering with the state must raise OAuthStateError."""
    from ownchart.core.oauth_state import (
        sign_oauth_state,
        decode_oauth_state,
        OAuthStateError,
    )

    token = sign_oauth_state(
        user_id=uuid.uuid4(),
        person_record_id=uuid.uuid4(),
    )
    tampered = token[:-6] + "ABCDEF"
    with pytest.raises(OAuthStateError):
        decode_oauth_state(tampered)
