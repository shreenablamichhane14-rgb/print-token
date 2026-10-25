"""Email intake. Checks the mailbox over IMAP every 30 seconds instead of holding an IDLE
connection: Python's imaplib has no IDLE, and half a minute is fine for printing.

imaplib blocks, so every IMAP call runs in a worker thread through asyncio.to_thread and the event
loop never waits on the network. Only the headers of new mail are downloaded first; the full
message is fetched only when it was sent to one of our +tag addresses.
"""
from __future__ import annotations

import asyncio
import imaplib
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

from . import mailparse
from .config import Config
from .messages import describe, should_notify_mail
from .pipeline import Pipeline, PrintJob
from .policy import RateLimiter, decide_mail
from .store import Store
from .tokens import Tokens

log = logging.getLogger("printtoken.mail")

MONTHS = {m: i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), start=1)}
_SIZE = re.compile(rb"RFC822\.SIZE (\d+)")
_IDATE = re.compile(rb'INTERNALDATE "([^"]+)"')
HEADER_FETCH = "(UID RFC822.SIZE INTERNALDATE BODY.PEEK[HEADER.FIELDS (TO CC DELIVERED-TO X-ORIGINAL-TO)])"


def internaldate_ts(value: bytes) -> float | None:
    """Parse an IMAP INTERNALDATE like b'24-Oct-2026 23:15:01 +0000'. Done by hand because
    strptime's %b follows the Windows language setting."""
    try:
        day, mon, rest = value.decode().strip().split("-", 2)
        year, clock, zone = rest.split(" ")
        hh, mm, ss = (int(x) for x in clock.split(":"))
        offset = (1 if zone[0] == "+" else -1) * (int(zone[1:3]) * 3600 + int(zone[3:5]) * 60)
        moment = datetime(int(year), MONTHS[mon], int(day), hh, mm, ss, tzinfo=timezone.utc)
        return moment.timestamp() - offset
    except (ValueError, KeyError, IndexError):
        return None


def _first(values) -> bytes | None:
    return values[0] if values and values[0] else None


def _literal(data) -> tuple[bytes, bytes]:
    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2:
            return item[0], item[1]
    return b"", b""


@dataclass
class FetchedMail:
    uid: int
    raw: bytes | None
    size: int
    internal_ts: float | None
    picked_ts: float
    oversize: bool = False


class Mailbox:
    """One IMAP connection, reopened when it drops. Used by one thread at a time."""

    def __init__(self, cfg: Config, store: Store, connect=imaplib.IMAP4_SSL):
        self.cfg = cfg
        self.store = store
        self._connect = connect
        self._imap: imaplib.IMAP4_SSL | None = None
        self._uidvalidity: str | None = None
        self._uidnext: int | None = None
        # The saved position belongs to one mailbox. Keyed by address, so switching to a different
        # Gmail account starts fresh instead of trusting the old account's message numbers.
        self._key = f"imap:{cfg.imap_user.lower()}"

    def connect(self) -> None:
        imap = self._connect(self.cfg.imap_host, 993, timeout=30)
        imap.login(self.cfg.imap_user, self.cfg.imap_password)
        typ, _ = imap.select("INBOX", readonly=True)
        if typ != "OK":
            raise imaplib.IMAP4.error("could not open INBOX")
        validity = _first(imap.response("UIDVALIDITY")[1])
        nxt = _first(imap.response("UIDNEXT")[1])
        if nxt is None:
            typ, data = imap.uid("SEARCH", None, "ALL")
            uids = [int(x) for x in (data[0] or b"").split()] if typ == "OK" else []
            nxt = str(max(uids, default=0) + 1).encode()
        self._uidvalidity = validity.decode() if validity else ""
        self._uidnext = int(nxt)
        self._imap = imap

    def close(self) -> None:
        if self._imap is not None:
            try:
                self._imap.logout()
            except Exception:
                pass
        self._imap = None

    def fetch_new(self) -> list[FetchedMail]:
        try:
            return self._fetch_new()
        except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError):
            self.close()        # next poll reconnects
            raise

    def _fetch_new(self) -> list[FetchedMail]:
        if self._imap is None:
            self.connect()
        imap = self._imap
        imap.noop()             # lets the server report mail that arrived since the last check
        saved_validity = self.store.kv_get(f"{self._key}:uidvalidity")
        last = self.store.kv_get(f"{self._key}:last_uid")
        if saved_validity != self._uidvalidity or last is None:
            # First run, or the server renumbered the mailbox. Start from now and never print old mail.
            self.store.kv_set(f"{self._key}:uidvalidity", self._uidvalidity)
            self.store.kv_set(f"{self._key}:last_uid", (self._uidnext or 1) - 1)
            log.info("mailbox position set, watching for new mail only")
            return []
        last_uid = int(last)
        typ, data = imap.uid("SEARCH", None, f"UID {last_uid + 1}:*")
        if typ != "OK":
            raise imaplib.IMAP4.error("UID SEARCH failed")
        # "UID n:*" always includes the newest message, even when n is past it, so filter again.
        uids = sorted(u for u in (int(x) for x in (data[0] or b"").split()) if u > last_uid)
        found: list[FetchedMail] = []
        for uid in uids:
            typ, data = imap.uid("FETCH", str(uid), HEADER_FETCH)
            meta, headers = _literal(data)
            if typ == "OK" and headers:
                msg = mailparse.parse_headers(headers)
                if mailparse.find_tags(msg, self.cfg.mail_local, self.cfg.mail_domain):
                    found.append(self._fetch_body(imap, uid, meta))
            self.store.kv_set(f"{self._key}:last_uid", uid)
        return found

    def _fetch_body(self, imap, uid: int, meta: bytes) -> FetchedMail:
        size_match, date_match = _SIZE.search(meta), _IDATE.search(meta)
        size = int(size_match.group(1)) if size_match else 0
        internal = internaldate_ts(date_match.group(1)) if date_match else None
        # Attachments travel base64 encoded, which makes them about a third bigger.
        if size > self.cfg.max_bytes_mail * 4 // 3 + 65536:
            return FetchedMail(uid, None, size, internal, time.time(), oversize=True)
        typ, data = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
        _, raw = _literal(data)
        return FetchedMail(uid, raw if typ == "OK" else None, size, internal, time.time())


