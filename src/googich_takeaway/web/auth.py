"""Passwords, sessions, the first-run setup token and login rate limiting.

- Passwords are hashed with Argon2id. Only the hash is stored.
- Session tokens are random, sent only in an HttpOnly cookie, and stored as SHA-256 hashes, so a
  copy of the database does not let anyone log in.
- Until a password is set, the setup page needs a one-time token printed in the server log, so
  whoever reaches the page first on the network cannot claim the install.
- Repeated failed logins from one address are slowed down with an increasing lockout.
"""

import hashlib
import hmac
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_PASSWORD = 12
MAX_PASSWORD = 1024  # bounds the hashing work an attacker can request
SESSION_TOKEN_BYTES = 32

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    if len(password) > MAX_PASSWORD:
        return False
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_problem(password: str, confirm: str) -> str | None:
    if password != confirm:
        return "The passwords do not match."
    if len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    if len(password) > MAX_PASSWORD:
        return f"Use at most {MAX_PASSWORD} characters."
    return None


def new_token() -> str:
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


HASHING_AT_ONCE = 2
"""Password checks allowed at the same time. Each takes 64 MiB, so a burst of logins cannot
exhaust a small container's memory."""
_hashing = threading.BoundedSemaphore(HASHING_AT_ONCE)


def verify_password_limited(stored_hash: str, password: str, wait: float = 10.0) -> bool | None:
    """``verify_password``, at most ``HASHING_AT_ONCE`` at a time. None if the server was too
    busy to check within ``wait`` seconds."""
    if not _hashing.acquire(timeout=wait):
        return None
    try:
        return verify_password(stored_hash, password)
    finally:
        _hashing.release()


@dataclass
class LoginThrottle:
    """Per-address lockout after repeated failures: 5 free tries, then 30 s doubling to 15 min.

    Every attempt counts as a failure the moment it starts, and stops counting only when it
    succeeds, so a burst of simultaneous guesses cannot all slip past the check."""

    free_attempts: int = 5
    first_lock: float = 30.0
    max_lock: float = 900.0
    forget_after: float = 86400.0
    clock: Callable[[], float] = time.monotonic
    _failures: dict[str, tuple[int, float, float]] = field(default_factory=dict)
    """address: (failed attempts, locked until, last attempt)."""
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _now(self) -> float:
        return self.clock()

    def attempt(self, address: str) -> float:
        """Start an attempt: 0 if allowed (and counted as failed until ``succeeded``), else the
        seconds to wait. The check and the count are one step."""
        with self._lock:
            now = self._now()
            count, until, _ = self._failures.get(address, (0, 0.0, 0.0))
            if until > now:
                return until - now
            count += 1
            until = 0.0
            if count > self.free_attempts:
                lock = min(self.first_lock * 2 ** (count - self.free_attempts - 1), self.max_lock)
                until = now + lock
            self._failures[address] = (count, until, now)
            if len(self._failures) > 1000:
                self._forget_old(now)
            return 0.0

    def _forget_old(self, now: float) -> None:
        for key, (_, until, last) in list(self._failures.items()):
            if until <= now and now - last > self.forget_after:
                del self._failures[key]

    def succeeded(self, address: str) -> None:
        with self._lock:
            self._failures.pop(address, None)
