"""Batch B — per-model LLM pricing → estimated USD cost.

Pure functions in ``llm/pricing.py``. No DB, no network. Pins:

  - a known model computes cost across the four token buckets;
  - an unknown model returns 0.0 and never raises (a run must not die
    over a missing price);
  - non-dict / empty usage is tolerated;
  - a config override merges over (and can add to) the built-in table.
"""

from __future__ import annotations

from types import SimpleNamespace

from ownchart.llm import pricing
from ownchart.llm.pricing import estimate_cost_usd, price_for


def test_price_for_known_and_unknown():
    assert price_for("claude-haiku-4-5") == (0.80, 4.00)
    assert price_for("claude-opus-4-7") == (15.00, 75.00)
    assert price_for("gpt-nope-9") is None
    assert price_for(None) is None


def test_estimate_cost_splits_the_four_buckets():
    # haiku: $0.80/MTok in, $4.00/MTok out.
    usage = {
        "input_tokens": 1_000_000,          # 0.80
        "output_tokens": 1_000_000,         # 4.00
        "cache_read_input_tokens": 1_000_000,    # 0.80 * 0.10 = 0.08
        "cache_creation_input_tokens": 1_000_000,  # 0.80 * 1.25 = 1.00
    }
    cost = estimate_cost_usd("claude-haiku-4-5", usage)
    assert cost == round(0.80 + 4.00 + 0.08 + 1.00, 6)


def test_estimate_cost_partial_usage():
    # Only output tokens present; missing buckets treated as 0.
    cost = estimate_cost_usd("claude-sonnet-4-6", {"output_tokens": 500_000})
    assert cost == round((500_000 * 15.00) / 1_000_000.0, 6)


def test_unknown_model_is_zero_not_crash():
    assert estimate_cost_usd("no-such-model", {"input_tokens": 9_000_000}) == 0.0


def test_non_dict_usage_is_zero():
    assert estimate_cost_usd("claude-opus-4-7", None) == 0.0
    assert estimate_cost_usd("claude-opus-4-7", "not-a-dict") == 0.0  # type: ignore[arg-type]


def test_config_override_merges_over_builtin(monkeypatch):
    pricing._pricing_table.cache_clear()
    override = '{"future-model-x": [2.0, 6.0], "claude-opus-4-7": [10.0, 50.0]}'
    monkeypatch.setattr(
        pricing, "get_settings",
        lambda: SimpleNamespace(llm_pricing_overrides_json=override),
    )
    try:
        # New model becomes priced.
        assert price_for("future-model-x") == (2.0, 6.0)
        assert estimate_cost_usd("future-model-x", {"input_tokens": 1_000_000}) == 2.0
        # Existing model price is replaced by the override.
        assert price_for("claude-opus-4-7") == (10.0, 50.0)
    finally:
        pricing._pricing_table.cache_clear()


def test_bad_override_json_is_ignored(monkeypatch):
    pricing._pricing_table.cache_clear()
    monkeypatch.setattr(
        pricing, "get_settings",
        lambda: SimpleNamespace(llm_pricing_overrides_json="{not valid json"),
    )
    try:
        # Falls back to the built-in table rather than failing closed.
        assert price_for("claude-opus-4-7") == (15.00, 75.00)
    finally:
        pricing._pricing_table.cache_clear()
