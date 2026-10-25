from conftest import BASE, DOMAIN, GOOD_AUTH, make_mail, make_png

from printtoken import mailparse
from printtoken.mailwatch import internaldate_ts
from printtoken.policy import RateLimiter, decide_mail
from printtoken.tokens import Verdict

ACCEPT = frozenset({"pass"})


def parse(raw):
    return mailparse.parse(raw, BASE, DOMAIN)


def live_mail(tokens, **kw):
    tag, _ = tokens.mint("tg:1")
    kw.setdefault("to", f"{BASE}+{tag}@{DOMAIN}")
    return tag, parse(make_mail(**kw))


# --- reading the tag -------------------------------------------------------------------------

def test_tag_from_to_header():
    assert parse(make_mail()).tags == ["abcdefgh"]


def test_tag_ignores_case_and_gmail_dots():
    raw = make_mail(to=f"Print.Box+ABCDEFGH@GMAIL.com")
    assert parse(raw).tags == ["abcdefgh"]


def test_tag_from_cc_and_delivered_to():
    assert parse(make_mail(to="someone@example.com", cc=f"{BASE}+cccccccc@{DOMAIN}")).tags == ["cccccccc"]
    raw = make_mail(to="undisclosed-recipients:;", delivered_to=f"{BASE}+dddddddd@{DOMAIN}")
    assert parse(raw).tags == ["dddddddd"]


def test_other_mailbox_tags_are_ignored():
    raw = make_mail(to=f"someoneelse+abcdefgh@{DOMAIN}, {BASE}@{DOMAIN}")
    assert parse(raw).tags == []


# --- trusting authentication results ----------------------------------------------------------

def test_auth_results_read_from_top_header():
    assert parse(make_mail()).auth == {"dkim": "pass", "spf": "pass", "dmarc": "pass"}


def test_forged_lower_header_is_ignored():
    real = "mx.google.com; spf=fail smtp.mailfrom=ceo@bank.example; dmarc=fail (p=REJECT) header.from=bank.example"
    forged = "mx.google.com; dkim=pass; spf=pass; dmarc=pass header.from=bank.example"
    mail = parse(make_mail(auth=real, lower_auth=forged))
    assert mail.auth["dmarc"] == "fail"


def test_top_header_from_someone_else_is_not_trusted():
    mail = parse(make_mail(auth="mx.evil.example; dmarc=pass"))
    assert mail.auth == {}


def test_missing_auth_header():
    assert parse(make_mail(auth=None)).auth == {}


# --- the decision ----------------------------------------------------------------------------

def test_accepts_live_authenticated_mail_and_burns(tokens):
    tag, mail = live_mail(tokens)
    d = decide_mail(mail, tokens, ACCEPT)
    assert d.ok and d.reason == "accepted" and len(d.files) == 1
    assert tokens.check(tag) is Verdict.USED


def test_replay_is_rejected(tokens):
    tag, mail = live_mail(tokens)
    assert decide_mail(mail, tokens, ACCEPT).ok
    assert decide_mail(mail, tokens, ACCEPT).reason == "replayed_tag"


def test_forged_sender_does_not_burn_the_token(tokens):
    fail = "mx.google.com; spf=fail; dmarc=fail (p=REJECT) header.from=example.com"
    tag, forged = live_mail(tokens, auth=fail)
    d = decide_mail(forged, tokens, ACCEPT)
    assert not d.ok and d.reason == "dmarc_fail"
    assert tokens.check(tag) is Verdict.OK          # the real sender can still use it
    real = parse(make_mail(to=f"{BASE}+{tag}@{DOMAIN}"))
    assert decide_mail(real, tokens, ACCEPT).ok


def test_missing_auth_is_rejected(tokens):
    _, mail = live_mail(tokens, auth=None)
    assert decide_mail(mail, tokens, ACCEPT).reason == "dmarc_missing"


def test_unknown_expired_malformed_and_untagged(tokens):
    assert decide_mail(parse(make_mail(to=f"{BASE}+zzzzzzzz@{DOMAIN}")), tokens, ACCEPT).reason == "unknown_tag"
    assert decide_mail(parse(make_mail(to=f"{BASE}+hello@{DOMAIN}")), tokens, ACCEPT).reason == "malformed_tag"
    assert decide_mail(parse(make_mail(to=f"{BASE}@{DOMAIN}")), tokens, ACCEPT).reason == "no_tag"
    tag, _ = tokens.mint("tg:1", now=1000.0)
    late = parse(make_mail(to=f"{BASE}+{tag}@{DOMAIN}"))
    assert decide_mail(late, tokens, ACCEPT, now=1000.0 + 31 * 60).reason == "expired_tag"


def test_sender_allowlist(tokens):
    _, mail = live_mail(tokens)
    assert decide_mail(mail, tokens, ACCEPT, frozenset({"bob@example.com"})).reason == "sender_not_allowlisted"
    _, mail = live_mail(tokens)
    assert decide_mail(mail, tokens, ACCEPT, frozenset({"alice@example.com"})).ok


def test_no_attachment_burns_and_rejects(tokens):
    tag, mail = live_mail(tokens, attachments=[])
    assert decide_mail(mail, tokens, ACCEPT).reason == "no_attachment"
    assert tokens.check(tag) is Verdict.USED


def test_attachment_types_come_through(tokens):
    _, mail = live_mail(tokens, attachments=[("a.png", "image/png", make_png()),
                                             ("b.bin", "application/octet-stream", b"%PDF-1.7 fake")])
    d = decide_mail(mail, tokens, ACCEPT)
    assert [f.filename for f in d.files] == ["a.png", "b.bin"]


# --- small helpers ---------------------------------------------------------------------------

def test_internaldate_parsing():
    assert internaldate_ts(b"01-Jan-2026 00:00:00 +0000") == 1767225600.0
    assert internaldate_ts(b"31-Dec-2025 19:00:00 -0500") == 1767225600.0
    assert internaldate_ts(b"garbage") is None


def test_rate_limiter():
    r = RateLimiter(2, window=10)
    assert r.allow(0) and r.allow(1)
    assert not r.allow(2)
    assert r.allow(11)


def test_good_auth_constant_is_gmail_shaped():
    assert GOOD_AUTH.startswith("mx.google.com;")
