"""Transactional email — the send primitive for the invite / password-reset
/ email-verification flows (Email/SMTP workstream).

Design constraints:

  - **Stdlib only.** Uses ``smtplib`` off the event loop via
    ``asyncio.to_thread`` rather than pulling in an async-SMTP dependency
    (and the lockfile churn that follows). Transactional email volume on a
    self-hosted single-tenant instance is tiny; a threadpool send is fine.

  - **No-op when SMTP is unconfigured.** A self-hoster who never sets up
    SMTP keeps working — invites still return a copyable URL, and the
    password-reset / verification affordances simply don't send. ``enabled``
    (config.yaml) gated OFF, or a missing host / from_address, makes
    :func:`send_email` a logged no-op that returns ``False``.

  - **PHI-safe logging.** OwnChart is a health product; a recipient address
    is PII and an email body may quote health context. We NEVER log the body
    and never log a plaintext recipient — only a short salted-free sha256
    prefix (enough to correlate retries) and a count.

The higher-level compose helpers (``send_invitation_email`` etc.) live here
too so callers pass domain values (link, instance name) and this module owns
the template lookup + subject line.
"""

from __future__ import annotations

import asyncio
import hashlib
import smtplib
from email.message import EmailMessage

from .app_config import SmtpConfig, get_app_config
from .config import get_settings
from .email_templates import render_email
from .logger import get_logger

log = get_logger("ownchart.core.email")

# Bound the SMTP conversation so a hung/blackholed mail server can't pin a
# threadpool worker indefinitely.
_SMTP_TIMEOUT_SECONDS = 15


def public_url(path: str) -> str:
    """Compose an absolute, operator-facing link for embedding in an email.

    Uses ``settings.public_base_url`` (the canonical externally-reachable
    origin the operator configures on every real deployment) so links are
    correct behind a reverse proxy. Falls back to the localhost default only
    when unset (dev).
    """
    base = (get_settings().public_base_url or "").rstrip("/")
    if not base:
        base = "http://localhost:8080"
    if not path.startswith("/"):
        path = "/" + path
    return f"{base}{path}"


def is_smtp_configured() -> bool:
    """True when this instance can actually send mail.

    Requires ``smtp.enabled`` plus the minimum fields a send needs (host and
    a From address). Username/password are optional — some relays accept
    unauthenticated submission from a trusted host.
    """
    smtp = get_app_config().smtp
    return bool(smtp.enabled and smtp.host and smtp.from_address)


def _recipient_fingerprint(to: str) -> str:
    """Short sha256 prefix of the recipient — enough to correlate a retry in
    the logs without writing the address itself."""
    return hashlib.sha256(to.strip().lower().encode("utf-8")).hexdigest()[:12]


def _send_sync(msg: EmailMessage, smtp: SmtpConfig, password: str | None) -> None:
    """Blocking SMTP send. Runs in a worker thread via ``to_thread``."""
    with smtplib.SMTP(smtp.host, smtp.port, timeout=_SMTP_TIMEOUT_SECONDS) as server:
        server.ehlo()
        if smtp.use_tls:
            server.starttls()
            server.ehlo()
        if smtp.username and password:
            server.login(smtp.username, password)
        server.send_message(msg)


async def send_email(
    *,
    to: str,
    subject: str,
    html_body: str,
    text_body: str,
) -> bool:
    """Send one transactional email. Returns True if handed to the MTA.

    No-ops (returns False) when SMTP is unconfigured — callers treat a False
    return as "delivery unavailable, fall back to the manual path", never as
    an error. A send that raises is logged and re-raised to the caller only
    when the caller wants to react; the invite path swallows it (see
    :func:`send_invitation_email` callers). This function itself does not
    swallow — it lets exceptions propagate so the reset/verification paths
    can decide.
    """
    if not is_smtp_configured():
        log.info(
            "email_skipped_smtp_unconfigured",
            subject=subject,
            recipient=_recipient_fingerprint(to),
        )
        return False

    smtp = get_app_config().smtp
    password = get_settings().smtp_password
    password_str = password.get_secret_value() if password is not None else None

    msg = EmailMessage()
    msg["From"] = smtp.from_address  # type: ignore[assignment]
    msg["To"] = to
    msg["Subject"] = subject
    # Plain-text part is the fallback; HTML is the alternative. Order matters:
    # set_content first (text/plain), then add_alternative (text/html) so a
    # capable client renders the HTML and a text-only client shows the text.
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    await asyncio.to_thread(_send_sync, msg, smtp, password_str)
    log.info(
        "email_sent",
        subject=subject,
        recipient=_recipient_fingerprint(to),
        count=1,
    )
    return True


# ---------------------------------------------------------------------------
# Compose helpers — one per flow. Each owns its subject line + template name.


def _instance_name() -> str:
    return get_app_config().instance.name


async def send_invitation_email(*, to: str, invite_url: str, role: str, expiry_note: str) -> bool:
    """Deliver an invitation. The invite URL is still returned to the owner
    for the manual-copy fallback; this is best-effort delivery on top."""
    name = _instance_name()
    html_body, text_body = render_email(
        "invitation",
        {
            "instance_name": name,
            "invited_email": to,
            "role": role,
            "link": invite_url,
            "expiry_note": expiry_note,
        },
    )
    return await send_email(
        to=to,
        subject=f"You're invited to {name}",
        html_body=html_body,
        text_body=text_body,
    )


async def send_password_reset_email(*, to: str, reset_url: str, expiry_note: str) -> bool:
    name = _instance_name()
    html_body, text_body = render_email(
        "password_reset",
        {
            "instance_name": name,
            "invited_email": to,
            "link": reset_url,
            "expiry_note": expiry_note,
        },
    )
    return await send_email(
        to=to,
        subject=f"Reset your {name} password",
        html_body=html_body,
        text_body=text_body,
    )


async def send_email_verification_email(*, to: str, verify_url: str, expiry_note: str) -> bool:
    name = _instance_name()
    html_body, text_body = render_email(
        "email_verification",
        {
            "instance_name": name,
            "invited_email": to,
            "link": verify_url,
            "expiry_note": expiry_note,
        },
    )
    return await send_email(
        to=to,
        subject=f"Verify your email for {name}",
        html_body=html_body,
        text_body=text_body,
    )
