"""Loader + renderer for prompts/suggested_questions.v1.yaml.

home_ai.py used to build suggested questions inline as Python f-strings.
That worked but it meant changing the phrasing required a code deploy.
This module reads the YAML once at import time and renders entries
against runtime context (most recent major event, top topic, current
date) into the API response shape.

Gates determine whether an entry appears at all. Substitution is
flat: every {placeholder} in `visible_text`, `submitted_text`, and
the leaves of `scope_template` is resolved against the same context
dict the caller passes in.
"""

from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


def _substitute(value: Any, ctx: dict[str, Any]) -> Any:
    if isinstance(value, str):
        try:
            return value.format(**ctx)
        except (KeyError, IndexError):
            # Missing key — render the literal template so the UI can
            # show what's broken rather than blowing up.
            return value
    if isinstance(value, dict):
        return {k: _substitute(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, ctx) for v in value]
    return value


@lru_cache(maxsize=1)
def _load_yaml() -> dict[str, Any]:
    here = Path(__file__).resolve().parent.parent / "prompts" / "suggested_questions.v1.yaml"
    with here.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def render_suggested_questions(
    *,
    has_major_event: bool,
    major_event_ctx: dict[str, Any] | None,
    has_top_topic: bool,
    topic_ctx: dict[str, Any] | None,
    now: datetime,
) -> list[dict[str, Any]]:
    """Render the YAML suggestions against runtime context.

    `major_event_ctx` and `topic_ctx` should be ready-to-substitute
    dicts (e.g. {"event_label": ..., "iso_date": ..., "fact_id": ...,
    "fact_type": ..., "phrase": ..., "short_date": ...}).
    """
    cfg = _load_yaml()
    base_ctx: dict[str, Any] = {
        "iso_now": now.isoformat(),
        "iso_minus_30d": (now.replace(microsecond=0).isoformat()),
    }
    # Compute iso_minus_30d properly:
    from datetime import timedelta
    base_ctx["iso_minus_30d"] = (now - timedelta(days=30)).isoformat()
    base_ctx["iso_now"] = now.isoformat()

    out: list[dict[str, Any]] = []
    for entry in cfg.get("questions", []):
        gate = entry.get("gate", "always")
        if gate == "major_event":
            if not has_major_event or not major_event_ctx:
                continue
            ctx = {**base_ctx, **major_event_ctx}
        elif gate == "top_topic":
            if not has_top_topic or not topic_ctx:
                continue
            ctx = {**base_ctx, **topic_ctx}
        else:
            ctx = base_ctx

        visible = _substitute(entry["visible_text"], ctx)
        submitted = _substitute(entry["submitted_text"], ctx)
        scope_template = _substitute(entry.get("scope_template"), ctx)
        # scope_template may still contain "{...}" if a gate-less
        # entry references a placeholder we don't have. Drop string
        # leaves that still look like templates.
        if isinstance(scope_template, dict):
            scope_template = _strip_unresolved(scope_template)
        out.append({
            "visible_text": visible.strip(),
            "submitted_text": submitted.strip(),
            "scope_hint": entry.get("scope_hint", "whole_record"),
            "scope": scope_template,
        })
    return out


def _strip_unresolved(d: dict[str, Any]) -> dict[str, Any]:
    """Recursively drop dict leaves whose values still contain
    unresolved {placeholder} strings. Keeps the rest."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, dict):
            v2 = _strip_unresolved(v)
            if v2:
                out[k] = v2
        elif isinstance(v, list):
            out[k] = v
        elif isinstance(v, str):
            if "{" in v and "}" in v:
                continue  # unresolved placeholder, drop
            out[k] = v
        else:
            out[k] = v
    return out
