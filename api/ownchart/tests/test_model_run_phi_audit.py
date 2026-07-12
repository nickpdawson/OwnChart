"""Batch A item 4 — ModelRun PHI audit (deletion-completeness evidence).

``model_runs`` is append-only (every FK to it is ON DELETE SET NULL), so its
rows survive account deletion by design. That is only acceptable if the rows
carry NO patient text. These tests pin the audit finding structurally:

  - The table stores digests + metadata, never raw prompt/response TEXT.
  - ``prompt_artifact_path`` — the one pointer to on-disk prompt material —
    is defined but NEVER written by the single ModelRun construction site,
    so there are no prompt artifacts on disk tied to a purged record to GC.

If a future change adds a raw-text column or starts materializing prompt
artifacts, one of these assertions fails and the erasure story must be
revisited (GC the artifacts alongside the blob sweep).
"""

from __future__ import annotations

from pathlib import Path

from ownchart.models.model_run import ModelRun


def test_model_run_has_no_raw_text_columns():
    cols = set(ModelRun.__table__.columns.keys())
    # Digests + metadata are the allowed shape.
    assert {"input_hash", "output_hash"} <= cols
    # No column that could hold raw prompt/response PHI text.
    forbidden = {
        "input_text", "output_text", "prompt_text", "response_text",
        "raw_prompt", "raw_response", "prompt", "response",
        "messages", "system", "rendered_prompt", "input", "output",
    }
    leaked = forbidden & cols
    assert not leaked, f"ModelRun grew a raw-text column: {leaked}"


def test_prompt_artifact_path_defaults_none():
    run = ModelRun(
        provider="anthropic",
        model="claude-opus-4-7",
        purpose="extract",
        prompt_version="p@1",
        consent_state=True,
    )
    assert run.prompt_artifact_path is None


def test_prompt_artifact_path_is_never_written():
    """The audit's core finding: the only place a ModelRun is constructed
    (llm/anthropic_client.py) never sets prompt_artifact_path, so no prompt
    artifacts are materialized on disk. Guards against a silent regression
    that would start writing PHI-bearing artifacts that survive erasure."""
    src = Path(__file__).resolve().parents[1] / "llm" / "anthropic_client.py"
    text = src.read_text()
    assert "ModelRun(" in text, "expected the ModelRun construction site here"
    assert "prompt_artifact_path" not in text, (
        "anthropic_client.py now writes prompt_artifact_path — on-disk prompt "
        "artifacts would survive account deletion; GC them in "
        "core/account_deletion.py alongside the evidence blobs."
    )
