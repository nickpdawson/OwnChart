"""CCDA / C-CDA XML ingest.

Parses the high-value sections we need for V1: problems, procedures,
medications, encounters, and the patient header. Each emitted fact
carries an EvidenceAnchor pointing back to the source section.

This is intentionally lightweight — full CCDA template traversal is
big and brittle. We aim for "good enough to populate the dossier
dossier from a real ophtho CCDA" and surface uncertainties to the
review inbox rather than over-state.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from lxml import etree

from ..core.logger import get_logger

log = get_logger("ownchart.ingest.ccda")

NS = {
    "h": "urn:hl7-org:v3",
    "sdtc": "urn:hl7-org:sdtc",
}

# CCDA template IDs we care about for V1.
TEMPLATE = {
    "problems_section": "2.16.840.1.113883.10.20.22.2.5.1",
    "procedures_section": "2.16.840.1.113883.10.20.22.2.7.1",
    "medications_section": "2.16.840.1.113883.10.20.22.2.1.1",
    "encounters_section": "2.16.840.1.113883.10.20.22.2.22.1",
}


@dataclass
class CcdaFact:
    """An anchor + fact pair, both still in plain dataclass form so the
    caller can persist them in a single transaction."""

    fact_type: str  # condition | procedure | medication | encounter
    label: str
    description: str | None
    date_start: datetime | None
    date_end: datetime | None
    date_precision: str | None  # day | month | year | unknown
    coded_concepts: dict[str, Any]  # {snomed:[...], icd10:[...], rxnorm:[...]}
    section_path: str
    text_excerpt: str | None
    review_state: str = "needs_review"
    confidence: int | None = 70


@dataclass
class CcdaIngest:
    patient_name: str | None = None
    patient_dob: datetime | None = None
    document_title: str | None = None
    document_effective: datetime | None = None
    facts: list[CcdaFact] = field(default_factory=list)


def _x(elem: Any, xpath: str, ns: dict[str, str] = NS) -> list[Any]:
    return elem.xpath(xpath, namespaces=ns)


def _text_or_none(elem: Any, xpath: str) -> str | None:
    found = _x(elem, xpath)
    if not found:
        return None
    val = found[0]
    if isinstance(val, str):
        return val.strip() or None
    s = (val.text or "").strip()
    return s or None


def _attr(elem: Any, xpath: str, attr: str) -> str | None:
    found = _x(elem, xpath)
    if not found:
        return None
    return found[0].get(attr) or None


def _parse_hl7_date(s: str | None) -> tuple[datetime | None, str | None]:
    """HL7 dates: YYYYMMDDHHMMSS, with progressively shorter prefixes valid."""
    if not s:
        return None, None
    s = s.strip()
    # Drop timezone suffix if present (e.g. -0500)
    if "-" in s[8:] or "+" in s[8:]:
        s = s.split("-")[0].split("+")[0]
    try:
        if len(s) >= 14:
            return datetime.strptime(s[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc), "day"
        if len(s) >= 8:
            return datetime.strptime(s[:8], "%Y%m%d").replace(tzinfo=timezone.utc), "day"
        if len(s) >= 6:
            return datetime.strptime(s[:6], "%Y%m").replace(tzinfo=timezone.utc), "month"
        if len(s) >= 4:
            return datetime.strptime(s[:4], "%Y").replace(tzinfo=timezone.utc), "year"
    except ValueError:
        return None, None
    return None, None


def _section(root: Any, template_id: str) -> Any | None:
    nodes = _x(
        root,
        f".//h:section[h:templateId[@root='{template_id}']]",
    )
    return nodes[0] if nodes else None


def _coded(elem: Any) -> dict[str, list[dict[str, str]]]:
    """Pull SNOMED / ICD-10 / RxNorm codes off a code element."""
    out: dict[str, list[dict[str, str]]] = {}
    code_nodes = _x(elem, ".//h:code") + _x(elem, ".//h:value[@code]")
    seen: set[tuple[str, str]] = set()
    for c in code_nodes:
        system = c.get("codeSystem") or ""
        code = c.get("code") or ""
        display = c.get("displayName") or ""
        if not code:
            continue
        key = (system, code)
        if key in seen:
            continue
        seen.add(key)
        bucket = {
            "2.16.840.1.113883.6.96": "snomed",      # SNOMED CT
            "2.16.840.1.113883.6.90": "icd10",       # ICD-10-CM
            "2.16.840.1.113883.6.88": "rxnorm",      # RxNorm
            "2.16.840.1.113883.6.103": "icd9",       # ICD-9
            "2.16.840.1.113883.6.1": "loinc",        # LOINC
        }.get(system, "other")
        out.setdefault(bucket, []).append({"code": code, "display": display, "system": system})
    return out


def parse_ccda(xml_bytes: bytes) -> CcdaIngest:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    root = etree.fromstring(xml_bytes, parser=parser)

    out = CcdaIngest()

    # patient header
    out.patient_name = _text_or_none(
        root,
        "./h:recordTarget/h:patientRole/h:patient/h:name/h:given/text()",
    )
    family = _text_or_none(root, "./h:recordTarget/h:patientRole/h:patient/h:name/h:family/text()")
    if out.patient_name and family:
        out.patient_name = f"{out.patient_name} {family}"

    dob_str = _attr(root, "./h:recordTarget/h:patientRole/h:patient/h:birthTime", "value")
    out.patient_dob, _ = _parse_hl7_date(dob_str)

    out.document_title = _text_or_none(root, "./h:title/text()")
    eff = _attr(root, "./h:effectiveTime", "value")
    out.document_effective, _ = _parse_hl7_date(eff)

    # Problems (Conditions)
    sec = _section(root, TEMPLATE["problems_section"])
    if sec is not None:
        for entry in _x(sec, ".//h:observation"):
            label = _attr(entry, "./h:value", "displayName") or _text_or_none(entry, "./h:text/text()")
            if not label:
                continue
            d_low = _attr(entry, ".//h:effectiveTime/h:low", "value")
            d_high = _attr(entry, ".//h:effectiveTime/h:high", "value")
            ds, dp = _parse_hl7_date(d_low)
            de, _ = _parse_hl7_date(d_high)
            out.facts.append(
                CcdaFact(
                    fact_type="condition",
                    label=label,
                    description=_text_or_none(entry, ".//h:text//text()"),
                    date_start=ds,
                    date_end=de,
                    date_precision=dp,
                    coded_concepts=_coded(entry),
                    section_path="/ClinicalDocument/component/structuredBody/component/section[problems]/entry/observation",
                    text_excerpt=_text_or_none(entry, ".//h:text//text()"),
                )
            )

    # Procedures
    sec = _section(root, TEMPLATE["procedures_section"])
    if sec is not None:
        for entry in _x(sec, ".//h:procedure"):
            label = _attr(entry, "./h:code", "displayName") or _text_or_none(entry, "./h:text/text()")
            if not label:
                continue
            d_low = _attr(entry, ".//h:effectiveTime", "value") or _attr(entry, ".//h:effectiveTime/h:low", "value")
            ds, dp = _parse_hl7_date(d_low)
            out.facts.append(
                CcdaFact(
                    fact_type="procedure",
                    label=label,
                    description=_text_or_none(entry, ".//h:text//text()"),
                    date_start=ds,
                    date_end=None,
                    date_precision=dp,
                    coded_concepts=_coded(entry),
                    section_path="/ClinicalDocument/component/structuredBody/component/section[procedures]/entry/procedure",
                    text_excerpt=_text_or_none(entry, ".//h:text//text()"),
                )
            )

    # Medications
    sec = _section(root, TEMPLATE["medications_section"])
    if sec is not None:
        for entry in _x(sec, ".//h:substanceAdministration"):
            label = _attr(entry, ".//h:manufacturedMaterial/h:code", "displayName") or _text_or_none(
                entry, ".//h:text/text()"
            )
            if not label:
                continue
            d_low = _attr(entry, ".//h:effectiveTime/h:low", "value")
            d_high = _attr(entry, ".//h:effectiveTime/h:high", "value")
            ds, dp = _parse_hl7_date(d_low)
            de, _ = _parse_hl7_date(d_high)
            out.facts.append(
                CcdaFact(
                    fact_type="medication",
                    label=label,
                    description=_text_or_none(entry, ".//h:text//text()"),
                    date_start=ds,
                    date_end=de,
                    date_precision=dp,
                    coded_concepts=_coded(entry),
                    section_path="/ClinicalDocument/component/structuredBody/component/section[medications]/entry/substanceAdministration",
                    text_excerpt=_text_or_none(entry, ".//h:text//text()"),
                )
            )

    # Encounters
    sec = _section(root, TEMPLATE["encounters_section"])
    if sec is not None:
        for entry in _x(sec, ".//h:encounter"):
            label = _attr(entry, "./h:code", "displayName") or _text_or_none(entry, "./h:text/text()") or "Encounter"
            d_low = _attr(entry, ".//h:effectiveTime", "value") or _attr(entry, ".//h:effectiveTime/h:low", "value")
            ds, dp = _parse_hl7_date(d_low)
            out.facts.append(
                CcdaFact(
                    fact_type="encounter",
                    label=label,
                    description=_text_or_none(entry, ".//h:text//text()"),
                    date_start=ds,
                    date_end=None,
                    date_precision=dp,
                    coded_concepts=_coded(entry),
                    section_path="/ClinicalDocument/component/structuredBody/component/section[encounters]/entry/encounter",
                    text_excerpt=_text_or_none(entry, ".//h:text//text()"),
                )
            )

    log.info(
        "ccda_parsed",
        fact_count=len(out.facts),
        has_patient_name=bool(out.patient_name),
    )
    return out


def make_uuid_for_claim(_: CcdaFact) -> uuid.UUID:
    return uuid.uuid4()
