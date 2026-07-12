"""Per-model LLM pricing → estimated USD cost for a ModelRun.

One place that knows what a token costs. Used at two sites:

  1. ``llm/anthropic_client.py`` stamps ``estimated_cost_usd`` on every
     ModelRun from the provider-reported token usage.
  2. ``routes/llm_providers.py`` (the ``/usage`` endpoint) reads the
     stored column, and falls back to recomputing for legacy rows that
     predate the column.

Prices are Anthropic public per-token pricing for the claude-opus /
claude-sonnet / claude-haiku families (USD per 1M tokens, input/output).
Cache reads bill at 10% of input; cache writes (ephemeral 5-min) at 125%
of input — Anthropic's cache curve.

Unknown model → cost 0.0 + a logged warning. A missing price must never
crash an LLM run; we would rather under-count spend than drop the call.

Overridable via config: set ``OWNCHART_LLM_PRICING_OVERRIDES_JSON`` to a
JSON object ``{"model-id": [input_per_mtok, output_per_mtok]}``. Entries
merge over (and replace) the built-in table so an operator can price a
model we haven't shipped, or correct a stale number, without a code
change.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from ..core.config import get_settings
from ..core.logger import get_logger

log = get_logger("ownchart.llm.pricing")


# (input $/MTok, output $/MTok). Anthropic public pricing 2026-01.
_BUILTIN_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-4-7":              (15.00, 75.00),
    "claude-opus-4-7[1m]":          (15.00, 75.00),
    "claude-opus-4-6":              (15.00, 75.00),
    "claude-sonnet-4-6":            (3.00,  15.00),
    "claude-sonnet-4-5":            (3.00,  15.00),
    "claude-haiku-4-5":             (0.80,  4.00),
    "claude-haiku-4-5-20251001":    (0.80,  4.00),
}

# Ephemeral-cache modifiers on the INPUT price.
_CACHE_READ_MULTIPLIER = 0.10
_CACHE_CREATE_MULTIPLIER = 1.25


@lru_cache(maxsize=1)
def _pricing_table() -> dict[str, tuple[float, float]]:
    """Built-in table merged with any config override.

    Cached; the override is read once at first use. Bad JSON is logged
    and ignored (we keep the built-in table rather than fail closed on
    every run).
    """
    table = dict(_BUILTIN_USD_PER_MTOK)
    raw = getattr(get_settings(), "llm_pricing_overrides_json", None)
    if not raw:
        return table
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError) as e:
        log.warning("llm_pricing_override_parse_failed", error=str(e))
        return table
    if not isinstance(parsed, dict):
        log.warning("llm_pricing_override_not_object", got=type(parsed).__name__)
        return table
    for model, pair in parsed.items():
        try:
            in_per_m, out_per_m = pair
            table[str(model)] = (float(in_per_m), float(out_per_m))
        except (TypeError, ValueError):
            log.warning("llm_pricing_override_bad_entry", model=model, value=pair)
            continue
    return table


def price_for(model: str | None) -> tuple[float, float] | None:
    """Return ``(input_per_mtok, output_per_mtok)`` for a model, or None.

    None means "we don't have a price" — the UI renders "—" and the run
    is counted as unknown-cost rather than $0.00 of real spend.
    """
    if not model:
        return None
    return _pricing_table().get(model)


def estimate_cost_usd(model: str | None, usage: dict[str, Any] | None) -> float:
    """Estimated USD cost for one run's token usage.

    Never raises. Unknown model or non-dict usage → 0.0 (with a logged
    warning on unknown model, so a systematically-mispriced model shows
    up in logs rather than silently under-billing forever).

    ``input_tokens`` from Anthropic is the NON-cached input count (it
    already excludes ``cache_read_input_tokens``), so the four buckets
    are billed separately without double counting.
    """
    if not isinstance(usage, dict):
        return 0.0
    prices = price_for(model)
    if prices is None:
        log.warning("llm_pricing_unknown_model", model=model)
        return 0.0
    in_per_m, out_per_m = prices
    inp = int(usage.get("input_tokens") or 0)
    outp = int(usage.get("output_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_create = int(usage.get("cache_creation_input_tokens") or 0)
    cost = (
        (inp * in_per_m) / 1_000_000.0
        + (outp * out_per_m) / 1_000_000.0
        + (cache_read * in_per_m * _CACHE_READ_MULTIPLIER) / 1_000_000.0
        + (cache_create * in_per_m * _CACHE_CREATE_MULTIPLIER) / 1_000_000.0
    )
    return round(cost, 6)
