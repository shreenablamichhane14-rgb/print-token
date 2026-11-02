"""One-time guest keys: the tokens, the stranger side of the bot, the email policy, the config
section, and the guard that turns a text bomb away before it is laid out."""
import asyncio
import sqlite3
import threading
import time
from datetime import datetime

import pytest
from conftest import BASE, DOMAIN, make_mail, make_pdf
from test_bot import OWNER, PIN, STRANGER, FakePipeline, FakeTG, msg
from test_mail import ACCEPT, parse

from printtoken import convert
from printtoken.config import Config, load
from printtoken.mailwatch import FetchedMail, MailWatcher
from printtoken.pins import PinGuard, make_hash
from printtoken.pipeline import JobResult, Pipeline, PrintJob
from printtoken.policy import AttemptLimiter, RateLimiter, decide_mail
from printtoken.store import Store
from printtoken.telegram import (GUEST_FILES_PER_HOUR, GUEST_REPLIES_PER_HOUR, Bot, help_text, invite_text,
                                 when)
from printtoken.tokens import (MintRefused, Tokens, Verdict, display_key, find_key, is_key_shaped,
                               is_tag_shaped, mask, new_key, normalise)

GUEST = 777
KEY = "abcdefghijkl"


class GuestTG(FakeTG):
    """FakeTG that also takes parse_mode and hands back message ids, like the real client."""

    def __init__(self, files=None):
        super().__init__(files)
        self.modes = []
        self._next = 100

    async def send(self, chat, text, parse_mode=None):
        self.sent.append((chat, text))
        self.modes.append(parse_mode)
        self._next += 1
        return self._next


class DryPrinter:
    is_dryrun = True


class GuestPipeline(FakePipeline):
    def __init__(self):
        super().__init__()
        self.printer = DryPrinter()


def make_guest_bot(tmp_path, store, tokens, files=None, mail_enabled=False, **overrides):
    cfg = Config(printer="DRYRUN", data_dir=tmp_path, bot_token="0" * 20, allowed_tg_ids=frozenset({OWNER}),
                 notify_chat_id=OWNER, pin_hash=make_hash(PIN), imap_host="imap.gmail.com",
                 imap_user="printbox@gmail.com", imap_password="x", mail_enabled=mail_enabled, **overrides)
    tg, pipeline = GuestTG(files), GuestPipeline()
    bot = Bot(cfg, tg, tokens, PinGuard(cfg.pin_hash, 5), pipeline, store, RateLimiter(20))
    bot.username = "printtokenbot"
    return bot, tg, pipeline


def run(coro):
    return asyncio.run(coro)


def to(tg, chat):
    return [text for c, text in tg.sent if c == chat]


def issue(tokens, note="prof", days=7):
    return tokens.mint_guest("tg:111", note, days)


def claimed_bot(tmp_path, store, tokens, files=None, **kw):
    """A bot with one key already redeemed by GUEST, and the chat log cleared."""
    bot, tg, pipeline = make_guest_bot(tmp_path, store, tokens, files=files, **kw)
    tag, _ = issue(tokens)
    run(bot.handle(msg(user=GUEST, text=display_key(tag))))
    tg.sent.clear()
    tg.deleted.clear()
    return bot, tg, pipeline, tag


def doc(name="a.pdf", file_id="d1", size=1000, user=GUEST, **extra):
    return msg(user=user, document={"file_id": file_id, "file_name": name, "file_size": size}, **extra)


# --- keys and tokens -------------------------------------------------------------------------

def test_key_shapes_and_normalise():
    tag = new_key()
    assert len(tag) == 12 and set(tag) <= set("abcdefghijklmnopqrstuvwxyz234567")
    assert is_tag_shaped(tag) and is_tag_shaped("abcdefgh") and not is_tag_shaped("abcdefghij")
    assert display_key(KEY) == "PT-ABCD-EFGH-IJKL"
    for form in ("PT-ABCD-EFGH-IJKL", "pt-abcd-efgh-ijkl", "ABCDEFGHIJKL", KEY, "ptabcdefghijkl",
                 "abcd-efgh-ijkl", "pt abcd efgh ijkl", " PT ABCD EFGH IJKL "):
        assert normalise(form) == KEY and is_key_shaped(form), form
    assert not is_key_shaped("hello there everyone") and not is_key_shaped("/print 482913")
    assert normalise("PT-ABCD-EFGH-IJK0") == "abcdefghijko"        # a zero is read as the letter o
    assert mask(KEY) == "abcd..." and mask(None) == "..."


def test_key_starting_with_pt_survives_every_form():
    key = "ptabcdefghij"
    for form in (display_key(key), key, key.upper(), "pt" + key, "PT-PTAB-CDEF-GHIJ", "ptab cdef ghij"):
        assert find_key(form) == key, form


def test_find_key_inside_other_text():
    shown = display_key(KEY)
    block = invite_text(KEY, time.time() + 86400, "printtokenbot", 5)
    en_dash, nbsp = chr(0x2013), chr(0xA0)          # what an iPhone keyboard puts in by itself
    for text in (f"Here's the key {shown} thanks", block, en_dash.join(("PT", "ABCD", "EFGH", "IJKL")),
                 "pt abcd efgh ijkl", f"{shown}.", f"/start {shown}", f"/key {shown}", KEY, f"  {KEY}\n",
                 nbsp.join(("PT", "ABCD", "EFGH", "IJKL"))):
        assert find_key(text) == KEY, text
    assert find_key("considerably") == "considerably"      # a bare 12-letter word counts only on its own
    assert find_key("that is considerably more") is None
    assert find_key("considerably", bare=False) is None
    assert find_key("yes please do") is None and find_key("/start") is None and find_key("") is None


