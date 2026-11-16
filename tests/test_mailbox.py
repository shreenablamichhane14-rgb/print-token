"""The IMAP side against a fake server: where the watcher starts, what it fetches, and the
"UID n:*" quirk that would otherwise hand back the same message on every check."""
import pytest
from conftest import make_mail

from printtoken.config import Config
from printtoken.mailwatch import Mailbox


class FakeIMAP:
    def __init__(self, messages: dict[int, bytes], uidvalidity: int = 1):
        self.messages = dict(messages)
        self.uidvalidity = uidvalidity
        self._responses: dict[str, list] = {}
        self.body_fetches: list[int] = []

    def __call__(self, host, port, timeout=None):       # stands in for imaplib.IMAP4_SSL(...)
        return self

    def login(self, user, password):
        return "OK", [b"ok"]

    def select(self, mailbox, readonly=False):
        top = max(self.messages, default=0)
        self._responses = {"UIDVALIDITY": [str(self.uidvalidity).encode()], "UIDNEXT": [str(top + 1).encode()]}
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):
        return code, self._responses.pop(code, [None])

    def noop(self):
        return "OK", [b""]

    def logout(self):
        return "BYE", [b""]

    def uid(self, command, *args):
        if command == "SEARCH":
            start = int(args[-1].split()[1].split(":")[0])
            top = max(self.messages, default=0)
            found = [u for u in sorted(self.messages) if u >= start]
            if not found and top:
                found = [top]                        # the real quirk: n:* past the end gives the newest
            return "OK", [" ".join(map(str, found)).encode()]
        uid, what = int(args[0]), args[1]
        raw = self.messages[uid]
        if "HEADER.FIELDS" in what:
            header = raw.split(b"\n\n", 1)[0] + b"\n\n"
            meta = (f'1 (UID {uid} RFC822.SIZE {len(raw)} INTERNALDATE "25-Oct-2026 10:00:00 +0000" '
                    f'BODY[HEADER.FIELDS (TO CC DELIVERED-TO X-ORIGINAL-TO)] {{{len(header)}}}').encode()
            return "OK", [(meta, header), b")"]
        self.body_fetches.append(uid)
        return "OK", [(f"1 (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw), b")"]


def cfg(tmp_path, user="printbox@gmail.com"):
    return Config(printer="DRYRUN", data_dir=tmp_path, bot_token="x" * 20, allowed_tg_ids=frozenset({1}),
                  notify_chat_id=1, pin_hash="", imap_host="imap.example", imap_user=user, imap_password="pw")


TAGGED = make_mail(to="printbox+abcdefgh@gmail.com")
OTHER = make_mail(to="someone@example.com")
KEY_IN_SUBJECT = make_mail(to="printbox@gmail.com", subject="my page PT-ABCD-EFGH-IJKL")
TO_REQUEST = make_mail(to="printbox+print@gmail.com", body="the key is PT-ABCD-EFGH-IJKL")
PLAIN = make_mail(to="printbox@gmail.com", subject="your bank info", body="PT-ABCD-EFGH-IJKL")


def test_first_check_only_sets_the_starting_point(tmp_path, store):
    server = FakeIMAP({1: TAGGED, 2: OTHER})
    box = Mailbox(cfg(tmp_path), store, connect=server)
    assert box.fetch_new() == []                     # old mail is never printed
    assert store.kv_get("imap:printbox@gmail.com:last_uid") == "2"


def test_only_tagged_new_mail_is_downloaded(tmp_path, store):
    server = FakeIMAP({1: OTHER})
    box = Mailbox(cfg(tmp_path), store, connect=server)
    box.fetch_new()
    server.messages[2] = TAGGED
    server.messages[3] = OTHER
    found = box.fetch_new()
    assert [m.uid for m in found] == [2]
    assert server.body_fetches == [2]                # untagged mail: headers only, never the body
    assert found[0].internal_ts == 1792922400.0


def test_a_key_in_the_subject_or_mail_to_the_request_address_is_downloaded(tmp_path, store):
    server = FakeIMAP({1: OTHER})
    box = Mailbox(cfg(tmp_path), store, connect=server)
    box.fetch_new()
    server.messages.update({2: KEY_IN_SUBJECT, 3: PLAIN, 4: TO_REQUEST, 5: OTHER})
    assert [m.uid for m in box.fetch_new()] == [2, 4]
    # Mail to the plain address with nothing in its subject is never opened, whatever its body says.
    assert server.body_fetches == [2, 4]


def test_uid_quirk_does_not_repeat_the_last_message(tmp_path, store):
    server = FakeIMAP({1: OTHER})
    box = Mailbox(cfg(tmp_path), store, connect=server)
    box.fetch_new()
    server.messages[2] = TAGGED
    assert len(box.fetch_new()) == 1
    assert box.fetch_new() == []                     # server answers "2" to "UID 3:*", ignored
    assert server.body_fetches == [2]


def test_a_different_mailbox_starts_fresh(tmp_path, store):
    first = Mailbox(cfg(tmp_path, "oldbox@gmail.com"), store, connect=FakeIMAP({1: OTHER, 2: OTHER}))
    first.fetch_new()
    old_mail = {uid: TAGGED for uid in range(1, 21)}  # same UIDVALIDITY, numbers past the old position
    second = Mailbox(cfg(tmp_path, "printbox@gmail.com"), store, connect=FakeIMAP(old_mail))
    assert second.fetch_new() == []                  # does not reuse oldbox's position
    assert store.kv_get("imap:printbox@gmail.com:last_uid") == "20"


# Some malformed address headers make the email package raise, and anyone can send them to the base
# address, no +tag needed. Which ones depends on the Python version: "<" crashes 3.11 and 3.12.3 but
# not 3.12.10 or later, while a lone double quote crashes all of them. Keep several, so these tests
# mean something on whatever Python the gateway runs on.
MALFORMED = [b"<", b'"', b"a@[", b":;:", b".:"]


@pytest.mark.parametrize("value", MALFORMED)
def test_a_malformed_address_header_does_not_stop_the_mailbox(tmp_path, store, value):
    fake = FakeIMAP({1: TAGGED})
    box = Mailbox(cfg(tmp_path), store, connect=fake)
    assert box.fetch_new() == []
    fake.messages[2] = b"From: x@evil.example\nTo: " + value + b"\nSubject: hi\n\nhello\n"
    fake.messages[3] = TAGGED                    # a real print request right behind it
    assert [m.uid for m in box.fetch_new()] == [3]
    assert store.kv_get("imap:printbox@gmail.com:last_uid") == "3"


@pytest.mark.parametrize("value", MALFORMED)
def test_a_malformed_from_header_does_not_stop_the_parse(value):
    from printtoken import mailparse
    raw = b"From: " + value + b"\nTo: printbox+abcdefgh@gmail.com\n\nhello\n"
    assert mailparse.parse(raw, "printbox", "gmail.com").tags == ["abcdefgh"]


def test_one_mail_that_fails_does_not_drop_the_rest_of_the_batch(tmp_path, store):
    import asyncio
    from printtoken.mailwatch import FetchedMail, MailWatcher

    items = [FetchedMail(5, b"bad", 3, None, 0.0), FetchedMail(6, b"good", 4, None, 0.0)]
    watcher = MailWatcher(cfg(tmp_path), store, tokens=None, pipeline=None, notify_owner=None, limiter=None)
    watcher.box.fetch_new = lambda: items
    handled = []

    async def handle(item):
        if item.raw == b"bad":
            raise IndexError("string index out of range")
        handled.append(item.uid)

    watcher.handle = handle
    asyncio.run(watcher.check_once())
    assert handled == [6]
    assert store.query("SELECT reason, detail FROM events") == [("internal_error", "uid=5 IndexError")]
