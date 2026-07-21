"""P1-9: unified password policy + email normalization.

Pure-function coverage for the single policy applied at every set-password
site (register/bootstrap/invite-accept, change, reset) and the email
canonicalizer used at every register/login/reset/invite-match lookup.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from ownchart.core.password_policy import (
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
    enforce_password_policy,
    password_policy_violations,
)
from ownchart.core.security import normalize_email


# ---------------------------------------------------------------------------
# password_policy_violations — pure


def test_ok_password_has_no_violations():
    assert password_policy_violations("goodpassword") == []


def test_min_length_boundary():
    assert password_policy_violations("a" * (PASSWORD_MIN_LENGTH - 1)) == ["min_length"]
    assert password_policy_violations("a" * PASSWORD_MIN_LENGTH) == []


def test_max_length_boundary():
    assert password_policy_violations("a" * PASSWORD_MAX_LENGTH) == []
    assert password_policy_violations("a" * (PASSWORD_MAX_LENGTH + 1)) == ["max_length"]


def test_empty_and_whitespace_only_are_empty_violation():
    assert password_policy_violations("") == ["empty"]
    assert password_policy_violations("   ") == ["empty"]
    assert password_policy_violations("\t\n ") == ["empty"]
    assert password_policy_violations(None) == ["empty"]


def test_empty_does_not_also_report_min_length():
    # A blank password is one clear violation, not two.
    assert password_policy_violations("") == ["empty"]


# ---------------------------------------------------------------------------
# enforce_password_policy — raises 422 password_policy


def test_enforce_passes_valid():
    enforce_password_policy("longenough")  # no raise


def test_enforce_raises_stable_code_and_requirements():
    with pytest.raises(HTTPException) as ei:
        enforce_password_policy("short")
    exc = ei.value
    assert exc.status_code == 422
    assert exc.detail["code"] == "password_policy"
    assert exc.detail["requirements"]["min_length"] == PASSWORD_MIN_LENGTH
    assert exc.detail["requirements"]["max_length"] == PASSWORD_MAX_LENGTH
    assert "min_length" in exc.detail["violations"]


def test_enforce_rejects_oversized_password_dos_guard():
    with pytest.raises(HTTPException) as ei:
        enforce_password_policy("a" * (PASSWORD_MAX_LENGTH + 1))
    assert "max_length" in ei.value.detail["violations"]


# ---------------------------------------------------------------------------
# normalize_email — pure


def test_normalize_email_strips_and_lowercases():
    assert normalize_email("  Nick@Example.COM ") == "nick@example.com"


def test_normalize_email_handles_none_and_blank():
    assert normalize_email(None) == ""
    assert normalize_email("   ") == ""


def test_normalize_email_is_idempotent():
    once = normalize_email("Foo@Bar.io")
    assert normalize_email(once) == once
