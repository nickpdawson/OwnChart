import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import get_settings

_ph = PasswordHasher()
_settings = get_settings()
# Timed serializer (HIGH-5): the signed payload now carries an issue
# timestamp so a captured cookie can be expired server-side. Combined
# with the `stv` (session_token_version) field embedded by the auth
# layer, this gives us BOTH time-based expiry (a stolen cookie is
# useless after SESSION_MAX_AGE_SECONDS) and instant revocation (bump
# the user's session_token_version to invalidate every live cookie).
_serializer = URLSafeTimedSerializer(
    _settings.session_secret.get_secret_value(), salt="ownchart-session",
)


def hash_password(plain: str) -> str:
    return _ph.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _ph.verify(hashed, plain)
    except VerifyMismatchError:
        return False


def sign_session(payload: dict) -> str:
    return _serializer.dumps(payload)


def unsign_session(token: str, *, max_age: int | None = None) -> dict | None:
    """Verify + decode a session cookie.

    Enforces the signing timestamp: a cookie older than ``max_age``
    seconds (default ``settings.session_max_age_seconds``) is rejected
    exactly like a tampered signature — returns ``None``. Callers that
    want an unbounded read (none currently) can pass ``max_age`` large,
    but the default is the load-bearing expiry gate for web sessions.
    """
    if max_age is None:
        max_age = _settings.session_max_age_seconds
    try:
        return _serializer.loads(token, max_age=max_age)
    except (SignatureExpired, BadSignature):
        # SignatureExpired subclasses BadSignature, but list it
        # explicitly so the intent (expiry is a hard failure) is clear.
        return None


# --- Invitation tokens (FU-MULTITENANT-ONBOARDING) -------------------------
#
# Tokens are 32 random bytes encoded url-safe (~43 chars). The DB
# stores only the argon2id hash plus the first 8 chars as a non-secret
# lookup prefix. The prefix index makes "find this token" cheap
# without rainbow-table risk; verification is still constant-time
# via argon2.

INVITE_TOKEN_BYTES = 32
INVITE_LOOKUP_PREFIX_LEN = 8


def generate_invite_token() -> str:
    """Return a fresh url-safe random token. ~256 bits of entropy."""
    return secrets.token_urlsafe(INVITE_TOKEN_BYTES)


def invite_lookup_prefix(token: str) -> str:
    """Return the prefix used for the indexed DB lookup. Stable
    derivation: first INVITE_LOOKUP_PREFIX_LEN chars of the raw token."""
    return token[:INVITE_LOOKUP_PREFIX_LEN]


def hash_invite_token(token: str) -> str:
    """Hash an invite token with the same argon2id parameters used
    for passwords. Stored once at creation; verified on accept."""
    return _ph.hash(token)


def verify_invite_token(token: str, hashed: str) -> bool:
    """Constant-time verify against the stored argon2 hash."""
    try:
        return _ph.verify(hashed, token)
    except VerifyMismatchError:
        return False


# --- Password-reset tokens (Email/SMTP workstream) -------------------------
#
# Same at-rest shape as invite tokens: 32 random bytes url-safe, stored as
# an argon2id hash plus an indexed 8-char lookup prefix. A reset token is a
# single-use bearer credential to the account, so it is short-lived
# (settings.password_reset_token_ttl_seconds) and consumed on first use.

RESET_TOKEN_BYTES = 32
RESET_LOOKUP_PREFIX_LEN = 8


def generate_reset_token() -> str:
    """Return a fresh url-safe random reset token (~256 bits entropy)."""
    return secrets.token_urlsafe(RESET_TOKEN_BYTES)


def reset_lookup_prefix(token: str) -> str:
    """Indexed-lookup prefix for a reset token (first N chars, non-secret)."""
    return token[:RESET_LOOKUP_PREFIX_LEN]


def hash_reset_token(token: str) -> str:
    """argon2id hash of a reset token. Stored at issue; verified on consume."""
    return _ph.hash(token)


def verify_reset_token(token: str, hashed: str) -> bool:
    """Constant-time verify a reset token against its stored argon2 hash."""
    try:
        return _ph.verify(hashed, token)
    except VerifyMismatchError:
        return False


# --- Email-verification tokens (Email/SMTP workstream) ---------------------
#
# Verification is not credential-equivalent (it only flips
# users.email_verified), so — unlike reset — it needs no DB row. A signed,
# timestamped itsdangerous token carrying {uid, email} is sufficient:
# single-use is enforced by the flag itself (re-verifying an already-verified
# account is idempotent), and binding the email means a subsequent address
# change silently invalidates an outstanding link.

_email_verify_serializer = URLSafeTimedSerializer(
    _settings.session_secret.get_secret_value(),
    salt="ownchart-email-verify",
)


def make_email_verification_token(user_id: str, email: str) -> str:
    """Sign a {uid, email} verification token. The signature covers both
    fields, so the token can't be replayed against a different account or
    survive an email change."""
    return _email_verify_serializer.dumps({"uid": str(user_id), "email": email})


def read_email_verification_token(
    token: str, *, max_age: int,
) -> dict | None:
    """Verify + decode a verification token. Returns the {uid, email}
    payload, or None when the signature is bad or the token is older than
    ``max_age`` seconds."""
    try:
        payload = _email_verify_serializer.loads(token, max_age=max_age)
    except (SignatureExpired, BadSignature):
        return None
    if not isinstance(payload, dict) or "uid" not in payload or "email" not in payload:
        return None
    return payload
