"""SMART on FHIR client primitives.

Three layers:

  1. Discovery: fetch /.well-known/smart-configuration → cache authorize/token endpoints.
  2. Auth: PKCE pair generation, authorize URL composition, token exchange,
     refresh.
  3. Patient fetch: structured query plan (44 patient-scoped queries +
     reference chasing + attachment extraction). Borrowed from
     jmandel/health-skillz (MIT).

Token-at-rest encryption is the route layer's responsibility — this module
returns plaintext tokens to callers who immediately encrypt-and-persist.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx

from ..core.logger import get_logger

log = get_logger("ownchart.ingest.fhir")

REQUEST_TIMEOUT_S = 30.0
MAX_CONCURRENT_FHIR_REQUESTS = 5


class FhirAuthError(RuntimeError):
    """The FHIR server rejected the bearer token (HTTP 401).

    Raised by fetch_patient_record when the token is dead, so the route
    can mark the connection expired instead of persisting an empty
    snapshot as a successful sync. Six connections sat in that state
    for six months (2026-05 → 2026-11) because every failure here was
    swallowed and the UI kept saying "connected".
    """


class FhirFetchError(RuntimeError):
    """Nothing could be fetched and no 401 explains it (network, 5xx)."""


# ---------------------------------------------------------------------------
# Discovery + auth
# ---------------------------------------------------------------------------


@dataclass
class SmartConfig:
    fhir_base: str
    authorize_endpoint: str
    token_endpoint: str
    raw: dict[str, Any]


@dataclass
class TokenResponse:
    access_token: str
    refresh_token: str | None
    expires_in: int | None
    scope: str | None
    patient: str | None
    id_token: str | None
    raw: dict[str, Any]


def _strip_slash(url: str) -> str:
    return url.rstrip("/")


def _smart_config_url(fhir_base: str) -> str:
    return f"{_strip_slash(fhir_base)}/.well-known/smart-configuration"


async def discover_smart_config(fhir_base: str) -> SmartConfig:
    url = _smart_config_url(fhir_base)
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as c:
        r = await c.get(url, headers={"Accept": "application/json"})
        r.raise_for_status()
        raw = r.json()
    auth = raw.get("authorization_endpoint")
    tok = raw.get("token_endpoint")
    if not auth or not tok:
        raise RuntimeError(f"smart-configuration missing authorization/token endpoint at {url}")
    return SmartConfig(
        fhir_base=_strip_slash(fhir_base),
        authorize_endpoint=auth,
        token_endpoint=tok,
        raw=raw,
    )


def generate_pkce_pair() -> tuple[str, str]:
    """Returns (verifier, S256 challenge). Verifier is 64 base64url chars."""
    verifier = secrets.token_urlsafe(48)[:64]  # well within 43-128
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def build_authorize_url(
    *,
    authorize_endpoint: str,
    client_id: str,
    redirect_uri: str,
    scope: str,
    state: str,
    pkce_challenge: str,
    aud: str,
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "aud": aud,                     # required by SMART — equals fhir_base
        "code_challenge": pkce_challenge,
        "code_challenge_method": "S256",
    }
    sep = "&" if "?" in authorize_endpoint else "?"
    return f"{authorize_endpoint}{sep}{urlencode(params)}"


def _token_auth(client_id: str, client_secret: str | None) -> httpx.BasicAuth | None:
    """Confidential-client auth for the token endpoint.

    Epic issues refresh tokens to patient-facing apps ONLY when the
    app authenticates as a confidential client (client secret or JWT);
    a public PKCE client gets `offline_access` echoed in the granted
    scope but no refresh_token. With a secret configured we send HTTP
    Basic `client_id:client_secret` per RFC 6749 §2.3.1, which is what
    Epic's docs specify for both the code exchange and the refresh.
    """
    if not client_secret:
        return None
    return httpx.BasicAuth(client_id, client_secret)


async def exchange_code_for_token(
    *,
    token_endpoint: str,
    client_id: str,
    code: str,
    redirect_uri: str,
    pkce_verifier: str,
    client_secret: str | None = None,
) -> TokenResponse:
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": pkce_verifier,
    }
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as c:
        r = await c.post(
            token_endpoint,
            data=body,
            headers={"Accept": "application/json"},
            auth=_token_auth(client_id, client_secret),
        )
        if not r.is_success:
            raise RuntimeError(f"token exchange failed: HTTP {r.status_code} — {r.text[:300]}")
        raw = r.json()
    return _parse_token_response(raw)


async def refresh_access_token(
    *,
    token_endpoint: str,
    client_id: str,
    refresh_token: str,
    client_secret: str | None = None,
) -> TokenResponse:
    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as c:
        r = await c.post(
            token_endpoint,
            data=body,
            headers={"Accept": "application/json"},
            auth=_token_auth(client_id, client_secret),
        )
        if not r.is_success:
            raise RuntimeError(f"token refresh failed: HTTP {r.status_code} — {r.text[:300]}")
        raw = r.json()
    return _parse_token_response(raw)


def _parse_token_response(raw: dict[str, Any]) -> TokenResponse:
    return TokenResponse(
        access_token=raw["access_token"],
        refresh_token=raw.get("refresh_token"),
        expires_in=raw.get("expires_in"),
        scope=raw.get("scope"),
        patient=raw.get("patient"),
        id_token=raw.get("id_token"),
        raw=raw,
    )


# ---------------------------------------------------------------------------
# Patient fetch — query plan from jmandel/health-skillz (MIT)
# ---------------------------------------------------------------------------


# Patient-scoped queries. `params` is a dict of additional FHIR search params;
# `patient` is added automatically with the connection's patient_fhir_id.
PATIENT_SEARCH_QUERIES: list[dict[str, Any]] = [
    {"resourceType": "Patient", "label": "Patient", "params": {}, "patientInPath": True},
    # Observations by category
    *[{"resourceType": "Observation", "label": label, "params": {"category": cat}}
      for cat, label in [
          ("laboratory", "Labs"),
          ("vital-signs", "Vitals"),
          ("social-history", "Social History"),
          ("survey", "Surveys"),
          ("exam", "Exams"),
          ("therapy", "Therapy"),
          ("activity", "Activity"),
          ("imaging", "Imaging"),
          ("procedure", "Obs Procedures"),
          ("sdoh", "SDOH"),
          ("functional-status", "Functional"),
          ("disability-status", "Disability"),
          ("cognitive-status", "Cognitive"),
          ("clinical-test", "Clinical Tests"),
      ]],
    # Conditions by category
    *[{"resourceType": "Condition", "label": label, "params": {"category": cat}}
      for cat, label in [
          ("problem-list-item", "Problems"),
          ("health-concern", "Health Concerns"),
          ("encounter-diagnosis", "Diagnoses"),
      ]],
    # DiagnosticReports
    {"resourceType": "DiagnosticReport", "label": "Lab Reports",
     "params": {"category": "http://terminology.hl7.org/CodeSystem/v2-0074|LAB"}},
    {"resourceType": "DiagnosticReport", "label": "Radiology",
     "params": {"category": "http://loinc.org|LP29708-2"}},
    # Documents
    {"resourceType": "DocumentReference", "label": "Clinical Notes",
     "params": {"category": "clinical-note"}},
    {"resourceType": "DocumentReference", "label": "Documents", "params": {}},
    # Care plans + service requests
    {"resourceType": "CarePlan", "label": "Care Plan",
     "params": {"category": "http://hl7.org/fhir/us/core/CodeSystem/careplan-category|assess-plan"}},
    {"resourceType": "ServiceRequest", "label": "Services", "params": {}},
    # Patient-only resources without category
    *[{"resourceType": rt, "label": label, "params": params}
      for rt, label, params in [
          ("AllergyIntolerance", "Allergies", {}),
          ("CareTeam", "Care Team", {"status": "active"}),
          ("Coverage", "Coverage", {}),
          ("Device", "Devices", {}),
          ("Encounter", "Encounters", {}),
          ("FamilyMemberHistory", "Family History", {}),
          ("Goal", "Goals", {}),
          ("Immunization", "Immunizations", {}),
          ("MedicationDispense", "Med Dispensing", {}),
          ("MedicationRequest", "Medications", {"intent": "order"}),
          ("MedicationStatement", "Med History", {}),
          ("Procedure", "Procedures", {}),
          ("QuestionnaireResponse", "Questionnaires", {}),
          ("RelatedPerson", "Related Persons", {}),
      ]],
]

# Resources we don't directly search for, but chase by reference.
REFERENCE_RESOURCE_TYPES = {
    "Practitioner",
    "PractitionerRole",
    "Organization",
    "Location",
    "Medication",
    "Specimen",
    "Questionnaire",
    "Provenance",
}


@dataclass
class FhirSnapshot:
    fhir: dict[str, list[dict]] = field(default_factory=dict)  # resourceType -> [resource]
    error: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    # Requests the server rejected with 401 — a dead or revoked token.
    auth_failures: int = 0
    # Other failed requests (non-2xx other than 403/404, transport
    # errors), as short strings for last_error / logs.
    fetch_failures: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def note_failure(self, what: str) -> None:
        if len(self.fetch_failures) < 50:
            self.fetch_failures.append(what)

    def add(self, resource: dict[str, Any]) -> None:
        rt = resource.get("resourceType")
        if not rt:
            return
        rid = resource.get("id")
        bucket = self.fhir.setdefault(rt, [])
        # Dedup by id within a resourceType.
        if rid:
            for existing in bucket:
                if existing.get("id") == rid:
                    return
        bucket.append(resource)
        self.counts[rt] = self.counts.get(rt, 0) + 1


async def _fetch_paginated(
    client: httpx.AsyncClient,
    initial_url: str,
    *,
    max_pages: int = 50,
    snap: FhirSnapshot | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    url: str | None = initial_url
    pages = 0
    while url and pages < max_pages:
        r = await client.get(url, headers={"Accept": "application/fhir+json"})
        if r.status_code == 401:
            # Dead token. Let the caller decide — a single 401 with a
            # working Patient read is a scope quirk; 401 everywhere is
            # an expired session that must be surfaced, not swallowed.
            raise FhirAuthError(f"HTTP 401 at {url[:120]}")
        if not r.is_success:
            if snap is not None and r.status_code not in (403, 404):
                snap.note_failure(f"HTTP {r.status_code} {url[:100]}")
            # 403 on a known-by-the-vendor-not-supported endpoint is
            # informational, not an error — don't flag it as a
            # warning that suggests a bug. The fetcher is deliberately
            # over-broad; some resources aren't granted on V1 tokens
            # or aren't exposed by the vendor for patient-scoped
            # search at all (e.g., Athena's /Medication, /ServiceRequest,
            # /Coverage, /Device, /FamilyMemberHistory, /RelatedPerson,
            # /QuestionnaireResponse on V1 scopes — Nick's seen all of
            # these). Log at info level with an explanatory event name
            # so the iOS dev / Nick can grep for them but the alert
            # bar doesn't light up.
            event = (
                "fhir_resource_unauthorized"
                if r.status_code == 403
                else "fhir_fetch_non2xx"
            )
            level = "info" if r.status_code == 403 else "warning"
            getattr(log, level)(
                event,
                url=url[:200],
                status=r.status_code,
                note=(
                    "Endpoint not granted for this token or not "
                    "supported by the vendor for patient-scoped search. "
                    "Reconnect with broader scopes if data is expected."
                    if r.status_code == 403 else None
                ),
            )
            break
        bundle = r.json()
        for entry in bundle.get("entry", []) or []:
            res = entry.get("resource")
            if isinstance(res, dict):
                out.append(res)
        next_url = None
        for link in bundle.get("link", []) or []:
            if link.get("relation") == "next":
                next_url = link.get("url")
                break
        url = next_url
        pages += 1
    return out


def _queries_for_vendor(ehr_vendor: str | None) -> list[dict[str, Any]]:
    """Return the search-query manifest for a vendor.

    Currently all vendors use the same manifest (PATIENT_SEARCH_QUERIES).
    Earlier (2026-05-13 morning) we swapped /MedicationRequest +
    /MedicationStatement + /MedicationDispense for a single
    /Medication query on Athena, assuming Athena V1's
    `patient/Medication.read` granted patient-scoped med search.
    Turned out it doesn't — `GET /Medication?patient=...` returns
    403 on Athena V1 too. Athena patient-scoped med data is
    V2-only.
    Current resolution: keep the three-subtype queries for everyone.
    Athena connectors that have V2 med scopes granted
    (patient/MedicationRequest.rs etc., now in the default scope
    string for new Athena connectors) will get real med data via
    the three subtype searches. Legacy V1-only Athena tokens will
    keep getting 403s on these endpoints — which is informational,
    not a failure, and logged that way.
    """
    return PATIENT_SEARCH_QUERIES


async def fetch_patient_record(
    *,
    fhir_base: str,
    access_token: str,
    patient_fhir_id: str,
    ehr_vendor: str | None = None,
) -> FhirSnapshot:
    """Run all patient-scoped queries + reference chasing.

    Attachment binary fetch (DocumentReference content) is intentionally
    deferred to the route layer where we control SourceDocument creation.

    `ehr_vendor` tunes the query manifest — see _queries_for_vendor.
    """
    base = _strip_slash(fhir_base)
    snap = FhirSnapshot()
    queries = _queries_for_vendor(ehr_vendor)
    sem = asyncio.Semaphore(MAX_CONCURRENT_FHIR_REQUESTS)

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT_S,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/fhir+json",
        },
    ) as client:

        async def run_query(q: dict[str, Any]) -> None:
            params = dict(q.get("params") or {})
            rt = q["resourceType"]
            if q.get("patientInPath"):
                # Patient itself — direct read by id.
                url = f"{base}/{rt}/{patient_fhir_id}"
                async with sem:
                    try:
                        r = await client.get(url)
                        if r.status_code == 401:
                            snap.auth_failures += 1
                        elif r.is_success:
                            snap.add(r.json())
                        else:
                            snap.note_failure(f"HTTP {r.status_code} Patient read")
                    except Exception as e:  # noqa: BLE001
                        snap.note_failure(f"Patient read: {type(e).__name__}")
                        log.warning("fhir_patient_read_failed", error=str(e))
                return
            params["patient"] = patient_fhir_id
            params.setdefault("_count", "100")
            url = f"{base}/{rt}?{urlencode(params)}"
            async with sem:
                try:
                    resources = await _fetch_paginated(client, url, snap=snap)
                    for r in resources:
                        snap.add(r)
                except FhirAuthError:
                    snap.auth_failures += 1
                except Exception as e:  # noqa: BLE001
                    snap.note_failure(f"{rt}: {type(e).__name__}")
                    log.warning("fhir_query_failed", resource=rt, label=q.get("label"), error=str(e))

        await asyncio.gather(*[run_query(q) for q in queries])

        # Decide whether this snapshot is real before chasing references.
        # A dead token yields 401 on every query; the old code logged
        # each one at warning level and then returned an empty snapshot
        # that the route persisted as a successful sync.
        if snap.auth_failures and (snap.total == 0 or "Patient" not in snap.fhir):
            log.warning(
                "fhir_snapshot_unauthorized",
                auth_failures=snap.auth_failures,
                fetched=snap.total,
            )
            raise FhirAuthError(
                f"{snap.auth_failures} of {len(queries)} requests were rejected "
                "with HTTP 401 — the access token is expired or revoked"
            )
        if snap.auth_failures:
            log.warning(
                "fhir_snapshot_partial_unauthorized",
                auth_failures=snap.auth_failures,
                fetched=snap.total,
            )
        if snap.total == 0:
            log.warning("fhir_snapshot_empty", failures=snap.fetch_failures[:10])
            detail = "; ".join(snap.fetch_failures[:5]) or "no resources returned"
            raise FhirFetchError(f"no resources could be fetched ({detail})")

        # Reference chasing — collect referenced resource ids, fetch.
        wanted: dict[str, set[str]] = {rt: set() for rt in REFERENCE_RESOURCE_TYPES}

        def _scan(value: Any) -> None:
            if isinstance(value, dict):
                ref = value.get("reference") if "reference" in value else None
                if isinstance(ref, str) and "/" in ref:
                    rt, rid = ref.split("/", 1)
                    if rt in REFERENCE_RESOURCE_TYPES and rid:
                        wanted[rt].add(rid)
                for v in value.values():
                    _scan(v)
            elif isinstance(value, list):
                for v in value:
                    _scan(v)

        for rt_resources in snap.fhir.values():
            for res in rt_resources:
                _scan(res)

        async def _fetch_one(rt: str, rid: str) -> None:
            url = f"{base}/{rt}/{rid}"
            async with sem:
                try:
                    r = await client.get(url)
                    if r.is_success:
                        snap.add(r.json())
                except Exception:  # noqa: BLE001
                    pass

        ref_tasks = []
        for rt, ids in wanted.items():
            for rid in ids:
                # Skip if already fetched somehow.
                existing = snap.fhir.get(rt, [])
                if any(e.get("id") == rid for e in existing):
                    continue
                ref_tasks.append(_fetch_one(rt, rid))
        if ref_tasks:
            await asyncio.gather(*ref_tasks)

    log.info("fhir_snapshot_built", counts=snap.counts)
    return snap
