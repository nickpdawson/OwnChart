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
    user_id or a default. Static-source check — the record id can
    be passed directly or via a kwargs dict; either way the
    literal pair must appear in the function source."""
    from ownchart.llm.conversations import add_user_message_and_reply
    src = inspect.getsource(add_user_message_and_reply)
    assert "fetch_calendar_life_context(" in src
    # Accept either direct kwarg or kwargs-dict form (the FU-TEMPORAL
    # plumbing builds a kwargs dict to conditionally add time_min/max).
    assert (
        "person_record_id=conv_record_id" in src
        or '"person_record_id": conv_record_id' in src
    ), (
        "add_user_message_and_reply must scope the calendar fetch "
        "to the inherited person_record_id."
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


def test_general_ask_v2_exists_for_audit():
    """v2 must still be callable by id@2 for historical ModelRun
    audit even after v3 supersedes it. The 'latest is v2' check
    was rolled forward to test_general_ask_v3_is_latest_version."""
    from ownchart.llm import get_registry
    get_registry.cache_clear()
    v2 = get_registry().get("general_ask@2")
    assert v2.version == 2


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


# ---------------------------------------------------------------------------
# 6. Temporal window plumbing (FU-TEMPORAL-WINDOW 2026-05-22)


def test_conversations_imports_temporal_parser():
    from ownchart.llm import conversations as mod
    src = inspect.getsource(mod)
    assert "from ..retrieval.temporal import parse_temporal_window" in src


def test_conversations_calls_parse_temporal_window_on_question():
    """The chat path must parse the question for a temporal phrase
    BEFORE calling fetch_calendar_life_context; without this, a
    'last week' question gets future events from the default
    forward window."""
    from ownchart.llm.conversations import add_user_message_and_reply
    src = inspect.getsource(add_user_message_and_reply)
    assert "temporal = parse_temporal_window(content)" in src
    # And the parsed window is passed through to the calendar fetch.
    assert 'kwargs["time_min"] = temporal.time_min' in src
    assert 'kwargs["time_max"] = temporal.time_max' in src


def test_fetch_calendar_life_context_accepts_time_window_override():
    """The fetch helper signature must accept time_min/time_max
    keyword args so callers (chat + ask) can override the default
    rolling window."""
    import inspect as _inspect
    from ownchart.retrieval.calendar_life_context import (
        fetch_calendar_life_context,
    )
    sig = _inspect.signature(fetch_calendar_life_context)
    assert "time_min" in sig.parameters
    assert "time_max" in sig.parameters
    # Defaults to None so existing call sites keep working.
    assert sig.parameters["time_min"].default is None
    assert sig.parameters["time_max"].default is None


# ---------------------------------------------------------------------------
# 7. Wearable summary wiring (FU-ASK-RECENT-WEARABLE-SUMMARY)


def test_conversations_imports_wearable_summary():
    from ownchart.llm import conversations as mod
    src = inspect.getsource(mod)
    assert "from ..retrieval.wearable_summary import (" in src
    assert "summarize_wearable_window" in src
    assert "format_wearable_summary_block" in src
    assert "question_is_wearable_pattern" in src


def test_conversations_triggers_wearable_summary_when_pattern_detected():
    """When the question is wearable-pattern, the chat path must
    call summarize_wearable_window with a record-scoped window.
    Default (no temporal phrase) is trailing 7 days."""
    from ownchart.llm.conversations import add_user_message_and_reply
    src = inspect.getsource(add_user_message_and_reply)
    assert "question_is_wearable_pattern(content)" in src
    assert "summarize_wearable_window(" in src
    # Per-record scope.
    after = src.split("summarize_wearable_window(", 1)[1]
    block = after.split(")", 1)[0]
    assert "person_record_id=conv_record_id" in block


def test_conversations_appends_wearable_block_to_evidence():
    """The wearable summary block lands inside the evidence_block
    string the LLM sees — alongside fact_block and calendar_block."""
    from ownchart.llm.conversations import add_user_message_and_reply
    src = inspect.getsource(add_user_message_and_reply)
    assert "wearable_block = format_wearable_summary_block(" in src
    assert "evidence_block = fact_block + calendar_block + wearable_block" in src


def test_conversations_log_emits_wearable_counts():
    """Telemetry must include wearable counts so an operator can
    see whether the summary pass ran without reading the prompt."""
    from ownchart.llm.conversations import add_user_message_and_reply
    src = inspect.getsource(add_user_message_and_reply)
    for field in (
        "wearable_block_chars=",
        "wearable_summary_rows=",
        "wearable_telemetry=",
        "wearable_block_present=",
        "temporal_phrase=",
        "temporal_semantics=",
    ):
        assert field in src, (
            f"conversations_retrieval_shape log missing {field}"
        )


def test_conversations_wearable_telemetry_log_no_phi():
    """wearable_telemetry dict contains row counts per metric.
    Verify no field references row values, labels, or identifiers
    in the log emission."""
    from ownchart.llm.conversations import add_user_message_and_reply
    src = inspect.getsource(add_user_message_and_reply)
    # Find the conversations_retrieval_shape log block.
    marker = '"conversations_retrieval_shape"'
    idx = src.find(marker)
    open_idx = src.rfind("log.info(", 0, idx)
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
    log_block = src[open_idx:end]
    for term in (
        "wearable_block=wearable_block",  # would echo the block text
        "wearable_summaries=wearable_summaries",  # would echo the list
        "rows=rows",
        "label",
        "value",
        ".description",
    ):
        assert term not in log_block, (
            f"conversations_retrieval_shape log contains forbidden "
            f"term {term!r} that could leak PHI/values"
        )


# ---------------------------------------------------------------------------
# 8. general_ask v3 — hardened tone ban
#
# Bug 1 PM-filed regression: v2 banned "Honest answer up front" /
# "Honestly" but the model still leaked "So the honest read…".
# v3 bans the bare word "honest" as a self-label entirely and
# enumerates a broader phrase list.


def test_general_ask_v3_exists_for_audit():
    """v3 must still be callable by id@3 for historical ModelRun
    audit after v4 supersedes it. The 'latest is v3' check
    was rolled forward to test_general_ask_v4_is_latest_version."""
    from ownchart.llm import get_registry
    get_registry.cache_clear()
    assert get_registry().get("general_ask@3").version == 3


# All banned phrases — the bug PM caught ("honest read") plus the
# existing v2 set plus belt-and-suspenders additions for "honest
# take", "honest assessment", "I'll be honest", "if I'm honest",
# "in all honesty".
_BANNED_TONE_PHRASES = (
    "Honest answer up front",
    "Honestly",
    "To be honest",
    "Frankly",
    "The honest truth",
    "The honest answer",
    "The honest read",
    "Honest read",
    "Honest take",
    "Honest assessment",
    "I'll be honest",
    "If I'm honest",
    "In all honesty",
)


@pytest.mark.parametrize("phrase", _BANNED_TONE_PHRASES)
def test_general_ask_v3_enumerates_each_banned_phrase(phrase):
    """v3 must explicitly list every banned phrase so the model
    has a concrete pattern to avoid. This pins the regression
    PM filed when 'honest read' leaked under v2."""
    from ownchart.llm import get_registry
    get_registry.cache_clear()
    p = get_registry().get("general_ask@3")
    system = p.system.lower()
    assert phrase.lower() in system, (
        f"general_ask v3 missing banned phrase {phrase!r}; the "
        f"model needs concrete examples to avoid it"
    )


def test_general_ask_v3_has_positive_replacement_instruction():
    """v3 must tell the model HOW to express limitations plainly
    when it would have used a 'honest' framing. Per PM directive:
    'state limitations plainly.'"""
    from ownchart.llm import get_registry
    get_registry.cache_clear()
    p = get_registry().get("general_ask@3")
    text = p.system.lower()
    assert "state limitations plainly" in text, (
        "general_ask v3 must contain the positive replacement "
        "instruction 'state limitations plainly'"
    )
    # And concrete acceptable forms must be enumerated.
    assert "retrieved evidence does not include" in text


def test_general_ask_v3_does_not_use_be_honest_in_voice_rules():
    """v1 had 'Be honest when the evidence is missing or thin' in
    the Voice section; that line PRIMED the model to echo
    'honestly' back. v3 must replace it with 'State limitations
    plainly.'"""
    from ownchart.llm import get_registry
    get_registry.cache_clear()
    p = get_registry().get("general_ask@3")
    text = p.system
    # The old line is gone.
    assert "Be honest when the evidence" not in text
    # The new line is present.
    assert "State limitations plainly" in text


