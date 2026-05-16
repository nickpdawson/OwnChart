"""Per-visitor demo session helper tests.

The shared demo account (demo@ownchart.me) is logged into by every
visitor. Without the per-visitor cookie + scope filter, visitor B
sees visitor A's typed Ask chats. These tests pin the helpers that
prevent that leak.

Pure-function — no DB, no LLM, no HTTP.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from ownchart.core import demo_session as ds


def _fake_request(cookies: dict[str, str] | None = None):
    return SimpleNamespace(cookies=cookies or {})


# ---------------------------------------------------------------------------
# get_demo_session_id


def test_get_returns_none_when_cookie_absent():
    assert ds.get_demo_session_id(_fake_request()) is None


def test_get_returns_cookie_value():
    req = _fake_request({ds.DEMO_SESSION_COOKIE: "abc123"})
    assert ds.get_demo_session_id(req) == "abc123"


def test_get_caps_oversized_cookie_value():
    """Tampered or oversized cookies must not flow into JSONB."""
    huge = "x" * 5000
    req = _fake_request({ds.DEMO_SESSION_COOKIE: huge})
    out = ds.get_demo_session_id(req)
    assert out is not None
    assert len(out) == 64


# ---------------------------------------------------------------------------
# apply_demo_session_scope


def test_apply_noop_outside_demo_mode():
    """In non-demo mode, the scope is unchanged regardless of cookie."""
    req = _fake_request({ds.DEMO_SESSION_COOKIE: "abc"})
    with patch.object(ds, "get_settings", lambda: SimpleNamespace(demo_mode=False)):
        out = ds.apply_demo_session_scope({"type": "whole_record"}, req)
    assert out == {"type": "whole_record"}
    assert "demo_session_id" not in out


def test_apply_stamps_session_id_in_demo_mode():
    req = _fake_request({ds.DEMO_SESSION_COOKIE: "visitor-A"})
    with patch.object(ds, "get_settings", lambda: SimpleNamespace(demo_mode=True)):
        out = ds.apply_demo_session_scope({"type": "whole_record"}, req)
    assert out["demo_session_id"] == "visitor-A"
    assert out["type"] == "whole_record"


def test_apply_does_not_stamp_when_no_cookie():
    """First request from a visitor has no cookie yet — return scope
    without demo_session_id rather than fabricating an empty one."""
    req = _fake_request()
    with patch.object(ds, "get_settings", lambda: SimpleNamespace(demo_mode=True)):
        out = ds.apply_demo_session_scope({"type": "x"}, req)
    assert out == {"type": "x"}


def test_apply_does_not_mutate_input():
    original = {"type": "whole_record"}
    req = _fake_request({ds.DEMO_SESSION_COOKIE: "v1"})
    with patch.object(ds, "get_settings", lambda: SimpleNamespace(demo_mode=True)):
        ds.apply_demo_session_scope(original, req)
    assert "demo_session_id" not in original


def test_apply_handles_none_scope():
    req = _fake_request({ds.DEMO_SESSION_COOKIE: "v1"})
    with patch.object(ds, "get_settings", lambda: SimpleNamespace(demo_mode=True)):
        out = ds.apply_demo_session_scope(None, req)
    assert out == {"demo_session_id": "v1"}


# ---------------------------------------------------------------------------
# demo_session_matches — the gate behind every detail endpoint.


def test_matches_always_true_outside_demo_mode():
    req = _fake_request()
    with patch.object(ds, "get_settings", lambda: SimpleNamespace(demo_mode=False)):
        assert ds.demo_session_matches({"demo_session_id": "x"}, req) is True


def test_matches_true_when_row_has_no_demo_id():
    """Seeded / pre-demo rows have no demo_session_id — visible to all."""
    req = _fake_request({ds.DEMO_SESSION_COOKIE: "v1"})
    with patch.object(ds, "get_settings", lambda: SimpleNamespace(demo_mode=True)):
        assert ds.demo_session_matches({"type": "whole_record"}, req) is True
        assert ds.demo_session_matches(None, req) is True


def test_matches_true_when_cookie_matches_row():
    """Visitor reads their own conversation."""
    req = _fake_request({ds.DEMO_SESSION_COOKIE: "visitor-A"})
    with patch.object(ds, "get_settings", lambda: SimpleNamespace(demo_mode=True)):
        assert ds.demo_session_matches(
            {"demo_session_id": "visitor-A"}, req,
        ) is True


def test_matches_false_when_cookie_differs_from_row():
    """Visitor B tries to read visitor A's conversation — denied."""
    req = _fake_request({ds.DEMO_SESSION_COOKIE: "visitor-B"})
    with patch.object(ds, "get_settings", lambda: SimpleNamespace(demo_mode=True)):
        assert ds.demo_session_matches(
            {"demo_session_id": "visitor-A"}, req,
        ) is False


def test_matches_false_when_no_cookie_and_row_has_session_id():
    """A visitor without a cookie (e.g. cookies cleared) tries to
    read a stamped row — denied. Without this, anyone deleting
    cookies could read every visitor's stamped chat."""
    req = _fake_request()
    with patch.object(ds, "get_settings", lambda: SimpleNamespace(demo_mode=True)):
        assert ds.demo_session_matches(
            {"demo_session_id": "visitor-A"}, req,
        ) is False
