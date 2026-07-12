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
