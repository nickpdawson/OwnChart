"""FU-CAL-CONVERSATIONS-INTEGRATION + FU-ASK-RECENT-WEARABLE tests
(2026-05-22).

Two related fixes pinned here:

  1. The Chat-path (``llm.conversations.add_user_message_and_reply``)
     pulls calendar life-context the same way ``/api/ask`` does,
     uses the same privacy projector (no override), and emits a
     count-only ``conversations_retrieval_shape`` telemetry event.

  2. ``retrieval.topics._FACT_TYPE_SYNONYMS["observation"]`` now
     includes wearable vocabulary (sleep / HRV / resting heart rate
     / workout / training / steps / activity / energy) so wearable
     questions trigger the category-aware retrieval pass.

  3. ``general_ask.v2.yaml`` exists, is the latest version, and
     contains the same anti-self-labeled-honesty tone rule as
     ``ask_query.v2.yaml`` while preserving every medical safety
     rule from v1.
"""

from __future__ import annotations

import inspect
import re

import pytest


# ---------------------------------------------------------------------------
# 1. Conversations path wires calendar context


def test_conversations_imports_calendar_life_context():
    """add_user_message_and_reply must import the calendar helpers
    that build the projected life-context block — same code path
    /api/ask uses."""
    from ownchart.llm import conversations as mod
    src = inspect.getsource(mod)
    assert "from ..retrieval.calendar_life_context import" in src
    assert "fetch_calendar_life_context" in src
    assert "format_calendar_context_block" in src


def test_conversations_fetches_calendar_with_record_scope():
    """The fetch call site MUST pass conv_record_id (the
    person_record_id inherited from the Conversation row), not the
    user_id or a default. Static-source check on the call args."""
    from ownchart.llm.conversations import add_user_message_and_reply
    src = inspect.getsource(add_user_message_and_reply)
    # The fetch must run, and it must use the inherited record id.
    assert "fetch_calendar_life_context(" in src
    after = src.split("fetch_calendar_life_context(", 1)[1]
    block = after.split(")", 1)[0]
    assert "person_record_id=conv_record_id" in block, (
        "add_user_message_and_reply must pass person_record_id="
        "conv_record_id to fetch_calendar_life_context; the inherited "
        "record id is the load-bearing scope for the chat path."
    )


def test_conversations_does_not_override_source_consent():
    """No source_consent override on the call site. Consent must
    remain derived from the per-source llm_full_details_consent
    column inside fetch_calendar_life_context."""
    from ownchart.llm.conversations import add_user_message_and_reply
    src = inspect.getsource(add_user_message_and_reply)
    after = src.split("fetch_calendar_life_context(", 1)[1]
    block = after.split(")", 1)[0]
    assert "source_consent" not in block, (
        "conversations path must not pass source_consent — consent "
        "is per-source, controlled by llm_full_details_consent."
    )


def test_conversations_appends_calendar_block_to_evidence():
    """The calendar block is concatenated onto the fact-evidence
    block before the LLM call, so a single ``evidence_block``
    template var carries both. Pin the structural invariant."""
    from ownchart.llm.conversations import add_user_message_and_reply
    src = inspect.getsource(add_user_message_and_reply)
    assert "calendar_block = format_calendar_context_block(calendar_items)" in src
    # Pin the concatenation form so a future refactor can't drop
    # the calendar block silently.
    assert "evidence_block = fact_block + calendar_block" in src
    # And the LLM user_vars must reference the combined block.
    assert '"evidence_block": evidence_block' in src


def test_conversations_skips_calendar_fetch_when_no_record():
    """Legacy in-process callers (workers, tests) may invoke
    add_user_message_and_reply with conv.person_record_id=None.
    The calendar fetch must be guarded — it's a no-op there, not
    a None-passing crash."""
    from ownchart.llm.conversations import add_user_message_and_reply
    src = inspect.getsource(add_user_message_and_reply)
    # The fetch is wrapped in a `if conv_record_id is not None:` guard.
    assert "if conv_record_id is not None:" in src
    # And the variable is initialized to an empty list so the
    # downstream format/log doesn't crash.
    assert "calendar_items: list[dict[str, Any]] = []" in src


# ---------------------------------------------------------------------------
# 2. Count-only telemetry on the conversations path


def test_conversations_emits_retrieval_shape_log():
    """The chat path emits a count-only diagnostic event so an
    operator can see whether calendar context reached the prompt
    without reading the prompt body."""
    from ownchart.llm.conversations import add_user_message_and_reply
    src = inspect.getsource(add_user_message_and_reply)
    assert '"conversations_retrieval_shape"' in src
    for field in (
        "fact_count=",
        "fact_type_counts=",
        "extraction_method_counts=",
        "calendar_item_count=",
        "fact_block_chars=",
        "calendar_block_chars=",
        "context_block_chars=",
        "calendar_block_present=",
    ):
        assert field in src, (
            f"conversations_retrieval_shape log missing {field}"
        )


