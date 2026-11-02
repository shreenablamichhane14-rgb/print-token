"""Command line.

    python -m printtoken run              start the gateway
    python -m printtoken selftest         check printer, mailbox and bot without printing anything
    python -m printtoken setpin           set the PIN for /print and /invite (stores only a hash in ~/.env)
    python -m printtoken tgsetup          give the bot its name, description and command menu
    python -m printtoken tgtest           live Telegram round trip, finished with a dry run print
    python -m printtoken mint             make a print address locally (for testing the email path)
    python -m printtoken guest            make a one-time guest key locally [--days N] [--note TEXT]
    python -m printtoken guests           list guest keys with their state
    python -m printtoken revoke KEY       close a guest key early (the key, or the first characters guests shows)
    python -m printtoken dryrun [--note] FILE   convert FILE (or lay it out as a typed note) and "print"
                                          it to a PDF in data/dryrun
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
from .tokens import KEY_LEN, KeyRecord, MintRefused, Tokens, display_key, mask, normalise

BOT_NAME = "Print Token"
# What a guest reads before pressing Start, so it has to make sense to someone holding a key.
BOT_DESCRIPTION = ("Prints files on the owner's home printer. If the owner gave you a key, press Start, "
                   "send the key here, then send your file as a File.")
BOT_SHORT = "Prints files at home. Needs a key from the owner."


def bot_commands(mail_enabled: bool, guest_enabled: bool = True) -> list[dict]:
    commands = [{"command": "status", "description": "Queue and printer"},
                {"command": "help", "description": "How to use this bot"}]
    if guest_enabled:
        commands.insert(0, {"command": "invite", "description": "One-time guest key (needs PIN)"})
    if mail_enabled:
        commands.insert(0, {"command": "print", "description": "One-time email address (needs PIN)"})
    return commands


def _tokens_for(cfg: Config) -> tuple[Store, Tokens]:
    store = Store(cfg.data_dir / "printtoken.db")
    return store, Tokens(store, cfg.token_ttl_min, cfg.max_outstanding, cfg.mints_per_hour, cfg.guest_max_live)


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
    if cfg.mail_enabled or cfg.guest_enabled:
        result("PIN set for /print and /invite", bool(cfg.pin_hash),
               "" if cfg.pin_hash else "run: python -m printtoken setpin")
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
        # The menu goes to the allowlisted chats only and is cleared for everyone else, so a guest
        # who opens the bot sees no owner commands to tap.
        await tg.call("setMyCommands", commands=[], scope={"type": "default"})
        for chat_id in sorted(cfg.allowed_tg_ids):
            await tg.call("setMyCommands", commands=bot_commands(cfg.mail_enabled, cfg.guest_enabled),
                          scope={"type": "chat", "chat_id": chat_id})
        me = await tg.call("getMe")
        first = min(cfg.allowed_tg_ids, default=None)
        commands = await tg.call("getMyCommands", scope={"type": "chat", "chat_id": first}) if first else []
        print(f"bot @{me['username']} is now called {me['first_name']!r}")
        print("command menu (allowlisted chats only):", ", ".join("/" + c["command"] for c in commands))


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
    store, tokens = _tokens_for(cfg)
    try:
        tag, expires = tokens.mint("local")
    except MintRefused as exc:
        print(f"Refused: {exc}")
        return 1
    store.log_event(channel="local", kind="mint", outcome="ok", sender="local", detail=f"tag={mask(tag)}")
    print(f"{cfg.address_for(tag)}  (expires {time.strftime('%I:%M %p', time.localtime(expires))})")
    return 0


def cmd_dryrun(args) -> int:
    from . import notes
    cfg = load(need_secrets=False)
    data = Path(args.file).read_bytes()
    started = time.perf_counter()
    try:
        if args.note:
            # The file stands in for a typed Telegram message, so the clean page layout can be
            # checked on the laptop without the bot.
            pdf, pages = notes.to_pdf(data.decode("utf-8", errors="replace"))
            kind = "note"
        else:
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


def _bot_username(cfg: Config) -> str | None:
    """The bot's @name for the invite's deep link, or None when Telegram cannot be reached."""
    import httpx
    try:
        me = httpx.get(f"https://api.telegram.org/bot{cfg.bot_token}/getMe", timeout=10).json()
        return me.get("result", {}).get("username")
    except Exception:
        return None


def cmd_guest(args) -> int:
    """A guest key without the bot: the same forwardable block /invite sends, printed here."""
    from .telegram import invite_text
    cfg = load()
    if not cfg.guest_enabled:
        print("Guest keys are turned off in config.toml ([guest] enabled = false).")
        return 1
    store, tokens = _tokens_for(cfg)
    days = cfg.guest_key_days if args.days is None else args.days
    note = (args.note or "").strip()
    try:
        tag, expires = tokens.mint_guest("local", note, days)
    except MintRefused as exc:
        print(f"Refused: {exc}")
        return 1
    store.log_event(channel="local", kind="guest", outcome="minted", sender="local",
                    detail=f"tag={mask(tag)} note={note}")
    address = cfg.address_for(tag) if cfg.mail_enabled else None
    print(invite_text(tag, expires, _bot_username(cfg), min(cfg.guest_page_cap, cfg.page_cap), address))
    print(f"\nNote: {note or 'none'}. Forward the block above to the guest. The gateway "
          f"(python -m printtoken run) has to be running for the key to answer.")
    return 0


def cmd_guests(args) -> int:
    from .telegram import when
    cfg = load(need_secrets=False)
    _, tokens = _tokens_for(cfg)
    keys = tokens.guest_keys()
    if not keys:
        print("no guest keys yet")
        return 0
    for key in keys:
        holder = f" by {key.claimed_by}" if key.claimed_by else ""
        print(f"{mask(key.tag):8} {key.state:9} until {when(key.expires)}{holder}  {key.note or ''}".rstrip())
    return 0


def _guest_matches(tokens: Tokens, typed: str) -> list[KeyRecord]:
    """The guest keys a typed key names. `guests` shows only the first four characters (the full
    key is shown exactly once, in the invite), so anything shorter than a key is matched as a
    prefix, with and without a PT typed in front of it. A full-length key must match exactly, so
    a mistyped key never picks a different one."""
    if not typed:
        return []
    keys = tokens.guest_keys()
    if len(typed) >= KEY_LEN:
        return [k for k in keys if k.tag == typed]
    prefixes = [typed] + ([typed[2:]] if typed.startswith("pt") and len(typed) > 2 else [])
    for prefix in prefixes:
        found = [k for k in keys if k.tag.startswith(prefix)]
        if found:
            return found
    return []


def cmd_revoke(args) -> int:
    cfg = load(need_secrets=False)
    store, tokens = _tokens_for(cfg)
    matches = _guest_matches(tokens, normalise(args.key))
    if not matches:
        print("no such guest key")
        return 1
    if len(matches) > 1:
        print("more than one guest key starts like that, give more of it:")
        for key in matches:
            print(f"  {display_key(key.tag)}  {key.state:9} {key.note or ''}".rstrip())
        return 1
    rec = matches[0]
    if not tokens.spend(rec.tag):
        print(f"{display_key(rec.tag)} was already {rec.state}")
        return 1
    store.log_event(channel="local", kind="guest", outcome="revoked", sender="local", detail=f"tag={mask(rec.tag)}")
    print(f"revoked {display_key(rec.tag)} ({rec.note or 'no note'})")
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
    p.add_argument("--note", action="store_true",
                   help="lay FILE out as a typed note (the clean page) instead of detecting its type")
    p.set_defaults(func=cmd_dryrun)
    p = sub.add_parser("guest")
    p.add_argument("--days", type=int, default=None, help="days the key stays open (default: [guest] key_days)")
    p.add_argument("--note", default="", help="who the key is for, shown only to the owner")
    p.set_defaults(func=cmd_guest)
    sub.add_parser("guests").set_defaults(func=cmd_guests)
    p = sub.add_parser("revoke")
    p.add_argument("key", help="the key in any form (PT-ABCD-EFGH-IJKL or the bare characters), or the first "
                               "characters that guests shows")
    p.set_defaults(func=cmd_revoke)
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
