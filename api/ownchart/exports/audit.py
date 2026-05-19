"""Slice 4 export audit event types.

Five event_type constants used on AuditEvent rows for the export
lifecycle (BE-9 §Audit). Defined as module constants so the route
layer and the worker reference the same strings — a typo at one
callsite can't silently land an unrecognized event_type that the
"export activity" UI doesn't query.

Audit row shape (matches existing AuditEvent model):
  user_id          → who initiated
  person_record_id → which record the export covers
  event_type       → one of the five constants below
  subject_type     → always "export_job"
  subject_id       → str(export_job.id)
  detail           → JSONB with kind-specific metadata
"""

from __future__ import annotations

EXPORT_REQUESTED = "export_requested"
EXPORT_COMPLETED = "export_completed"
EXPORT_FAILED = "export_failed"
EXPORT_DOWNLOADED = "export_downloaded"
EXPORT_DELETED = "export_deleted"

EXPORT_AUDIT_EVENT_TYPES: tuple[str, ...] = (
    EXPORT_REQUESTED,
    EXPORT_COMPLETED,
    EXPORT_FAILED,
    EXPORT_DOWNLOADED,
    EXPORT_DELETED,
)

# Subject type marker — always the same for export events. Keeping
# it as a separate const so the route + worker reference one string.
EXPORT_SUBJECT_TYPE = "export_job"