def _slice_log_block(src: str) -> str:
    """Return the parenthesized argument block of the log.info(
    "conversations_retrieval_shape", ...) call, balanced parens."""
    marker = '"conversations_retrieval_shape"'
    idx = src.find(marker)
    assert idx > 0
    open_idx = src.rfind("log.info(", 0, idx)
    assert open_idx > 0
    depth = 0
    end = open_idx
    for i, ch in enumerate(src[open_idx:], start=open_idx):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
    return src[open_idx:end]


def test_conversations_retrieval_shape_log_no_phi():
    """The conversations_retrieval_shape log emission must not
    reference question text, answer text, prompt body, titles,
    labels, descriptions, or any per-row identifier other than the
    record-scope id."""
    from ownchart.llm.conversations import add_user_message_and_reply
    src = inspect.getsource(add_user_message_and_reply)
    log_block = _slice_log_block(src)
    for term in (
        "content",  # the user message
        "answer",
        "result.tool_input",
        "context_block=context_block",  # would echo the prompt body
        "evidence_block=evidence_block",
        "prompt",
        "title",
        "label",
        ".description",
        "f.id",
        "fact.id",
        "external_id",
    ):
        assert term not in log_block, (
            f"conversations_retrieval_shape log contains forbidden "
            f"PHI/prompt-body term {term!r}"
        )


# ---------------------------------------------------------------------------
# 3. Wearable retrieval — synonym table expansion


def test_observation_synonyms_include_wearable_vocabulary():
    """Wearable vocabulary must trigger the observation
    category-aware pass. The cheap fix for the question 'compare
    my sleep, HRV, resting HR, training last week' that was
    returning zero wearable rows on /api/conversations."""
    from ownchart.retrieval.topics import _FACT_TYPE_SYNONYMS
    obs = _FACT_TYPE_SYNONYMS["observation"]
    required = {
        "sleep", "hrv", "heart rate", "resting heart rate",
        "workout", "workouts", "training", "exercise",
        "steps", "activity",
    }
    missing = required - set(obs)
    assert not missing, (
        f"observation synonyms missing required wearable terms: "
        f"{sorted(missing)}"
    )


def test_existing_clinical_observation_synonyms_preserved():
    """The wearable expansion must NOT remove the existing
    clinical terms ('observation', 'observations', 'vital',
    'vitals'). Adding-only, never removing."""
    from ownchart.retrieval.topics import _FACT_TYPE_SYNONYMS
    obs = set(_FACT_TYPE_SYNONYMS["observation"])
    assert {"observation", "observations", "vital", "vitals"} <= obs


def test_detect_category_resolves_wearable_tokens_to_observation():
    """The token-level resolver returns {'observation'} for each
    wearable vocabulary token in isolation."""
    from ownchart.retrieval.topics import _detect_category_fact_types
    for word in (
        "sleep", "hrv", "workout", "training",
        "steps", "activity", "exercise",
    ):
        out = _detect_category_fact_types([word], word)
        assert "observation" in out, (
            f"wearable token {word!r} should resolve to observation "
            f"fact_type via the single-token branch; got {out}"
        )


def test_detect_category_resolves_multiword_wearable_phrases():
    """Multi-word phrases like 'heart rate' / 'resting heart rate'
    must resolve via the phrase-match branch (single tokens won't
    match because the resolver only checks multi-word entries
    against the raw lowercased query)."""
    from ownchart.retrieval.topics import _detect_category_fact_types
    out_hr = _detect_category_fact_types(
        ["heart", "rate"], "what is my heart rate trend"
    )
    assert "observation" in out_hr
    out_rhr = _detect_category_fact_types(
        ["resting", "heart", "rate"],
        "show me my resting heart rate last month",
    )
    assert "observation" in out_rhr


def test_clinical_categories_still_resolve():
    """Regression — wearable expansion must not break existing
    clinical category resolution."""
    from ownchart.retrieval.topics import _detect_category_fact_types
    cases = [
        (["medications"], "what medications am i on", "medication"),
        (["surgeries"], "list my surgeries", "procedure"),
        (["allergies"], "what am i allergic to", "allergy"),
        (["labs"], "show me my labs", "lab_result"),
        (["vitals"], "what are my vitals", "observation"),
    ]
    for tokens, raw, expected in cases:
        out = _detect_category_fact_types(tokens, raw)
        assert expected in out, (
            f"clinical token {tokens!r} should resolve to {expected!r}; "
            f"got {out}"
        )


def test_wearable_synonym_terms_do_not_collide_with_clinical_categories():
    """The wearable terms added to observation must not appear as
    keys/values under a DIFFERENT fact_type — collision would
    randomly assign category depending on iteration order."""
    from ownchart.retrieval.topics import _FACT_TYPE_SYNONYMS
    wearable_terms = {
        "sleep", "hrv", "heart rate", "resting heart rate",
        "workout", "training", "exercise", "steps", "activity",
    }
    for fact_type, words in _FACT_TYPE_SYNONYMS.items():
        if fact_type == "observation":
            continue
        for w in words:
            assert w not in wearable_terms, (
                f"wearable term {w!r} also listed under {fact_type!r}; "
                "collision would route the same query to two fact types"
            )


