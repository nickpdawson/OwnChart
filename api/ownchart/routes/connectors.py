"""SMART on FHIR connector endpoints.

GET  /api/connectors                       — list with status for this user
POST /api/connectors/{slug}/connect        — start OAuth (returns authorize URL)
GET  /api/connectors/callback              — token exchange (EHR redirects here)
POST /api/connectors/{conn_id}/sync        — pull FHIR snapshot, persist
POST /api/connectors/{conn_id}/disconnect

Tokens are encrypted at rest (AES-256-GCM via core/crypto). The
public-base-url + `/api/connectors/callback` path must match the redirect_uri
registered with the EHR vendor (e.g. on file with Epic at fhir.epic.com).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import get_settings
from ..core.crypto import decrypt_str, encrypt
from ..core.db import get_session
from ..core.logger import get_logger
import os
import re

from ..ingest import fhir as fhir_ingest
from ..ingest import fhir_attachments, pdf as pdf_ingest, provider_directory, storage
from ..ingest.fact_classifier import review_state_for_fhir
from ..models.evidence_anchor import EvidenceAnchor
from ..models.extracted_fact import ExtractedFact
from ..models.oauth_session import OAuthSession
from ..models.provider_connection import ProviderConnection
from ..models.provider_connector import ProviderConnector
from ..models.source_document import SourceDocument
from ..models.user import User
from .auth import get_current_user

router = APIRouter()
log = get_logger("ownchart.routes.connectors")
_settings = get_settings()


def _redirect_uri() -> str:
    return f"{_settings.public_base_url.rstrip('/')}/api/connectors/callback"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ConnectorSummary(BaseModel):
    id: str
    slug: str
    name: str
    ehr_vendor: str | None
    fhir_base: str
    enabled: bool
    has_client_id: bool
    connection: dict | None  # {id, status, expires_at, last_synced_at, patient_display_name}


class ConnectStartResponse(BaseModel):
    authorize_url: str
    state: str
    connector: str


class SyncResponse(BaseModel):
    connection_id: str
    counts: dict
    source_id: str  # the SourceDocument that holds the raw FHIR snapshot
    fact_count: int
    attachment_count: int
    attachment_errors: list[str]
    error: str | None


class DirectoryEntryReadout(BaseModel):
    name: str
    fhir_base: str
    ehr_vendor: str
    suggested_slug: str
    has_client_id: bool


class CreateConnectorRequest(BaseModel):
    name: str
    fhir_base: str
    ehr_vendor: str  # 'epic' | 'athena' | 'cerner' | 'unknown'
    scopes: str | None = None


_SLUG_RE = re.compile(r"[^a-zA-Z0-9]+")


def _slugify(name: str) -> str:
    s = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return s or "provider"


def _client_id_env_for(ehr_vendor: str) -> str:
    return f"OWNCHART_{ehr_vendor.upper()}_CLIENT_ID"


# Per-vendor SMART scope defaults. Epic accepts the wildcard
# `patient/*.read` and grants all approved resource scopes; Athena's
# R4 SMART V1 catalog is more restrictive — wildcard fails Okta
# policy evaluation. Two specific Athena quirks shape the default:
#
# 1. V1 has no per-patient Medication SEARCH endpoint. The V1 scope
#    `patient/Medication.read` covers Medication-by-id reads only;
#    `GET /Medication?patient=...` returns 403. To actually pull
#    a patient's medication list from Athena, we need V2-shaped
#    scopes (`patient/MedicationRequest.rs`, MedicationStatement,
#    MedicationDispense), which Athena's dev portal exposes
#    separately. Including BOTH V1 and V2 med scopes is harmless
#    when the V2 ones are approved on the app and gives the
#    fetcher real data to pull.
#
# 2. offline_access is gated by a separate Okta authorization-
#    server policy from FHIR scopes. Including it when the app
#    doesn't have it pre-approved causes the whole request to
#    fail access_denied. We omit it from the default; can be
#    added per-connector via DB override once the operator gets
#    Athena dev support to enable it.
#
# See memory/reference_athena_smart_quirks.md for the full catalog.
_ATHENA_DEFAULT_SCOPES = (
    "openid fhirUser launch/patient "
    # V1 resource scopes
    "patient/Patient.read patient/Observation.read patient/Condition.read "
    "patient/Medication.read patient/AllergyIntolerance.read "
    "patient/Procedure.read patient/Immunization.read "
    "patient/DiagnosticReport.read patient/Encounter.read "
    "patient/CarePlan.read patient/CareTeam.read patient/Goal.read "
    "patient/DocumentReference.read "
    # V2 medication scopes — required for patient-scoped med SEARCH
    "patient/MedicationRequest.rs patient/MedicationStatement.rs "
    "patient/MedicationDispense.rs"
)
_DEFAULT_SCOPES = "openid fhirUser launch/patient patient/*.read"

# ModMed (Modernizing Medicine) — Drummond-certified FHIR R4 API,
# per-practice fhir_base (NOT multi-tenant like Athena), specialty-
# focused EHR (derm / ophth / ortho / GI / plastic / urology).
# First-connect untested as of 2026-05-13 (Nick's app is "Pending for
# Approval"). Start with the standard wildcard; if it fails at first
# connect like Athena did, swap to an explicit per-resource list and
# update memory/reference_modmed_smart_quirks.md.
#
# Drummond cert constrains them to USCDI shapes, so this scope set
# should cover Patient, Condition, AllergyIntolerance, MedicationRequest,
# Observation, Procedure, Immunization, DiagnosticReport, DocumentReference,
# CarePlan, CareTeam, Goal, Encounter, Provenance.
_MODMED_DEFAULT_SCOPES = (
    "openid fhirUser launch/patient offline_access patient/*.read"
)


def _default_scopes_for(ehr_vendor: str | None) -> str:
    v = (ehr_vendor or "").lower()
    if v == "athena":
        return _ATHENA_DEFAULT_SCOPES
    if v == "modmed":
        return _MODMED_DEFAULT_SCOPES
    return _DEFAULT_SCOPES


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


@router.get("")
async def list_connectors(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[ConnectorSummary]:
    cs = (await db.execute(
        select(ProviderConnector).order_by(ProviderConnector.name)
    )).scalars().all()
    conns = (await db.execute(
        select(ProviderConnection).where(ProviderConnection.user_id == user.id)
    )).scalars().all()
    by_connector: dict[uuid.UUID, ProviderConnection] = {c.connector_id: c for c in conns}

    out = []
    for c in cs:
        link = by_connector.get(c.id)
        out.append(
            ConnectorSummary(
                id=str(c.id),
                slug=c.slug,
                name=c.name,
                ehr_vendor=c.ehr_vendor,
                fhir_base=c.fhir_base,
                enabled=c.enabled,
                has_client_id=bool(c.client_id),
                connection=(
                    {
                        "id": str(link.id),
                        "status": link.status,
                        "expires_at": link.expires_at.isoformat() if link.expires_at else None,
                        "last_synced_at": link.last_synced_at.isoformat() if link.last_synced_at else None,
                        "patient_display_name": link.patient_display_name,
                        "cached_resource_counts": link.cached_resource_counts,
                    }
                    if link
                    else None
                ),
            )
        )
    return out


# ---------------------------------------------------------------------------
# OAuth start
# ---------------------------------------------------------------------------


@router.get("/directory/search")
async def directory_search(
    q: str = Query(default="", description="Substring/keyword query (e.g. 'stanford')"),
    vendor: str = Query(default="epic"),
    limit: int = Query(default=25, le=100),
    _user: User = Depends(get_current_user),
) -> list[DirectoryEntryReadout]:
    """Search a vendor's published FHIR endpoint directory.

    Today: Epic only. Athena/Cerner directories aren't open enough to
    mirror cleanly; for those vendors, the user pastes a fhir_base URL
    via POST /api/connectors directly.
    """
    if vendor != "epic":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Directory search isn't available for {vendor} yet. Use POST /api/connectors with a known fhir_base.",
        )
    entries = await provider_directory.get_directory(vendor)
    matches = provider_directory.search(entries, q, limit=limit)
    has_cid = bool(os.environ.get(_client_id_env_for(vendor)))
    return [
        DirectoryEntryReadout(
            name=e.name,
            fhir_base=e.fhir_base,
            ehr_vendor=e.ehr_vendor,
            suggested_slug=_slugify(e.name),
            has_client_id=has_cid,
        )
        for e in matches
    ]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_connector(
    body: CreateConnectorRequest,
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> ConnectorSummary:
    """Materialize a directory hit (or a manually-typed entry) into a
    provider_connectors row this user can then Connect to."""
    if not body.name.strip() or not body.fhir_base.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="name and fhir_base required")
    if not body.fhir_base.startswith(("https://", "http://")):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="fhir_base must be an absolute URL")
    slug = _slugify(body.name)
    existing = (await db.execute(
        select(ProviderConnector).where(ProviderConnector.slug == slug)
    )).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Connector '{slug}' already exists",
        )

    client_id = os.environ.get(_client_id_env_for(body.ehr_vendor))
    scopes = body.scopes or _default_scopes_for(body.ehr_vendor)

    c = ProviderConnector(
        slug=slug,
        name=body.name.strip()[:255],
        ehr_vendor=body.ehr_vendor,
        fhir_base=body.fhir_base.strip(),
        scopes=scopes,
        client_id=client_id,
        enabled=True,
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return ConnectorSummary(
        id=str(c.id),
        slug=c.slug,
        name=c.name,
        ehr_vendor=c.ehr_vendor,
        fhir_base=c.fhir_base,
        enabled=c.enabled,
        has_client_id=bool(c.client_id),
        connection=None,
    )


@router.delete("/{conn_id_or_slug}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connector(
    conn_id_or_slug: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> None:
    """Delete a connector if no user has an active connection on it.

    Disabling (vs deleting) is via the `enabled` flag in the seed yaml or
    a future UI toggle — but actual deletion (e.g. removing a typo'd
    connector) goes here.
    """
    # Resolve by id or by slug.
    target: ProviderConnector | None = None
    try:
        target = await db.get(ProviderConnector, uuid.UUID(conn_id_or_slug))
    except ValueError:
        pass
    if target is None:
        target = (await db.execute(
            select(ProviderConnector).where(ProviderConnector.slug == conn_id_or_slug)
        )).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    has_conn = (await db.execute(
        select(ProviderConnection).where(ProviderConnection.connector_id == target.id).limit(1)
    )).scalar_one_or_none()
    if has_conn is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Connector has at least one connection. Disconnect first, then retry.",
        )
    await db.delete(target)
    await db.commit()
    log.info("connector_deleted", slug=target.slug, user_id=str(user.id))


@router.post("/{slug}/connect")
async def start_connect(
    slug: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> ConnectStartResponse:
    c = (await db.execute(
        select(ProviderConnector).where(ProviderConnector.slug == slug)
    )).scalar_one_or_none()
    if c is None or not c.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown or disabled connector")
    if not c.client_id:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail=f"{c.name} has no client_id configured. Register OwnChart with the EHR and set client_id.",
        )

    # Discover authorize/token endpoints if we don't already have them cached.
    if not c.authorize_endpoint or not c.token_endpoint:
        try:
            smart = await fhir_ingest.discover_smart_config(c.fhir_base)
        except Exception as e:  # noqa: BLE001
            # Network or HTTP failure on the EHR's SMART config endpoint.
            # Surface a useful 502 instead of a bare 500 — this happens for
            # endpoints that block egress, return non-standard responses,
            # or whose host is unreachable from this deployment.
            log.warning(
                "smart_discovery_failed",
                slug=c.slug,
                fhir_base=c.fhir_base,
                error=f"{type(e).__name__}: {e}",
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    f"Could not reach {c.name}'s SMART configuration endpoint "
                    f"({type(e).__name__}). The provider's FHIR base URL may be "
                    "wrong, behind a firewall, or non-standard."
                ),
            ) from e
        c.authorize_endpoint = smart.authorize_endpoint
        c.token_endpoint = smart.token_endpoint
        c.smart_config_url = c.smart_config_url or fhir_ingest._smart_config_url(c.fhir_base)
        c.raw_config = smart.raw

    verifier, challenge = fhir_ingest.generate_pkce_pair()
    sess = OAuthSession(
        user_id=user.id,
        connector_id=c.id,
        pkce_verifier=verifier,
        redirect_back_to="/connectors",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    db.add(sess)
    await db.commit()
    await db.refresh(sess)

    authorize_url = fhir_ingest.build_authorize_url(
        authorize_endpoint=c.authorize_endpoint,
        client_id=c.client_id,
        redirect_uri=_redirect_uri(),
        scope=c.scopes,
        state=str(sess.id),
        pkce_challenge=challenge,
        aud=c.fhir_base,
    )
    log.info("connector_oauth_start", connector=slug, user_id=str(user.id))
    return ConnectStartResponse(authorize_url=authorize_url, state=str(sess.id), connector=slug)


# ---------------------------------------------------------------------------
# OAuth callback — EHR redirects user-browser here with ?code=&state=
# ---------------------------------------------------------------------------


@router.get("/callback")
async def oauth_callback(
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    redirect_to = "/connectors"
    if error:
        log.warning("oauth_callback_provider_error", error=error, desc=error_description)
        return RedirectResponse(
            url=f"{redirect_to}?error={error}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    if not code or not state:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing code or state")

    try:
        state_uuid = uuid.UUID(state)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Bad state") from e

    sess = await db.get(OAuthSession, state_uuid)
    if sess is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown state")
    if sess.user_id != user.id:
        # Someone else's state. Refuse.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="State does not match session user")
    if sess.expires_at < datetime.now(timezone.utc):
        await db.delete(sess)
        await db.commit()
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="State expired; restart connect flow")

    connector = await db.get(ProviderConnector, sess.connector_id)
    if connector is None or not connector.token_endpoint or not connector.client_id:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Connector misconfigured")

    try:
        tok = await fhir_ingest.exchange_code_for_token(
            token_endpoint=connector.token_endpoint,
            client_id=connector.client_id,
            code=code,
            redirect_uri=_redirect_uri(),
            pkce_verifier=sess.pkce_verifier,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("oauth_token_exchange_failed", connector=connector.slug, error=str(e))
        await db.delete(sess)
        await db.commit()
        return RedirectResponse(
            url=f"{redirect_to}?error=token_exchange_failed",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=tok.expires_in)
        if tok.expires_in
        else None
    )

    # Upsert the connection. One per (user, connector); refresh tokens on
    # re-connect.
    existing = (await db.execute(
        select(ProviderConnection).where(
            ProviderConnection.user_id == user.id,
            ProviderConnection.connector_id == connector.id,
        )
    )).scalar_one_or_none()
    if existing is None:
        existing = ProviderConnection(user_id=user.id, connector_id=connector.id)
        db.add(existing)

    existing.access_token_enc = encrypt(tok.access_token)
    existing.refresh_token_enc = encrypt(tok.refresh_token) if tok.refresh_token else None
    existing.expires_at = expires_at
    existing.scope_granted = tok.scope
    existing.patient_fhir_id = tok.patient
    existing.status = "connected"
    existing.last_error = None

    # Single-use state.
    await db.delete(sess)
    await db.commit()

    log.info(
        "connector_oauth_complete",
        connector=connector.slug,
        user_id=str(user.id),
        has_refresh=bool(tok.refresh_token),
        patient=tok.patient,
    )
    return RedirectResponse(
        url=f"{redirect_to}?connected={connector.slug}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------------------------------------------------------------------------
# Sync — pull FHIR snapshot, persist as one SourceDocument + facts
# ---------------------------------------------------------------------------


_FHIR_TO_CLAIM = {
    "Encounter": "encounter",
    "Condition": "condition",
    "Procedure": "procedure",
    "MedicationRequest": "medication",
    "MedicationStatement": "medication",
    "MedicationDispense": "medication",
    "AllergyIntolerance": "condition",
    "Immunization": "procedure",
    "Observation": "observation",
    "DiagnosticReport": "observation",
    "ImagingStudy": "imaging_study",
}


def _coding_display(cc: dict | None) -> str | None:
    """Return a human-readable display string from a CodeableConcept,
    or None. Prefers `.text` over `.coding[].display`."""
    if not isinstance(cc, dict):
        return None
    text = cc.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    for cod in (cc.get("coding") or []):
        if isinstance(cod, dict):
            disp = cod.get("display")
            if isinstance(disp, str) and disp.strip():
                return disp.strip()
    return None


def _label_for(resource: dict) -> str:
    """Derive a patient-readable label for a FHIR resource.

    Priority: code.text → code.coding[].display → resource-type-
    specific fields (Encounter.type / class / serviceType,
    MedicationRequest.medicationCodeableConcept, etc.) → "Resource
    on YYYY-MM-DD" date fallback → "Resource (no display)" last
    resort.

    NEVER returns "{type} {resource_id}" — the FHIR resource ID is
    opaque to the patient and was the source of "Encounter
    enZx7fBPAul..." garbage in the Notable moments rail (UX polish
    pass 2026-05-10).
    """
    # 1. CodeableConcept on `code` — covers Procedure, Observation,
    # Condition, DiagnosticReport, AllergyIntolerance.
    disp = _coding_display(resource.get("code"))
    if disp:
        return disp[:512]

    rt = resource.get("resourceType", "Resource")

    # 2. Resource-type-specific fields.
    if rt == "Encounter":
        # type is a list of CodeableConcept; first non-empty wins.
        for t in (resource.get("type") or []):
            disp = _coding_display(t)
            if disp:
                return disp[:512]
        # class is a Coding (singular) — "AMB" / "IMP" / etc.
        cls = resource.get("class")
        if isinstance(cls, dict):
            d = cls.get("display") or cls.get("code")
            if isinstance(d, str) and d.strip():
                # Make "AMB" read as "Ambulatory visit" — the codes
                # are well-known and a few labels go a long way.
                pretty = _ENCOUNTER_CLASS_LABEL.get(d.strip().upper(), d.strip())
                return f"{pretty}"[:512]
        # serviceType / reasonCode CodeableConcepts.
        for fld in ("serviceType", "priority"):
            disp = _coding_display(resource.get(fld))
            if disp:
                return disp[:512]
        for r in (resource.get("reasonCode") or []):
            disp = _coding_display(r)
            if disp:
                return disp[:512]

    elif rt in {"MedicationRequest", "MedicationDispense", "MedicationStatement", "MedicationAdministration"}:
        disp = _coding_display(resource.get("medicationCodeableConcept"))
        if disp:
            return disp[:512]
        # medicationReference is to a Medication resource we don't
        # follow here; the contained Medication is sometimes inline.
        ref = resource.get("medicationReference")
        if isinstance(ref, dict):
            d = ref.get("display")
            if isinstance(d, str) and d.strip():
                return d.strip()[:512]

    elif rt == "AllergyIntolerance":
        disp = _coding_display(resource.get("code"))
        if disp:
            return disp[:512]

    elif rt == "Immunization":
        disp = _coding_display(resource.get("vaccineCode"))
        if disp:
            return disp[:512]

    # 3. Last resort: humane date-based fallback. Better to render
    # "Encounter on 2026-05-01" than "Encounter enZx7fBPAul..." —
    # the resource ID conveys nothing to the patient.
    for fld in (
        "performedDateTime", "occurrenceDateTime", "effectiveDateTime",
        "onsetDateTime", "authoredOn", "recordedDate", "issued",
    ):
        v = resource.get(fld)
        if isinstance(v, str) and len(v) >= 10:
            return f"{rt} on {v[:10]}"[:512]
    for pf in ("performedPeriod", "effectivePeriod", "occurrencePeriod", "period"):
        period = resource.get(pf)
        if isinstance(period, dict):
            v = period.get("start") or period.get("end")
            if isinstance(v, str) and len(v) >= 10:
                return f"{rt} on {v[:10]}"[:512]

    # 4. Final fallback — explicit "no display" so it's grep-able in
    # the data when we need to fix root cause. Never returns the
    # opaque resource ID.
    return f"{rt} (no display)"[:512]


# Encounter.class is a coded value-set; render the common ones in
# plain English. Falls through to the raw code when unrecognized.
_ENCOUNTER_CLASS_LABEL: dict[str, str] = {
    "AMB":     "Ambulatory visit",
    "EMER":    "Emergency visit",
    "IMP":     "Inpatient stay",
    "ACUTE":   "Acute inpatient stay",
    "NONAC":   "Non-acute inpatient stay",
    "OBSENC":  "Observation",
    "HH":      "Home health visit",
    "VR":      "Virtual visit",
    "FLD":     "Field visit",
    "PRENC":   "Pre-admission",
    "SS":      "Short-stay visit",
}


def _parse_iso(v: object) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def _date_for(resource: dict) -> tuple[datetime | None, str | None]:
    # Singular dateTime fields, in priority order. The order matters when a
    # resource has both — e.g. DiagnosticReport.effectiveDateTime should beat
    # DiagnosticReport.issued because the former is the clinical event date.
    for fld in (
        "effectiveDateTime",
        "performedDateTime",
        "occurrenceDateTime",
        "onsetDateTime",
        "authoredOn",
        "recordedDate",        # Condition uses this
        "assertedDate",        # Condition (legacy)
        "issued",              # DiagnosticReport / Observation fallback
    ):
        d = _parse_iso(resource.get(fld))
        if d is not None:
            return d, "day"
    # Period-shaped fields. Procedure uses performedPeriod, Encounter uses
    # period, Observation/DiagnosticReport may use effectivePeriod.
    for pf in ("performedPeriod", "effectivePeriod", "occurrencePeriod", "period"):
        period = resource.get(pf)
        if isinstance(period, dict):
            d = _parse_iso(period.get("start") or period.get("end"))
            if d is not None:
                return d, "day"
    return None, None


_ENCOUNTER_REF_RE = re.compile(r"^Encounter/(.+)$")


def _build_encounter_date_index(snap_fhir: dict[str, list[dict]]) -> dict[str, tuple[datetime, str]]:
    """Map encounter id → its earliest dated period start (or end fallback).

    Used so resources that reference an Encounter but lack their own date
    (observed on at least one EHR: Procedure.performedDateTime missing, but the
    parent Encounter has period.start) can fall back cleanly.
    """
    out: dict[str, tuple[datetime, str]] = {}
    for enc in snap_fhir.get("Encounter", []) or []:
        eid = enc.get("id")
        if not isinstance(eid, str):
            continue
        d, p = _date_for(enc)
        if d is not None:
            out[eid] = (d, p or "day")
    return out


def _encounter_id_from(resource: dict) -> str | None:
    """Pull the linked encounter id from FHIR's `encounter.reference` shape.

    Handles both string-shaped references ("Encounter/abc123") and the
    full Reference object form ({"reference": "Encounter/abc123"}).
    """
    enc = resource.get("encounter")
    if isinstance(enc, dict):
        ref = enc.get("reference")
    else:
        ref = enc
    if not isinstance(ref, str):
        return None
    m = _ENCOUNTER_REF_RE.match(ref)
    return m.group(1) if m else None


def _date_for_with_fallback(
    resource: dict, encounter_dates: dict[str, tuple[datetime, str]]
) -> tuple[datetime | None, str | None]:
    """Resource's own date; if missing, fall back to its linked Encounter."""
    d, p = _date_for(resource)
    if d is not None:
        return d, p
    eid = _encounter_id_from(resource)
    if eid and eid in encounter_dates:
        return encounter_dates[eid]
    return None, None