def test_general_ask_v3_preserves_medical_safety_unchanged():
    """Tone-only change — every medical safety / source authority
    / medication chronology rule must survive verbatim."""
    from ownchart.llm import get_registry
    get_registry.cache_clear()
    p = get_registry().get("general_ask@3")
    system = p.system
    assert "treatment instructions" in system
    assert "dosing changes" in system
    assert "self-harm" in system
    assert "safety_response" in system
    assert "primary_event" in system
    assert "self_reported_history" in system
    assert "Earliest tracker log" in system
    assert "originating prescription" in system


def test_active_general_ask_prompt_has_no_banned_phrases_in_voice_rules():
    """Sanity scan: the active prompt (latest version) must not
    use any banned phrase as instruction text — only enumerate
    them under the Tone rules section as forbidden examples.
    A future edit that accidentally USES 'honestly' in a Voice
    rule would trip this."""
    from ownchart.llm import get_registry
    get_registry.cache_clear()
    p = get_registry().get("general_ask")
    text = p.system
    # Find the Tone rules section — banned phrases are listed
    # there as examples and that's expected. Outside that section,
    # banned phrases must not appear.
    tone_start = text.find("Tone rules")
    assert tone_start > 0
    tone_end_marker = "Evidence contract"
    tone_end = text.find(tone_end_marker, tone_start)
    assert tone_end > tone_start
    voice_section_pre_tone = text[:tone_start]
    rest_after_tone = text[tone_end:]
    outside_tone = voice_section_pre_tone + rest_after_tone
    for phrase in ("honestly", "to be honest", "frankly"):
        # The pattern check is case-insensitive — these words
        # used as INSTRUCTION text outside the Tone rules section
        # would prime the model.
        assert phrase.lower() not in outside_tone.lower(), (
            f"banned phrase {phrase!r} appears as instruction text "
            f"outside the Tone rules section — would prime the model"
        )


