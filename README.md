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
  prints. Messages from anybody else get no reply at all, unless they carry a guest key (below).
- **Typed notes:** plain text typed into the bot is tidied into a clean page (a short first line
  on its own becomes the title, `- ` and `1. ` lines stay a list, lines pressed apart by the phone
  are joined back) and laid out by PyMuPDF's Story engine in a Times-style serif (Nimbus Roman)
  with 1 inch margins and a page footer. The bot keeps it as a draft for 10 minutes and nothing
  prints until `/go` (`/cancel` drops it), so a stray message never prints by itself. A `.txt`
  sent as a file keeps the monospaced layout, which suits code and logs.
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
| Guest key | one outside person redeems a 12-character key (60 bits) once; 10 wrong keys from one account, then an hour of silence; answers to a guest's chatter: 6 an hour | the same key as `box+<key>@gmail.com`, one attachment, DMARC still required, the sender allowlist not applied |
| Guest volume | 2 live keys, 7 days, one print of 5 pages, 5 guest prints an hour on their own budget, 10 files an hour looked at per guest account, 3 failed prints close the key | same key, same page cap, the shared hourly print limit |

## Guest key

A guest key lets one outside person print one thing, once, without being on the allowlist and
without the owner handling the file. It is 12 characters from the same alphabet as the email tags
(60 bits, no 0, 1, 8 or 9), shown as `PT-ABCD-EFGH-IJKL`, and it works on both channels:
whichever one prints first spends it.

**Issuing one.** In Telegram, `/invite <PIN> <note>` (the note is for the owner only: say who the
key is for). The bot deletes the message with the PIN and sends two replies: a block to forward to
the guest exactly as it is (the key as a tap-to-copy code span, a `t.me/<bot>?start=<key>` link
that hands the key over when the guest presses Start, the expiry, and the email address once mail
is on), then an owner-only note. The bot removes its own copy of the block once the key is claimed
or finished (Telegram allows that for 48 hours). Without a PIN on the gateway,
`python -m printtoken guest --note "..."` prints the same block on the laptop. `guests` lists the
keys with their state (live, claimed, printing, used, expired) and `revoke` closes one early, given
the full key or just the first characters `guests` shows (the full key is shown only once).

**What the guest sees.** Nothing until they send the key: a stranger's messages get silence, as
before. The key is found inside whatever they send (the forwarded invite, the deep link's `/start`
payload, a key with a sentence around it, en dashes, a trailing period, a `0` typed for `O`). A
right key is deleted from the chat and answered with what to send next, and the owner is told who
claimed it. Then one file, as a File or as a photo. Word files are turned away before the key is
touched, with instructions to export a PDF. The guest gets "Got <name>, checking it now" and then
either the result or what went wrong plus the fact that the key is still unused. The owner gets a
message for the claim, for the print or the failure, and when a flood of wrong keys or of files
locks an account.

**Limits.** 2 live keys, 7 days each, one print of up to 5 pages (never more than the owner's own
cap), 5 guest prints an hour on a budget separate from the owner's, 3 failed prints close a key,
10 wrong keys from one account mean an hour of silence (the owner is told once), and 10 files an
hour from one guest account, printable or not, after which its files are ignored for the rest of
the hour (the owner is told once, so the key can be revoked). A key held by a print that a crash
cut off is released at the next start. Keys reach the event log only as their first four
characters. Five of these settings live under `[guest]` in `config.toml`: `enabled`, `key_days`
(7), `max_live` (2), `page_cap` (5) and `bad_keys_per_hour` (10). The other four are constants at
the top of `printtoken/telegram.py`, not config: `GUEST_PRINTS_PER_HOUR` (5),
`GUEST_REPLIES_PER_HOUR` (6, answers to chatter), `GUEST_FILES_PER_HOUR` (10) and
`GUEST_FAILURES_PER_KEY` (3). `enabled = false` turns the whole thing off, including keys already
issued.

**By email, once mail is on.** The invite also carries `box+<12 characters>@gmail.com`. The guest
emails the file from their own address (Gmail's `dmarc=pass` is still required; the sender
allowlist is not applied to a guest key), one attachment, and gets no email back: the owner's
Telegram gets the confirmation. A key mid-print on one channel is refused on the other with
`key_in_use`.

**Before a guest tries it.** 1. `python -m printtoken setpin` once (`/invite` needs it).
2. `python -m printtoken tgsetup` after this change, so the new description and the per-chat
command menu are live (strangers see no commands). 3. `python -m printtoken run`, and keep the
machine awake until the guest is done, or run it on the Pi. 4. `/invite PIN note`, forward the
first reply. 5. Expect two messages: claimed, then printed. If neither comes, run
`python -m printtoken report --days 1` and look for `guest` events. On the Pi, check `timedatectl`
first: the expiry times in the messages use the machine's own time zone.

## Setup

1. Python 3.12+: `pip install -r requirements.txt`. On the Pi also `sudo apt install cups`.
2. Copy `config.example.toml` to `config.toml` and set `printer` to the exact printer name
   (Windows: Settings > Printers; Linux: `lpstat -e`), or leave `"DRYRUN"`.
   The file holds no secrets: every `*_env` entry names a key in `~/.env`.
3. In `~/.env`: `PRINT_TOKEN_BOT_TOKEN` (from @BotFather, used by this program only),
   `PRINT_TOKEN_ALLOWED_IDS` and `PRINT_TOKEN_NOTIFY_CHAT` (your numeric Telegram id).
   For email later: `PRINT_TOKEN_GMAIL` and `PRINT_TOKEN_GMAIL_APP_PASSWORD` (a Google App
   Password for a Gmail account used only for printing), then `enabled = true` under `[mail]`.
4. `python -m printtoken setpin` (needed for `/print` and `/invite`; stores a salted scrypt hash, never the PIN).
5. `python -m printtoken selftest`, then `python -m printtoken tgsetup` (bot name and menu) and
   `python -m printtoken tgtest` (live round trip through Telegram, finished with a dry run).
6. `python -m printtoken run`

Other commands: `dryrun [--note] FILE`, `mint` (local address, email testing),
`guest [--days N] [--note TEXT]`, `guests`, `revoke KEY`, `report [--days N] [--csv out.csv]`.
Tests: `python -m pytest -p no:faulthandler` (139 tests).

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
15. **PyMuPDF `Story.place()` hands back the same content until `draw()` runs for that page.** A
    loop that only places to count the pages first never finishes; keep the place, draw, end_page
    shape in `notes.py`.

## Threat model

Defends against: a leaked address (expires, single use), forged email senders, replay (atomic
burn), floods (bounded queue, rate limits, page cap), and anything inbound (nothing listens).
Does not defend against: a compromised gateway machine, someone with the Gmail or Telegram
account, or Google and Telegram themselves. Documents pass through both services, so no privacy
property is claimed.

## Layout

```
printtoken/   config.py store.py tokens.py pins.py mailparse.py policy.py convert.py notes.py printer.py
              pipeline.py telegram.py mailwatch.py messages.py report.py app.py __main__.py
tests/        139 tests: tokens and guest keys, mail parsing and policy, the mailbox, conversion and
              printing (Windows and CUPS), typed notes, blocking, the bot and the command line
data/         (git-ignored) printtoken.db, printtoken.log, dryrun/
```

## Not done yet

Email switched on with its own Gmail account, the move to the Raspberry Pi 5 (CUPS backend
written and unit tested, not yet run on the Pi), Word/DOCX input, starting automatically on boot,
the attack tests and the uptime trial.
