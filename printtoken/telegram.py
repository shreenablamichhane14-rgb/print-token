"""Telegram intake by long polling. The laptop asks Telegram for new messages and Telegram never
connects in, so nothing on this PC is open to the internet.

Telegram has already proven who sent each message (the numeric user id is not something a sender
can fake), so the bot only checks that id against an allowlist. The PIN is a second factor for
minting email addresses, because an address works for anyone who holds it.

One exception to the silence strangers get: a guest key. The owner mints one with /invite, forwards
it, and the one account that sends it back may print exactly once. The key is deleted from the
chat like the PIN, every answer to a guest is counted, and a wrong key gets no answer at all.
"""
from __future__ import annotations

import asyncio
import html
import logging
import time
from dataclasses import dataclass
from datetime import datetime

import httpx

from . import convert, notes
from .config import Config
from .messages import describe
from .pins import PinGuard
from .pipeline import JobResult, Pipeline, PrintJob
from .policy import AttemptLimiter, RateLimiter
from .store import Store
from .tokens import KeyRecord, MintRefused, Tokens, Verdict, display_key, find_key, mask

API = "https://api.telegram.org"
log = logging.getLogger("printtoken.telegram")

DRAFT_TTL = 600     # seconds a typed note waits for /go before it is forgotten


@dataclass
class Draft:
    """A typed note already rendered to PDF, waiting for the owner to answer /go."""
    pdf: bytes
    pages: int
    words: int
    created: float


def _command(text: str) -> str:
    """The /command word of a message in lower case, without any @botname suffix, so that /go
    matches exactly and a message like /gone does not. Plain text gives ""."""
    if not text.startswith("/"):
        return ""
    return text.split(maxsplit=1)[0].partition("@")[0].lower()


def _pages(n: int) -> str:
    return f"{n} page{'s' if n != 1 else ''}"


def help_text(mail_enabled: bool, guest_enabled: bool = True) -> str:
    lines = ["Print Token",
             "Send a PDF, PNG, JPEG or .txt here as a File (paperclip, then File) and it prints at home."]
    if mail_enabled:
        lines.append("/print PIN  gives you an email address that prints one email, once, within 30 minutes.")
    else:
        lines.append("Email printing is coming soon.")
    if guest_enabled:        # the command menu hides /invite in the same case, so the help agrees with it
        lines.append("/invite PIN note  makes a one-time guest key for one outside person.")
    lines.append("/status  shows the queue and the printer.")
    lines.append("Or type any text here and it prints as a clean page (you confirm with /go).")
    return "\n".join(lines)


GUEST_PRINTS_PER_HOUR = 5       # guests have their own budget, separate from the owner's
GUEST_REPLIES_PER_HOUR = 6      # answers to a guest who is not sending a file, per account
GUEST_FAILURES_PER_KEY = 3      # failed prints before a key is closed
GUEST_FILES_PER_HOUR = 10       # files looked at for one guest account, printable or not, then silence


def when(ts: float) -> str:
    """'Monday, November 9, 3:15 PM EST' in the gateway machine's own time zone. Written without
    %-d (Windows has no such code), and a zone name the system spells out is cut to initials."""
    dt = datetime.fromtimestamp(ts).astimezone()
    zone = dt.strftime("%Z")
    if " " in zone:
        zone = "".join(word[0] for word in zone.split())
    clock = dt.strftime("%I:%M %p").lstrip("0")
    return f"{dt:%A}, {dt:%B} {dt.day}, {clock} {zone}".rstrip()


def invite_text(tag: str, expires: float, username: str | None, page_cap: int,
                address: str | None = None, html_mode: bool = False) -> str:
    """The block the owner forwards to the guest. Nothing in it is meant for the owner, so it goes
    out exactly as it is. On the guest's phone a forwarded message is labelled with the bot's name,
    so the owner is "the person who sent you this key", never "me". In HTML mode the key and the
    address are code spans, which Telegram copies to the clipboard with one tap and keeps when the
    message is forwarded."""
    key = display_key(tag)

    def code(s: str) -> str:
        return f"<code>{html.escape(s)}</code>" if html_mode else s

    def text(s: str) -> str:
        return html.escape(s) if html_mode else s

    if username:
        how = (f"Tap https://t.me/{username}?start={key} and press Start. If nothing happens, send the key "
               f"to the bot as a message.")
    else:
        how = "Open the Print Token bot in Telegram, press Start, then send the key as a message."
    lines = [f"Print Token guest key: {code(key)}",
             text(how),
             text(f"Then send your file as a File (paperclip, then File): a PDF or a picture, up to {page_cap} "
                  f"pages. Word files: export to PDF first."),
             text(f"Good for one print until {when(expires)}. If the bot does not answer within a minute, "
                  f"tell the person who sent you this key.")]
    if address:
        lines.append(f"Or email the file to {code(address)} from your own email address, one email only. "
                     f"You will not get an email back; the person who sent you this key gets the confirmation "
                     f"and will let you know.")
    return "\n".join(lines)


