"""Password hashing, opaque tokens, cookies, CSRF, and login throttling."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass

COOKIE_NAME = "hermes_portal_session"
CSRF_HEADER = "X-Portal-CSRF"

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SALT_BYTES = 16


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=0,
    )
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N,
        _SCRYPT_R,
        _SCRYPT_P,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n_s, r_s, p_s, salt_b64, digest_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected), maxmem=0
        )
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(actual, expected)


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)


def constant_time_equal(left: str, right: str) -> bool:
    if not left or not right:
        return False
    try:
        return hmac.compare_digest(left, right)
    except TypeError:
        return False


@dataclass
class _Bucket:
    failures: int
    window_started: float


class LoginLimiter:
    """Small in-process throttle. One portal worker is the MVP deployment model."""

    def __init__(self, *, max_attempts: int, window_seconds: int) -> None:
        self.max_attempts = max(1, int(max_attempts))
        self.window_seconds = max(1, int(window_seconds))
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                return True
            if now - bucket.window_started >= self.window_seconds:
                self._buckets.pop(key, None)
                return True
            return bucket.failures < self.max_attempts

    def fail(self, key: str) -> None:
        now = time.time()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None or now - bucket.window_started >= self.window_seconds:
                self._buckets[key] = _Bucket(1, now)
            else:
                bucket.failures += 1

    def reset(self, key: str) -> None:
        with self._lock:
            self._buckets.pop(key, None)
