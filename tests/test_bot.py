import asyncio
import re

from conftest import make_pdf

from printtoken.config import Config
from printtoken.pins import PinGuard, make_hash
from printtoken.policy import RateLimiter
from printtoken.telegram import Bot

OWNER, STRANGER, PIN = 111, 999, "482913"


class FakeTG:
    def __init__(self, files=None):
        self.sent, self.deleted, self.files = [], [], files or {}

    async def send(self, chat, text):
        self.sent.append((chat, text))

    async def delete(self, chat, message_id):
        self.deleted.append(message_id)
        return True

    async def download(self, file_id):
        return self.files[file_id]


class FakeQueue:
    def __init__(self):
        self.items = []

    def qsize(self):
        return len(self.items)


class FakePipeline:
    def __init__(self):
        self.queue = FakeQueue()

    def submit(self, job):
        self.queue.items.append(job)
        return True


def make_bot(tmp_path, store, tokens, files=None, mail_enabled=True):
    cfg = Config(printer="DRYRUN", data_dir=tmp_path, bot_token="0" * 20, allowed_tg_ids=frozenset({OWNER}),
                 notify_chat_id=OWNER, pin_hash=make_hash(PIN), imap_host="imap.gmail.com",
                 imap_user="printbox@gmail.com", imap_password="x", mail_enabled=mail_enabled)
    tg, pipeline = FakeTG(files), FakePipeline()
    bot = Bot(cfg, tg, tokens, PinGuard(cfg.pin_hash, 5), pipeline, store, RateLimiter(20))
    return bot, tg, pipeline


def msg(user=OWNER, text=None, chat_type="private", **extra):
    m = {"message_id": 5, "date": 1_800_000_000, "from": {"id": user}, "chat": {"id": user, "type": chat_type}}
    if text is not None:
        m["text"] = text
    m.update(extra)
    return m


def test_stranger_gets_silence(tmp_path, store, tokens):
    bot, tg, _ = make_bot(tmp_path, store, tokens)
    asyncio.run(bot.handle(msg(user=STRANGER, text="/print 482913")))
    assert tg.sent == []
    assert store.scalar("SELECT reason FROM events") == "not_allowlisted"


def test_group_chats_are_ignored(tmp_path, store, tokens):
    bot, tg, _ = make_bot(tmp_path, store, tokens)
    asyncio.run(bot.handle(msg(text="/print 482913", chat_type="group")))
    assert tg.sent == [] and tokens.outstanding() == 0


def test_wrong_pin(tmp_path, store, tokens):
    bot, tg, _ = make_bot(tmp_path, store, tokens)
    asyncio.run(bot.handle(msg(text="/print 000000")))
    assert "Wrong PIN" in tg.sent[0][1]
    assert tg.deleted == [5]                       # the PIN message is removed from the chat
    assert tokens.outstanding() == 0


def test_right_pin_mints_an_address(tmp_path, store, tokens):
    bot, tg, _ = make_bot(tmp_path, store, tokens)
    asyncio.run(bot.handle(msg(text=f"/print {PIN}")))
    reply = tg.sent[0][1]
    assert re.search(r"printbox\+[a-z2-7]{8}@gmail\.com", reply)
    assert tokens.outstanding() == 1 and tg.deleted == [5]


def test_print_says_email_is_not_ready_while_mail_is_off(tmp_path, store, tokens):
    bot, tg, _ = make_bot(tmp_path, store, tokens, mail_enabled=False)
    asyncio.run(bot.handle(msg(text=f"/print {PIN}")))
    assert "isn't set up yet" in tg.sent[0][1]
    assert tokens.outstanding() == 0 and tg.deleted == [5]   # PIN message still removed


def test_unknown_file_type_is_refused_before_printing(tmp_path, store, tokens):
    from printtoken.pipeline import Pipeline, PrintJob

    class NoPaper:
        is_dryrun = True

        def print_pdf(self, pdf, job_name):
            raise AssertionError("an unknown file must never reach the printer")

    pipeline = Pipeline(store, NoPaper(), page_cap=20)
    result = pipeline.process(PrintJob("telegram", "tg:111", "virus.exe", b"MZ\x90\x00\x03\x00\x00\x00"))
    assert not result.ok and result.reason == "unsupported_type"
    pipeline.pool.shutdown(wait=False)


def test_print_is_off_until_a_pin_is_set(tmp_path, store, tokens):
    bot, tg, _ = make_bot(tmp_path, store, tokens)
    bot.pins = PinGuard("", 5)
    asyncio.run(bot.handle(msg(text="/print 482913")))
    assert "No PIN is set" in tg.sent[0][1] and tokens.outstanding() == 0


def test_photo_is_turned_away_with_advice(tmp_path, store, tokens):
    bot, tg, pipeline = make_bot(tmp_path, store, tokens)
    asyncio.run(bot.handle(msg(photo=[{"file_id": "p1", "width": 90, "height": 90}])))
    assert "as a File" in tg.sent[0][1] and pipeline.queue.items == []


def test_oversize_document(tmp_path, store, tokens):
    bot, tg, pipeline = make_bot(tmp_path, store, tokens)
    asyncio.run(bot.handle(msg(document={"file_id": "d1", "file_name": "big.pdf", "file_size": 30 * 1048576})))
    assert "20 MB" in tg.sent[0][1] and pipeline.queue.items == []


def test_document_is_queued(tmp_path, store, tokens):
    pdf = make_pdf()
    bot, tg, pipeline = make_bot(tmp_path, store, tokens, files={"d1": pdf})
    asyncio.run(bot.handle(msg(document={"file_id": "d1", "file_name": "a.pdf", "file_size": len(pdf)})))
    job = pipeline.queue.items[0]
    assert job.data == pdf and job.reply_chat == OWNER and job.channel == "telegram"
    assert tg.sent[0][1].startswith("Got a.pdf")