# ---------------------------------------------------------------------------
# 4. general_ask v2 prompt — tone rule + calendar acknowledgement


def test_general_ask_v2_exists_and_is_latest():
    """The conversations path uses get_registry().get('general_ask')
    — the bare id resolves to the latest version. v2 ships with the
    tone fix; v1 stays callable by id@1 for ModelRun audit."""
    from ownchart.llm import get_registry
    get_registry.cache_clear()
    reg = get_registry()
    v1 = reg.get("general_ask@1")
    v2 = reg.get("general_ask@2")
    latest = reg.get("general_ask")
    assert v1.version == 1
    assert v2.version == 2
    assert latest.version == 2


def test_general_ask_v2_forbids_self_labeled_honesty():
    """The v2 prompt explicitly forbids 'Honest answer',
    'Honestly', etc. Same anti-self-labeling rule as ask_query.v2."""
    from ownchart.llm import get_registry
    get_registry.cache_clear()
    p = get_registry().get("general_ask@2")
    system = p.system.lower()
    assert "do not preface" in system or "forbidden openings" in system
    for phrase in (
        "honest answer up front",
        "honestly",
        "to be honest",
        "frankly",
    ):
        assert phrase.lower() in system, (
            f"general_ask v2 missing forbidden phrase example {phrase!r}"
        )


def test_general_ask_v2_acknowledges_calendar_context():
    """v2 must tell the model that a '## Calendar context' block
    may appear in the evidence section — otherwise the model
    might keep dismissing calendar entries as un-citable."""
    from ownchart.llm import get_registry
    get_registry.cache_clear()
    p = get_registry().get("general_ask@2")
    text = p.system + " " + p.user_template
    assert "Calendar context" in text or "calendar context" in text.lower(), (
        "general_ask v2 must reference the Calendar context block "
        "so the model knows how to consume it"
    )
    # And the model must know calendar entries cite via event/source.
    assert "citation_type=event" in p.system or "citation_type=event" in p.user_template, (
        "general_ask v2 must instruct the model to cite calendar "
        "entries via citation_type=event"
    )


def test_general_ask_v2_preserves_medical_safety_rules():
    """Tone-only change — medical safety rules MUST NOT be
    weakened by the v2 edit."""
    from ownchart.llm import get_registry
    get_registry.cache_clear()
    p = get_registry().get("general_ask@2")
    system = p.system
    assert "treatment instructions" in system
    assert "dosing changes" in system
    assert "self-harm" in system
    assert "safety_response" in system
    # Source Authority Doctrine preserved.
    assert "primary_event" in system
    assert "self_reported_history" in system
    # Medication chronology rules preserved.
    assert "Earliest tracker log" in system
    assert "originating prescription" in system


def test_general_ask_v2_tool_schema_unchanged():
    """v2 must keep the same tool name + required-field shape as
    v1 so the route layer's downstream parsing is stable."""
    from ownchart.llm import get_registry
    get_registry.cache_clear()
    v1 = get_registry().get("general_ask@1")
    v2 = get_registry().get("general_ask@2")
    # Same tool name.
    v1_tool = v1.tools[0]
    v2_tool = v2.tools[0]
    assert v1_tool["name"] == v2_tool["name"] == "emit_answer"
    # Same required fields.
    v1_req = set(v1_tool["input_schema"].get("required", []))
    v2_req = set(v2_tool["input_schema"].get("required", []))
    assert v1_req == v2_req


# ---------------------------------------------------------------------------
# 5. Doctrine pin — wearable expansion does not leak into the
# observation bucket from other category branches.


def test_wearable_terms_only_in_observation_bucket():
    """Defensive: every wearable term I added must be uniquely in
    the observation bucket. A future refactor that adds the same
    word to e.g. 'symptom' would route a wearable question to two
    fact types and break DISTINCT-ON-label deduplication."""
    from ownchart.retrieval.topics import _FACT_TYPE_SYNONYMS
    additions = {
        "sleep", "sleeping", "slept",
        "hrv", "heart rate variability",
        "heart rate", "resting heart rate", "resting hr",
        "rhr", "pulse",
        "workout", "workouts", "training", "trained", "exercise",
        "exercises", "exercising",
        "steps", "step count",
        "activity", "activities",
        "calories", "energy",
    }
    for ft, words in _FACT_TYPE_SYNONYMS.items():
        if ft == "observation":
            continue
        for w in words:
            assert w not in additions, (
                f"wearable term {w!r} found under {ft!r} bucket — "
                "remove the duplicate so retrieval routing stays "
                "deterministic"
            )
