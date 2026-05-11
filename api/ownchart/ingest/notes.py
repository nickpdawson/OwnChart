"""Patient-reported note quick-capture.

A note is short structured text the patient writes themselves
("Tonight I experienced right knee pain after running…").

Storage: the original markdown/plain text is written to the evidence
vault as a UTF-8 file (preserved unchanged), and we create:
  - SourceDocument(source_type='note')
  - EvidenceAnchor(anchor_type='note_full', text_excerpt=body)
  - ExtractedFact(fact_type='life_context_event', extraction_method='patient_self_report')

This gives notes timeline presence by default. Future: LLM extraction
on the note body to mine symptoms/meds/body site/timing into structured
facts of their own (gated on consent).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class NoteIngestRequest:
    body: str
    title: str | None = None
    occurred_at: str | None = None  # ISO date or timestamp
    body_site: str | None = None
    laterality: str | None = None  # left | right | bilateral | unknown
