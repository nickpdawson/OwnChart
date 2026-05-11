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