# ---------------------------------------------------------------------------
# 9. general_ask v4 — structural ban on "honest" (PM-caught
# 2026-05-22 evening: v3 leaked "honest gap").


def test_general_ask_v4_is_latest_version():
    from ownchart.llm import get_registry
    get_registry.cache_clear()
    reg = get_registry()
    assert reg.get("general_ask@1").version == 1
    assert reg.get("general_ask@2").version == 2
    assert reg.get("general_ask@3").version == 3
    assert reg.get("general_ask@4").version == 4
    assert reg.get("general_ask").version == 4


def test_general_ask_v4_contains_absolute_structural_ban():
    """v4 replaces the v3 enumerated whack-a-mole list with a
    structural rule: NO inflection of 'honest' anywhere in the
    output, period. The phrase 'absolute ban' must appear so a
    future cleanup that softens this trips immediately."""
    from ownchart.llm import get_registry
    get_registry.cache_clear()
    p = get_registry().get("general_ask@4")
    system = p.system
    assert "ABSOLUTE BAN" in system, (
        "v4 must declare an ABSOLUTE BAN — the v3 enumerated list "
        "was insufficient (PM caught 'honest gap' leaking)"
    )
    # The rule must reference "any inflection" or equivalent
    # structural language.
    assert "any inflection" in system.lower() or (
        "any grammatical role" in system.lower()
    ), (
        "v4 must use structural language ('any inflection' / 'any "
        "grammatical role') so the model can't argue a synonym "
        "around the ban"
    )


