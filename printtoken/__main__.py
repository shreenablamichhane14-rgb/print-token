"""Command line.

    python -m printtoken run              start the gateway
    python -m printtoken selftest         check printer, mailbox and bot without printing anything
    python -m printtoken setpin           set the /print PIN (stores only a hash in ~/.env)
    python -m printtoken tgsetup          give the bot its name, description and command menu
    python -m printtoken tgtest           live Telegram round trip, finished with a dry run print
    python -m printtoken mint             make a print address locally (for testing the email path)
    python -m printtoken dryrun FILE      convert FILE and "print" it to a PDF in data/dryrun
    python -m printtoken report [--csv]   the measurement summary
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import imaplib
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from . import convert
from .config import Config, ConfigError, load
from .pins import make_hash
from .printer import DRYRUN, PrinterError, make_printer
from .report import export_csv, summary
from .store import Store
from .tokens import MintRefused, Tokens

BOT_NAME = "Print Token"
BOT_DESCRIPTION = ("Send me a PDF, picture or text file as a File and I print it at home. "
                   "Only works for allowed accounts.")
BOT_SHORT = "Prints files at home. Private bot."


def bot_commands(mail_enabled: bool) -> list[dict]:
    commands = [{"command": "status", "description": "Queue and printer"},
                {"command": "help", "description": "How to use this bot"}]
    if mail_enabled:
        commands.insert(0, {"command": "print", "description": "One-time email address (needs PIN)"})
    return commands


def cmd_run(args) -> int:
    from .app import run, setup_logging
    cfg = load()
    setup_logging(cfg, args.verbose)
    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        pass
    return 0


def cmd_selftest(args) -> int:
    import httpx
    cfg = load()
    ok = True

    def result(name: str, passed: bool, note: str = "") -> None:
        nonlocal ok
        ok &= passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}{': ' + note if note else ''}")

    try:
        make_printer(cfg.printer).check()
        result("printer installed", True, cfg.printer)
    except PrinterError as exc:
        result("printer installed", False, str(exc))
    if cfg.mail_enabled:
        try:
            imap = imaplib.IMAP4_SSL(cfg.imap_host, 993, timeout=20)
            imap.login(cfg.imap_user, cfg.imap_password)
            typ, data = imap.select("INBOX", readonly=True)
            imap.logout()
            result("mailbox login", typ == "OK", f"{data[0].decode()} messages")
        except Exception as exc:
            result("mailbox login", False, exc.__class__.__name__)
    else:
        print("  [SKIP] mailbox: email printing is turned off in config.toml")
    try:
        base = f"https://api.telegram.org/bot{cfg.bot_token}"
        me = httpx.get(f"{base}/getMe", timeout=15).json()
        hook = httpx.get(f"{base}/getWebhookInfo", timeout=15).json()
        result("bot token", me.get("ok", False), "@" + me.get("result", {}).get("username", "?"))
        result("no webhook set (long polling allowed)", not hook.get("result", {}).get("url"))
    except Exception as exc:
        result("bot token", False, exc.__class__.__name__)
    result("allowlist", bool(cfg.allowed_tg_ids), f"{len(cfg.allowed_tg_ids)} Telegram id(s)")
    if cfg.mail_enabled:
        result("PIN set for /print", bool(cfg.pin_hash), "" if cfg.pin_hash else "run: python -m printtoken setpin")
    print("ALL PASS" if ok else "SOMETHING FAILED")
    return 0 if ok else 1


def cmd_setpin(args) -> int:
    from dotenv import set_key
    cfg_env = Path(os.environ.get("PRINT_TOKEN_ENV") or Path.home() / ".env")
    pin = getpass.getpass("New PIN (6 or more digits): ")
    if len(pin) < 6:
        print("Too short.")
        return 1
    if getpass.getpass("Again: ") != pin:
        print("They did not match.")
        return 1
    set_key(str(cfg_env), args.key, make_hash(pin), quote_mode="never")
    print(f"Saved the hash as {args.key} in {cfg_env}. The PIN itself is not stored anywhere.")
    return 0


async def _tgsetup(cfg: Config) -> None:
    import httpx
    from .telegram import TelegramClient
    async with httpx.AsyncClient() as client:
        tg = TelegramClient(cfg.bot_token, client)
        await tg.call("setMyName", name=BOT_NAME)
        await tg.call("setMyDescription", description=BOT_DESCRIPTION)
        await tg.call("setMyShortDescription", short_description=BOT_SHORT)
        await tg.call("setMyCommands", commands=bot_commands(cfg.mail_enabled))
        me = await tg.call("getMe")
        commands = await tg.call("getMyCommands")
        print(f"bot @{me['username']} is now called {me['first_name']!r}")
        print("command menu:", ", ".join("/" + c["command"] for c in commands))


def cmd_tgsetup(args) -> int:
    asyncio.run(_tgsetup(load()))
    return 0


def _test_pdf() -> bytes:
    import pymupdf
    with pymupdf.open() as doc:
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 96), "Print Token self test", fontsize=24)
        page.insert_text((72, 130), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), fontsize=12)
        page.insert_text((72, 150), "Dry run only. This page is not printed on paper.", fontsize=12)
        return doc.tobytes()


async def _tgtest(cfg: Config) -> int:
    """Bot -> owner's chat -> back to the bot -> dry run print. Nothing reaches paper."""
    import httpx
    from .telegram import TelegramClient
    if not cfg.notify_chat_id:
        print("no notify chat id configured")
        return 1
    chat = cfg.notify_chat_id
    sent = _test_pdf()
    async with httpx.AsyncClient() as client:
        tg = TelegramClient(cfg.bot_token, client)
        me = await tg.call("getMe")
        await tg.call("sendMessage", chat_id=chat,
                      text="Print Token self test: the bot can reach you. A test file comes next, "
                           "and it only goes through a dry run, nothing prints on paper.")
        t0 = time.perf_counter()
        message = await tg.send_document(chat, "selftest.pdf", sent, caption="Self test file (dry run only)")
        t1 = time.perf_counter()
        received = await tg.download(message["document"]["file_id"])
        t2 = time.perf_counter()
        same = hashlib.sha256(sent).hexdigest() == hashlib.sha256(received).hexdigest()
        pdf, pages, kind = convert.to_pdf(received)
        info = make_printer(DRYRUN, cfg.data_dir / "dryrun").print_pdf(pdf, "tgtest selftest.pdf")
        t3 = time.perf_counter()
        verdict = "PASSED" if same else "FAILED (the downloaded file did not match)"
        await tg.call("sendMessage", chat_id=chat,
                      text=f"Self test {verdict}: uploaded {len(sent):,} bytes in {t1 - t0:.2f}s, "
                           f"downloaded them back in {t2 - t1:.2f}s, dry run print of {pages} page in "
                           f"{t3 - t2:.2f}s. Nothing was printed on paper.")
    print(f"bot @{me['username']} -> chat: upload {len(sent)} bytes in {t1 - t0:.2f}s")
    print(f"getFile download: {len(received)} bytes in {t2 - t1:.2f}s, sha256 match: {same}")
    print(f"dry run: {kind}, {pages} page(s), {t3 - t2:.2f}s, output {info['output']}")
    return 0 if same else 1


