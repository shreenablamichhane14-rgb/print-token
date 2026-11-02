"""Expiring single-use print addresses and one-time guest keys.

A mail token is 40 random bits written as 8 lowercase base32 characters. Lowercase because Gmail
treats addresses case-insensitively, and base32 because it has no 0, 1, 8 or 9 for people to
misread. A guest key is 12 characters from the same alphabet (60 bits), shown to people as
PT-ABCD-EFGH-IJKL and stored lowercase without the dashes. Burning a token, claiming a key and
holding it for a print are each one UPDATE with a WHERE clause, so two messages racing for the
same row cannot both win.
"""
from __future__ import annotations

import base64
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum

from .store import Store

ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
KEY_LEN = 12
TAG_RE = re.compile(r"^(?:[a-z2-7]{8}|[a-z2-7]{12})$")
# A key written inside other text: the PT prefix, then three groups of four with up to three
# separator characters between them (a dash of any kind, a space, the period a phone adds).
KEY_IN_TEXT = re.compile(r"(?i)\bpt[^a-z2-7]{0,3}([a-z2-7]{4})[^a-z2-7]{0,3}([a-z2-7]{4})"
                         r"[^a-z2-7]{0,3}([a-z2-7]{4})\b")
_NOT_ALPHABET = re.compile(r"[^a-z2-7]")
# A key on its own, without the PT prefix: the twelve characters in one run, or three groups of
# four with a separator between each. Nothing looser counts, because a message that is only
# twelve letters ("will do thanks") would otherwise be taken for a wrong key, and a wrong key is
# a strike against the account.
BARE_KEY = re.compile(r"(?i)^(?:[a-z2-7]{12}|[a-z2-7]{4}[^a-z2-7]{1,3}[a-z2-7]{4}[^a-z2-7]{1,3}[a-z2-7]{4})\.?$")


class Verdict(str, Enum):
    OK = "ok"
    UNKNOWN = "unknown_tag"
    EXPIRED = "expired_tag"
    USED = "replayed_tag"
    CLAIMED = "key_claimed"         # a guest key already redeemed by a different account
    RESERVED = "key_in_use"         # a guest key whose one print is in progress


class MintRefused(Exception):
    """No new address or key right now (too many live, or the hourly limit)."""


@dataclass(frozen=True)
class KeyRecord:
    """One row of the tokens table plus its state at the moment it was read."""
    tag: str
    kind: str                       # "mail" for an address, "guest" for a key
    note: str | None
    created: float
    expires: float
    claimed_by: str | None
    claimed: float | None
    reserved: float | None
    used: float | None
    invite_msg: str | None
    state: str                      # live, claimed, printing, used or expired


_COLUMNS = "tag, kind, note, created, expires, claimed_by, claimed, reserved, used, invite_msg"


def _record(row: tuple, now: float) -> KeyRecord:
    tag, kind, note, created, expires, claimed_by, claimed, reserved, used, invite_msg = row
    if used is not None:
        state = "used"
    elif expires <= now:
        state = "expired"
    elif reserved is not None:
        state = "printing"
    elif claimed_by:
        state = "claimed"
    else:
        state = "live"
    return KeyRecord(tag, kind, note, created, expires, claimed_by, claimed, reserved, used,
                     invite_msg, state)


def new_tag() -> str:
    return base64.b32encode(secrets.token_bytes(5)).decode("ascii").lower()


def new_key() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(KEY_LEN))


def is_tag_shaped(tag: str) -> bool:
    return bool(TAG_RE.match(tag))


def display_key(tag: str) -> str:
    """The form people see: PT-ABCD-EFGH-IJKL."""
    body = tag.upper()
    return "PT-" + "-".join(body[i:i + 4] for i in range(0, len(body), 4))


def mask(tag: str | None) -> str:
    """The first four characters and dots, which is all a log or a listing needs."""
    return (tag or "")[:4] + "..."


def normalise(text: str) -> str:
    """What a person typed, reduced to the stored form: lowercase, nothing outside the alphabet,
    a zero read as the letter o, and the PT prefix dropped only when a whole key follows it (one
    key in a thousand starts with pt itself)."""
    body = _NOT_ALPHABET.sub("", text.lower().replace("0", "o"))
    if len(body) == KEY_LEN + 2 and body.startswith("pt"):
        body = body[2:]
    return body


def is_key_shaped(text: str) -> bool:
    """True when the whole message is a key: dashed or not, with or without PT, any case."""
    return len(normalise(text)) == KEY_LEN


def find_key(text: str, bare: bool = True) -> str | None:
    """The key inside a message, or None. The PT form is found anywhere in the text (a forwarded
    invite, a /start deep link payload, a key typed with a sentence around it). With `bare`, a
    message that is nothing but the twelve characters, in one run or in groups of four, counts
    as well; a sentence that happens to hold twelve letters does not."""
    match = KEY_IN_TEXT.search(text.replace("0", "o"))
    if match:
        return "".join(match.groups()).lower()
    if bare:
        body = normalise(text)
        if len(body) == KEY_LEN and BARE_KEY.match(text.strip().replace("0", "o")):
            return body
    return None


