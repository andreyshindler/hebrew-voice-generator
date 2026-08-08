"""Password hashing and session tokens, using only the standard library.

Passwords use :func:`hashlib.scrypt` - memory-hard, in the stdlib, and no
binary wheel to build. The stored format is
``scrypt$<n>$<r>$<p>$<salt_b64>$<dk_b64>``, so cost parameters can be raised
later and upgraded transparently on the next successful login.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Tuple

__all__ = [
    "hash_password",
    "verify_password",
    "needs_rehash",
    "new_session_token",
    "hash_session_token",
    "new_email_token",
    "new_csrf_token",
    "constant_time_equals",
    "DEFAULT_N",
    "DEFAULT_R",
    "DEFAULT_P",
]

DEFAULT_N = 1 << 15
DEFAULT_R = 8
DEFAULT_P = 1
_DKLEN = 32
_SALT_BYTES = 16

#: scrypt's memory use is roughly 128 * n * r bytes; guard against a stored
#: record (or a bad setting) asking for something absurd.
_MAX_N = 1 << 20

#: Long passwords cost time without adding entropy, and an unbounded input is
#: a cheap way to make the server do work.
MAX_PASSWORD_BYTES = 1024


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(raw: str) -> bytes:
    return base64.b64decode(raw.encode("ascii"))


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    if n <= 0 or (n & (n - 1)) != 0 or n > _MAX_N:
        raise ValueError("scrypt n must be a power of two within a sane range")
    encoded = password.encode("utf-8")[:MAX_PASSWORD_BYTES]
    # maxmem must cover 128 * n * r plus scrypt's own overhead.
    return hashlib.scrypt(
        encoded, salt=salt, n=n, r=r, p=p, dklen=_DKLEN, maxmem=256 * n * r + (1 << 20)
    )


def hash_password(
    password: str, *, n: int = DEFAULT_N, r: int = DEFAULT_R, p: int = DEFAULT_P
) -> str:
    """Hash a password into the storable ``scrypt$...`` string."""
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = _derive(password, salt, n, r, p)
    return f"scrypt${n}${r}${p}${_b64e(salt)}${_b64e(dk)}"


def _parse(encoded: str) -> Tuple[int, int, int, bytes, bytes]:
    parts = encoded.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        raise ValueError("unrecognised password hash format")
    n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
    return n, r, p, _b64d(parts[4]), _b64d(parts[5])


def verify_password(password: str, encoded: str) -> bool:
    """Check a password against a stored hash, in constant time."""
    try:
        n, r, p, salt, expected = _parse(encoded)
        candidate = _derive(password, salt, n, r, p)
    except (ValueError, TypeError, base64.binascii.Error):  # type: ignore[attr-defined]
        return False
    return hmac.compare_digest(candidate, expected)


def needs_rehash(
    encoded: str, *, n: int = DEFAULT_N, r: int = DEFAULT_R, p: int = DEFAULT_P
) -> bool:
    """True if the stored hash uses weaker parameters than the current ones."""
    try:
        cur_n, cur_r, cur_p, _, _ = _parse(encoded)
    except (ValueError, TypeError):
        return True
    return (cur_n, cur_r, cur_p) != (n, r, p)


def dummy_verify(*, n: int = DEFAULT_N, r: int = DEFAULT_R, p: int = DEFAULT_P) -> None:
    """Burn the same work as a real verification.

    Called when the email doesn't exist, so response timing doesn't reveal
    whether an account is registered.
    """
    _derive("dummy-password", b"\x00" * _SALT_BYTES, n, r, p)


def new_session_token() -> str:
    """A fresh opaque session token; only the client ever sees this value."""
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    """What actually goes in the database, so a DB leak yields no cookies."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_email_token() -> str:
    """A token for an emailed link. Only its SHA-256 is stored."""
    return secrets.token_urlsafe(32)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
