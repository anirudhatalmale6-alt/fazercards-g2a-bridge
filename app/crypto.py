"""Encryption for the one piece of data that is worth real money: the keys.

Purchased codes are written to the database encrypted with a Fernet key held in
``ENCRYPTION_KEY``.  A database dump on its own is therefore not a pile of
resellable game keys.

``fingerprint()`` is a keyed hash of the plaintext.  It lets us put a unique
index on the codes -- so the same code can never be delivered twice -- without
storing them in the clear.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

from cryptography.fernet import Fernet, InvalidToken

_ENV_KEY = "ENCRYPTION_KEY"


class EncryptionNotConfigured(RuntimeError):
    pass


def generate_key() -> str:
    """Print-and-paste helper for first deployment (``bridge keygen``)."""
    return Fernet.generate_key().decode()


def _fernet() -> Fernet:
    raw = os.environ.get(_ENV_KEY)
    if not raw:
        raise EncryptionNotConfigured(
            f"{_ENV_KEY} is not set. Generate one with `python -m app.cli keygen` "
            "and put it in your .env -- the bridge refuses to store keys in plaintext."
        )
    try:
        return Fernet(raw.encode())
    except (ValueError, TypeError) as exc:
        raise EncryptionNotConfigured(f"{_ENV_KEY} is not a valid Fernet key: {exc}") from exc


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ValueError(
            "Could not decrypt a stored key -- ENCRYPTION_KEY has changed since it was written"
        ) from exc


def fingerprint(plaintext: str) -> str:
    """Keyed digest of a code, safe to index and compare."""
    secret = os.environ.get(_ENV_KEY, "")
    if not secret:
        raise EncryptionNotConfigured(f"{_ENV_KEY} is not set")
    digest = hmac.new(secret.encode(), plaintext.strip().encode(), hashlib.sha256)
    return base64.b16encode(digest.digest()).decode().lower()[:64]
