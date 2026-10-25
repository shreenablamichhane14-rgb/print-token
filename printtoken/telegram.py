"""Telegram intake by long polling. The laptop asks Telegram for new messages and Telegram never
connects in, so nothing on this PC is open to the internet.

Telegram has already proven who sent each message (the numeric user id is not something a sender
can fake), so the bot only checks that id against an allowlist. The PIN is a second factor for
minting email addresses, because an address works for anyone who holds it.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

import httpx

from .config import Config
from .messages import describe
from .pins import PinGuard
from .pipeline import JobResult, Pipeline, PrintJob
from .policy import RateLimiter
from .store import Store
from .tokens import MintRefused, Tokens

API = "https://api.telegram.org"
log = logging.getLogger("printtoken.telegram")

def help_text(mail_enabled: bool) -> str:
    lines = ["Print Token",
             "Send a PDF, PNG, JPEG or .txt here as a File (paperclip, then File) and it prints at home."]
    if mail_enabled:
        lines.append("/print PIN  gives you an email address that prints one email, once, within 30 minutes.")
    else:
        lines.append("Email printing is coming soon.")
    lines.append("/status  shows the queue and the printer.")
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

    async def send(self, chat_id: int, text: str) -> None:
        try:
            await self.call("sendMessage", chat_id=chat_id, text=text, disable_web_page_preview=True)
        except (httpx.HTTPError, TelegramError) as exc:
            log.warning("sendMessage failed: %s", exc.__class__.__name__)

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
        self.help = help_text(cfg.mail_enabled)

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
            # No reply at all, so a stranger learns nothing about what the bot does.
            self.store.log_event(channel="telegram", kind="message", outcome="rejected",
                                 reason="not_allowlisted", sender=f"tg:{user}")
            return
        text = (msg.get("text") or "").strip()
        if text.startswith("/print"):
            await self.mint(msg, user, chat["id"], text)
        elif "document" in msg:
            await self.document(msg, user, chat["id"])
        elif "photo" in msg:
            self._reject(user, "photo_compressed")
            await self.tg.send(chat["id"], "Telegram compresses photos, so they print blurry. Send it again as "
                                           "a File (paperclip, then File) and it prints at full quality.")
        elif text.startswith("/status"):
            await self.status(chat["id"])
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
                             detail=f"tag={tag}")
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

    async def status(self, chat: int) -> None:
        last = self.store.query("SELECT filename, ts FROM events WHERE outcome = 'printed' "
                                "ORDER BY id DESC LIMIT 1")
        last_text = (f"{last[0][0]} at {datetime.fromtimestamp(last[0][1]).strftime('%b %d %I:%M %p')}"
                     if last else "nothing yet")
        await self.tg.send(chat, f"Queue: {self.pipeline.queue.qsize()} waiting.\n"
                                 f"Live addresses: {self.tokens.outstanding()}.\n"
                                 f"Last print: {last_text}.\nPrinter: {self.cfg.printer}")

    async def on_result(self, job: PrintJob, result: JobResult) -> None:
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
