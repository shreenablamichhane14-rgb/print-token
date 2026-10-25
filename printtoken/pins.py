"""The PIN that has to come with /print before the bot hands out an address.

Only a salted scrypt hash is stored. Wrong guesses are counted per Telegram user, and after too many
in an hour that user is locked out for the rest of the hour.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

N, R, P = 2 ** 14, 8, 1


def make_hash(pin: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(pin.encode(), salt=salt, n=N, r=R, p=P, dklen=32)
    return f"scrypt${N}${R}${P}${salt.hex()}${digest.hex()}"


def matches(pin: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(pin.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(bytes.fromhex(digest)))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got.hex(), digest)


class PinGuard:
    def __init__(self, stored_hash: str, max_failures_per_hour: int = 5):
        self.stored = stored_hash
        self.max_failures = max_failures_per_hour
        self._failures: dict[int, deque[float]] = defaultdict(deque)

    def _recent(self, user: int, now: float) -> deque[float]:
        q = self._failures[user]
        while q and q[0] <= now - 3600:
            q.popleft()
        return q

    def locked(self, user: int, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return len(self._recent(user, now)) >= self.max_failures

    @property
    def configured(self) -> bool:
        return bool(self.stored)

    def verify(self, user: int, pin: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if not self.configured or self.locked(user, now):
            return False
        if pin and matches(pin, self.stored):
            return True
        self._recent(user, now).append(now)
        return False