@router.post("/{conn_id}/sync")
async def sync_connection(
    conn_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> SyncResponse:
    conn = await db.get(ProviderConnection, conn_id)
    if conn is None or conn.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    connector = await db.get(ProviderConnector, conn.connector_id)
    if connector is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Connector missing")

    access_token = decrypt_str(conn.access_token_enc)
    if not access_token:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No access token; reconnect")

    # Refresh if expired (or close to it).
    now = datetime.now(timezone.utc)
    if conn.expires_at and conn.expires_at <= now + timedelta(seconds=30):
        refresh_token = decrypt_str(conn.refresh_token_enc)
        if refresh_token and connector.token_endpoint and connector.client_id:
            try:
                tok = await fhir_ingest.refresh_access_token(
                    token_endpoint=connector.token_endpoint,
                    client_id=connector.client_id,
                    refresh_token=refresh_token,
                )
                access_token = tok.access_token
                conn.access_token_enc = encrypt(tok.access_token)
                if tok.refresh_token:
                    conn.refresh_token_enc = encrypt(tok.refresh_token)
                conn.expires_at = (
                    datetime.now(timezone.utc) + timedelta(seconds=tok.expires_in)
                    if tok.expires_in
                    else None
                )
                await db.commit()
            except Exception as e:  # noqa: BLE001
                conn.status = "expired"
                conn.last_error = f"refresh failed: {e}"
                await db.commit()
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Refresh failed; reconnect") from e

    if not conn.patient_fhir_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No patient context on this connection. Re-connect to obtain launch/patient scope.",
        )

    snap = await fhir_ingest.fetch_patient_record(
        fhir_base=connector.fhir_base,
        access_token=access_token,
        patient_fhir_id=conn.patient_fhir_id,
        ehr_vendor=connector.ehr_vendor,
    )

    # Persist the raw bundle as one SourceDocument (source_type='fhir_bundle').
    bundle_bytes = json.dumps(snap.fhir, sort_keys=True, default=str).encode("utf-8")

    async def _stream():
        yield bundle_bytes

    blob = await storage.write_blob(_stream(), suffix=".json")

    src = SourceDocument(
        owner_user_id=user.id,
        source_type="fhir_bundle",
        original_filename=f"{connector.slug}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json",
        storage_uri=blob.storage_uri,
        hash=f"sha256:{blob.sha256}",
        mime_type="application/fhir+json",
        acquired_at=datetime.now(timezone.utc),
        source_system=f"{connector.ehr_vendor or 'fhir'}:{connector.slug}",
        source_label=connector.name,
        raw_metadata={
            "fhir_base": connector.fhir_base,
            "patient_fhir_id": conn.patient_fhir_id,
            "counts": snap.counts,
            "deduplicated": blob.already_existed,
            "size_bytes": blob.size_bytes,
        },
    )
    db.add(src)
    await db.flush()

    # Normalize each clinically-meaningful resource into ExtractedFact +
    # EvidenceAnchor. DocumentReference attachments become separate
    # SourceDocuments in a follow-up — for V1, we just point facts at
    # the FHIR snapshot itself.
    encounter_dates = _build_encounter_date_index(snap.fhir)
    fact_count = 0
    fallback_dated = 0
    for rt, resources in snap.fhir.items():
        fact_type = _FHIR_TO_CLAIM.get(rt)
        if not fact_type:
            continue
        for res in resources:
            anchor = EvidenceAnchor(
                source_document_id=src.id,
                anchor_type="fhir_resource",
                section_path=f"{rt}/{res.get('id', '?')}",
                text_excerpt=None,
            )
            db.add(anchor)
            await db.flush()
            own_date, _ = _date_for(res)
            ds, dp = _date_for_with_fallback(res, encounter_dates)
            if ds is not None and own_date is None:
                fallback_dated += 1
            label = _label_for(res)
            db.add(
                ExtractedFact(
                    fact_type=fact_type,
                    label=label,
                    description=None,
                    date_start=ds,
                    date_end=None,
                    date_precision=dp,
                    confidence=85,  # FHIR is structured + provider-attested; high baseline
                    review_state=review_state_for_fhir(label),
                    evidence_anchor_ids=[anchor.id],
                    extraction_method="fhir_resource",
                )
            )
            fact_count += 1
    if fallback_dated:
        log.info("encounter_date_fallback_applied", count=fallback_dated)

    # ----------------------------------------------------------------------
    # Attachments — DocumentReference + DiagnosticReport binary content
    # ----------------------------------------------------------------------
    refs = fhir_attachments.collect_attachment_refs(snap.fhir)
    attachment_summary = await fhir_attachments.fetch_attachments(
        fhir_base=connector.fhir_base,
        access_token=access_token,
        refs=refs,
    )
    attachment_count = 0
    for att in attachment_summary.fetched:
        suffix = fhir_attachments.ext_for_mime(att.mime)

        async def _stream(b: bytes = att.bytes_):
            yield b

        att_blob = await storage.write_blob(_stream(), suffix=suffix)
        meta = fhir_attachments.summary_for_metadata(att, conn.id)
        if att.plaintext:
            meta["plaintext_excerpt"] = att.plaintext[:2000]
        att_src = SourceDocument(
            owner_user_id=user.id,
            source_type=fhir_attachments.derive_source_type(att.mime),
            original_filename=(att.ref.title or f"{att.ref.source_resource_type}-{att.ref.source_resource_id}-{att.ref.content_index}")[:512],
            storage_uri=att_blob.storage_uri,
            hash=f"sha256:{att_blob.sha256}",
            mime_type=att.mime,
            acquired_at=datetime.now(timezone.utc),
            source_system=f"{connector.ehr_vendor or 'fhir'}:{connector.slug}",
            source_label=connector.name,
            raw_metadata=meta,
        )
        db.add(att_src)
        await db.flush()

        # If the attachment is a PDF, run the same render+OCR pipeline that
        # manual /api/sources/pdf upload triggers — pages on disk + per-
        # page EvidenceAnchors + Tesseract excerpts where there's no text
        # layer. This makes attachment-derived PDFs first-class on the
        # source detail page, the dossier source-link, and the future
        # Claude Vision extract-facts path.
        if att_src.source_type == "pdf":
            try:
                await pdf_ingest.process_pdf_source(
                    db, att_src, att.bytes_, refine_source_type=True,
                )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "attachment_pdf_pipeline_failed",
                    source_id=str(att_src.id),
                    error=str(e),
                )
        # Clinical notes (RTF/HTML/text DocumentReferences) and ccda_xml
        # encounter summaries: schedule the LLM extractor as a
        # background task so the user gets a fast 201 on /sync and the
        # facts land within ~30s. This is the auto-extraction hook
        # Nick asked about 2026-05-14 — without it, only the manual
        # backfill script populates facts, and new syncs leave content
        # invisible to EI until someone reruns the script.
        elif att_src.source_type in ("clinical_note", "ccda_xml"):
            if (att.plaintext or "") and len(att.plaintext or "") >= 40:
                background_tasks.add_task(
                    _extract_clinical_note_in_background,
                    source_id=att_src.id,
                    user_id=user.id,
                )

        # Anchor on the FHIR snapshot pointing back to the attachment SourceDocument.
        anchor = EvidenceAnchor(
            source_document_id=src.id,
            anchor_type="fhir_attachment",
            section_path=f"{att.ref.source_resource_type}/{att.ref.source_resource_id}/content/{att.ref.content_index} → SourceDocument/{att_src.id}",
            text_excerpt=(att.plaintext or "")[:2000] or None,
        )
        db.add(anchor)
        attachment_count += 1

    conn.last_synced_at = datetime.now(timezone.utc)
    conn.cached_resource_counts = {**snap.counts, "_attachments": attachment_count}
    await db.commit()

    log.info(
        "connector_sync_complete",
        connector=connector.slug,
        counts=snap.counts,
        facts=fact_count,
        attachments=attachment_count,
        attachment_errors=len(attachment_summary.errors),
    )
    return SyncResponse(
        connection_id=str(conn.id),
        counts=snap.counts,
        source_id=str(src.id),
        fact_count=fact_count,
        attachment_count=attachment_count,
        attachment_errors=attachment_summary.errors[:20],
        error=None,
    )