def test_general_ask_v4_enumerates_honest_gap_explicitly():
    """The PM-caught leak phrase. Must appear in the forbidden
    enumeration so a similar phrase in a future regression flags
    immediately."""
    from ownchart.llm import get_registry
    get_registry.cache_clear()
    p = get_registry().get("general_ask@4")
    text = p.system.lower()
    assert "honest gap" in text, (
        "v4 must explicitly enumerate 'honest gap' — the literal "
        "phrase the model leaked under v3"
    )


def test_general_ask_v4_includes_self_check_instruction():
    """v4 instructs the model to scan its own output for 'honest'
    before emitting. This is the additional layer that the
    enumerated list alone didn't catch."""
    from ownchart.llm import get_registry
    get_registry.cache_clear()
    p = get_registry().get("general_ask@4")
    system = p.system.lower()
    assert "before emitting" in system or "scan it once" in system or (
        "scan your output" in system
    ), (
        "v4 must contain a self-check instruction so the model "
        "runs a final pass to remove any 'honest' leak"
    )


def test_general_ask_v4_has_acceptable_forms_enumerated():
    """The positive replacement: concrete acceptable forms for
    stating a limitation. Without these the model wouldn't know
    HOW to express the gap."""
    from ownchart.llm import get_registry
    get_registry.cache_clear()
    p = get_registry().get("general_ask@4")
    text = p.system
    assert "Acceptable forms" in text or "acceptable forms" in text
    # Specific concrete examples PM listed.
    assert "retrieved evidence does not include" in text
    assert "isn't in the retrieved evidence" in text


@pytest.mark.parametrize(
    "phrase",
    [
        "honest gap",          # the PM-caught regression
        "honest read",          # carried over from v3
        "honest take",
        "honest answer",
        "the honest truth",
        "in all honesty",
        "honestly",
        "to be honest",
        "frankly",
        "honest picture",       # v4 additions
        "honest reality",
        "honest summary",
    ],
)
def test_general_ask_v4_enumerates_each_phrase(phrase):
    """v4 must enumerate every phrase as a concrete example so
    the model has a list to avoid. Anchors against future
    leak variants."""
    from ownchart.llm import get_registry
    get_registry.cache_clear()
    p = get_registry().get("general_ask@4")
    text = p.system.lower()
    assert phrase.lower() in text, (
        f"v4 missing forbidden example phrase {phrase!r}"
    )


def test_general_ask_v4_allows_quotation_exception():
    """The narrow exception: model may use 'honest' when QUOTING
    the user's question text. This must be explicit so the model
    doesn't fail-closed and refuse to echo a user word."""
    from ownchart.llm import get_registry
    get_registry.cache_clear()
    p = get_registry().get("general_ask@4")
    text = p.system.lower()
    assert ("quoting" in text and "user" in text) or (
        "echoing the user" in text
    ), (
        "v4 must surface the quotation exception so the model "
        "doesn't refuse to echo a user word"
    )


def test_general_ask_v4_preserves_medical_safety_unchanged():
    """Tone-only change — every medical safety / source authority
    / medication chronology rule must survive verbatim."""
    from ownchart.llm import get_registry
    get_registry.cache_clear()
    p = get_registry().get("general_ask@4")
    system = p.system
    assert "treatment instructions" in system
    assert "dosing changes" in system
    assert "self-harm" in system
    assert "safety_response" in system
    assert "primary_event" in system
    assert "self_reported_history" in system
    assert "Earliest tracker log" in system
    assert "originating prescription" in system


def test_general_ask_v4_tool_schema_unchanged():
    """v4 must keep the same tool name + required-field shape as
    v1/v2/v3 so route-layer parsing is stable across versions."""
    from ownchart.llm import get_registry
    get_registry.cache_clear()
    reg = get_registry()
    versions = [reg.get(f"general_ask@{i}") for i in (1, 2, 3, 4)]
    tools = [v.tools[0] for v in versions]
    names = {t["name"] for t in tools}
    assert names == {"emit_answer"}
    required_sets = [
        frozenset(t["input_schema"].get("required", [])) for t in tools
    ]
    # All four versions must agree on required fields.
    assert all(r == required_sets[0] for r in required_sets)