class MailWatcher:
    def __init__(self, cfg: Config, store: Store, tokens: Tokens, pipeline: Pipeline,
                 notify_owner: Callable[[str], Awaitable[None]], limiter: RateLimiter):
        self.cfg = cfg
        self.store = store
        self.tokens = tokens
        self.pipeline = pipeline
        self.notify_owner = notify_owner
        self.limiter = limiter
        self.box = Mailbox(cfg, store)

    async def run(self) -> None:
        while True:
            started = time.time()
            try:
                for item in await asyncio.to_thread(self.box.fetch_new):
                    await self.handle(item)
                self.store.kv_set("imap_last_ok", time.time())
            except Exception as exc:
                log.warning("mailbox check failed: %s", exc.__class__.__name__)
            await asyncio.sleep(max(1.0, self.cfg.poll_seconds - (time.time() - started)))

    async def handle(self, item: FetchedMail) -> None:
        if item.oversize or item.raw is None:
            self.store.log_event(channel="email", kind="mail", outcome="rejected", reason="oversize",
                                 bytes=item.size, received_ts=item.internal_ts, picked_ts=item.picked_ts)
            await self.notify_owner(f"Rejected an email to a print address: {describe('oversize')}.")
            return
        mail = await asyncio.to_thread(mailparse.parse, item.raw, self.cfg.mail_local, self.cfg.mail_domain)
        decision = decide_mail(mail, self.tokens, self.cfg.dmarc_accept, self.cfg.sender_allowlist)
        common = dict(channel="email", sender=mail.from_addr, sent_ts=mail.date_ts,
                      received_ts=item.internal_ts, picked_ts=item.picked_ts)
        auth = ",".join(f"{k}={v}" for k, v in sorted(mail.auth.items())) or "none"
        if not decision.ok:
            self.store.log_event(kind="mail", outcome="rejected", reason=decision.reason,
                                 detail=f"tag={decision.tag} auth={auth}", **common)
            if should_notify_mail(decision.reason):
                await self.notify_owner(f"Rejected an email from {mail.from_addr or 'an unknown sender'}: "
                                        f"{describe(decision.reason)}.")
            return
        self.store.log_event(kind="mail", outcome="accepted", reason="accepted",
                             detail=f"tag={decision.tag} auth={auth} files={len(decision.files)}", **common)
        for att in decision.files:
            if not self.limiter.allow():
                self.store.log_event(kind="print", outcome="rejected", reason="rate_limited",
                                     filename=att.filename, **common)
                await self.notify_owner(f"Email from {mail.from_addr}: {att.filename} skipped, "
                                        f"{describe('rate_limited')}.")
                continue
            job = PrintJob(channel="email", sender=mail.from_addr, filename=att.filename, data=att.data,
                           sent_ts=mail.date_ts, received_ts=item.internal_ts, picked_ts=item.picked_ts)
            if not self.pipeline.submit(job):
                await self.notify_owner(f"Email from {mail.from_addr}: {att.filename} skipped, "
                                        f"{describe('queue_full')}.")
