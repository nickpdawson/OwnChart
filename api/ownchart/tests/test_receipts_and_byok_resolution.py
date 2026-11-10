"""Prompt receipts, numeric prompt versions, BYOK-aware provider
resolution, PHI-safe error strings, revision-aware Home cache key
(audit 2026-11-10, prompts/provider/retention sections)."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from ownchart.llm import anthropic_client as ac
from ownchart.llm.prompts import PromptRegistry, get_registry
from ownchart.llm.providers import registry
from ownchart.llm.providers.base import ProviderUnavailable
from ownchart.routes.home_ai import _insight_cache_key


# ---------------------------------------------------------------------------
# Prompt registry


def _write_prompt(dir_: Path, name: str, pid: str, version: int, body: str = "x"):
    (dir_ / name).write_text(
        f"id: {pid}\nversion: {version}\nmodel: m\npurpose: p\nsystem: {body}\n"
        f"user_template: 'hi {{q}}'\n",
        encoding="utf-8",
    )


def test_latest_prompt_is_highest_numeric_version_not_filename_order(tmp_path):
    # "v10" sorts before "v2" lexically; the registry must still pick 10.
    _write_prompt(tmp_path, "ask.v10.yaml", "ask", 10)
    _write_prompt(tmp_path, "ask.v2.yaml", "ask", 2)
    _write_prompt(tmp_path, "ask.v9.yaml", "ask", 9)
    reg = PromptRegistry(tmp_path)
    assert reg.get("ask").version == 10
    assert reg.get("ask@2").version == 2


def test_prompt_carries_file_hash_and_name(tmp_path):
    _write_prompt(tmp_path, "brief.v1.yaml", "brief", 1, body="alpha")
    reg = PromptRegistry(tmp_path)
    p = reg.get("brief")
    assert p.source_file == "brief.v1.yaml"
    assert p.content_sha256 and len(p.content_sha256) == 64
    # Change the text, change the hash.
    _write_prompt(tmp_path, "brief.v1.yaml", "brief", 1, body="beta")
    assert PromptRegistry(tmp_path).get("brief").content_sha256 != p.content_sha256


def test_shipped_prompts_all_have_hashes():
    for p in get_registry().all():
        assert p.content_sha256 and p.source_file, p.version_tag


# ---------------------------------------------------------------------------
# BYOK-aware resolution


def _fake_registry(monkeypatch, configured: dict[str, bool]):
    providers = {
        k: SimpleNamespace(key=k, label=k.title(), is_configured=lambda v=v: v, call=None)
        for k, v in configured.items()
    }
    monkeypatch.setattr(registry, "_PROVIDERS", providers)


def test_user_key_makes_an_unconfigured_provider_usable(monkeypatch):
    _fake_registry(monkeypatch, {"anthropic": True, "openai": False, "local_echo": True})
    with pytest.raises(ProviderUnavailable):
        registry.resolve("openai")
    assert registry.resolve("openai", extra_configured=frozenset({"openai"})).key == "openai"


def test_user_key_also_feeds_the_fallback_chain(monkeypatch):
    _fake_registry(monkeypatch, {"anthropic": False, "openai": False, "local_echo": True})
    assert registry.resolve(None).key == "local_echo"
    assert registry.resolve(None, extra_configured=frozenset({"openai"})).key == "openai"


async def test_call_with_tool_retries_resolution_with_user_keys(monkeypatch):
    calls: list = []

    async def _provider_call(request):
        from ownchart.llm.providers.base import LlmResponse
        calls.append(request)
        return LlmResponse(raw_text="ok", tool_input={"a": 1}, stop_reason="end_turn",
                           usage={"input_tokens": 1}, provider="openai", model=request.model)

    openai = SimpleNamespace(key="openai", label="OpenAI", is_configured=lambda: False, call=_provider_call)

    def _resolve(pref=None, *, extra_configured=frozenset()):
        if "openai" in extra_configured:
            return openai
        raise ProviderUnavailable("OpenAI is selected but has no API key configured.")

    monkeypatch.setattr(ac, "resolve_provider", _resolve)

    async def _keys(db, user):
        return frozenset({"openai"})

    async def _ok(*a, **k):
        return None

    monkeypatch.setattr(ac, "_user_credential_providers", _keys)
    monkeypatch.setattr(ac, "enforce_monthly_ceiling", _ok)
    monkeypatch.setattr(ac, "authorize_egress", _ok)

    async def _user_key(db, user, provider_key):
        return "sk-user", uuid.uuid4()
    monkeypatch.setattr(ac, "_resolve_user_api_key", _user_key)

    class _S:
        def __init__(self):
            self.added = []

        def add(self, o):
            self.added.append(o)

        async def commit(self):
            pass

        async def get(self, *a, **k):
            return None

    prompt = SimpleNamespace(model="gpt", system="s", tools=[], version_tag="t@1",
                             render=lambda **kw: "r", content_sha256="abc", source_file="t.v1.yaml")
    res = await ac.call_with_tool(_S(), SimpleNamespace(id=uuid.uuid4(), phi_consent_granted=True),
                                  prompt, user_vars={}, purpose="unit", provider="openai")
    assert res.provider == "openai"
    assert len(calls) == 1
    assert res.usage["billed_to"] == "user_byok"
    assert res.usage["prompt_sha256"] == "abc"
    assert res.usage["prompt_file"] == "t.v1.yaml"


# ---------------------------------------------------------------------------
# PHI-safe error strings


def test_safe_error_drops_message_body_keeps_class_status_hint():
    class VendorError(Exception):
        status_code = 429

    e = VendorError("rate limit exceeded; request body: patient John took 20mg …")
    s = ac._safe_error(e)
    assert s == "VendorError http_429 rate_limited"
    assert "John" not in s and "20mg" not in s


def test_safe_error_without_status_or_hint():
    assert ac._safe_error(RuntimeError("something about a dossier")) == "RuntimeError"


# ---------------------------------------------------------------------------
# Home cache key includes the evidence revision


def test_cache_key_changes_with_revision():
    u, r = uuid.uuid4(), uuid.uuid4()
    a = _insight_cache_key(u, r, "2026-11-10", None, revision="aaa")
    b = _insight_cache_key(u, r, "2026-11-10", None, revision="bbb")
    assert a != b
    assert _insight_cache_key(u, r, "2026-11-10", None, revision="aaa") == a