class Tokens:
    def __init__(self, store: Store, ttl_min: int = 30, max_outstanding: int = 3, mints_per_hour: int = 3,
                 max_guest_live: int = 2):
        self.store = store
        self.ttl = ttl_min * 60
        self.max_outstanding = max_outstanding
        self.mints_per_hour = mints_per_hour
        self.max_guest_live = max_guest_live

    def mint(self, who: str, now: float | None = None) -> tuple[str, float]:
        now = time.time() if now is None else now
        live = self.outstanding(now)
        if live >= self.max_outstanding:
            raise MintRefused(f"{live} addresses are still live, use one or wait for them to expire")
        self._check_hourly(now)
        expires = now + self.ttl
        for _ in range(5):
            tag = new_tag()
            try:
                self.store.write("INSERT INTO tokens(tag, created, expires, minted_by, kind) "
                                 "VALUES(?, ?, ?, ?, 'mail')", (tag, now, expires, who))
                return tag, expires
            except sqlite3.IntegrityError:
                continue
        raise MintRefused("could not make a unique address")

    def mint_guest(self, who: str, note: str, days: int, now: float | None = None) -> tuple[str, float]:
        """A key for one outside person: one print, `days` to use it."""
        now = time.time() if now is None else now
        live = self.guest_live(now)
        if live >= self.max_guest_live:
            # Read on a phone: there is no /revoke in Telegram, closing a key is a laptop command.
            them = "one" if live != 1 else "it"
            raise MintRefused(f"{live} guest key{'s are' if live != 1 else ' is'} still live, wait for {them} "
                              f"to expire or close {them} from the laptop (python -m printtoken revoke KEY)")
        self._check_hourly(now)
        expires = now + days * 86400
        for _ in range(5):
            tag = new_key()
            try:
                self.store.write("INSERT INTO tokens(tag, created, expires, minted_by, kind, note) "
                                 "VALUES(?, ?, ?, ?, 'guest', ?)", (tag, now, expires, who, note.strip() or None))
                return tag, expires
            except sqlite3.IntegrityError:
                continue
        raise MintRefused("could not make a unique key")

    def _check_hourly(self, now: float) -> None:
        # One window for addresses and guest keys together: both are things the owner hands out.
        recent = self.store.scalar("SELECT COUNT(*) FROM tokens WHERE created > ?", (now - 3600,))
        if recent >= self.mints_per_hour:
            raise MintRefused(f"limit is {self.mints_per_hour} new addresses or keys an hour")

    def check(self, tag: str, now: float | None = None) -> Verdict:
        """Is the token live? Does not burn it. A guest key held for a print answers RESERVED."""
        now = time.time() if now is None else now
        rows = self.store.query("SELECT expires, used, reserved FROM tokens WHERE tag = ?", (tag.lower(),))
        if not rows:
            return Verdict.UNKNOWN
        expires, used, reserved = rows[0]
        if used is not None:
            return Verdict.USED
        if expires <= now:
            return Verdict.EXPIRED
        if reserved is not None:
            return Verdict.RESERVED
        return Verdict.OK

    def burn(self, tag: str, now: float | None = None) -> Verdict:
        """Mark the token used. Only one caller can ever get OK for a given token, and a guest key
        mid-print on Telegram cannot be burned by email at the same time."""
        now = time.time() if now is None else now
        changed, _ = self.store.write(
            "UPDATE tokens SET used = ? WHERE tag = ? AND used IS NULL AND reserved IS NULL AND expires > ?",
            (now, tag.lower(), now))
        if changed == 1:
            return Verdict.OK
        verdict = self.check(tag, now)
        return Verdict.USED if verdict is Verdict.OK else verdict

    def outstanding(self, now: float | None = None) -> int:
        """Live email addresses. Guest keys live for days and are counted on their own."""
        now = time.time() if now is None else now
        return self.store.scalar("SELECT COUNT(*) FROM tokens WHERE kind = 'mail' AND used IS NULL "
                                 "AND expires > ?", (now,))

    def guest_live(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        return self.store.scalar("SELECT COUNT(*) FROM tokens WHERE kind = 'guest' AND used IS NULL "
                                 "AND expires > ?", (now,))

    def get(self, tag: str, now: float | None = None) -> KeyRecord | None:
        now = time.time() if now is None else now
        rows = self.store.query(f"SELECT {_COLUMNS} FROM tokens WHERE tag = ?", (tag.lower(),))
        return _record(rows[0], now) if rows else None

    # --- guest keys ----------------------------------------------------------------------------

    def claim(self, tag: str, who: str, now: float | None = None) -> Verdict:
        """Bind a guest key to the account that sent it. One UPDATE, so when several accounts send
        the same key at once exactly one wins; the winner may send it again and still get OK. A
        key held for a print nobody claimed (an email print in progress) answers RESERVED."""
        now = time.time() if now is None else now
        changed, _ = self.store.write(
            "UPDATE tokens SET claimed_by = ?, claimed = COALESCE(claimed, ?) WHERE tag = ? AND kind = 'guest' "
            "AND used IS NULL AND reserved IS NULL AND expires > ? AND (claimed_by IS NULL OR claimed_by = ?)",
            (who, now, tag.lower(), now, who))
        if changed == 1:
            return Verdict.OK
        return self._why_not(tag, who, now)

    def reserve(self, tag: str, who: str, now: float | None = None, claimed_only: bool = True) -> Verdict:
        """Hold a guest key for one print. Telegram holds only a key the same account claimed.
        Email (claimed_only False) holds any live key, because there the key itself plus a DMARC
        pass is the permission. Holding is not claiming: the sender is recorded by spend() once
        the print succeeds, so a failed email attempt leaves the key open to the Telegram path."""
        now = time.time() if now is None else now
        if claimed_only:
            changed, _ = self.store.write(
                "UPDATE tokens SET reserved = ? WHERE tag = ? AND kind = 'guest' AND claimed_by = ? "
                "AND used IS NULL AND reserved IS NULL AND expires > ?", (now, tag.lower(), who, now))
        else:
            changed, _ = self.store.write(
                "UPDATE tokens SET reserved = ? WHERE tag = ? AND kind = 'guest' AND used IS NULL "
                "AND reserved IS NULL AND expires > ?", (now, tag.lower(), now))
        if changed == 1:
            return Verdict.OK
        return self._why_not(tag, who, now, claimed_only)

    def _why_not(self, tag: str, who: str, now: float, claimed_only: bool = True) -> Verdict:
        """Why a claim or a reservation changed no row."""
        rec = self.get(tag, now)
        if rec is None or rec.kind != "guest":
            return Verdict.UNKNOWN
        if rec.used is not None:
            return Verdict.USED
        if rec.expires <= now:
            return Verdict.EXPIRED
        if claimed_only and rec.claimed_by and rec.claimed_by != who:
            return Verdict.CLAIMED
        if rec.reserved is not None:
            return Verdict.RESERVED
        return Verdict.UNKNOWN

    def spend(self, tag: str, now: float | None = None, who: str | None = None) -> bool:
        """Mark a guest key used, after its print or to revoke it. No expiry check: a print that
        started before the expiry still completes. `who` is recorded as the claimant when nobody
        claimed the key before (an email print), so the listing says who spent it. Returns whether
        a row changed."""
        now = time.time() if now is None else now
        changed, _ = self.store.write(
            "UPDATE tokens SET used = ?, reserved = NULL, claimed_by = COALESCE(claimed_by, ?), "
            "claimed = COALESCE(claimed, ?) WHERE tag = ? AND used IS NULL",
            (now, who, now if who else None, tag.lower()))
        return changed == 1

    def release(self, tag: str) -> None:
        """Let go of a held key after a failed print, so the guest can send another file."""
        self.store.write("UPDATE tokens SET reserved = NULL WHERE tag = ? AND used IS NULL", (tag.lower(),))

    def release_all(self) -> int:
        """Drop every reservation. Safe only when no print is in flight, which is true at startup:
        the queue lives in memory, so a reservation that survived a restart belongs to a job that
        is gone and would otherwise keep the key dead on both channels until it expired."""
        changed, _ = self.store.write("UPDATE tokens SET reserved = NULL WHERE reserved IS NOT NULL AND used IS NULL")
        return changed

    def claim_for(self, who: str, now: float | None = None) -> KeyRecord | None:
        """The guest key this account redeemed: a live one if there is one, else the newest."""
        now = time.time() if now is None else now
        rows = self.store.query(
            f"SELECT {_COLUMNS} FROM tokens WHERE kind = 'guest' AND claimed_by = ? "
            "ORDER BY (used IS NULL AND expires > ?) DESC, claimed DESC, created DESC LIMIT 1", (who, now))
        return _record(rows[0], now) if rows else None

    def guest_keys(self, now: float | None = None) -> list[KeyRecord]:
        now = time.time() if now is None else now
        rows = self.store.query(f"SELECT {_COLUMNS} FROM tokens WHERE kind = 'guest' ORDER BY created DESC")
        return [_record(row, now) for row in rows]

    def set_invite_msg(self, tag: str, value: str | None) -> None:
        """Remember (or forget) the chat:message_id of the /invite reply that carries this key."""
        self.store.write("UPDATE tokens SET invite_msg = ? WHERE tag = ?", (value, tag.lower()))