def test_twelve_letter_chatter_is_not_a_key():
    # Things a guest might type before the key arrives; each one happens to be twelve letters.
    for text in ("ok thanks a lot", "will do thanks", "see you soon ok", "sounds good ok", "many thanks ok",
                 "is this the bot", "ok see you then", "printing test", "Thanks a lot, ok"):
        assert find_key(text) is None, text
    # A key typed on its own still counts: one run, groups of four, upper case, a trailing period.
    for text in ("abcd efgh ijkl", "abcd-efgh-ijkl", "ABCDEFGHIJKL", "abcdefghijkl.", " abcdefghijkl "):
        assert find_key(text) == KEY, text
    assert find_key("zzzz zzzz zzzz") == "zzzzzzzzzzzz"          # a wrong key in that shape is still a guess


def test_mint_guest_caps_and_outstanding(store):
    t = Tokens(store, ttl_min=30, max_outstanding=3, mints_per_hour=10, max_guest_live=2)
    a, expires = t.mint_guest("tg:1", "first", 7, now=1000.0)
    assert expires == 1000.0 + 7 * 86400
    t.mint_guest("tg:1", "", 7, now=1000.0)
    with pytest.raises(MintRefused, match="guest keys are still live") as refused:
        t.mint_guest("tg:1", "third", 7, now=1000.0)
    # Read on the phone, where there is no /revoke: the message names the laptop command.
    assert "python -m printtoken revoke" in str(refused.value) and "revoke one" not in str(refused.value)
    assert t.guest_live(now=1000.0) == 2 and t.outstanding(now=1000.0) == 0
    for _ in range(3):                                   # guest keys never block /print
        t.mint("tg:1", now=1000.0)
    assert t.outstanding(now=1000.0) == 3
    with pytest.raises(MintRefused, match="addresses are still live"):
        t.mint("tg:1", now=1000.0)
    rec = t.get(a, now=1000.0)
    assert (rec.note, rec.kind, rec.state) == ("first", "guest", "live")
    assert t.get("zzzzzzzzzzzz") is None


def test_hourly_window_is_shared(tokens):
    tokens.mint_guest("tg:1", "", 7, now=1000.0)
    tokens.mint("tg:1", now=1001.0)
    tokens.mint_guest("tg:1", "", 7, now=1002.0)
    with pytest.raises(MintRefused, match="an hour"):
        tokens.mint("tg:1", now=1003.0)


def test_claim_once(tokens):
    tag, _ = tokens.mint_guest("tg:1", "", 7, now=1000.0)
    assert tokens.claim(tag, "tg:5", now=1001.0) is Verdict.OK
    assert tokens.claim(tag, "tg:6", now=1002.0) is Verdict.CLAIMED
    assert tokens.claim(tag.upper(), "tg:5", now=1003.0) is Verdict.OK       # the claimant may repeat it
    assert tokens.get(tag).claimed == 1001.0                                  # without moving the stamp
    mail_tag, _ = tokens.mint("tg:1", now=1000.0)
    assert tokens.claim(mail_tag, "tg:5", now=1001.0) is Verdict.UNKNOWN     # addresses are not keys
    assert tokens.claim("zzzzzzzzzzzz", "tg:5", now=1001.0) is Verdict.UNKNOWN
    assert tokens.claim(tag, "tg:5", now=1000.0 + 8 * 86400) is Verdict.EXPIRED
    assert tokens.spend(tag, now=1004.0)
    assert tokens.claim(tag, "tg:5", now=1005.0) is Verdict.USED


def test_reserve_spend_release(tokens):
    tag, _ = tokens.mint_guest("tg:1", "", 7, now=1000.0)
    tokens.claim(tag, "tg:5", now=1001.0)
    assert tokens.reserve(tag, "tg:6", now=1002.0) is Verdict.CLAIMED
    assert tokens.reserve(tag, "tg:5", now=1002.0) is Verdict.OK
    assert tokens.get(tag, now=1002.0).state == "printing"
    assert tokens.reserve(tag, "tg:5", now=1003.0) is Verdict.RESERVED
    assert tokens.check(tag, now=1003.0) is Verdict.RESERVED
    assert tokens.burn(tag, now=1003.0) is Verdict.RESERVED              # email cannot take a held key
    tokens.release(tag)
    assert tokens.check(tag, now=1004.0) is Verdict.OK
    assert tokens.reserve(tag, "tg:5", now=1004.0) is Verdict.OK
    assert tokens.spend(tag, now=1005.0) and not tokens.spend(tag, now=1006.0)
    assert tokens.reserve(tag, "tg:5", now=1006.0) is Verdict.USED
    assert tokens.get(tag, now=1006.0).state == "used"
    late, _ = tokens.mint_guest("tg:1", "", 1, now=1000.0)
    tokens.claim(late, "tg:5", now=1001.0)
    assert tokens.reserve(late, "tg:5", now=1000.0 + 2 * 86400) is Verdict.EXPIRED


def test_failed_email_attempt_leaves_the_telegram_path_open(tokens):
    tag, _ = tokens.mint_guest("tg:1", "prof", 7, now=1000.0)
    assert tokens.reserve(tag, "mail:alice@example.com", now=1001.0, claimed_only=False) is Verdict.OK
    assert tokens.get(tag, now=1002.0).claimed_by is None                       # held, not claimed
    assert tokens.claim(tag, "tg:5", now=1002.0) is Verdict.RESERVED            # not while the email prints
    tokens.release(tag)                                                          # the email print failed
    assert tokens.claim(tag, "tg:5", now=1003.0) is Verdict.OK                  # the guest moves to Telegram
    assert tokens.get(tag, now=1004.0).claimed_by == "tg:5"
    assert tokens.spend(tag, now=1005.0, who="mail:bob@example.com") and tokens.get(tag).claimed_by == "tg:5"
    other, _ = tokens.mint_guest("tg:1", "", 7, now=1000.0)
    tokens.reserve(other, "mail:alice@example.com", now=1001.0, claimed_only=False)
    assert tokens.spend(other, now=1002.0, who="mail:alice@example.com")        # the sender is recorded here
    rec = tokens.get(other, now=1003.0)
    assert (rec.state, rec.claimed_by, rec.claimed) == ("used", "mail:alice@example.com", 1002.0)


