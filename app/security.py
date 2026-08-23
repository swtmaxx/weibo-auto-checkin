from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

_password_hasher = PasswordHasher()


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
