"""The decision for the email channel, kept as one pure function so it can be tested on its own.

Order matters. The token is checked first (cheap, and most junk stops there), then the sender is
authenticated, and only THEN is the token burned. If the burn came first, anyone who saw a live
address could burn it with a forged message and the real sender would be locked out.

A guest key arriving by email (box+<12 characters>@...) goes through the same first two steps and
is then held for one print rather than burned, after its one attachment has been looked at, so a
Word file or an empty email costs the guest nothing. The sender allowlist does not apply to a
guest: the key is the permission, and the DMARC check has already run.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

from . import convert
from .mailparse import Attachment, ParsedMail
from .tokens import Tokens, Verdict, is_tag_shaped

MAX_ATTACHMENTS = 5
PRINTABLE = ("pdf", "png", "jpeg", "text")


@dataclass
class Decision:
    ok: bool
    reason: str
    tag: str | None = None
    files: list[Attachment] = field(default_factory=list)
    guest: bool = False             # the tag is a guest key, held (not burned) for one print


def decide_mail(mail: ParsedMail, tokens: Tokens, dmarc_accept: frozenset[str],
                sender_allowlist: frozenset[str] = frozenset(), now: float | None = None,
                guest_enabled: bool = True) -> Decision:
    now = time.time() if now is None else now
    if not mail.tags:
        return Decision(False, "no_tag")
    tag = next((t for t in mail.tags if is_tag_shaped(t)), None)
    if tag is None:
        return Decision(False, "malformed_tag", mail.tags[0])
    verdict = tokens.check(tag, now)
    if verdict is not Verdict.OK:
        return Decision(False, verdict.value, tag)
    dmarc = mail.auth.get("dmarc", "missing")
    if dmarc not in dmarc_accept:
        return Decision(False, f"dmarc_{dmarc}", tag)
    record = tokens.get(tag, now)
    if record is not None and record.kind == "guest":
        return _decide_guest(mail, tokens, tag, now, guest_enabled)
    if sender_allowlist and mail.from_addr not in sender_allowlist:
        return Decision(False, "sender_not_allowlisted", tag)
    verdict = tokens.burn(tag, now)
    if verdict is not Verdict.OK:
        return Decision(False, verdict.value, tag)
    files = [a for a in mail.attachments if a.data]
    if not files:
        return Decision(False, "no_attachment", tag)
    if len(files) > MAX_ATTACHMENTS:
        return Decision(False, "too_many_attachments", tag)
    return Decision(True, "accepted", tag, files)


def _decide_guest(mail: ParsedMail, tokens: Tokens, tag: str, now: float, guest_enabled: bool) -> Decision:
    """A guest key by email: one attachment, looked at before the key is held."""
    if not guest_enabled:
        return Decision(False, "guest_off", tag)
    files = [a for a in mail.attachments if a.data]
    if not files:
        return Decision(False, "no_attachment", tag)
    if convert.sniff(files[0].data) not in PRINTABLE:
        return Decision(False, "unsupported_type", tag)
    verdict = tokens.reserve(tag, f"mail:{mail.from_addr}", now, claimed_only=False)
    if verdict is not Verdict.OK:
        return Decision(False, verdict.value, tag)
    return Decision(True, "accepted", tag, files[:1], guest=True)


class RateLimiter:
    """At most `limit` events in any rolling hour."""

    def __init__(self, limit: int, window: float = 3600):
        self.limit = limit
        self.window = window
        self._times: deque[float] = deque()

    def allow(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        while self._times and self._times[0] <= now - self.window:
            self._times.popleft()
        if len(self._times) >= self.limit:
            return False
        self._times.append(now)
        return True


class AttemptLimiter:
    """At most `max_per_hour` recorded events per user in any rolling hour, after which that user
    is locked until the oldest event ages out. One structure for wrong PINs (PinGuard), wrong
    guest keys, and how often the bot answers a guest who is not sending a file."""

    def __init__(self, max_per_hour: int, window: float = 3600):
        self.max_per_hour = max_per_hour
        self.window = window
        self._events: dict[int | str, deque[float]] = defaultdict(deque)

    def _recent(self, user: int | str, now: float) -> deque[float]:
        q = self._events[user]
        while q and q[0] <= now - self.window:
            q.popleft()
        return q

    def count(self, user: int | str, now: float | None = None) -> int:
        now = time.time() if now is None else now
        return len(self._recent(user, now))

    def locked(self, user: int | str, now: float | None = None) -> bool:
        return self.count(user, now) >= self.max_per_hour

    def fail(self, user: int | str, now: float | None = None) -> None:
        """Record one event for this user."""
        now = time.time() if now is None else now
        self._recent(user, now).append(now)