def test_claim_race_has_one_winner(tokens):
    tag, _ = tokens.mint_guest("tg:1", "", 7, now=1000.0)
    results, barrier = [], threading.Barrier(8)

    def racer(i):
        barrier.wait()
        results.append(tokens.claim(tag, f"tg:{i}", now=1001.0))

    threads = [threading.Thread(target=racer, args=(i,)) for i in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert results.count(Verdict.OK) == 1 and results.count(Verdict.CLAIMED) == 7


def test_guest_keys_and_claim_for(tokens):
    live, _ = tokens.mint_guest("tg:1", "live one", 7, now=1000.0)
    old, _ = tokens.mint_guest("tg:1", "old one", 1, now=900.0)
    tokens.claim(old, "tg:5", now=901.0)
    tokens.spend(old, now=902.0)
    assert tokens.claim_for("tg:5", now=1000.0).tag == old
    tokens.claim(live, "tg:5", now=1001.0)
    assert tokens.claim_for("tg:5", now=1002.0).tag == live             # the live key wins over the spent one
    assert {k.note: k.state for k in tokens.guest_keys(now=1002.0)} == {"live one": "claimed", "old one": "used"}
    assert tokens.claim_for("tg:9", now=1002.0) is None
    tokens.reserve(live, "tg:5", now=1003.0)
    assert tokens.claim_for("tg:5", now=1004.0).state == "printing"
    assert [k.state for k in tokens.guest_keys(now=1000.0 + 8 * 86400)] == ["expired", "used"]


def test_release_all_frees_a_reservation_left_by_a_crash(tmp_path):
    path = tmp_path / "t.db"
    store = Store(path)
    tokens = Tokens(store)
    tag, _ = tokens.mint_guest("tg:1", "", 7, now=1000.0)
    tokens.claim(tag, "tg:5", now=1001.0)
    assert tokens.reserve(tag, "tg:5", now=1002.0) is Verdict.OK
    store.close()
    fresh = Tokens(Store(path))                         # the next start, with the in-memory queue gone
    assert fresh.release_all() == 1 and fresh.release_all() == 0
    assert fresh.reserve(tag, "tg:5", now=1003.0) is Verdict.OK
    fresh.store.close()


def test_old_database_gains_the_guest_columns(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE tokens(tag TEXT PRIMARY KEY, created REAL NOT NULL, expires REAL NOT NULL, "
               "used REAL, minted_by TEXT)")
    db.execute("INSERT INTO tokens VALUES('abcdefgh', 1, 2, NULL, 'tg:1')")
    db.commit()
    db.close()
    store = Store(path)
    cols = {row[1] for row in store.query("PRAGMA table_info(tokens)")}
    assert {"kind", "note", "claimed_by", "claimed", "reserved", "invite_msg"} <= cols
    assert store.scalar("SELECT kind FROM tokens") == "mail"
    store.close()
    Store(path).close()                                 # opening it again adds nothing and does not fail


def test_attempt_limiter_and_pin_guard_share_it():
    lim = AttemptLimiter(3, window=10)
    for _ in range(3):
        assert not lim.locked(7, now=100)
        lim.fail(7, now=100)
    assert lim.locked(7, now=100) and not lim.locked(8, now=100) and lim.count(7, now=100) == 3
    assert not lim.locked(7, now=111)
    guard = PinGuard(make_hash("482913"), max_failures_per_hour=2)
    assert isinstance(guard.attempts, AttemptLimiter)
    guard.verify(1, "000000", now=5)
    guard.verify(1, "000000", now=6)
    assert guard.locked(1, now=7) and not guard.verify(1, "482913", now=7)


def test_when_is_portable():
    ts = time.time()
    dt = datetime.fromtimestamp(ts)
    text = when(ts)
    assert text.startswith(f"{dt:%A}, {dt:%B} {dt.day}, ") and not text.split(", ")[2].startswith("0")


# --- the bot: strangers and guests -------------------------------------------------------------

def test_stranger_chatter_is_silent_and_never_a_strike(tmp_path, store, tokens):
    bot, tg, _ = make_guest_bot(tmp_path, store, tokens)
    for text in ("hello", "/start", "/print 482913", "yes please do"):
        run(bot.handle(msg(user=STRANGER, text=text)))
    assert tg.sent == [] and tg.deleted == []
    assert [r[0] for r in store.query("SELECT reason FROM events")] == ["not_allowlisted"] * 4
    assert bot.key_guard.count(STRANGER) == 0
    tag, _ = issue(tokens)
    run(bot.handle(msg(user=STRANGER, text=display_key(tag))))          # chatter never blocks a real key
    assert to(tg, STRANGER)[0].startswith("Key accepted")


def test_wrong_key_is_silent_and_counted(tmp_path, store, tokens):
    bot, tg, _ = make_guest_bot(tmp_path, store, tokens)
    run(bot.handle(msg(user=STRANGER, text="PT-ZZZZ-ZZZZ-ZZZZ")))
    assert tg.sent == [] and tg.deleted == []
    assert store.query("SELECT kind, reason FROM events") == [("guest", "unknown_tag")]
    assert bot.key_guard.count(STRANGER) == 1


def test_right_key_in_every_form_claims(tmp_path, store):
    tokens = Tokens(store, mints_per_hour=20, max_guest_live=10)
    bot, tg, _ = make_guest_bot(tmp_path, store, tokens)
    forms = [display_key, lambda k: f"/start {display_key(k)}", lambda k: f"here you go {display_key(k).lower()} thanks",
             lambda k: invite_text(k, time.time() + 86400, "printtokenbot", 5), lambda k: f"{display_key(k)}.",
             lambda k: k.upper()]
    for i, form in enumerate(forms):
        user = 500 + i
        tag, expires = tokens.mint_guest("tg:111", f"guest {i}", 7)
        tg.sent.clear()
        tg.deleted.clear()
        run(bot.handle(msg(user=user, text=form(tag))))
        assert tg.deleted == [5], form(tag)                                  # the key is gone from the chat
        welcome = to(tg, user)
        assert len(welcome) == 1 and welcome[0].startswith("Key accepted") and "up to 5 pages" in welcome[0]
        assert when(expires) in welcome[0] and "Word files will not print" in welcome[0]
        assert to(tg, OWNER) == [f'Guest key "guest {i}" was claimed by Telegram user {user}.']
        assert tokens.claim_for(f"tg:{user}").tag == tag


def test_second_claimant_gets_one_line_and_no_strike(tmp_path, store, tokens):
    bot, tg, _, tag = claimed_bot(tmp_path, store, tokens)
    run(bot.handle(msg(user=STRANGER, text=display_key(tag))))
    assert to(tg, STRANGER) == ["That key was already claimed by another account. Ask the owner for a new one."]
    assert bot.key_guard.count(STRANGER) == 0 and tokens.get(tag).claimed_by == f"tg:{GUEST}"


def test_claimed_guest_resending_the_key_is_not_a_second_claim(tmp_path, store, tokens):
    bot, tg, _, tag = claimed_bot(tmp_path, store, tokens)
    run(bot.handle(msg(user=GUEST, text=display_key(tag))))
    assert to(tg, GUEST) == ["Key already accepted. Send the file as a File (paperclip, then File), or as a photo."]
    assert to(tg, OWNER) == [] and tg.deleted == [5]
    run(bot.handle(msg(user=GUEST, text="will do thanks")))              # 12 letters, but not a guess
    assert to(tg, GUEST)[-1].startswith("I am waiting for a file")
    assert bot.key_guard.count(GUEST) == 0
    assert store.scalar("SELECT COUNT(*) FROM events WHERE outcome = 'claimed'") == 1


def test_claimed_guest_document_is_queued_on_the_guest_budget(tmp_path, store, tokens):
    pdf = make_pdf()
    bot, tg, pipeline, tag = claimed_bot(tmp_path, store, tokens, files={"d1": pdf})
    run(bot.handle(doc(size=len(pdf))))
    job = pipeline.queue.items[0]
    assert job.sender == f"guest:tg:{GUEST}" and job.page_cap == 5 and job.guest_tag == tag and job.data == pdf
    assert job.reply_chat == GUEST and job.channel == "telegram"
    assert to(tg, GUEST) == ["Got a.pdf, checking it now."]
    assert tokens.get(tag).state == "printing"
    assert len(bot.limiter._times) == 0                                   # the owner's budget is untouched
    run(bot.handle(doc(file_id="never-downloaded")))                      # a second file while the first runs
    assert to(tg, GUEST)[-1].startswith("One file at a time")
    assert len(pipeline.queue.items) == 1


def test_guest_photo_is_accepted(tmp_path, store, tokens):
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 32
    bot, tg, pipeline, tag = claimed_bot(tmp_path, store, tokens, files={"p9": jpeg})
    run(bot.handle(msg(user=GUEST, photo=[{"file_id": "p1", "width": 90, "height": 90, "file_size": 10},
                                          {"file_id": "p9", "width": 1280, "height": 960, "file_size": 36}])))
    job = pipeline.queue.items[0]
    assert job.filename == "photo.jpg" and job.data == jpeg and job.guest_tag == tag


def test_key_and_file_in_one_message(tmp_path, store, tokens):
    pdf = make_pdf()
    bot, tg, pipeline = make_guest_bot(tmp_path, store, tokens, files={"d1": pdf})
    tag, _ = issue(tokens)
    run(bot.handle(doc(size=len(pdf), caption=display_key(tag))))
    assert to(tg, GUEST)[0].startswith("Key accepted") and to(tg, GUEST)[1] == "Got a.pdf, checking it now."
    assert pipeline.queue.items[0].guest_tag == tag


def test_word_file_is_turned_away_before_the_key_is_touched(tmp_path, store, tokens):
    bot, tg, pipeline, tag = claimed_bot(tmp_path, store, tokens, files={"w": b"PK\x03\x04" + b"\x00" * 20})
    run(bot.handle(doc("Syllabus.docx", "w")))
    reply = to(tg, GUEST)[0]
    assert "Word file" in reply and "PDF" in reply and reply.endswith("Your key is still unused.")
    assert pipeline.queue.items == [] and tokens.get(tag).state == "claimed"
    assert store.scalar("SELECT reason FROM events WHERE kind = 'guest' AND outcome = 'rejected'") == "unsupported_type"


def test_result_ok_spends_the_key(tmp_path, store, tokens):
    pdf = make_pdf(2)
    bot, tg, pipeline, tag = claimed_bot(tmp_path, store, tokens, files={"d1": pdf})
    run(bot.handle(doc(size=len(pdf))))
    job = pipeline.queue.items[0]
    tg.sent.clear()
    run(bot.on_result(job, JobResult(True, "printed", pages=2)))
    assert tokens.get(tag).state == "used"
    assert to(tg, GUEST) == ["Received a.pdf (2 pages) and ran it through the gateway. "
                             "The owner will confirm it came out."]
    assert to(tg, OWNER) == [f'Guest key "prof" printed a.pdf (2 pages) for Telegram user {GUEST}. This was a dry run.']
    run(bot.handle(doc(size=len(pdf))))                                   # the same person, afterwards
    assert to(tg, GUEST)[-1] == "This key already did its one print. Ask the owner for a new key if you need another."
    assert len(pipeline.queue.items) == 1
    for _ in range(10):
        run(bot.handle(msg(user=GUEST, text="thanks")))
    assert len(to(tg, GUEST)) == 7                                        # answers run out, then silence
    assert bot.key_guard.count(GUEST) == 0


def test_result_ok_wording_with_a_real_printer(tmp_path, store, tokens):
    pdf = make_pdf()
    bot, tg, pipeline, tag = claimed_bot(tmp_path, store, tokens, files={"d1": pdf})
    pipeline.printer = type("Real", (), {"is_dryrun": False})()
    run(bot.handle(doc("notes.pdf", size=len(pdf))))
    tg.sent.clear()
    run(bot.on_result(pipeline.queue.items[0], JobResult(True, "printed", pages=1)))
    assert to(tg, GUEST) == ["Printed notes.pdf (1 page). Thanks for trying Print Token."]
    assert to(tg, OWNER) == [f'Guest key "prof" printed notes.pdf (1 page) for Telegram user {GUEST}.']


def test_result_failure_releases_then_closes_after_three(tmp_path, store, tokens):
    pdf = make_pdf()
    bot, tg, pipeline, tag = claimed_bot(tmp_path, store, tokens, files={"d1": pdf})
    for n in range(1, 4):
        run(bot.handle(doc(size=len(pdf))))
        job = pipeline.queue.items[-1]
        tg.sent.clear()
        run(bot.on_result(job, JobResult(False, "print_error", detail="offline")))
        guest, owner = to(tg, GUEST)[0], to(tg, OWNER)[0]
        if n < 3:
            assert tokens.get(tag).state == "claimed"
            assert guest.startswith("Did not print a.pdf: the printer reported an error.")
            assert guest.endswith("Your key is still unused. The owner has been told, please try again in a few minutes.")
            assert owner.endswith("the printer reported an error (offline). The key is open again.")
        else:
            assert tokens.get(tag).state == "used"
            assert "this key is now closed" in guest and owner.endswith("closed after too many failed prints.")
    assert store.scalar("SELECT reason FROM events WHERE outcome = 'closed'") == "too_many_failures"


def test_failure_wording_for_problems_the_guest_can_fix(tmp_path, store, tokens):
    pdf = make_pdf()
    bot, tg, pipeline, tag = claimed_bot(tmp_path, store, tokens, files={"d1": pdf})
    run(bot.handle(doc("long.pdf", size=len(pdf))))
    tg.sent.clear()
    run(bot.on_result(pipeline.queue.items[0], JobResult(False, "over_page_cap", pages=7,
                                                         detail="7 pages, the limit is 5")))
    assert to(tg, GUEST) == ["long.pdf has 7 pages and a guest key covers 5. Send a shorter file or just the pages "
                             "you need. Your key is still unused, fix that and send it again."]
    assert tokens.get(tag).state == "claimed"
    run(bot.handle(doc("x.bin", size=len(pdf))))
    tg.sent.clear()
    word = "Word and Office files are not supported yet, export it as a PDF and send that"
    run(bot.on_result(pipeline.queue.items[1], JobResult(False, "unsupported_type", detail=word)))
    assert to(tg, GUEST) == [f"Did not print x.bin: the file type is not supported ({word}). "
                             f"Your key is still unused, fix that and send it again."]


def test_ten_wrong_keys_lock_the_account_and_tell_the_owner_once(tmp_path, store, tokens):
    bot, tg, _ = make_guest_bot(tmp_path, store, tokens)
    for letter in "ABCDEFGHIJ":
        run(bot.handle(msg(user=STRANGER, text=f"PT-ZZZZ-ZZZZ-ZZ{letter}Z")))
    assert to(tg, STRANGER) == []
    assert to(tg, OWNER) == [f"Telegram user {STRANGER} sent 10 wrong guest keys and is ignored for an hour."]
    tag, _ = issue(tokens)
    run(bot.handle(msg(user=STRANGER, text=display_key(tag))))           # right key, locked account: silence
    assert tg.sent[-1][0] == OWNER and tokens.get(tag).state == "live"
    assert store.scalar("SELECT COUNT(*) FROM events WHERE reason = 'guest_locked'") == 1
    assert store.scalar("SELECT COUNT(*) FROM events WHERE reason = 'unknown_tag'") == 10


def test_invite_gates(tmp_path, store, tokens):
    bot, tg, _ = make_guest_bot(tmp_path, store, tokens)
    run(bot.handle(msg(text="/invite 000000 someone")))
    assert "Wrong PIN" in to(tg, OWNER)[0] and tg.deleted == [5] and tokens.guest_live() == 0
    bot.pins = PinGuard("", 5)
    run(bot.handle(msg(text=f"/invite {PIN}")))
    assert "No PIN is set" in to(tg, OWNER)[-1] and "/invite is off" in to(tg, OWNER)[-1]
    off, tg2, _ = make_guest_bot(tmp_path, store, tokens, guest_enabled=False)
    run(off.handle(msg(text=f"/invite {PIN} x")))
    assert "turned off" in to(tg2, OWNER)[0] and tokens.guest_live() == 0


def test_invite_success_with_mail_off(tmp_path, store, tokens):
    bot, tg, _ = make_guest_bot(tmp_path, store, tokens, mail_enabled=False)
    run(bot.handle(msg(text=f"/invite {PIN} the guest from work")))
    block, owner_note = to(tg, OWNER)
    assert tg.deleted == [5]
    key = tokens.guest_keys()[0]
    shown = display_key(key.tag)
    assert f"<code>{shown}</code>" in block and f"https://t.me/printtokenbot?start={shown}" in block
    assert when(key.expires) in block and "up to 5 pages" in block
    assert "the guest from work" not in block and "Forward" not in block and "@" not in block
    assert tg.modes == ["HTML", None]
    assert "Note: the guest from work" in owner_note and owner_note.startswith("Forward the message above")
    assert key.note == "the guest from work" and key.invite_msg == f"{OWNER}:101"
    assert store.scalar("SELECT detail FROM events WHERE outcome = 'minted'") == f"tag={mask(key.tag)} note=the guest from work"


def test_invite_with_mail_on_carries_the_address(tmp_path, store, tokens):
    bot, tg, _ = make_guest_bot(tmp_path, store, tokens, mail_enabled=True)
    run(bot.handle(msg(text=f"/invite {PIN}")))
    block = to(tg, OWNER)[0]
    tag = tokens.guest_keys()[0].tag
    assert f"<code>printbox+{tag}@gmail.com</code>" in block and "one email only" in block


def test_invite_without_a_bot_username_still_works(tmp_path, store, tokens):
    bot, tg, _ = make_guest_bot(tmp_path, store, tokens)
    bot.username = None
    run(bot.handle(msg(text=f"/invite {PIN}")))
    block = to(tg, OWNER)[0]
    assert "t.me" not in block and "None" not in block and "press Start" in block


def test_invite_reply_is_removed_once_claimed(tmp_path, store, tokens):
    bot, tg, _ = make_guest_bot(tmp_path, store, tokens)
    run(bot.handle(msg(text=f"/invite {PIN} prof")))
    tag = tokens.guest_keys()[0].tag
    tg.deleted.clear()
    run(bot.handle(msg(user=GUEST, text=display_key(tag))))
    assert tg.deleted == [5, 101]                 # the guest's key message, then the owner's invite copy
    assert tokens.get(tag).invite_msg is None


def test_guests_disabled_means_silence_everywhere(tmp_path, store, tokens):
    tag, _ = issue(tokens)
    tokens.claim(tag, f"tg:{GUEST}")
    bot, tg, pipeline = make_guest_bot(tmp_path, store, tokens, files={"d1": make_pdf()}, guest_enabled=False)
    run(bot.handle(msg(user=GUEST, text=display_key(tag))))
    run(bot.handle(doc()))
    assert tg.sent == [] and pipeline.queue.items == []
    assert {r[0] for r in store.query("SELECT reason FROM events")} == {"not_allowlisted"}


def test_full_key_never_reaches_the_event_log(tmp_path, store, tokens):
    pdf = make_pdf()
    bot, tg, pipeline = make_guest_bot(tmp_path, store, tokens, files={"d1": pdf}, mail_enabled=True)
    run(bot.handle(msg(text=f"/invite {PIN} prof")))
    tag = tokens.guest_keys()[0].tag
    run(bot.handle(msg(user=GUEST, text=display_key(tag))))
    run(bot.handle(doc(size=len(pdf))))
    run(bot.on_result(pipeline.queue.items[0], JobResult(True, "printed", pages=1)))
    run(bot.handle(msg(user=GUEST, text=display_key(tag))))                # a replay by Telegram
    watcher = MailWatcher(bot.cfg, store, tokens, pipeline, bot.notify_owner, RateLimiter(20))
    raw = make_mail(to=f"{BASE}+{tag}@{DOMAIN}")
    run(watcher.handle(FetchedMail(1, raw, len(raw), None, time.time())))  # and a replay by email
    rows = store.query("SELECT detail, sender, filename FROM events")
    assert len(rows) >= 5                       # minted, claimed, spent, two replays (the fake pipeline logs nothing)
    assert all(tag not in " ".join(str(v) for v in row) for row in rows)
    assert store.scalar("SELECT COUNT(*) FROM events WHERE reason = 'replayed_tag'") == 2


def test_chatter_of_twelve_letters_is_silent_and_never_a_strike(tmp_path, store, tokens):
    bot, tg, _ = make_guest_bot(tmp_path, store, tokens)
    phrases = ("ok thanks a lot", "will do thanks", "is this the bot", "ok see you then")
    for text in phrases:
        run(bot.handle(msg(user=STRANGER, text=text)))
    assert tg.sent == [] and bot.key_guard.count(STRANGER) == 0
    assert [r[0] for r in store.query("SELECT reason FROM events")] == ["not_allowlisted"] * len(phrases)
    tag, _ = issue(tokens)
    run(bot.handle(msg(user=STRANGER, text=display_key(tag))))
    assert to(tg, STRANGER)[0].startswith("Key accepted")


def test_a_flood_of_files_from_one_guest_is_bounded(tmp_path, store, tokens):
    word = b"PK\x03\x04" + b"\x00" * 20
    bot, tg, pipeline, tag = claimed_bot(tmp_path, store, tokens, files={"w": word})
    downloads = 0
    real_download = tg.download

    async def counting(file_id):
        nonlocal downloads
        downloads += 1
        return await real_download(file_id)

    tg.download = counting
    for _ in range(GUEST_FILES_PER_HOUR + 5):
        run(bot.handle(doc("Syllabus.docx", "w", size=20 * 1048576)))
    assert downloads == GUEST_FILES_PER_HOUR and len(to(tg, GUEST)) == GUEST_FILES_PER_HOUR
    assert to(tg, OWNER) == [f'Guest key "prof": Telegram user {GUEST} has sent {GUEST_FILES_PER_HOUR} files in '
                             f"an hour. Further files from that account are ignored for the rest of the hour."]
    assert store.scalar("SELECT COUNT(*) FROM events WHERE reason = 'too_many_files'") == 5
    assert tokens.get(tag).state == "claimed" and pipeline.queue.items == []    # the key is untouched
    run(bot.handle(msg(user=GUEST, text="hello?")))                              # chatter is still answered
    assert to(tg, GUEST)[-1].startswith("I am waiting for a file")


def test_files_sent_while_a_print_runs_do_not_burn_the_file_budget(tmp_path, store, tokens):
    pdf = make_pdf()
    bot, tg, pipeline, tag = claimed_bot(tmp_path, store, tokens, files={"d1": pdf})
    run(bot.handle(doc(size=len(pdf))))
    tg.sent.clear()
    for _ in range(GUEST_REPLIES_PER_HOUR + 10):                   # a photo album, say, while the first file runs
        run(bot.handle(doc(file_id="never-downloaded")))
    answers = to(tg, GUEST)
    assert len(answers) == GUEST_REPLIES_PER_HOUR and all(a.startswith("One file at a time") for a in answers)
    assert bot.file_guard.count(GUEST) == 1 and len(pipeline.queue.items) == 1
    run(bot.on_result(pipeline.queue.items[0], JobResult(False, "print_error", detail="offline")))
    tg.sent.clear()
    run(bot.handle(doc(size=len(pdf))))                            # the budget for real files is intact
    assert to(tg, GUEST) == ["Got a.pdf, checking it now."]


def test_invite_block_speaks_of_the_sender_not_of_the_bot():
    # On the guest's phone the forward is labelled with the bot's name, so "me" would read as the bot.
    for address in (None, "printbox+abcdefghijkl@gmail.com"):
        block = invite_text(KEY, time.time() + 86400, "printtokenbot", 5, address)
        assert "tell the person who sent you this key" in block
        assert "tell me" not in block and "I get" not in block and " me." not in block
    assert "the person who sent you this key gets the confirmation" in block


def test_help_hides_invite_when_guests_are_off(tmp_path, store, tokens):
    assert "/invite" in help_text(False) and "/invite" not in help_text(False, guest_enabled=False)
    on, _, _ = make_guest_bot(tmp_path, store, tokens)
    off, _, _ = make_guest_bot(tmp_path, store, tokens, guest_enabled=False)
    assert "/invite" in on.help and "/invite" not in off.help and "/status" in off.help


def test_owner_notice_falls_back_to_the_mask_without_a_note(tmp_path, store, tokens):
    bot, tg, _ = make_guest_bot(tmp_path, store, tokens)
    tag, _ = tokens.mint_guest("tg:111", "", 7)
    run(bot.handle(msg(user=GUEST, text=display_key(tag))))
    assert to(tg, OWNER) == [f"Guest key {mask(tag)} was claimed by Telegram user {GUEST}."]


# --- email ------------------------------------------------------------------------------------

def test_guest_key_by_email_is_held_not_burned(tokens):
    tag, _ = tokens.mint_guest("tg:1", "prof", 7)
    to_addr = f"{BASE}+{tag}@{DOMAIN}"
    two = [("a.pdf", "application/pdf", make_pdf()), ("b.pdf", "application/pdf", make_pdf())]
    d = decide_mail(parse(make_mail(to=to_addr, attachments=two)), tokens, ACCEPT,
                    frozenset({"owner@example.com"}))                       # the allowlist does not apply
    assert d.ok and d.guest and [f.filename for f in d.files] == ["a.pdf"]
    assert tokens.check(tag) is Verdict.RESERVED and tokens.get(tag).claimed_by is None     # held, not claimed
    assert decide_mail(parse(make_mail(to=to_addr)), tokens, ACCEPT).reason == "key_in_use"
    tokens.spend(tag, who="mail:alice@example.com")                     # what the result handler does
    assert tokens.get(tag).claimed_by == "mail:alice@example.com"
    assert decide_mail(parse(make_mail(to=to_addr)), tokens, ACCEPT).reason == "replayed_tag"


def test_guest_key_by_email_costs_nothing_until_a_printable_file_arrives(tokens):
    tag, _ = tokens.mint_guest("tg:1", "", 7)
    to_addr = f"{BASE}+{tag}@{DOMAIN}"
    docx = [("Syllabus.docx", "application/octet-stream", b"PK\x03\x04" + b"\x00" * 20)]
    assert decide_mail(parse(make_mail(to=to_addr, attachments=docx)), tokens, ACCEPT).reason == "unsupported_type"
    assert decide_mail(parse(make_mail(to=to_addr, attachments=[])), tokens, ACCEPT).reason == "no_attachment"
    assert decide_mail(parse(make_mail(to=to_addr)), tokens, ACCEPT, guest_enabled=False).reason == "guest_off"
    fail = "mx.google.com; spf=fail; dmarc=fail (p=REJECT) header.from=example.com"
    assert decide_mail(parse(make_mail(to=to_addr, auth=fail)), tokens, ACCEPT).reason == "dmarc_fail"
    assert tokens.check(tag) is Verdict.OK
    assert decide_mail(parse(make_mail(to=to_addr)), tokens, ACCEPT).ok


def test_mail_watcher_queues_a_guest_job(tmp_path, store, tokens):
    bot, tg, pipeline = make_guest_bot(tmp_path, store, tokens, mail_enabled=True)
    tag, _ = tokens.mint_guest("tg:1", "prof", 7)
    watcher = MailWatcher(bot.cfg, store, tokens, pipeline, bot.notify_owner, RateLimiter(20))
    raw = make_mail(to=f"{BASE}+{tag}@{DOMAIN}")
    run(watcher.handle(FetchedMail(1, raw, len(raw), None, time.time())))
    job = pipeline.queue.items[0]
    assert job.sender == "guest:mail:alice@example.com" and job.page_cap == 5 and job.guest_tag == tag
    assert job.reply_chat is None and tokens.get(tag).state == "printing"
    run(bot.on_result(job, JobResult(True, "printed", pages=1)))
    assert tokens.get(tag).state == "used"
    assert to(tg, OWNER) == ['Guest key "prof" printed doc.pdf (1 page) for email from alice@example.com. '
                             "This was a dry run."]
    assert tokens.get(tag).claimed_by == "mail:alice@example.com"       # recorded when the key is spent


def test_guest_can_move_to_telegram_after_a_failed_email_print(tmp_path, store, tokens):
    bot, tg, pipeline = make_guest_bot(tmp_path, store, tokens, mail_enabled=True)
    tag, _ = tokens.mint_guest("tg:1", "prof", 7)
    watcher = MailWatcher(bot.cfg, store, tokens, pipeline, bot.notify_owner, RateLimiter(20))
    raw = make_mail(to=f"{BASE}+{tag}@{DOMAIN}", attachments=[("long.pdf", "application/pdf", make_pdf(7))])
    run(watcher.handle(FetchedMail(1, raw, len(raw), None, time.time())))
    assert tokens.get(tag).state == "printing" and tokens.get(tag).claimed_by is None
    run(bot.handle(msg(user=GUEST, text=display_key(tag))))            # the key arrives while the email prints
    assert to(tg, GUEST) == ["One file at a time: your first file is still going through. If it fails you will "
                             "get a message and can send another."]
    assert bot.key_guard.count(GUEST) == 0
    run(bot.on_result(pipeline.queue.items[0], JobResult(False, "over_page_cap", pages=7,
                                                         detail="7 pages, the limit is 5")))
    tg.sent.clear()
    run(bot.handle(msg(user=GUEST, text=display_key(tag))))            # the same guest tries Telegram instead
    assert to(tg, GUEST)[0].startswith("Key accepted") and tokens.get(tag).claimed_by == f"tg:{GUEST}"
    assert tokens.get(tag).state == "claimed"


# --- config -----------------------------------------------------------------------------------

def test_config_guest_section_defaults_and_overrides(tmp_path):
    env = tmp_path / "env"
    env.write_text("T_BOT=0123456789abcdef\nT_IDS=5\n")
    base = 'printer = "DRYRUN"\n[telegram]\ntoken_env = "T_BOT"\nallowed_ids_env = "T_IDS"\n[mail]\nenabled = false\n'
    plain = tmp_path / "plain.toml"
    plain.write_text(base)
    cfg = load(config_path=plain, env_path=env)
    fields = ("guest_enabled", "guest_key_days", "guest_max_live", "guest_page_cap", "guest_bad_keys_per_hour")
    assert tuple(getattr(cfg, f) for f in fields) == (True, 7, 2, 5, 10)
    custom = tmp_path / "custom.toml"
    custom.write_text(base + "[guest]\nenabled = false\nkey_days = 2\nmax_live = 1\npage_cap = 3\n"
                             "bad_keys_per_hour = 4\n")
    cfg = load(config_path=custom, env_path=env)
    assert tuple(getattr(cfg, f) for f in fields) == (False, 2, 1, 3, 4)


# --- the command line ----------------------------------------------------------------------------

def test_revoke_takes_the_fragment_the_listing_shows(tmp_path, monkeypatch, capsys):
    from printtoken.__main__ import main
    data = tmp_path / "data"
    (tmp_path / "env").write_text("T_BOT=0123456789abcdef\nT_IDS=5\n")
    (tmp_path / "config.toml").write_text(f'printer = "DRYRUN"\ndata_dir = "{data.as_posix()}"\n[telegram]\n'
                                          'token_env = "T_BOT"\nallowed_ids_env = "T_IDS"\n[mail]\nenabled = false\n')
    monkeypatch.setenv("PRINT_TOKEN_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("PRINT_TOKEN_ENV", str(tmp_path / "env"))
    store = Store(data / "printtoken.db")
    tokens = Tokens(store, max_guest_live=5)
    tag, _ = tokens.mint_guest("local", "prof", 7)
    for fixed in ("abcdefghijkl", "abcdzzzzzzzz"):                     # two keys that share four characters
        store.write("INSERT INTO tokens(tag, created, expires, minted_by, kind) VALUES(?, ?, ?, 'local', 'guest')",
                    (fixed, time.time(), time.time() + 86400))
    store.close()
    assert main(["guests"]) == 0
    listing = capsys.readouterr().out
    assert mask(tag) in listing and tag not in listing                  # the listing never shows a whole key
    assert main(["revoke", mask(tag)]) == 0                             # so what it shows has to be enough
    assert capsys.readouterr().out.strip() == f"revoked {display_key(tag)} (prof)"
    assert main(["revoke", mask(tag)]) == 1 and "already used" in capsys.readouterr().out
    assert main(["revoke", "zzzz"]) == 1 and "no such guest key" in capsys.readouterr().out
    assert main(["revoke", "PT-ZZZZ-ZZZZ-ZZZZ"]) == 1 and "no such guest key" in capsys.readouterr().out
    assert main(["revoke", "abcd"]) == 1                                # two keys start like that
    out = capsys.readouterr().out
    assert "more than one" in out and "PT-ABCD-EFGH-IJKL" in out and "PT-ABCD-ZZZZ-ZZZZ" in out
    assert main(["revoke", "PT-ABCDE"]) == 0 and "revoked PT-ABCD-EFGH-IJKL" in capsys.readouterr().out
    fresh = Store(data / "printtoken.db")
    assert sorted(k.state for k in Tokens(fresh).guest_keys()) == ["live", "used", "used"]
    fresh.close()


# --- the page cap, before anything is drawn ----------------------------------------------------

def test_text_bomb_is_refused_in_under_a_second():
    started = time.perf_counter()
    with pytest.raises(convert.TooLong):
        convert.to_pdf(b"\n" * 1_000_000, page_cap=5)
    with pytest.raises(convert.TooLong):
        convert.to_pdf(b"x" * (convert.MAX_TEXT_BYTES + 1))
    assert time.perf_counter() - started < 1.0
    assert convert.to_pdf(b"short\n", page_cap=5)[1] == 1


def test_heic_is_named_rather_than_a_mystery():
    heic = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 16
    assert convert.sniff(heic) == "heic"
    with pytest.raises(convert.Unsupported, match="HEIC") as caught:
        convert.to_pdf(heic)
    # The owner is asked for files, not photos, so the pipeline's advice must not send them round
    # in a circle; only a guest, who may send photos, is told to send it as a photo.
    assert "as a photo" not in str(caught.value) and "JPEG or PDF" in str(caught.value)
    assert "as a photo" in Bot._unprintable("IMG_0001.HEIC", "heic")


def test_pipeline_uses_the_smaller_cap(store):
    class Counting:
        is_dryrun = True

        def print_pdf(self, pdf, job_name):
            return {"pages": convert._page_count(pdf), "output": None}

    pipeline = Pipeline(store, Counting(), page_cap=20)
    long = PrintJob("telegram", "guest:tg:9", "long.pdf", make_pdf(7), page_cap=5, guest_tag="k")
    r = pipeline.process(long)
    assert not r.ok and r.reason == "over_page_cap" and r.detail == "7 pages, the limit is 5"
    wide = PrintJob("telegram", "guest:tg:9", "wide.pdf", make_pdf(25), page_cap=50, guest_tag="k")
    assert pipeline.process(wide).detail == "25 pages, the limit is 20"   # a guest cap never raises the owner's
    text = PrintJob("telegram", "guest:tg:9", "big.txt", b"\n" * 5000, page_cap=5, guest_tag="k")
    r = pipeline.process(text)
    assert r.reason == "over_page_cap" and "more than 5 pages" in r.detail
    assert pipeline.process(PrintJob("telegram", "tg:1", "ok.pdf", make_pdf(3), page_cap=5)).ok
    pipeline.pool.shutdown(wait=False)
