"""Batch B — ModelRun stamps acting user/record + computed cost.

Drives ``call_with_tool`` with a fake provider + scripted session (no
DB, no network). Pins:

  - a billed run stamps user_id, person_record_id, and an
    estimated_cost_usd derived from the provider's token usage;
  - the ceiling is enforced on the billed path (choke point);
  - the free local_echo path is $0 and is never budget-checked;
  - an unknown model → cost 0.0 with no crash.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from ownchart.llm import anthropic_client as ac
from ownchart.llm.providers.base import LlmResponse


def _prompt(model: str = "claude-haiku-4-5"):
    return SimpleNamespace(
        model=model,
        system="you are a test",
        tools=[],
        version_tag="test_prompt@1",
        render=lambda **kw: "rendered",
    )


def _user():
    return SimpleNamespace(id=uuid.uuid4(), phi_consent_granted=True)


class _NoCredResult:
    def scalar_one_or_none(self):
        return None


class _FakeSession:
    """Minimal AsyncSession: no BYOK credential, records adds + commits."""

    def __init__(self):
        self.added = []
        self.commits = 0

    async def execute(self, *a, **k):
        return _NoCredResult()

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def get(self, *a, **k):  # pragma: no cover - not hit (no BYOK)
        return None


def _fake_provider(key: str, usage: dict):
    async def _call(request):
        return LlmResponse(
            raw_text="ok", tool_input={"answer": "hi"},
            stop_reason="end_turn", usage=dict(usage),
            provider=key, model=request.model,
        )
    return SimpleNamespace(key=key, is_configured=lambda: True, call=_call)


def _install(monkeypatch, *, provider_key: str, usage: dict):
    calls = {"enforced": 0}

    async def _enforce(db, user, **k):
        calls["enforced"] += 1

    monkeypatch.setattr(ac, "resolve_provider",
                        lambda pref=None: _fake_provider(provider_key, usage))
    monkeypatch.setattr(ac, "enforce_monthly_ceiling", _enforce)
    _allow_egress(monkeypatch)
    return calls


def _allow_egress(monkeypatch):
    # The egress policy (consent / AI-off / membership) has its own suite
    # in test_llm_egress_policy.py; the scripted session here answers None
    # to every query, which the membership check would read as revoked.
    async def _ok(db, user, **k):
        return None
    monkeypatch.setattr(ac, "authorize_egress", _ok)


async def _run(session, user, prompt, *, record_id=None):
    return await ac.call_with_tool(
        session, user, prompt,
        user_vars={}, purpose="unit_test",
        person_record_id=record_id,
    )


async def test_billed_run_stamps_user_record_and_cost(monkeypatch):
    _install(monkeypatch, provider_key="anthropic",
             usage={"input_tokens": 1_000_000})
    session = _FakeSession()
    user = _user()
    rid = uuid.uuid4()

    await _run(session, user, _prompt("claude-haiku-4-5"), record_id=rid)

    assert session.commits == 1
    run = session.added[0]
    assert run.user_id == user.id
    assert run.person_record_id == rid
    # haiku input = $0.80 / MTok → 1M input tokens = $0.80.
    assert float(run.estimated_cost_usd) == 0.80
    assert run.usage["billed_to"] == "deployment_default"


async def test_billed_path_enforces_ceiling(monkeypatch):
    calls = _install(monkeypatch, provider_key="anthropic",
                     usage={"input_tokens": 100})
    await _run(_FakeSession(), _user(), _prompt())
    assert calls["enforced"] == 1  # choke point ran on the billed path


async def test_local_echo_is_free_and_unenforced(monkeypatch):
    calls = _install(monkeypatch, provider_key="local_echo",
                     usage={"input_tokens": 1_000_000})
    session = _FakeSession()
    await _run(session, _user(), _prompt("claude-haiku-4-5"))

    run = session.added[0]
    assert float(run.estimated_cost_usd) == 0.0     # never billed
    assert run.usage["billed_to"] == "local_echo"
    assert calls["enforced"] == 0                    # ceiling not checked


async def test_unknown_model_costs_zero_without_crashing(monkeypatch):
    _install(monkeypatch, provider_key="anthropic",
             usage={"input_tokens": 9_000_000})
    session = _FakeSession()
    await _run(session, _user(), _prompt("totally-made-up-model"))

    run = session.added[0]
    assert float(run.estimated_cost_usd) == 0.0


async def test_over_budget_refusal_writes_no_run(monkeypatch):
    from ownchart.llm.budget import MonthlyBudgetExceeded

    async def _enforce(db, user, **k):
        raise MonthlyBudgetExceeded(
            ceiling_usd=25.0, spent_usd=30.0,
            resets_at=__import__("datetime").datetime(2026, 8, 1,
                tzinfo=__import__("datetime").timezone.utc),
        )

    monkeypatch.setattr(ac, "resolve_provider",
                        lambda pref=None: _fake_provider("anthropic", {"input_tokens": 1}))
    monkeypatch.setattr(ac, "enforce_monthly_ceiling", _enforce)
    _allow_egress(monkeypatch)

    session = _FakeSession()
    with pytest.raises(MonthlyBudgetExceeded):
        await _run(session, _user(), _prompt())
    # Refused before the call: no ModelRun, no commit, no spend.
    assert session.added == []
    assert session.commits == 0
