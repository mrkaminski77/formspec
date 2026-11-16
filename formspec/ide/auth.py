"""Password hashing and signed-cookie session helpers for the FormSpec IDE."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time


def hash_password(password: str) -> str:
    """Hash `password` with a random salt using scrypt."""
    if len(password) < 4:
        raise ValueError("Password must be at least 4 characters long.")
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=16384,
        r=8,
        p=1,
        dklen=32,
    )
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify `password` against a stored `scrypt$<salt>$<hash>` string."""
    parts = stored_hash.split("$")
    if len(parts) != 3 or parts[0] != "scrypt":
        return False
    _, salt_hex, digest_hex = parts
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except ValueError:
        return False

    actual = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=16384,
        r=8,
        p=1,
        dklen=32,
    )
    return hmac.compare_digest(actual, expected)


class SessionManager:
    """Creates and verifies HMAC-SHA256 signed session tokens."""

    COOKIE_NAME = "formspec_session"

    def __init__(self, secret: str | None = None, ttl_seconds: int = 86400 * 7) -> None:
        raw_secret = secret or os.environ.get("FORMSPEC_SECRET") or secrets.token_hex(32)
        self._key = raw_secret.encode("utf-8")
        self.ttl_seconds = ttl_seconds

    def create_token(self, user_id: int) -> str:
        issued = int(time.time())
        payload = f"{user_id}:{issued}"
        sig = hmac.new(self._key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{payload}:{sig}"

    def verify_token(self, token: str | None) -> int | None:
        if not token:
            return None
        parts = token.split(":")
        if len(parts) != 3:
            return None
        uid_str, issued_str, sig = parts
        payload = f"{uid_str}:{issued_str}"
        expected = hmac.new(self._key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        try:
            uid = int(uid_str)
            issued = int(issued_str)
        except ValueError:
            return None
        if int(time.time()) - issued > self.ttl_seconds:
            return None
        return uid
