"""Password/PIN hashing, tokens and login rate limiting."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
from datetime import timedelta

from novaquest.core import clock
from novaquest.core.db import Tx
from novaquest.core.errors import RateLimited

def _pin_mmap_threshold() -> None:
    """glibc raises its mmap threshold after big blocks are freed, so later 16 MB scrypt buffers come from
    per-thread arenas and are never returned to the OS. Pinning the threshold (128 KB) makes every large
    buffer an mmap that is released on free. Best effort: only on glibc."""
    try:
        import ctypes
        import ctypes.util

        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
        libc.mallopt(-3, 128 * 1024)  # M_MMAP_THRESHOLD
        libc.mallopt(-8, 2)           # M_ARENA_MAX
    except (OSError, AttributeError):
        pass


_pin_mmap_threshold()
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1
# Each scrypt call uses 128*N*r bytes (16 MB). Cap concurrent hashing so a whole class signing in at once
# costs at most 4 x 16 MB, and logins can't starve other requests of CPU.
_HASH_SLOTS = threading.BoundedSemaphore(4)


def _scrypt(secret: str, salt: bytes) -> bytes:
    with _HASH_SLOTS:
        return hashlib.scrypt(secret.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)


def hash_secret(secret: str) -> str:
    """Hash a password or PIN with scrypt and a random salt."""
    salt = secrets.token_bytes(16)
    digest = _scrypt(secret, salt)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_secret(secret: str, stored: str | None) -> bool:
    """Check a password or PIN against its stored hash in constant time."""
    if not stored or not stored.startswith("scrypt$"):
        return False
    _, salt_b64, digest_b64 = stored.split("$", 2)
    digest = _scrypt(secret, base64.b64decode(salt_b64))
    return hmac.compare_digest(digest, base64.b64decode(digest_b64))


def new_pin() -> str:
    """Generate a 4-digit PIN without easy patterns like 1111 or 1234."""
    while True:
        pin = f"{secrets.randbelow(10000):04d}"
        if len(set(pin)) > 1 and pin not in "0123456789" and pin not in "9876543210":
            return pin


def new_token(nbytes: int = 32) -> str:
    """Generate a URL-safe random token."""
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    """Hash a bearer token for storage (tokens are high-entropy, so SHA-256 suffices)."""
    return hashlib.sha256(token.encode()).hexdigest()


def check_password_strength(password: str) -> str | None:
    """Return a message if the password is too weak, else None."""
    if len(password) < 10:
        return "Use at least 10 characters."
    if password.lower() == password or password.upper() == password:
        return "Mix upper- and lower-case letters."
    if not any(c.isdigit() for c in password):
        return "Include at least one number."
    return None


def assert_not_locked(tx: Tx, login_key: str) -> None:
    """Raise RateLimited if this login key is locked after repeated failures."""
    row = tx.one("SELECT locked_until FROM login_failures WHERE login_key = %s", [login_key])
    if row and row["locked_until"] and row["locked_until"] > clock.now():
        mins = max(1, int((row["locked_until"] - clock.now()).total_seconds() // 60) + 1)
        raise RateLimited(f"Too many wrong attempts. Try again in {mins} min, or ask your teacher.")


def record_login_failure(tx: Tx, login_key: str, max_failures: int, lock_minutes: int) -> None:
    """Count a failed login; lock the key after too many failures."""
    now = clock.now()
    row = tx.one("SELECT failures FROM login_failures WHERE login_key = %s", [login_key])
    failures = (row["failures"] if row else 0) + 1
    locked = now + timedelta(minutes=lock_minutes) if failures >= max_failures else None
    tx.exec(
        """INSERT INTO login_failures(login_key, failures, locked_until, updated_at) VALUES (%s, %s, %s, %s)
           ON CONFLICT (login_key) DO UPDATE SET failures = EXCLUDED.failures,
             locked_until = EXCLUDED.locked_until, updated_at = EXCLUDED.updated_at""",
        [login_key, 0 if locked else failures, locked, now],
    )


def clear_login_failures(tx: Tx, login_key: str) -> None:
    """Reset the failure counter after a successful login."""
    tx.exec("DELETE FROM login_failures WHERE login_key = %s", [login_key])