# ---------------------------------------------------------------------------
# Disconnect
# ---------------------------------------------------------------------------


@router.post("/{conn_id}/disconnect", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect(
    conn_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> None:
    conn = await db.get(ProviderConnection, conn_id)
    if conn is None or conn.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    # Wipe tokens but keep the row for audit (status=revoked).
    conn.access_token_enc = None
    conn.refresh_token_enc = None
    conn.status = "revoked"
    await db.commit()
    log.info("connector_disconnected", connection_id=str(conn.id), user_id=str(user.id))


# ---------------------------------------------------------------------------
# Background extraction hooks — invoked from /sync after a clinical_note
# or ccda_xml attachment lands. Opens a fresh SessionLocal because the
# request's session closes when the response is sent.

async def _extract_clinical_note_in_background(
    *,
    source_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    from ..core.db import SessionLocal
    from ..extract.clinical_note import extract_clinical_note
    from ..models.source_document import SourceDocument as _Src
    from ..models.user import User as _User

    async with SessionLocal() as db:
        src = await db.get(_Src, source_id)
        usr = await db.get(_User, user_id)
        if src is None or usr is None:
            return
        try:
            await extract_clinical_note(db, usr, src)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "clinical_note_extract_background_failed",
                source_id=str(source_id),
                error=f"{type(e).__name__}: {e}",
            )
