"""AES-256-GCM at-rest encryption for OAuth tokens (and other sensitive blobs).

The data encryption key (DEK) is loaded from `OWNCHART_TOKEN_DEK` (base64
32 bytes). Each ciphertext is `nonce(12) || tag(16) || ciphertext` → kept as
a single bytea column.

Doctrine: tokens are PHI-equivalent (give access to PHI). Plain logging of
token values is forbidden by the PHI-safe logger redactor; encryption at
rest is the second layer.
"""

from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import get_settings

_NONCE_LEN = 12


class TokenCryptoError(RuntimeError):
    pass


def _load_key() -> bytes:
    s = get_settings()
    if s.token_dek is None:
        raise TokenCryptoError(
            "OWNCHART_TOKEN_DEK is not set. Run deploy.sh which generates it, "
            "or set it manually as base64-encoded 32 bytes."
        )
    raw = base64.b64decode(s.token_dek.get_secret_value())
    if len(raw) != 32:
        raise TokenCryptoError(
            f"OWNCHART_TOKEN_DEK must decode to 32 bytes (got {len(raw)})"
        )
    return raw


def encrypt(plaintext: str | bytes) -> bytes:
    """Encrypt to a single bytes blob (nonce || ciphertext+tag)."""
    if isinstance(plaintext, str):
        plaintext = plaintext.encode("utf-8")
    key = _load_key()
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, plaintext, associated_data=None)
    return nonce + ct


def decrypt(blob: bytes | None) -> bytes | None:
    if not blob:
        return None
    if len(blob) <= _NONCE_LEN:
        raise TokenCryptoError("ciphertext blob too short")
    nonce, ct = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    key = _load_key()
    try:
        return AESGCM(key).decrypt(nonce, ct, associated_data=None)
    except InvalidTag as e:
        raise TokenCryptoError("AES-GCM tag mismatch — wrong DEK or corrupted ciphertext") from e


def decrypt_str(blob: bytes | None) -> str | None:
    raw = decrypt(blob)
    return raw.decode("utf-8") if raw is not None else None


def generate_dek_b64() -> str:
    """Generate a fresh DEK as base64; for use in deploy.sh / one-shot setup."""
    return base64.b64encode(os.urandom(32)).decode("ascii")
