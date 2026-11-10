"""Citations may only name evidence the model was shown (audit item 7)."""

from __future__ import annotations

import uuid

from ownchart.llm.citations import filter_citations, filter_fact_ids, filter_statements


def test_fact_and_source_citations_outside_evidence_are_dropped_and_counted():
    f_ok, f_other, s_ok = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    cits = [
        {"citation_type": "fact", "subject_id": str(f_ok), "claim_label": "source_backed"},
        {"citation_type": "fact", "subject_id": str(f_other)},     # another record
        {"citation_type": "fact", "subject_id": "not-a-uuid"},      # hallucinated
        {"citation_type": "source", "subject_id": str(s_ok)},
        {"citation_type": "source", "subject_id": str(uuid.uuid4())},
        {"citation_type": "fact", "subject_id": str(f_ok)},         # duplicate
    ]
    kept, audit = filter_citations(cits, fact_ids={f_ok}, source_ids={s_ok})
    assert [(c["citation_type"], c["subject_id"]) for c in kept] == [
        ("fact", str(f_ok)), ("source", str(s_ok)),
    ]
    assert audit.accepted == 2
    assert audit.dropped == 3
    assert audit.reasons["not_in_evidence:fact"] == 1
    assert audit.reasons["not_in_evidence:source"] == 1
    assert audit.reasons["malformed"] == 1


def test_unverifiable_types_no_longer_pass_through():
    kept, audit = filter_citations(
        [{"citation_type": "episode", "subject_id": str(uuid.uuid4())},
         {"citation_type": "event", "subject_id": str(uuid.uuid4())}],
        fact_ids=set(), source_ids=set(),
    )
    assert kept == []
    assert audit.reasons == {"unverifiable_type:episode": 1, "unverifiable_type:event": 1}


def test_anchor_citations_need_the_shown_anchor_set():
    a = uuid.uuid4()
    cit = [{"citation_type": "anchor", "subject_id": str(a)}]
    kept, _ = filter_citations(cit, fact_ids=set(), source_ids=set())
    assert kept == []
    kept, _ = filter_citations(cit, fact_ids=set(), source_ids=set(), anchor_ids={a})
    assert len(kept) == 1


def test_filter_fact_ids_keeps_only_supplied_and_dedupes():
    a, b = uuid.uuid4(), uuid.uuid4()
    kept, dropped = filter_fact_ids([str(a), str(b), str(a), "junk", 42], {a})
    assert kept == [str(a)]
    assert dropped == 3


def test_filter_statements_flags_unsupported_instead_of_dropping():
    a = uuid.uuid4()
    out, dropped = filter_statements(
        [{"statement": "ok", "fact_ids": [str(a), str(uuid.uuid4())]},
         {"statement": "nothing real", "fact_ids": [str(uuid.uuid4())]},
         "garbage"],
        {a},
    )
    assert out[0]["fact_ids"] == [str(a)] and "unsupported" not in out[0]
    assert out[1]["fact_ids"] == [] and out[1]["unsupported"] is True
    assert dropped == 2
    assert len(out) == 2
