"""Email/SMTP workstream — the send primitive (core/email.py).

Proves:
  - No-op (returns False, never touches smtplib) when SMTP is unconfigured.
  - Sends via a MOCKED smtplib when configured — asserts recipient / subject
    / STARTTLS / login, never opens a real socket.
  - `public_url` composition and `is_smtp_configured` gating.
  - PHI-safe logging surface: the send helper never logs the body.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pydantic import SecretStr

from ownchart.core import email as email_mod
from ownchart.core.app_config import AppConfig, InstanceConfig, SmtpConfig


def _cfg(*, enabled=True, host="smtp.test", from_address="OwnChart <no@reply.test>"):
    return AppConfig(
        instance=InstanceConfig(name="OwnChart"),
        smtp=SmtpConfig(
            enabled=enabled,
            host=host,
            port=587,
            use_tls=True,
            username="mailer",
            from_address=from_address,
        ),
    )


def _settings(password="s3cret", base="https://inst.test"):
    return SimpleNamespace(
        smtp_password=SecretStr(password) if password else None,
        public_base_url=base,
    )


# ---------------------------------------------------------------------------
# is_smtp_configured / public_url


def test_is_smtp_configured_true_when_enabled_and_fields_present():
    with patch.object(email_mod, "get_app_config", lambda: _cfg()):
        assert email_mod.is_smtp_configured() is True


def test_is_smtp_configured_false_when_disabled():
    with patch.object(email_mod, "get_app_config", lambda: _cfg(enabled=False)):
        assert email_mod.is_smtp_configured() is False


def test_is_smtp_configured_false_when_no_host():
    with patch.object(email_mod, "get_app_config", lambda: _cfg(host=None)):
        assert email_mod.is_smtp_configured() is False


def test_public_url_uses_base_and_normalizes_slash():
    with patch.object(email_mod, "get_settings", lambda: _settings(base="https://x.test/")):
        assert email_mod.public_url("verify") == "https://x.test/verify"
        assert email_mod.public_url("/reset/abc") == "https://x.test/reset/abc"


def test_public_url_falls_back_to_localhost():
    with patch.object(email_mod, "get_settings", lambda: _settings(base="")):
        assert email_mod.public_url("/x") == "http://localhost:8080/x"


# ---------------------------------------------------------------------------
# send_email — no-op when unconfigured


async def test_send_email_noop_when_unconfigured():
    smtp_ctor = MagicMock()
    with patch.object(email_mod, "get_app_config", lambda: _cfg(enabled=False)), \
         patch.object(email_mod.smtplib, "SMTP", smtp_ctor):
        result = await email_mod.send_email(
            to="a@b.test", subject="Hi", html_body="<p>x</p>", text_body="x",
        )
    assert result is False
    # The MTA must not have been contacted at all.
    smtp_ctor.assert_not_called()


# ---------------------------------------------------------------------------
# send_email — sends via mocked smtplib when configured


async def test_send_email_sends_via_mocked_smtp():
    smtp_ctor = MagicMock()
    server = smtp_ctor.return_value.__enter__.return_value
    with patch.object(email_mod, "get_app_config", lambda: _cfg()), \
         patch.object(email_mod, "get_settings", lambda: _settings()), \
         patch.object(email_mod.smtplib, "SMTP", smtp_ctor):
        result = await email_mod.send_email(
            to="patient@example.com",
            subject="Reset your OwnChart password",
            html_body="<p>hello</p>",
            text_body="hello",
        )
    assert result is True
    smtp_ctor.assert_called_once()
    # host + port passed positionally.
    args, kwargs = smtp_ctor.call_args
    assert args[0] == "smtp.test"
    assert args[1] == 587
    # STARTTLS negotiated (use_tls=True) and authenticated.
    assert server.starttls.called
    server.login.assert_called_once_with("mailer", "s3cret")
    # The message actually handed off carries the right envelope.
    assert server.send_message.called
    sent_msg = server.send_message.call_args.args[0]
    assert sent_msg["To"] == "patient@example.com"
    assert sent_msg["Subject"] == "Reset your OwnChart password"
    assert sent_msg["From"] == "OwnChart <no@reply.test>"


async def test_send_email_skips_login_without_credentials():
    cfg = _cfg()
    cfg.smtp.username = None
    smtp_ctor = MagicMock()
    server = smtp_ctor.return_value.__enter__.return_value
    with patch.object(email_mod, "get_app_config", lambda: cfg), \
         patch.object(email_mod, "get_settings", lambda: _settings(password=None)), \
         patch.object(email_mod.smtplib, "SMTP", smtp_ctor):
        result = await email_mod.send_email(
            to="a@b.test", subject="s", html_body="<p>h</p>", text_body="h",
        )
    assert result is True
    server.login.assert_not_called()


# ---------------------------------------------------------------------------
# Compose helpers use templates + subjects


async def test_send_invitation_email_composes_and_sends():
    captured = {}

    async def _fake_send(*, to, subject, html_body, text_body):
        captured.update(to=to, subject=subject, html=html_body, text=text_body)
        return True

    with patch.object(email_mod, "get_app_config", lambda: _cfg()), \
         patch.object(email_mod, "send_email", _fake_send):
        ok = await email_mod.send_invitation_email(
            to="invitee@example.com",
            invite_url="https://inst.test/invite/tok",
            role="owner",
            expiry_note="This invitation expires in 7 days.",
        )
    assert ok is True
    assert captured["to"] == "invitee@example.com"
    assert "OwnChart" in captured["subject"]
    assert "https://inst.test/invite/tok" in captured["html"]
    assert "https://inst.test/invite/tok" in captured["text"]
