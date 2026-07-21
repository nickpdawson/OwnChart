"""First-owner bootstrap setup token (P0-1 release review).

The very first `POST /api/auth/register` on a fresh DB creates the
instance owner (`is_instance_admin=True`). Without a gate, anyone who
can reach the instance during the window between "DB is up" and "owner
has registered" can claim ownership — a takeover race.

This module is that gate. On API startup, when (and only when) the DB
has zero users, we mint a high-entropy one-time **setup token** and make
it available to the operator two ways:

  1. Logged once to the API container log (``docker compose logs api``).
  2. Written to ``<data_dir>/setup_token`` (mode 0600), which the api
     process already owns.

`POST /api/auth/register` on the empty-DB path requires this token.
The moment the first admin exists, the token file is deleted and the
bootstrap path is closed forever (subsequent registers go through the
invite / self-registration rules instead).

Design notes:
  - **Constant-time compare** via ``secrets.compare_digest`` — the
    verification path must not leak the token through timing.
  - **Regenerate on restart if unclaimed.** Each boot with zero users
    writes a fresh random token, so a token glimpsed in an old log
    stops working after the next restart.
  - **Env escape hatch.** ``OWNCHART_SETUP_TOKEN`` pins the token for
    deployment automation; when unset a random per-boot token is used.
    When pinned, the pinned value is the source of truth and is also
    mirrored to the file for operator convenience.
  - **No secret in chat / no echo.** Callers log the token exactly once
    at startup; nothing else prints it.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from .config import get_settings
from .logger import get_logger

log = get_logger("ownchart.core.setup_token")

# Env var that pins the setup token (deployment automation). Read
# directly from os.environ (not Settings) so it stays out of the typed
# settings surface and is never accidentally logged with the rest.
_ENV_PIN = "OWNCHART_SETUP_TOKEN"

# 32 random bytes url-safe (~256 bits) — same strength as invite tokens.
_SETUP_TOKEN_BYTES = 32

_TOKEN_FILENAME = "setup_token"


def setup_token_path() -> Path:
    """Absolute path of the on-disk setup-token file under the data dir."""
    return get_settings().data_dir / _TOKEN_FILENAME


def _env_pin() -> str | None:
    raw = os.environ.get(_ENV_PIN)
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


def generate_setup_token() -> str:
    """Return a fresh url-safe random setup token (~256 bits entropy)."""
    return secrets.token_urlsafe(_SETUP_TOKEN_BYTES)


def _read_token_file() -> str | None:
    path = setup_token_path()
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as exc:  # unreadable file — treat as no token
        log.warning("setup_token_file_unreadable", error=str(exc))
        return None
    return raw or None


def _write_token_file(token: str) -> None:
    """Write the token to ``<data_dir>/setup_token`` with 0600 perms.

    The data dir is the one the api process already owns; create it if
    a bare deployment hasn't yet. Best-effort: a write failure is logged
    but not fatal — the token is still in the startup log and (when
    pinned) the env var, so bootstrap remains possible.
    """
    path = setup_token_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Create/replace with owner-only perms. Open with a restrictive
        # mode so the secret is never briefly world-readable.
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, token.encode("utf-8"))
        finally:
            os.close(fd)
        # Re-assert perms in case the file pre-existed with looser mode.
        os.chmod(str(path), 0o600)
    except OSError as exc:
        log.warning("setup_token_file_write_failed", error=str(exc))


def active_setup_token() -> str | None:
    """The token currently accepted for bootstrap, or None if none.

    The env pin wins when set; otherwise the on-disk file is the source
    of truth. Returns None when neither is present (no bootstrap-by-token
    possible — the register route then refuses the empty-DB path)."""
    pin = _env_pin()
    if pin is not None:
        return pin
    return _read_token_file()


def verify_setup_token(provided: str | None) -> bool:
    """Constant-time check of a caller-supplied setup token.

    False when no token is provided, when no active token exists, or on
    mismatch. Uses ``secrets.compare_digest`` so a wrong-but-same-length
    guess can't be distinguished from a wrong-length one by timing."""
    active = active_setup_token()
    if not active or not provided:
        return False
    return secrets.compare_digest(provided, active)


def clear_setup_token() -> None:
    """Invalidate the setup token — called the instant the first admin
    exists. Deletes the on-disk file. The env pin (if any) is not ours to
    delete, but the register route only consults the token on the
    zero-user path, which can never recur once an owner exists."""
    path = setup_token_path()
    try:
        path.unlink()
        log.info("setup_token_cleared")
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("setup_token_clear_failed", error=str(exc))


def provision_setup_token_if_needed(*, has_users: bool) -> None:
    """Startup hook. Called from the API lifespan after seeds run.

    - **Zero users:** mint (or adopt the env-pinned) setup token, write
      it to the data dir, and log it ONCE so the operator can retrieve
      it. Regenerates a fresh random token on every unclaimed restart.
    - **Users exist:** the bootstrap window is closed — delete any stale
      token file so an old secret can't linger on disk.
    """
    if has_users:
        # Bootstrap already happened (or demo seeded a user). Ensure no
        # stale token survives on disk.
        if setup_token_path().exists():
            clear_setup_token()
        return

    pin = _env_pin()
    token = pin if pin is not None else generate_setup_token()
    _write_token_file(token)
    source = "env_pin" if pin is not None else "generated"
    # Log the token exactly once. This is the only place it is printed.
    log.info(
        "setup_token_provisioned",
        source=source,
        setup_token=token,
        path=str(setup_token_path()),
        hint=(
            "First-owner bootstrap: pass this token to POST /api/auth/register "
            "(or the web /setup page). It is invalidated once the owner exists."
        ),
    )
