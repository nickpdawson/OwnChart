"""Connector sync must surface dead provider sessions, not hide them.

Background (2026-11-10): six of seven EHR connections on the personal
instance had been "connected" with an expired one-hour Epic access token
since 2026-05-09. Every FHIR query returned 401, the fetcher swallowed each
one, the route persisted an empty bundle as a successful sync and bumped
last_synced_at, and the UI said "Pulled 0 resources". Pins:

  - fetch_patient_record raises FhirAuthError when the token is rejected
    (401 everywhere, or 401 with no Patient fetched).
  - fetch_patient_record raises FhirFetchError when nothing at all came
    back and no 401 explains it.
  - A 401 on a few resources with a working Patient read is a scope quirk,
    not an auth failure — the snapshot is returned.
  - /sync on an expired connection with no refresh token → 409, status
    "expired", nothing persisted.
  - /sync whose fetch raises FhirAuthError → 409 + status "expired".
  - /sync whose fetch raises FhirFetchError → 502 + status "error".
  - Epic default scopes request offline_access; other vendors unchanged.
  - Token endpoint calls send HTTP Basic auth only when a client secret
    is configured.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from ownchart.ingest import fhir
from ownchart.routes import connectors
from ownchart.routes.connectors import _default_scopes_for
from ownchart.tests.conftest import authed_client


# ---------------------------------------------------------------------------
# Scopes


def test_epic_default_scopes_request_offline_access():
    scopes = _default_scopes_for("epic").split()
    assert "offline_access" in scopes
    assert "patient/*.read" in scopes
    assert "launch/patient" in scopes


def test_other_vendor_defaults_do_not_gain_offline_access():
    # Athena + Cerner gate offline_access behind separate approvals and
    # fail the whole authorize request when it is requested unapproved.
    for vendor in ("athena", "cerner", "modmed", None):
        assert "offline_access" not in _default_scopes_for(vendor), vendor


def test_client_secret_env_name_and_lookup(monkeypatch):
    assert connectors._client_secret_env_for("epic") == "OWNCHART_EPIC_CLIENT_SECRET"
    monkeypatch.delenv("OWNCHART_EPIC_CLIENT_SECRET", raising=False)
    assert connectors._client_secret_for("epic") is None
    assert connectors._client_secret_for(None) is None
    monkeypatch.setenv("OWNCHART_EPIC_CLIENT_SECRET", "s3cret")
    assert connectors._client_secret_for("epic") == "s3cret"


def test_token_auth_only_with_secret():
    assert fhir._token_auth("cid", None) is None
    assert fhir._token_auth("cid", "") is None
    auth = fhir._token_auth("cid", "sec")
    assert isinstance(auth, httpx.BasicAuth)


# ---------------------------------------------------------------------------
# fetch_patient_record


def _install_transport(monkeypatch, handler):
    real = httpx.AsyncClient

    def factory(**kw):
        kw.pop("transport", None)
        return real(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(fhir.httpx, "AsyncClient", factory)


def _bundle(*resources):
    return httpx.Response(
        200,
        json={
            "resourceType": "Bundle",
            "entry": [{"resource": r} for r in resources],
            "link": [],
        },
    )


async def _fetch():
    return await fhir.fetch_patient_record(
        fhir_base="https://ehr.example/api/FHIR/R4/",
        access_token="dead",
        patient_fhir_id="p1",
        ehr_vendor="epic",
    )


async def test_fetch_raises_auth_error_when_everything_is_401(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"resourceType": "OperationOutcome"})

    _install_transport(monkeypatch, handler)
    with pytest.raises(fhir.FhirAuthError):
        await _fetch()


async def test_fetch_raises_auth_error_when_patient_read_is_401(monkeypatch):
    # Patient 401 + everything else 403: no data, token is the problem.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/Patient/p1"):
            return httpx.Response(401)
        return httpx.Response(403)

    _install_transport(monkeypatch, handler)
    with pytest.raises(fhir.FhirAuthError):
        await _fetch()


async def test_fetch_tolerates_scope_401s_when_patient_read_works(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/Patient/p1"):
            return httpx.Response(200, json={"resourceType": "Patient", "id": "p1"})
        if "/Condition" in path:
            return _bundle({"resourceType": "Condition", "id": "c1"})
        if "/Coverage" in path:
            return httpx.Response(401)
        return httpx.Response(403)

    _install_transport(monkeypatch, handler)
    snap = await _fetch()
    assert snap.counts["Patient"] == 1
    assert snap.counts["Condition"] == 1
    assert snap.auth_failures == 1


async def test_fetch_raises_fetch_error_when_nothing_returns_and_no_401(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    _install_transport(monkeypatch, handler)
    with pytest.raises(fhir.FhirFetchError) as ei:
        await _fetch()
    assert "503" in str(ei.value)


async def test_fetch_403_and_404_are_not_failures(monkeypatch):
    # Vendor-not-supported endpoints are informational; a snapshot with
    # just the Patient is a valid (thin) sync.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/Patient/p1"):
            return httpx.Response(200, json={"resourceType": "Patient", "id": "p1"})
        return httpx.Response(404 if "Medication" in request.url.path else 403)

    _install_transport(monkeypatch, handler)
    snap = await _fetch()
    assert snap.total == 1
    assert snap.fetch_failures == []


async def test_exchange_sends_basic_auth_only_with_secret(monkeypatch):
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"access_token": "a", "expires_in": 3600})

    _install_transport(monkeypatch, handler)
    await fhir.exchange_code_for_token(
        token_endpoint="https://ehr.example/oauth2/token",
        client_id="cid",
        code="code",
        redirect_uri="https://own.example/api/connectors/callback",
        pkce_verifier="v",
    )
    await fhir.refresh_access_token(
        token_endpoint="https://ehr.example/oauth2/token",
        client_id="cid",
        refresh_token="r",
        client_secret="sec",
    )
    assert seen[0] is None
    assert seen[1] is not None and seen[1].startswith("Basic ")


# ---------------------------------------------------------------------------
# /sync route


class _FakeDB:
    def __init__(self, conn, connector):
        self.conn = conn
        self.connector = connector
        self.commits = 0
        self.added: list[object] = []

    async def get(self, model, pk):
        if model is connectors.ProviderConnection:
            return self.conn if pk == self.conn.id else None
        if model is connectors.ProviderConnector:
            return self.connector
        return None

    async def commit(self):
        self.commits += 1

    async def flush(self):
        pass

    def add(self, obj):
        self.added.append(obj)


def _wire(app, monkeypatch, *, expires_at, refresh=None):
    uid = uuid.uuid4()
    rid = uuid.uuid4()
    connector = connectors.ProviderConnector(
        id=uuid.uuid4(),
        slug="bozeman-health",
        name="Bozeman Health",
        ehr_vendor="epic",
        fhir_base="https://ehr.example/api/FHIR/R4/",
        token_endpoint="https://ehr.example/oauth2/token",
        client_id="cid",
        scopes=_default_scopes_for("epic"),
        enabled=True,
    )
    conn = connectors.ProviderConnection(
        id=uuid.uuid4(),
        user_id=uid,
        person_record_id=rid,
        connector_id=connector.id,
        access_token_enc="enc:tok",
        refresh_token_enc=refresh,
        expires_at=expires_at,
        patient_fhir_id="p1",
        status="connected",
    )
    db = _FakeDB(conn, connector)

    from ownchart.core.db import get_session

    async def _session():
        yield db

    app.dependency_overrides[get_session] = _session
    monkeypatch.setattr(connectors, "decrypt_str", lambda v: "tok" if v else None)
    client = authed_client(app, user_id=uid, active_record_id=rid, role="owner")
    return client, db


def test_sync_expired_without_refresh_token_is_409_and_marks_expired(app_fixture, monkeypatch):
    client, db = _wire(
        app_fixture,
        monkeypatch,
        expires_at=datetime.now(timezone.utc) - timedelta(days=180),
    )

    async def _never(**kw):
        raise AssertionError("must not fetch with a dead token")

    monkeypatch.setattr(connectors.fhir_ingest, "fetch_patient_record", _never)
    r = client.post(f"/api/connectors/{db.conn.id}/sync")
    assert r.status_code == 409, r.text
    assert "Reconnect" in r.json()["detail"]
    assert db.conn.status == "expired"
    assert "no refresh token" in (db.conn.last_error or "")
    assert db.commits == 1
    assert db.added == []


def test_sync_401_from_provider_is_409_and_marks_expired(app_fixture, monkeypatch):
    client, db = _wire(app_fixture, monkeypatch, expires_at=None)

    async def _unauthorized(**kw):
        raise fhir.FhirAuthError("44 of 45 requests were rejected with HTTP 401")

    monkeypatch.setattr(connectors.fhir_ingest, "fetch_patient_record", _unauthorized)
    r = client.post(f"/api/connectors/{db.conn.id}/sync")
    assert r.status_code == 409, r.text
    assert db.conn.status == "expired"
    assert "401" in (db.conn.last_error or "")
    assert db.added == []  # no SourceDocument, no facts


def test_sync_empty_fetch_is_502_and_marks_error_but_keeps_tokens(app_fixture, monkeypatch):
    client, db = _wire(
        app_fixture,
        monkeypatch,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=50),
    )

    async def _empty(**kw):
        raise fhir.FhirFetchError("no resources could be fetched (HTTP 503 ...)")

    monkeypatch.setattr(connectors.fhir_ingest, "fetch_patient_record", _empty)
    r = client.post(f"/api/connectors/{db.conn.id}/sync")
    assert r.status_code == 502, r.text
    assert db.conn.status == "error"
    assert db.conn.access_token_enc == "enc:tok"
    assert db.added == []


def test_sync_response_detail_is_json_string(app_fixture, monkeypatch):
    # The web row renders `detail` verbatim; keep it a plain string.
    client, db = _wire(
        app_fixture,
        monkeypatch,
        expires_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    r = client.post(f"/api/connectors/{db.conn.id}/sync")
    body = json.loads(r.text)
    assert isinstance(body["detail"], str)
