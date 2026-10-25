"""The decision for the email channel, kept as one pure function so it can be tested on its own.

Order matters. The token is checked first (cheap, and most junk stops there), then the sender is
authenticated, and only THEN is the token burned. If the burn came first, anyone who saw a live
address could burn it with a forged message and the real sender would be locked out.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from .mailparse import Attachment, ParsedMail
from .tokens import Tokens, Verdict, is_tag_shaped

MAX_ATTACHMENTS = 5


@dataclass
class Decision:
    ok: bool
    reason: str
    tag: str | None = None
    files: list[Attachment] = field(default_factory=list)


def decide_mail(mail: ParsedMail, tokens: Tokens, dmarc_accept: frozenset[str],
                sender_allowlist: frozenset[str] = frozenset(), now: float | None = None) -> Decision:
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
