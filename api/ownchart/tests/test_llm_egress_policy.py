"""One egress policy before any model call (audit 2026-11-10, item 3).

Pins, with a fake session and a fake provider (no DB, no network):

  - consent false, disabled account, privacy mode off, and a revoked
    record membership each raise LlmEgressDenied from call_with_tool
    BEFORE the provider is invoked and before any ModelRun is written;
  - an explicit provider that is unknown (`local`, `gemini`) or
    unconfigured raises ProviderUnavailable instead of falling back;
  - `auto` / None still walk the fallback chain;
  - the HTTP mapping is 412 for consent and 403 otherwise, 503 for an
    unavailable provider.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ownchart.llm import anthropic_client as ac
from ownchart.llm import egress
from ownchart.llm.providers import registry
from ownchart.llm.providers.base import LlmResponse, ProviderUnavailable


def _prompt():
    return SimpleNamespace(
        model="claude-haiku-4-5", system="s", tools=[],
        version_tag="t@1", render=lambda **kw: "rendered",
    )


def _user(**over):
    base = dict(id=uuid.uuid4(), phi_consent_granted=True, disabled=False)
    base.update(over)
    return SimpleNamespace(**base)


class _Result:
    def __init__(self, value):
        self._v = value

    def scalar_one_or_none(self):
        return self._v


class _Session:
    """Returns `membership` for Membership queries and None otherwise."""

    def __init__(self, *, membership=None, privacy_mode=None):
        self.membership = membership
        self.privacy_mode = privacy_mode
        self.added = []
        self.commits = 0

    async def execute(self, stmt, *a, **k):
        text = str(stmt)
        if "memberships" in text:
            return _Result(self.membership)
        if "user_settings" in text and self.privacy_mode is not None:
            return _Result(SimpleNamespace(value=self.privacy_mode, key="ai.privacy_mode"))
        return _Result(None)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def get(self, *a, **k):
        return None


def _provider(calls: list):
    async def _call(request):
        calls.append(request)
        return LlmResponse(
            raw_text="ok", tool_input={"a": 1}, stop_reason="end_turn",
            usage={"input_tokens": 1}, provider="fake", model=request.model,
        )
    return SimpleNamespace(key="anthropic", label="Fake", is_configured=lambda: True, call=_call)


def _install(monkeypatch, calls):
    monkeypatch.setattr(ac, "resolve_provider", lambda pref=None: _provider(calls))

    async def _enforce(db, user, **k):
        return None
    monkeypatch.setattr(ac, "enforce_monthly_ceiling", _enforce)


async def _run(session, user, record_id=None):
    return await ac.call_with_tool(
        session, user, _prompt(), user_vars={}, purpose="unit_test",
        person_record_id=record_id,
    )


# ---------------------------------------------------------------------------
# Denials happen before the provider sees anything


async def test_consent_false_blocks_before_provider(monkeypatch):
    calls: list = []
    _install(monkeypatch, calls)
    s = _Session()
    with pytest.raises(egress.LlmEgressDenied) as ei:
        await _run(s, _user(phi_consent_granted=False))
    assert ei.value.code == "phi_consent_required"
    assert calls == []
    assert s.added == [] and s.commits == 0


async def test_disabled_account_blocks(monkeypatch):
    calls: list = []
    _install(monkeypatch, calls)
    with pytest.raises(egress.LlmEgressDenied) as ei:
        await _run(_Session(), _user(disabled=True))
    assert ei.value.code == "account_disabled"
    assert calls == []


async def test_privacy_mode_off_blocks(monkeypatch):
    calls: list = []
    _install(monkeypatch, calls)
    with pytest.raises(egress.LlmEgressDenied) as ei:
        await _run(_Session(privacy_mode="off"), _user())
    assert ei.value.code == "ai_off"
    assert calls == []


async def test_revoked_membership_blocks_when_record_in_scope(monkeypatch):
    calls: list = []
    _install(monkeypatch, calls)
    with pytest.raises(egress.LlmEgressDenied) as ei:
        await _run(_Session(membership=None), _user(), record_id=uuid.uuid4())
    assert ei.value.code == "record_access_revoked"
    assert calls == []


async def test_active_membership_and_consent_allow_the_call(monkeypatch):
    calls: list = []
    _install(monkeypatch, calls)
    s = _Session(membership=uuid.uuid4())
    await _run(s, _user(), record_id=uuid.uuid4())
    assert len(calls) == 1
    assert s.commits == 1  # ModelRun written


async def test_no_record_in_scope_skips_membership_check(monkeypatch):
    calls: list = []
    _install(monkeypatch, calls)
    await _run(_Session(membership=None), _user())
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Provider resolution fails closed


def _fake_registry(monkeypatch, configured: dict[str, bool]):
    providers = {
        k: SimpleNamespace(key=k, label=k.title(), is_configured=lambda v=v: v, call=None)
        for k, v in configured.items()
    }
    monkeypatch.setattr(registry, "_PROVIDERS", providers)


def test_explicit_unknown_provider_raises(monkeypatch):
    _fake_registry(monkeypatch, {"anthropic": True, "openai": False, "local_echo": True})
    for name in ("local", "gemini", "nope"):
        with pytest.raises(ProviderUnavailable):
            registry.resolve(name)


def test_explicit_unconfigured_provider_raises_instead_of_falling_back(monkeypatch):
    _fake_registry(monkeypatch, {"anthropic": True, "openai": False, "local_echo": True})
    with pytest.raises(ProviderUnavailable) as ei:
        registry.resolve("openai")
    assert "Openai" in str(ei.value)


def test_auto_and_none_walk_fallback_chain(monkeypatch):
    _fake_registry(monkeypatch, {"anthropic": False, "openai": True, "local_echo": True})
    assert registry.resolve(None).key == "openai"
    assert registry.resolve("auto").key == "openai"
    _fake_registry(monkeypatch, {"anthropic": False, "openai": False, "local_echo": True})
    assert registry.resolve(None).key == "local_echo"


def test_explicit_configured_provider_is_used(monkeypatch):
    _fake_registry(monkeypatch, {"anthropic": True, "openai": True, "local_echo": True})
    assert registry.resolve("openai").key == "openai"
    assert registry.resolve("local_echo").key == "local_echo"


def test_settings_registry_no_longer_advertises_local_or_gemini():
    from ownchart.settings.registry import get_registry
    choices = get_registry().get("ai.default_provider").choices
    assert "local" not in choices and "gemini" not in choices
    assert "auto" in choices


# ---------------------------------------------------------------------------
# HTTP mapping


def test_http_mapping_for_denials_and_unavailable_provider():
    from ownchart.main import app

    @app.get("/__test/egress/{code}")
    async def _raise(code: str):
        if code == "provider":
            raise ProviderUnavailable("Openai is selected but has no API key configured.")
        raise egress.LlmEgressDenied(code, "refused")

    try:
        c = TestClient(app)
        r = c.get("/__test/egress/phi_consent_required")
        assert r.status_code == 412
        assert r.json()["detail"]["error"] == "phi_consent_required"
        r = c.get("/__test/egress/ai_off")
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "ai_off"
        r = c.get("/__test/egress/provider")
        assert r.status_code == 503
        assert "no API key" in r.json()["detail"]["message"]
    finally:
        app.router.routes[:] = [
            rt for rt in app.router.routes
            if getattr(rt, "path", "") != "/__test/egress/{code}"
        ]
