"""Email/SMTP workstream — file-based template rendering.

Pins that the three transactional templates render from files, substitute
the expected placeholders, HTML-escape user-facing values, and are immune to
the template-injection hole where a substituted value smuggles in another
``{{token}}``.
"""

from __future__ import annotations

import pytest

from ownchart.core.email_templates import render_email


def test_all_three_templates_render():
    ctx = {
        "instance_name": "OwnChart",
        "invited_email": "person@example.com",
        "role": "owner",
        "link": "https://ex.test/invite/abc",
        "expiry_note": "This link expires in 24 hours.",
    }
    for name in ("invitation", "password_reset", "email_verification"):
        html_body, text_body = render_email(name, ctx)
        assert "https://ex.test/invite/abc" in html_body
        assert "https://ex.test/invite/abc" in text_body
        assert "OwnChart" in html_body
        # No unresolved known-key tokens remain.
        for key in ctx:
            assert "{{" + key + "}}" not in html_body
            assert "{{" + key + "}}" not in text_body


def test_html_escapes_values():
    html_body, text_body = render_email(
        "invitation",
        {
            "instance_name": "<script>alert(1)</script>",
            "invited_email": "a@b.com",
            "role": "owner",
            "link": "https://x.test/i",
            "expiry_note": "soon",
        },
    )
    assert "<script>alert(1)</script>" not in html_body
    assert "&lt;script&gt;" in html_body
    # Text part is not markup — no escaping required there.
    assert "<script>alert(1)</script>" in text_body


def test_no_template_injection_via_value():
    """A value that itself contains ``{{link}}`` must NOT be re-expanded into
    the real link — single-pass substitution closes that hole."""
    html_body, text_body = render_email(
        "password_reset",
        {
            "instance_name": "Acme",
            "invited_email": "attacker{{link}}@b.com",
            "link": "https://secret.test/reset/TOKEN",
            "expiry_note": "soon",
        },
    )
    # The literal injected token survives verbatim (HTML-escaped braces are
    # still literal braces — html.escape doesn't touch '{'), never swapped
    # for the real reset URL.
    assert "attacker{{link}}@b.com" in text_body
    # The injected occurrence did not pull in a SECOND copy of the URL.
    assert text_body.count("https://secret.test/reset/TOKEN") == 1


def test_unknown_token_left_verbatim():
    html_body, _ = render_email(
        "email_verification",
        {
            "instance_name": "Acme",
            "invited_email": "a@b.com",
            "link": "https://x.test/v",
            "expiry_note": "soon",
        },
    )
    # Templates don't reference {{nonexistent}}, but prove the renderer's
    # contract directly: an unknown token is preserved, not blanked.
    from ownchart.core.email_templates import _render
    assert _render("keep {{nope}}", {"a": "b"}, escape=False) == "keep {{nope}}"


def test_missing_template_file_raises():
    with pytest.raises(FileNotFoundError):
        render_email("does_not_exist", {})
