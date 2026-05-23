import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from itsdangerous import BadSignature, URLSafeSerializer

from .config import get_settings

_ph = PasswordHasher()
_settings = get_settings()
_serializer = URLSafeSerializer(_settings.session_secret.get_secret_value(), salt="ownchart-session")


def hash_password(plain: str) -> str:
    return _ph.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _ph.verify(hashed, plain)
    except VerifyMismatchError:
        return False


def sign_session(payload: dict) -> str:
    return _serializer.dumps(payload)


def unsign_session(token: str) -> dict | None:
    try:
        return _serializer.loads(token)
    except BadSignature:
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
