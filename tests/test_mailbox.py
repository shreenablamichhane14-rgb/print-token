"""The IMAP side against a fake server: where the watcher starts, what it fetches, and the
"UID n:*" quirk that would otherwise hand back the same message on every check."""
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
