"""Wires the pieces together and runs them on one asyncio event loop."""
from __future__ import annotations

import asyncio
import logging
import time
from logging.handlers import RotatingFileHandler

import httpx

from .config import Config
from .mailwatch import MailWatcher
from .pins import PinGuard
from .pipeline import Pipeline
from .policy import RateLimiter
from .printer import make_printer
from .store import Store
from .telegram import Bot, TelegramClient
from .tokens import Tokens

log = logging.getLogger("printtoken")


class Redact(logging.Filter):
    """Blank out secrets in every log line. The bot token is part of every Telegram URL, and
    libraries like httpx log URLs, so this runs on all records, not just ours."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [s for s in secrets if s and len(s) >= 6]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in self.secrets:
            message = message.replace(secret, "<redacted>")
        record.msg, record.args = message, ()
        return True


def setup_logging(cfg: Config, verbose: bool = False) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    redact = Redact([cfg.bot_token, cfg.imap_password, cfg.pin_hash])
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(cfg.data_dir / "printtoken.log", maxBytes=2_000_000, backupCount=3,
                                       encoding="utf-8")
    console = logging.StreamHandler()
    root = logging.getLogger()
    root.handlers.clear()
    for handler in (file_handler, console):
        handler.setFormatter(fmt)
        handler.addFilter(redact)
        root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    # httpx logs every request URL at INFO, and Telegram URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


async def heartbeat(store: Store, every: float = 60) -> None:
    """One row per minute while the gateway is running. Availability is counted from these, so a
    sleeping laptop shows up as downtime instead of being assumed away."""
    last = time.time()
    store.heartbeat(last)
    while True:
        await asyncio.sleep(every)
        now = time.time()
        if now - last > every * 2:
            store.log_event(channel="system", kind="gap", outcome="resumed",
                            detail=f"{now - last:.0f}s without a heartbeat (sleep or freeze)")
        store.heartbeat(now)
        last = now


async def run(cfg: Config) -> None:
    store = Store(cfg.data_dir / "printtoken.db")
    printer = make_printer(cfg.printer, cfg.data_dir / "dryrun")
    printer.check()
    tokens = Tokens(store, cfg.token_ttl_min, cfg.max_outstanding, cfg.mints_per_hour)
    pins = PinGuard(cfg.pin_hash, cfg.pin_failures_per_hour)
    limiter = RateLimiter(cfg.prints_per_hour)
    async with httpx.AsyncClient() as client:
        tg = TelegramClient(cfg.bot_token, client)
        me = await tg.call("getMe")
        pipeline = Pipeline(store, printer, cfg.page_cap, cfg.queue_size, cfg.workers)
        bot = Bot(cfg, tg, tokens, pins, pipeline, store, limiter)
        pipeline.notify = bot.on_result
        tasks = [pipeline.run(), bot.run(), heartbeat(store)]
        watcher = None
        if cfg.mail_enabled:
            watcher = MailWatcher(cfg, store, tokens, pipeline, bot.notify_owner, limiter)
            tasks.append(watcher.run())
        mailbox = cfg.imap_user if cfg.mail_enabled else "off"
        store.log_event(channel="system", kind="start", outcome="ok",
                        detail=f"bot=@{me.get('username')} printer={cfg.printer} email={'on' if watcher else 'off'}")
        log.info("running: bot @%s, email %s, printer %s", me.get("username"), mailbox, cfg.printer)
        try:
            await asyncio.gather(*tasks)
        finally:
            store.log_event(channel="system", kind="stop", outcome="ok")
            if watcher:
                watcher.box.close()
            pipeline.pool.shutdown(wait=False, cancel_futures=True)
            store.close()
