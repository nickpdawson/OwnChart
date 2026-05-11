"""PHI-safe structured logger.

Default behaviour: redact any field that smells like patient text. Raw payload
logging is gated behind OWNCHART_DEBUG_PAYLOADS=1 and emits a noisy WARN at
startup so it can never silently leak.
"""

import logging
import sys
from typing import Any

import structlog

from .config import get_settings

# Field names that almost certainly carry PHI. Match conservatively — we'd
# rather over-redact than leak.
_REDACT_KEYS = {
    "text",
    "excerpt",
    "ocr",
    "ocr_text",
    "page_text",
    "note",
    "notes",
    "patient",
    "patient_name",
    "name",
    "dob",
    "address",
    "phone",
    "email",
    "ssn",
    "mrn",
    "narrative",
    "summary",
    "answer",
    "query",
    "question",
    "prompt",
    "system",
    "user_message",
    "completion",
    "anthropic_response",
    "claim",
    "fact",
    "value",
    "raw",
    "body",
    "content",
    "image_b64",
    "embedding",
}

_REDACT_TOKEN = "[REDACTED-PHI]"


def _redact_phi(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    if settings.debug_payloads:
        return event_dict
    return _walk(event_dict)


def _walk(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: (_REDACT_TOKEN if k in _REDACT_KEYS else _walk(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(v) for v in value]
    return value


def configure_logging() -> None:
    settings = get_settings()

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.INFO,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact_phi,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    if settings.debug_payloads:
        # Big loud warning so this is never silent.
        log = structlog.get_logger("ownchart.startup")
        log.warning(
            "phi_debug_payloads_enabled",
            note="Raw payload logging is on. PHI may appear in logs.",
        )


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)
