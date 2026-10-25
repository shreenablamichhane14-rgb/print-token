# Print Token

A small print gateway for a home printer. Send a file to a Telegram bot, or email it to a
one-time address, and it prints at home. Nothing on the home network accepts connections from the
internet: the gateway reaches out to Telegram (HTTPS) and to Gmail (IMAP over SSL) and nothing
ever has to reach in.

CIS2910C IT Capstone project, Shrinab Lamichhane, Fall 2026.

Status: the Telegram side works and is tested; email is built and tested but switched off until
its Gmail account is set up. Developed on a Windows laptop, moving to a Raspberry Pi 5 so it can
stay on all the time.

## How it works

```
 phone or laptop ── Telegram ──┐                      ┌── worker thread ─┐
   (anywhere)                  ├─> event loop ─> queue                  ├─> PDF ─> printer
                ── email ──────┘   (network only)     └── worker thread ─┘
                 to box+<tag>@gmail.com
```

- **Telegram:** an allowlisted Telegram account sends a PDF, PNG, JPEG or .txt *as a file* and it
  prints. Messages from anybody else get no reply at all.
- **Email (when enabled):** `/print <PIN>` in Telegram returns an address `box+<tag>@gmail.com`
  that works for one email within 30 minutes. The gateway reads new mail headers every 30 s,
  downloads a message only if it went to a `+tag` address, and then decides in this order: the tag
  was issued and is still live, then Gmail's own `Authentication-Results` header says
  `dmarc=pass`, and only then is the tag burned and the attachments queued.
- **Printing:** every file becomes a PDF first (type decided from its first bytes, never the
  name). Windows: pages are rendered with PyMuPDF and drawn through Windows GDI. Linux (the Pi):
  the PDF goes to CUPS with `lp -o fit-to-page`. `DRYRUN` never uses paper.
- **Records:** every print request, every rejection reason and a once-a-minute heartbeat go into
  `data/printtoken.db` (SQLite). `python -m printtoken report` turns that into numbers.

## Guardrails against spam and unknown prints

| | Telegram | Email |
|---|---|---|
| Who can print | allowlisted Telegram user ids only; strangers get silence | only mail sent to a live one-time `+tag`; everything else is ignored after reading headers |
| Forged sender | not possible, Telegram proves the account | rejected unless Gmail's top `Authentication-Results` says `dmarc=pass` |
| Spam | | Gmail's spam filter runs first (the gateway only reads the inbox); unknown tags are logged, not answered |
| Addresses | `/print` needs a PIN, 5 wrong tries locks it for an hour | 30 minutes, one use, 3 live at most, 3 new per hour |
| Files | PDF, PNG, JPEG, plain text only (detected from the bytes); 20 MB | same types; 25 MB; at most 5 attachments |
| Volume | 20 prints an hour, 20 pages per file, queue of 10 | same limits, same queue |

## Setup

1. Python 3.12+: `pip install -r requirements.txt`. On the Pi also `sudo apt install cups`.
2. Copy `config.example.toml` to `config.toml` and set `printer` to the exact printer name
   (Windows: Settings > Printers; Linux: `lpstat -e`), or leave `"DRYRUN"`.
   The file holds no secrets: every `*_env` entry names a key in `~/.env`.
3. In `~/.env`: `PRINT_TOKEN_BOT_TOKEN` (from @BotFather, used by this program only),
   `PRINT_TOKEN_ALLOWED_IDS` and `PRINT_TOKEN_NOTIFY_CHAT` (your numeric Telegram id).
   For email later: `PRINT_TOKEN_GMAIL` and `PRINT_TOKEN_GMAIL_APP_PASSWORD` (a Google App
   Password for a Gmail account used only for printing), then `enabled = true` under `[mail]`.
4. `python -m printtoken setpin` (only needed for email; stores a salted scrypt hash, never the PIN).
5. `python -m printtoken selftest`, then `python -m printtoken tgsetup` (bot name and menu) and
   `python -m printtoken tgtest` (live round trip through Telegram, finished with a dry run).
6. `python -m printtoken run`

Other commands: `dryrun FILE`, `mint` (local address, email testing), `report [--days N] [--csv out.csv]`.
Tests: `python -m pytest -p no:faulthandler` (61 tests).

## Pitfalls for whoever works on this next

1. **Never print to the default printer.** Windows 10/11 can move the default to the last printer
   used. Name the printer in `config.toml`.
2. **One program per bot token.** Telegram answers a second long-poller with error 409.
3. **httpx logs URLs, and Telegram URLs contain the bot token.** `app.py` turns httpx/httpcore
   logging down to WARNING and a filter blanks the token, app password and PIN hash in every line.
4. **Gmail search `deliveredto:` only knows the base address**, not the `+tag`. Read the tag from
   the To, Cc, Delivered-To and X-Original-To headers.
5. **Only the topmost `Authentication-Results` header counts, and only if `mx.google.com` wrote
   it.** Lower ones can be typed by the sender (RFC 8601 section 5).
6. **Check the sender before burning the tag**, or a forged email can burn a live address.
7. **Mail an account sends to itself has no `Authentication-Results` header**, so it is rejected.
   Test the email side from a different account.
8. **IMAP `UID n:*` returns the newest message even when n is past it.** Filter `uid > last`
   again. The saved position is per mailbox address, so switching accounts starts fresh.
9. **EXIF orientation.** Phone photos are often stored sideways with a rotation tag; apply
   `ImageOps.exif_transpose` before printing.
10. **Telegram compresses photos**; the bot asks for files instead. Bot downloads stop at 20 MB.
11. **PyMuPDF is AGPL** (dual licensed by Artifex). Fine for personal use; check before selling.
12. **Laptops sleep.** Heartbeats make sleep visible as downtime, which is why it is moving to a Pi.
13. **pytest on Windows prints "Windows fatal exception 0x80040155"** in the dry run tests. It is a
    handled COM error inside Microsoft Print to PDF; run with `-p no:faulthandler`.
14. **Several Pythons on one PC.** `python` and `py` can open different versions, and packages
    installed for one are missing from another (`ModuleNotFoundError`). Check `python --version`.

## Threat model

Defends against: a leaked address (expires, single use), forged email senders, replay (atomic
burn), floods (bounded queue, rate limits, page cap), and anything inbound (nothing listens).
Does not defend against: a compromised gateway machine, someone with the Gmail or Telegram
account, or Google and Telegram themselves. Documents pass through both services, so no privacy
property is claimed.

## Layout

```
printtoken/   config.py store.py tokens.py pins.py mailparse.py policy.py convert.py printer.py
              pipeline.py telegram.py mailwatch.py messages.py report.py app.py __main__.py
tests/        61 tests: tokens, mail parsing and policy, the mailbox, conversion and printing
              (Windows and CUPS), blocking, the bot
data/         (git-ignored) printtoken.db, printtoken.log, dryrun/
```

## Not done yet

Email switched on with its own Gmail account, the move to the Raspberry Pi 5 (CUPS backend
written and unit tested, not yet run on the Pi), Word/DOCX input, starting automatically on boot,
the attack tests and the uptime trial.