class TelegramError(Exception):
    def __init__(self, code: int, description: str):
        super().__init__(f"Telegram error {code}: {description}")
        self.code = code
        self.description = description


class TelegramClient:
    def __init__(self, token: str, client: httpx.AsyncClient):
        self._base = f"{API}/bot{token}"
        self._files = f"{API}/file/bot{token}"
        self._client = client

    async def call(self, method: str, http_timeout: float = 30, **params):
        resp = await self._client.post(f"{self._base}/{method}", json=params, timeout=http_timeout)
        try:
            data = resp.json()
        except ValueError:
            raise TelegramError(resp.status_code, "response was not JSON") from None
        if not data.get("ok"):
            raise TelegramError(data.get("error_code", resp.status_code), data.get("description", ""))
        return data["result"]

    async def send(self, chat_id: int, text: str, parse_mode: str | None = None) -> int | None:
        """Returns the new message id, or None when Telegram could not be reached. A message
        with markup Telegram rejects is sent again as plain text rather than lost."""
        params: dict = dict(chat_id=chat_id, text=text, disable_web_page_preview=True)
        if parse_mode:
            params["parse_mode"] = parse_mode
        try:
            try:
                result = await self.call("sendMessage", **params)
            except TelegramError:
                if not parse_mode:
                    raise
                params.pop("parse_mode")
                result = await self.call("sendMessage", **params)
            return result.get("message_id")
        except (httpx.HTTPError, TelegramError) as exc:
            log.warning("sendMessage failed: %s", exc.__class__.__name__)
            return None

    async def delete(self, chat_id: int, message_id: int) -> bool:
        try:
            await self.call("deleteMessage", chat_id=chat_id, message_id=message_id)
            return True
        except (httpx.HTTPError, TelegramError) as exc:
            log.info("deleteMessage failed: %s", exc)
            return False

    async def send_document(self, chat_id: int, filename: str, data: bytes, caption: str = "") -> dict:
        resp = await self._client.post(f"{self._base}/sendDocument",
                                       data={"chat_id": str(chat_id), "caption": caption},
                                       files={"document": (filename, data, "application/pdf")}, timeout=60)
        payload = resp.json()
        if not payload.get("ok"):
            raise TelegramError(payload.get("error_code", resp.status_code), payload.get("description", ""))
        return payload["result"]

    async def download(self, file_id: str) -> bytes:
        info = await self.call("getFile", file_id=file_id)
        # The file URL contains the bot token, so its errors are reported without the URL.
        resp = await self._client.get(f"{self._files}/{info['file_path']}", timeout=120)
        if resp.status_code != 200:
            raise TelegramError(resp.status_code, "file download failed")
        return resp.content


