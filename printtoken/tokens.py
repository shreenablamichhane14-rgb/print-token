"""Expiring single-use print addresses.

A token is 40 random bits written as 8 lowercase base32 characters. Lowercase because Gmail treats
addresses case-insensitively, and base32 because it has no 0, 1, 8 or 9 for people to misread.
Burning a token is one UPDATE with a WHERE clause, so two messages racing for the same token
cannot both win.
"""
from __future__ import annotations

import base64
import re
import secrets
import sqlite3
import time
from enum import Enum

from .store import Store

TAG_RE = re.compile(r"^[a-z2-7]{8}$")


class Verdict(str, Enum):
    OK = "ok"
    UNKNOWN = "unknown_tag"
    EXPIRED = "expired_tag"
    USED = "replayed_tag"


class MintRefused(Exception):
    """No new address right now (too many live, or the hourly limit)."""


def new_tag() -> str:
    return base64.b32encode(secrets.token_bytes(5)).decode("ascii").lower()


def is_tag_shaped(tag: str) -> bool:
    return bool(TAG_RE.match(tag))


class Tokens:
    def __init__(self, store: Store, ttl_min: int = 30, max_outstanding: int = 3, mints_per_hour: int = 3):
        self.store = store
        self.ttl = ttl_min * 60
        self.max_outstanding = max_outstanding
        self.mints_per_hour = mints_per_hour

    def mint(self, who: str, now: float | None = None) -> tuple[str, float]:
        now = time.time() if now is None else now
        live = self.store.scalar("SELECT COUNT(*) FROM tokens WHERE used IS NULL AND expires > ?", (now,))
        if live >= self.max_outstanding:
            raise MintRefused(f"{live} addresses are still live, use one or wait for them to expire")
        recent = self.store.scalar("SELECT COUNT(*) FROM tokens WHERE created > ?", (now - 3600,))
        if recent >= self.mints_per_hour:
            raise MintRefused(f"limit is {self.mints_per_hour} new addresses an hour")
        expires = now + self.ttl
        for _ in range(5):
            tag = new_tag()
            try:
                self.store.write("INSERT INTO tokens(tag, created, expires, minted_by) VALUES(?, ?, ?, ?)",
                                 (tag, now, expires, who))
                return tag, expires
            except sqlite3.IntegrityError:
                continue
        raise MintRefused("could not make a unique address")

    def check(self, tag: str, now: float | None = None) -> Verdict:
        """Is the token live? Does not burn it."""
        now = time.time() if now is None else now
        rows = self.store.query("SELECT expires, used FROM tokens WHERE tag = ?", (tag.lower(),))
        if not rows:
            return Verdict.UNKNOWN
        expires, used = rows[0]
        if used is not None:
            return Verdict.USED
        if expires <= now:
            return Verdict.EXPIRED
        return Verdict.OK

    def burn(self, tag: str, now: float | None = None) -> Verdict:
        """Mark the token used. Only one caller can ever get OK for a given token."""
        now = time.time() if now is None else now
        changed, _ = self.store.write(
            "UPDATE tokens SET used = ? WHERE tag = ? AND used IS NULL AND expires > ?",
            (now, tag.lower(), now))
        if changed == 1:
            return Verdict.OK
        verdict = self.check(tag, now)
        return Verdict.USED if verdict is Verdict.OK else verdict

    def outstanding(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        return self.store.scalar("SELECT COUNT(*) FROM tokens WHERE used IS NULL AND expires > ?", (now,))
