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


async def exchange_code_for_token(
    *,
    token_endpoint: str,
    client_id: str,
    code: str,
    redirect_uri: str,
    pkce_verifier: str,
) -> TokenResponse:
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": pkce_verifier,
    }
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as c:
        r = await c.post(token_endpoint, data=body, headers={"Accept": "application/json"})
        if not r.is_success:
            raise RuntimeError(f"token exchange failed: HTTP {r.status_code} — {r.text[:300]}")
        raw = r.json()
    return _parse_token_response(raw)


async def refresh_access_token(
    *,
    token_endpoint: str,
    client_id: str,
    refresh_token: str,
) -> TokenResponse:
    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as c:
        r = await c.post(token_endpoint, data=body, headers={"Accept": "application/json"})
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
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    url: str | None = initial_url
    pages = 0
    while url and pages < max_pages:
        r = await client.get(url, headers={"Accept": "application/fhir+json"})
        if not r.is_success:
            log.warning("fhir_fetch_non2xx", url=url[:200], status=r.status_code)
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


async def fetch_patient_record(
    *,
    fhir_base: str,
    access_token: str,
    patient_fhir_id: str,
) -> FhirSnapshot:
    """Run all patient-scoped queries + reference chasing.

    Attachment binary fetch (DocumentReference content) is intentionally
    deferred to the route layer where we control SourceDocument creation.
    """
    base = _strip_slash(fhir_base)
    snap = FhirSnapshot()
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
                        if r.is_success:
                            snap.add(r.json())
                    except Exception as e:  # noqa: BLE001
                        log.warning("fhir_patient_read_failed", error=str(e))
                return
            params["patient"] = patient_fhir_id
            params.setdefault("_count", "100")
            url = f"{base}/{rt}?{urlencode(params)}"
            async with sem:
                try:
                    resources = await _fetch_paginated(client, url)
                    for r in resources:
                        snap.add(r)
                except Exception as e:  # noqa: BLE001
                    log.warning("fhir_query_failed", resource=rt, label=q.get("label"), error=str(e))

        await asyncio.gather(*[run_query(q) for q in PATIENT_SEARCH_QUERIES])

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