class Bot:
    def __init__(self, cfg: Config, tg: TelegramClient, tokens: Tokens, pins: PinGuard,
                 pipeline: Pipeline, store: Store, limiter: RateLimiter):
        self.cfg = cfg
        self.tg = tg
        self.tokens = tokens
        self.pins = pins
        self.pipeline = pipeline
        self.store = store
        self.limiter = limiter
        self.key_guard = AttemptLimiter(cfg.guest_bad_keys_per_hour)    # wrong guest keys, per stranger
        self.reply_guard = AttemptLimiter(GUEST_REPLIES_PER_HOUR)       # chatter answered, per guest
        self.guest_limiter = RateLimiter(GUEST_PRINTS_PER_HOUR)         # never the owner's budget
        self.file_guard = AttemptLimiter(GUEST_FILES_PER_HOUR)         # files looked at, per guest account
        self.guest_failures: dict[str, int] = {}                        # failed prints per key
        self.username: str | None = None                                # set by app.run from getMe
        self.help = help_text(cfg.mail_enabled, cfg.guest_enabled)
        self.drafts: dict[int, Draft] = {}

    async def run(self) -> None:
        offset = int(self.store.kv_get("tg_offset", "0"))
        backoff = 1
        while True:
            try:
                updates = await self.tg.call("getUpdates", http_timeout=65, offset=offset, timeout=50,
                                             allowed_updates=["message"])
                backoff = 1
            except TelegramError as exc:
                if exc.code == 409:
                    # Another program is long polling the same bot token. Telegram only allows one.
                    log.error("another program is using this bot token: %s", exc.description)
                    self.store.log_event(channel="telegram", kind="poll", outcome="error",
                                         reason="token_conflict", detail=exc.description)
                    await asyncio.sleep(30)
                else:
                    log.warning("getUpdates failed: %s", exc)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                continue
            except httpx.HTTPError as exc:
                log.warning("getUpdates network error: %s", exc.__class__.__name__)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            for update in updates:
                # Saved before handling: a crash mid-message skips that message rather than
                # printing it twice after a restart.
                offset = update["update_id"] + 1
                self.store.kv_set("tg_offset", offset)
                message = update.get("message")
                if not message:
                    continue
                try:
                    await self.handle(message)
                except Exception:
                    log.exception("handling message failed")

    async def handle(self, msg: dict) -> None:
        chat = msg["chat"]
        user = (msg.get("from") or {}).get("id")
        if chat.get("type") != "private":
            return
        if user not in self.cfg.allowed_tg_ids:
            await self.stranger(msg, user, chat["id"])
            return
        text = (msg.get("text") or "").strip()
        if text.startswith("/print"):
            await self.mint(msg, user, chat["id"], text)
        elif text.startswith("/invite"):
            await self.invite(msg, user, chat["id"], text)
        elif "document" in msg:
            await self.document(msg, user, chat["id"])
        elif "photo" in msg:
            self._reject(user, "photo_compressed")
            await self.tg.send(chat["id"], "Telegram compresses photos, so they print blurry. Send it again as "
                                           "a File (paperclip, then File) and it prints at full quality.")
        elif text.startswith("/status"):
            await self.status(chat["id"])
        elif _command(text) == "/go":
            await self.go(msg, user, chat["id"])
        elif _command(text) == "/cancel":
            await self.cancel(user, chat["id"])
        elif text and not text.startswith("/"):
            # Typed text becomes a draft page. Nothing prints until /go, so a stray message can
            # never print by itself.
            await self.note(user, chat["id"], text)
        else:
            await self.tg.send(chat["id"], self.help)

    def _reject(self, user: int, reason: str, detail: str | None = None, kind: str = "message") -> None:
        self.store.log_event(channel="telegram", kind=kind, outcome="rejected", reason=reason,
                             sender=f"tg:{user}", detail=detail)

    async def mint(self, msg: dict, user: int, chat: int, text: str) -> None:
        parts = text.split(maxsplit=1)
        pin = parts[1].strip() if len(parts) > 1 else ""
        # Telegram lets a bot delete incoming messages in a private chat, so the PIN does not sit
        # in the chat history.
        await self.tg.delete(chat, msg["message_id"])
        if not self.cfg.mail_enabled:
            await self.tg.send(chat, "Email printing isn't set up yet. For now, send the file here as a "
                                     "File (paperclip, then File) and it prints.")
            return
        if not self.pins.configured:
            await self.tg.send(chat, "No PIN is set on the gateway yet, so /print is off. "
                                     "On the laptop run: python -m printtoken setpin")
            return
        if self.pins.locked(user):
            self._reject(user, "pin_locked", kind="mint")
            await self.tg.send(chat, "Too many wrong PINs. Try again in an hour.")
            return
        if not self.pins.verify(user, pin):
            self._reject(user, "bad_pin", kind="mint")
            await self.tg.send(chat, "Wrong PIN. Use /print followed by your PIN.")
            return
        try:
            tag, expires = self.tokens.mint(f"tg:{user}")
        except MintRefused as exc:
            self._reject(user, "mint_refused", str(exc), kind="mint")
            await self.tg.send(chat, f"No new address: {exc}.")
            return
        until = datetime.fromtimestamp(expires).strftime("%I:%M %p").lstrip("0")
        self.store.log_event(channel="telegram", kind="mint", outcome="ok", sender=f"tg:{user}",
                             detail=f"tag={mask(tag)}")
        await self.tg.send(chat, f"Print address, works for one email until {until}:\n"
                                 f"{self.cfg.address_for(tag)}")

    async def document(self, msg: dict, user: int, chat: int) -> None:
        doc = msg["document"]
        name = doc.get("file_name") or "file"
        size = doc.get("file_size") or 0
        if size > self.cfg.max_bytes_telegram:
            self._reject(user, "oversize", f"{size} bytes", kind="print")
            await self.tg.send(chat, f"{name} is {size / 1048576:.1f} MB. Telegram only lets bots download "
                                     f"files up to {self.cfg.max_bytes_telegram // 1048576} MB.")
            return
        if not self.limiter.allow():
            self._reject(user, "rate_limited", kind="print")
            await self.tg.send(chat, "Hourly print limit reached, try again later.")
            return
        try:
            data = await self.tg.download(doc["file_id"])
        except (httpx.HTTPError, TelegramError) as exc:
            self._reject(user, "download_failed", exc.__class__.__name__, kind="print")
            await self.tg.send(chat, "Could not download that file from Telegram, send it again.")
            return
        job = PrintJob(channel="telegram", sender=f"tg:{user}", filename=name, data=data,
                       sent_ts=float(msg["date"]), received_ts=float(msg["date"]),
                       picked_ts=time.time(), reply_chat=chat)
        if not self.pipeline.submit(job):
            await self.tg.send(chat, "The print queue is full, try again in a few minutes.")
            return
        ahead = self.pipeline.queue.qsize() - 1
        await self.tg.send(chat, f"Got {name}, printing." + (f" {ahead} job(s) ahead of it." if ahead > 0 else ""))

    # --- guests ------------------------------------------------------------------------------

    @property
    def guest_cap(self) -> int:
        return min(self.cfg.guest_page_cap, self.cfg.page_cap)

    @staticmethod
    def _who(msg: dict, user: int) -> str:
        """'Telegram user 123 (Name)' for the owner's messages. The name is whatever the account
        typed into Telegram, so it is cut short and kept on one line."""
        name = " ".join(str((msg.get("from") or {}).get("first_name") or "").split())[:40]
        return f"Telegram user {user}" + (f" ({name})" if name else "")

    @staticmethod
    def _label(rec: KeyRecord | None, tag: str = "") -> str:
        """'Guest key "the guest from work"' for the owner's messages: the note is quoted because
        it is free text and would otherwise run into the sentence; without a note, the mask."""
        if rec and rec.note:
            return f'Guest key "{rec.note}"'
        return f"Guest key {mask(rec.tag if rec else tag)}"

    def _welcome(self, expires: float) -> str:
        return (f"Key accepted, I removed it from the chat. Now send your file (again, if you sent it before "
                f"the key): tap the paperclip, choose File, and pick a PDF or a picture, up to {self.guest_cap} "
                f"pages. A photo works too. Word files will not print, export them to PDF first. "
                f"This key is good for one print until {when(expires)}.")

    @staticmethod
    def _key_problem(verdict: Verdict, rec: KeyRecord | None) -> str:
        if verdict is Verdict.RESERVED:
            return ("One file at a time: your first file is still going through. If it fails you will get a "
                    "message and can send another.")
        if verdict is Verdict.USED:
            return "This key already did its one print. Ask the owner for a new key if you need another."
        if verdict is Verdict.EXPIRED:
            on = f" on {when(rec.expires)}" if rec else ""
            return f"This key expired{on}. Ask the owner for a new one."
        if verdict is Verdict.CLAIMED:
            return "That key was already claimed by another account. Ask the owner for a new one."
        return "That key cannot be used right now. Ask the owner for a new one."

    async def _guest_reply(self, user: int, chat: int, text: str) -> None:
        """An answer to a guest who is not sending a file: a few an hour, then silence, so a
        chatty account cannot keep the bot talking."""
        if self.reply_guard.locked(user):
            return
        self.reply_guard.fail(user)
        await self.tg.send(chat, text)

    async def stranger(self, msg: dict, user: int, chat: int) -> None:
        """Everything from an account that is not on the allowlist. Silence is the rule; the only
        things answered are a guest key and the account that redeemed one."""
        who = f"tg:{user}"
        if self.cfg.guest_enabled:
            text = (msg.get("text") or msg.get("caption") or "").strip()
            key = find_key(text)
            current = self.tokens.claim_for(who)
            if current and current.state in ("claimed", "printing"):
                if "document" in msg or "photo" in msg:
                    await self.guest_document(msg, user, chat, current)
                elif key and key != current.tag and find_key(text, bare=False):
                    await self.claim_key(msg, user, chat, key)      # a second key from the owner
                elif key == current.tag:
                    await self.tg.delete(chat, msg["message_id"])
                    await self._guest_reply(user, chat, "Key already accepted. Send the file as a File "
                                                        "(paperclip, then File), or as a photo.")
                else:
                    await self._guest_reply(user, chat, "I am waiting for a file. Tap the paperclip, choose "
                                                        "File, and send a PDF or a picture (not a Word file).")
                return
            if key:
                await self.claim_key(msg, user, chat, key)
                return
            if current:                 # their key is used or expired: one line, so they know why
                verdict = Verdict.USED if current.state == "used" else Verdict.EXPIRED
                await self._guest_reply(user, chat, self._key_problem(verdict, current))
                return
        # No reply at all, so a stranger learns nothing about what the bot does.
        self.store.log_event(channel="telegram", kind="message", outcome="rejected",
                             reason="not_allowlisted", sender=who)

    async def claim_key(self, msg: dict, user: int, chat: int, key: str) -> None:
        who = f"tg:{user}"
        if self.key_guard.locked(user):
            self._reject(user, "guest_locked", kind="guest")
            return
        verdict = self.tokens.claim(key, who)
        if verdict is Verdict.OK:
            # The key message goes the way of the PIN message: out of the chat history.
            await self.tg.delete(chat, msg["message_id"])
            rec = self.tokens.get(key)
            note = (rec.note if rec else None) or ""
            self.store.log_event(channel="telegram", kind="guest", outcome="claimed", sender=who,
                                 detail=f"tag={mask(key)} note={note}")
            await self.tg.send(chat, self._welcome(rec.expires if rec else time.time()))
            await self.notify_owner(f"{self._label(rec, key)} was claimed by {self._who(msg, user)}.")
            if rec:
                await self._drop_invite(rec)
            if "document" in msg or "photo" in msg:         # the key and the file in one message
                await self.guest_document(msg, user, chat, self.tokens.get(key) or rec)
            return
        self._reject(user, verdict.value, kind="guest")
        rec = self.tokens.get(key)
        if rec is not None and rec.kind == "guest" and rec.claimed_by == who:
            # Their own key, now used or expired: say so, and it is not a strike.
            await self._guest_reply(user, chat, self._key_problem(verdict, rec))
            return
        if verdict is Verdict.UNKNOWN:
            # A guess. Counted, and silence: a stranger learns nothing from a wrong key.
            self.key_guard.fail(user)
            if self.key_guard.locked(user):
                await self.notify_owner(f"{self._who(msg, user)} sent {self.key_guard.max_per_hour} wrong "
                                        f"guest keys and is ignored for an hour.")
            return
        # A key the owner really issued, from an account it does not fit: one short line.
        await self._guest_reply(user, chat, self._key_problem(verdict, rec))

    @staticmethod
    def _unprintable(name: str, kind: str) -> str | None:
        """Advice for a file that will never print, decided from its first bytes before the key
        is touched. Word is the likely first mistake, so it gets the fullest answer."""
        if kind == "zip":
            return (f"{name} is a Word file, and I can only print PDFs and pictures for now. In Word, tap the "
                    f"three dots, choose Export (or Share) and pick PDF, then send that PDF here as a File. "
                    f"A clear photo of the page works too. Your key is still unused.")
        if kind == "heic":
            return (f"{name} is an iPhone photo in HEIC form, which I cannot print. Send it as a photo instead "
                    f"(paperclip, then the picture itself). Your key is still unused.")
        if kind in ("unknown", "empty"):
            return f"{name} is not something I can print (PDF, PNG, JPEG or plain text). Your key is still unused."
        return None

    async def guest_document(self, msg: dict, user: int, chat: int, key: KeyRecord) -> None:
        """One file from a guest. It is downloaded and looked at before the key is held, so a Word
        file, an empty file or a download hiccup leaves the key exactly as it was. Every file is a
        download of up to 20 MB, so one account gets a bounded number an hour, printable or not,
        and silence after that; the owner is told once, so they can revoke the key."""
        who = f"tg:{user}"
        if key.state == "printing":
            await self._guest_reply(user, chat, self._key_problem(Verdict.RESERVED, key))
            return
        if self.file_guard.locked(user):
            self._reject(user, "too_many_files", kind="guest")
            return
        self.file_guard.fail(user)
        if self.file_guard.locked(user):
            await self.notify_owner(f"{self._label(key)}: {self._who(msg, user)} has sent {GUEST_FILES_PER_HOUR} "
                                    f"files in an hour. Further files from that account are ignored for the "
                                    f"rest of the hour.")
        if "document" in msg:
            doc = msg["document"]
            name = doc.get("file_name") or "file"
        else:
            doc = msg["photo"][-1]      # the largest size Telegram made, a JPEG
            name = "photo.jpg"
        size = doc.get("file_size") or 0
        if size > self.cfg.max_bytes_telegram:
            self._reject(user, "oversize", f"{size} bytes", kind="guest")
            await self.tg.send(chat, f"{name} is {size / 1048576:.1f} MB and Telegram only lets bots download "
                                     f"files up to {self.cfg.max_bytes_telegram // 1048576} MB. "
                                     f"Your key is still unused.")
            return
        try:
            data = await self.tg.download(doc["file_id"])
        except (httpx.HTTPError, TelegramError) as exc:
            self._reject(user, "download_failed", exc.__class__.__name__, kind="guest")
            await self.tg.send(chat, "Could not download that file from Telegram, send it again. "
                                     "Your key is still unused.")
            return
        kind = convert.sniff(data)
        advice = self._unprintable(name, kind)
        if advice:
            self._reject(user, "unsupported_type", kind, kind="guest")
            await self.tg.send(chat, advice)
            return
        verdict = self.tokens.reserve(key.tag, who)
        if verdict is not Verdict.OK:
            self._reject(user, verdict.value, kind="guest")
            await self.tg.send(chat, self._key_problem(verdict, self.tokens.get(key.tag)))
            return
        if not self.guest_limiter.allow():
            self.tokens.release(key.tag)
            self._reject(user, "rate_limited", kind="guest")
            await self.tg.send(chat, f"Guest prints are limited to {GUEST_PRINTS_PER_HOUR} an hour and that "
                                     f"limit is reached. Try again later, your key is still unused.")
            return
        job = PrintJob(channel="telegram", sender=f"guest:{who}", filename=name, data=data,
                       sent_ts=float(msg["date"]), received_ts=float(msg["date"]), picked_ts=time.time(),
                       reply_chat=chat, page_cap=self.guest_cap, guest_tag=key.tag)
        if not self.pipeline.submit(job):
            self.tokens.release(key.tag)
            await self.tg.send(chat, "The print queue is full, try again in a few minutes. Your key is still unused.")
            return
        await self.tg.send(chat, f"Got {name}, checking it now.")

    def _guest_problem(self, job: PrintJob, result: JobResult) -> str:
        name = job.filename
        if result.reason == "over_page_cap":
            cap = job.page_cap if job.page_cap is not None else self.cfg.page_cap
            if result.pages:
                return (f"{name} has {result.pages} pages and a guest key covers {cap}. Send a shorter file or "
                        f"just the pages you need.")
            return (f"{name} is longer than a guest key covers ({cap} pages). Send a shorter file or just the "
                    f"pages you need.")
        text = f"Did not print {name}: {describe(result.reason)}"
        if result.detail and result.reason == "unsupported_type":
            text += f" ({result.detail})"
        return text + "."

    async def _guest_result(self, job: PrintJob, result: JobResult) -> None:
        """The one print a guest key pays for. The key is spent or released before any message goes
        out, so a Telegram hiccup can never leave it held."""
        tag = job.guest_tag or ""
        rec = self.tokens.get(tag)
        label = self._label(rec, tag)
        who = job.sender.removeprefix("guest:")
        shown = f"Telegram user {who[3:]}" if who.startswith("tg:") else f"email from {who.removeprefix('mail:')}"
        pages = f"{result.pages} page{'s' if result.pages != 1 else ''}"
        dryrun = bool(getattr(getattr(self.pipeline, "printer", None), "is_dryrun", False))
        if result.ok:
            self.tokens.spend(tag, who=who)         # records an email sender as the claimant
            self.guest_failures.pop(tag, None)
            self.store.log_event(channel=job.channel, kind="guest", outcome="spent", sender=job.sender,
                                 filename=job.filename, pages=result.pages, detail=f"tag={mask(tag)}")
            if rec:
                await self._drop_invite(rec)
            if job.reply_chat:
                if dryrun:
                    text = (f"Received {job.filename} ({pages}) and ran it through the gateway. "
                            f"The owner will confirm it came out.")
                else:
                    text = f"Printed {job.filename} ({pages}). Thanks for trying Print Token."
                await self.tg.send(job.reply_chat, text)
            await self.notify_owner(f"{label} printed {job.filename} ({pages}) for {shown}."
                                    + (" This was a dry run." if dryrun else ""))
            return
        failures = self.guest_failures.get(tag, 0) + 1
        closed = failures >= GUEST_FAILURES_PER_KEY
        if closed:
            self.tokens.spend(tag, who=who)
            self.guest_failures.pop(tag, None)
            self.store.log_event(channel=job.channel, kind="guest", outcome="closed", reason="too_many_failures",
                                 sender=job.sender, detail=f"tag={mask(tag)} failures={failures}")
            if rec:
                await self._drop_invite(rec)
        else:
            self.guest_failures[tag] = failures
            self.tokens.release(tag)
        if job.reply_chat:
            if closed:
                tail = (f"That was failed try number {failures}, so this key is now closed. "
                        f"Ask the owner for a new one.")
            elif result.reason in ("unsupported_type", "over_page_cap", "oversize"):
                tail = "Your key is still unused, fix that and send it again."
            else:
                tail = "Your key is still unused. The owner has been told, please try again in a few minutes."
            await self.tg.send(job.reply_chat, f"{self._guest_problem(job, result)} {tail}")
        detail = f" ({result.detail})" if result.detail else ""
        state = "The key is now closed after too many failed prints." if closed else "The key is open again."
        await self.notify_owner(f"{label}: did not print {job.filename} for {shown}: "
                                f"{describe(result.reason)}{detail}. {state}")

    async def _drop_invite(self, rec: KeyRecord) -> None:
        """Remove the owner's copy of the invite once the key is claimed or finished. Telegram lets
        a bot delete its own message for 48 hours; after that the owner deletes it by hand."""
        if not rec.invite_msg:
            return
        chat_s, _, mid_s = rec.invite_msg.partition(":")
        if chat_s.lstrip("-").isdigit() and mid_s.isdigit():
            await self.tg.delete(int(chat_s), int(mid_s))
        self.tokens.set_invite_msg(rec.tag, None)

    async def invite(self, msg: dict, user: int, chat: int, text: str) -> None:
        """/invite PIN note: a one-time guest key for one outside person. The same PIN rules as
        /print, and the message is deleted so the PIN does not sit in the chat. No email check:
        the key works on Telegram alone, and the address line is added only when mail is on."""
        parts = text.split(maxsplit=2)
        pin = parts[1].strip() if len(parts) > 1 else ""
        note = parts[2].strip() if len(parts) > 2 else ""
        await self.tg.delete(chat, msg["message_id"])
        if not self.cfg.guest_enabled:
            await self.tg.send(chat, "Guest keys are turned off in config.toml ([guest] enabled = false).")
            return
        if not self.pins.configured:
            await self.tg.send(chat, "No PIN is set on the gateway yet, so /invite is off. "
                                     "On the laptop run: python -m printtoken setpin")
            return
        if self.pins.locked(user):
            self._reject(user, "pin_locked", kind="invite")
            await self.tg.send(chat, "Too many wrong PINs. Try again in an hour.")
            return
        if not self.pins.verify(user, pin):
            self._reject(user, "bad_pin", kind="invite")
            await self.tg.send(chat, "Wrong PIN. Use /invite, your PIN, then a short note about the guest.")
            return
        try:
            tag, expires = self.tokens.mint_guest(f"tg:{user}", note, self.cfg.guest_key_days)
        except MintRefused as exc:
            self._reject(user, "mint_refused", str(exc), kind="invite")
            await self.tg.send(chat, f"No new key: {exc}.")
            return
        self.store.log_event(channel="telegram", kind="guest", outcome="minted", sender=f"tg:{user}",
                             detail=f"tag={mask(tag)} note={note}")
        if not self.username:
            log.warning("bot username unknown, the invite carries no deep link")
        address = self.cfg.address_for(tag) if self.cfg.mail_enabled else None
        block = invite_text(tag, expires, self.username, self.guest_cap, address, html_mode=True)
        sent = await self.tg.send(chat, block, parse_mode="HTML")
        if sent:
            self.tokens.set_invite_msg(tag, f"{chat}:{sent}")
        await self.tg.send(chat, f"Forward the message above to your guest. I remove it from this chat once the "
                                 f"key is claimed or used, which Telegram allows for two days; after that delete "
                                 f"it yourself. Note: {note or 'none'}. You will get a message here when the key "
                                 f"is claimed and when it prints. The gateway has to be running for the key to "
                                 f"answer.")

    async def status(self, chat: int) -> None:
        last = self.store.query("SELECT filename, ts FROM events WHERE outcome = 'printed' "
                                "ORDER BY id DESC LIMIT 1")
        last_text = (f"{last[0][0]} at {datetime.fromtimestamp(last[0][1]).strftime('%b %d %I:%M %p')}"
                     if last else "nothing yet")
        await self.tg.send(chat, f"Queue: {self.pipeline.queue.qsize()} waiting.\n"
                                 f"Live addresses: {self.tokens.outstanding()}.\n"
                                 f"Last print: {last_text}.\nPrinter: {self.cfg.printer}")

    async def on_result(self, job: PrintJob, result: JobResult) -> None:
        if job.guest_tag:
            await self._guest_result(job, result)
            return
        chat = job.reply_chat or self.cfg.notify_chat_id
        if not chat:
            return
        if result.ok:
            text = f"Printed {job.filename} ({result.pages} page{'s' if result.pages != 1 else ''})."
        else:
            text = f"Did not print {job.filename}: {describe(result.reason)}."
            if result.detail and result.reason in ("over_page_cap", "unsupported_type"):
                text += f" ({result.detail})"
        if job.channel == "email":
            text = f"Email from {job.sender}: {text}"
        await self.tg.send(chat, text)

    async def notify_owner(self, text: str) -> None:
        if self.cfg.notify_chat_id:
            await self.tg.send(self.cfg.notify_chat_id, text)

    def _draft(self, user: int) -> Draft | None:
        """The user's waiting note, or None once it is older than DRAFT_TTL: a late /go must not
        print something typed long ago and forgotten."""
        draft = self.drafts.get(user)
        if draft and time.time() - draft.created > DRAFT_TTL:
            self.drafts.pop(user, None)
            self.store.log_event(channel="telegram", kind="note", outcome="expired", sender=f"tg:{user}")
            return None
        return draft

    async def note(self, user: int, chat: int, text: str) -> None:
        """Typed text becomes a PDF draft that waits for /go. The layout runs in a thread so the
        bot keeps answering while PyMuPDF works."""
        try:
            pdf, pages = await asyncio.to_thread(notes.to_pdf, text, max_pages=self.cfg.page_cap)
        except convert.Unsupported as exc:
            self._reject(user, "bad_note", str(exc), kind="note")
            await self.tg.send(chat, f"Nothing saved: {exc}.")
            return
        except Exception as exc:
            # A layout crash is answered and logged like any other failure, never met with silence.
            log.exception("note layout failed")
            self._reject(user, "bad_note", exc.__class__.__name__, kind="note")
            await self.tg.send(chat, "Nothing saved: that text could not be laid out. Send it as a .txt file instead.")
            return
        words = len(text.split())
        self.drafts[user] = Draft(pdf, pages, words, time.time())
        self.store.log_event(channel="telegram", kind="note", outcome="drafted", sender=f"tg:{user}",
                             pages=pages, bytes=len(pdf), detail=f"{words} words")
        await self.tg.send(chat, f"Note saved: {words} words, {_pages(pages)}. Send /go to print it, "
                                 "/cancel to drop it, or send more text to replace it.")

    async def go(self, msg: dict, user: int, chat: int) -> None:
        draft = self._draft(user)
        if draft is None:
            self._reject(user, "no_draft", kind="note")
            await self.tg.send(chat, "No note waiting. Send the text again.")
            return
        if not self.limiter.allow():
            # The draft stays, so /go works again once the hour has moved on.
            self._reject(user, "rate_limited", kind="print")
            await self.tg.send(chat, "Hourly print limit reached, try again later.")
            return
        name = f"note-{datetime.now():%Y-%m-%d-%H%M}.pdf"
        job = PrintJob(channel="telegram", sender=f"tg:{user}", filename=name, data=draft.pdf,
                       sent_ts=float(msg["date"]), received_ts=float(msg["date"]),
                       picked_ts=time.time(), reply_chat=chat)
        if not self.pipeline.submit(job):
            await self.tg.send(chat, "The print queue is full, try again in a few minutes.")
            return
        self.drafts.pop(user, None)
        ahead = self.pipeline.queue.qsize() - 1
        await self.tg.send(chat, f"Printing your note ({_pages(draft.pages)})."
                                 + (f" {ahead} job(s) ahead of it." if ahead > 0 else ""))

    async def cancel(self, user: int, chat: int) -> None:
        if self._draft(user) is None:
            await self.tg.send(chat, "No note waiting.")
            return
        self.drafts.pop(user, None)
        self.store.log_event(channel="telegram", kind="note", outcome="dropped", sender=f"tg:{user}")
        await self.tg.send(chat, "Dropped the note.")
