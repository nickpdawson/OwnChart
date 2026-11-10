"""Episode Intelligence composes the contract and validates citations."""

from __future__ import annotations

import uuid

from ownchart.llm import episode_intelligence as ei
from ownchart.llm.prompts import SHARED_CONTRACT_TOKEN, get_registry


def test_ei_v2_is_live_with_contract_and_machinery_rule():
    p = get_registry().get("episode_intelligence")
    assert p.version >= 2
    assert SHARED_CONTRACT_TOKEN not in p.system
    assert "Shared behavior contract" in p.system
    assert "Never mention \"the planner\"" in p.system
    assert "<<<EVIDENCE" in p.user_template
    # v1 rules preserved.
    v1 = get_registry().get("episode_intelligence@1")
    missing = [ln for ln in v1.system.splitlines() if ln.strip() and ln not in p.system]
    assert not missing, missing[:3]


def test_uuid_strings_in_walks_nested_payload():
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    payload = {
        "facts": [{"fact_id": str(a), "label": "x"}, {"id": str(b)}],
        "sources": {"items": [[{"source_id": str(c)}]]},
        "noise": ["not-a-uuid", 42, None, {"k": str(a)}],
    }
    assert ei.uuid_strings_in(payload) == {a, b, c}
    assert ei.uuid_strings_in(None) == set()
