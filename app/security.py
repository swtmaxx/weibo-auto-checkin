from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections.abc import Callable

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

_password_hasher = PasswordHasher()


class LoginThrottle:
    """In-memory failed-login counter that delays repeated attempts per client."""

    def __init__(
        self,
        *,
        base_delay: float = 1.0,
        max_delay: float = 15.0,
        window_seconds: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._window = window_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: dict[str, tuple[int, float]] = {}

    def delay_for(self, key: str) -> float:
        with self._lock:
            record = self._failures.get(key)
            if not record:
                return 0.0
            count, last_seen = record
            if self._clock() - last_seen > self._window:
                return 0.0
            return min(self._max_delay, self._base_delay * (2 ** (count - 1)))

    def record_failure(self, key: str) -> None:
        with self._lock:
            count, _ = self._failures.get(key, (0, 0.0))
            self._failures[key] = (count + 1, self._clock())

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


def secret_fingerprint(secret_key: str) -> str:
    """Short non-reversible fingerprint used to match configuration backups."""
    return hashlib.sha256(secret_key.encode("utf-8")).hexdigest()[:12]


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, password)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def same_secret(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return hmac.compare_digest(left, right)


def _fernet(secret_key: str) -> Fernet:
    digest = hashlib.sha256(secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(value: str, secret_key: str) -> str:
    return _fernet(secret_key).encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str, secret_key: str) -> str:
    try:
        return _fernet(secret_key).decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("stored Cookie cannot be decrypted") from exc


def encrypt_cookie(cookie: str, secret_key: str) -> str:
    return encrypt_secret(cookie, secret_key)


def decrypt_cookie(ciphertext: str, secret_key: str) -> str:
    return decrypt_secret(ciphertext, secret_key)