def cmd_tgtest(args) -> int:
    return asyncio.run(_tgtest(load()))


def cmd_mint(args) -> int:
    cfg = load()
    if not cfg.mail_enabled:
        print("Email printing is turned off in config.toml, so there is nothing to mint for.")
        return 1
    store = Store(cfg.data_dir / "printtoken.db")
    tokens = Tokens(store, cfg.token_ttl_min, cfg.max_outstanding, cfg.mints_per_hour)
    try:
        tag, expires = tokens.mint("local")
    except MintRefused as exc:
        print(f"Refused: {exc}")
        return 1
    store.log_event(channel="local", kind="mint", outcome="ok", sender="local", detail=f"tag={tag}")
    print(f"{cfg.address_for(tag)}  (expires {time.strftime('%I:%M %p', time.localtime(expires))})")
    return 0


def cmd_dryrun(args) -> int:
    cfg = load(need_secrets=False)
    data = Path(args.file).read_bytes()
    started = time.perf_counter()
    try:
        pdf, pages, kind = convert.to_pdf(data)
    except convert.Unsupported as exc:
        print(f"Rejected: {exc}")
        return 1
    converted = time.perf_counter()
    info = make_printer(DRYRUN, cfg.data_dir / "dryrun").print_pdf(pdf, f"dryrun {Path(args.file).name}")
    done = time.perf_counter()
    print(f"{kind}, {pages} page(s), convert {converted - started:.2f}s, spool {done - converted:.2f}s")
    print(f"output: {info['output']}")
    return 0


def cmd_report(args) -> int:
    cfg = load(need_secrets=False)
    store = Store(cfg.data_dir / "printtoken.db")
    since = time.time() - args.days * 86400 if args.days else None
    print(summary(store, since))
    if args.csv:
        n = export_csv(store, Path(args.csv))
        print(f"wrote {n} events to {args.csv}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="printtoken")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_run)
    sub.add_parser("selftest").set_defaults(func=cmd_selftest)
    p = sub.add_parser("setpin")
    p.add_argument("--key", default="PRINT_TOKEN_PIN_HASH")
    p.set_defaults(func=cmd_setpin)
    sub.add_parser("tgsetup").set_defaults(func=cmd_tgsetup)
    sub.add_parser("tgtest").set_defaults(func=cmd_tgtest)
    sub.add_parser("mint").set_defaults(func=cmd_mint)
    p = sub.add_parser("dryrun")
    p.add_argument("file")
    p.set_defaults(func=cmd_dryrun)
    p = sub.add_parser("report")
    p.add_argument("--days", type=float, default=0)
    p.add_argument("--csv")
    p.set_defaults(func=cmd_report)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"Config problem: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
