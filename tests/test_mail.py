from email.message import EmailMessage

from conftest import BASE, DOMAIN, GOOD_AUTH, make_mail, make_png

from printtoken import mailparse
from printtoken.mailwatch import internaldate_ts
from printtoken.messages import should_notify_mail
from printtoken.policy import RateLimiter, decide_mail
from printtoken.tokens import Verdict, display_key

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


def test_pictures_inside_the_text_are_not_attachments():
    """A logo in a signature travels as an inline part of a multipart/related message, and the
    email package lists it with the attachments when nothing else is attached."""
    logo = [("image001.png", "image/png", make_png((200, 60)))]
    assert parse(make_mail(inline=logo, attachments=[])).attachments == []
    assert parse(make_mail(inline=logo, attachments=[], html="<p>no plain text part</p>")).attachments == []
    assert [a.filename for a in parse(make_mail(inline=logo)).attachments] == ["doc.pdf"]
    # A part of such a message that says it is an attachment is still one.
    flat = EmailMessage()
    flat["To"] = f"{BASE}+abcdefgh@{DOMAIN}"
    flat.set_content("<p>see attached</p>", subtype="html")
    flat.add_related(make_png((200, 60)), maintype="image", subtype="png", filename="image001.png",
                     disposition="inline", cid="<pic0>")
    flat.add_related(b"%PDF-1.7 fake", maintype="application", subtype="pdf", filename="flat.pdf",
                     disposition="attachment")
    assert [a.filename for a in parse(flat.as_bytes()).attachments] == ["flat.pdf"]


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


# Gmail copies the envelope sender into its header, in the spf comment and in smtp.mailfrom, and
# "=" is legal in an address. A sender who passes SPF for their own domain can therefore put the
# text "dmarc=pass" in front of Gmail's real verdict on a forged From.
INJECTED_AUTH = ("mx.google.com; dkim=none; spf=pass (google.com: domain of x.dmarc=pass@evil.example "
                 "designates 203.0.113.9 as permitted sender) smtp.mailfrom=x.dmarc=pass@evil.example; "
                 "dmarc=fail (p=REJECT sp=REJECT dis=QUARANTINE) header.from=example.com")
QUOTED_AUTH = ('mx.google.com; spf=pass smtp.mailfrom="x;dmarc=pass"@evil.example; '
               "dmarc=fail (p=REJECT sp=REJECT dis=QUARANTINE) header.from=example.com")


def test_dmarc_text_inside_the_sender_address_is_not_a_result():
    assert parse(make_mail(auth=INJECTED_AUTH)).auth == {"dkim": "none", "spf": "pass", "dmarc": "fail"}
    assert parse(make_mail(auth=QUOTED_AUTH)).auth == {"spf": "pass", "dmarc": "fail"}


def test_injected_dmarc_text_cannot_pass_the_allowlist(tokens):
    tag, mail = live_mail(tokens, frm="Alice <alice@example.com>", auth=INJECTED_AUTH)
    decision = decide_mail(mail, tokens, ACCEPT, frozenset({"alice@example.com"}))
    assert decision.reason == "dmarc_fail"
    assert tokens.check(tag) is Verdict.OK


def test_comments_are_skipped_but_results_after_them_are_read():
    auth = "mx.google.com; dkim=pass (good (nested) signature) header.i=@example.com; dmarc=pass (p=NONE)"
    assert parse(make_mail(auth=auth)).auth == {"dkim": "pass", "dmarc": "pass"}


# --- a guest key pasted into the subject or the body --------------------------------------------

REQUEST = f"{BASE}+print@{DOMAIN}"


def guest_key(tokens):
    tag, _ = tokens.mint_guest("tg:1", "prof", 7)
    return tag, display_key(tag)


def test_key_in_the_subject_is_held_for_one_guest_print(tokens):
    tag, shown = guest_key(tokens)
    decision = decide_mail(parse(make_mail(to=REQUEST, subject=f"my page {shown}")), tokens, ACCEPT)
    assert decision.ok and decision.guest and decision.tag == tag and len(decision.files) == 1
    assert tokens.check(tag) is Verdict.RESERVED             # held for this one print, not burned


def test_key_in_the_body_works_too(tokens):
    tag, shown = guest_key(tokens)
    body = f"Hello,\n\nhere is my file. The key is {shown.lower()}\n\nThanks,\nTim\n"
    decision = decide_mail(parse(make_mail(to=REQUEST, subject="one page", body=body)), tokens, ACCEPT)
    assert decision.ok and decision.guest and decision.tag == tag


def test_key_in_a_body_that_is_only_html(tokens):
    tag, shown = guest_key(tokens)
    page = f"<html><body><p>Key: <b>{shown[:7]}</b>{shown[7:]}</p><br>sent from my phone</body></html>"
    mail = parse(make_mail(to=REQUEST, subject="print", html=page))
    assert "<b>" not in mail.text and decide_mail(mail, tokens, ACCEPT).tag == tag


def test_key_in_the_subject_needs_no_special_address(tokens):
    tag, shown = guest_key(tokens)
    decision = decide_mail(parse(make_mail(to=f"{BASE}@{DOMAIN}", subject=shown)), tokens, ACCEPT)
    assert decision.ok and decision.guest and decision.tag == tag


def test_request_address_without_a_key_is_turned_away_quietly(tokens):
    guest_key(tokens)
    decision = decide_mail(parse(make_mail(to=REQUEST)), tokens, ACCEPT)
    assert not decision.ok and decision.reason == "no_key" and not should_notify_mail("no_key")


def test_ordinary_words_are_never_taken_for_a_key(tokens):
    guest_key(tokens)
    plain = f"{BASE}@{DOMAIN}"
    assert decide_mail(parse(make_mail(to=plain, subject="your bank info")), tokens, ACCEPT).reason == "no_tag"
    assert decide_mail(parse(make_mail(to=plain, subject="abcdefghijkl")), tokens, ACCEPT).reason == "no_tag"
    looks_like = decide_mail(parse(make_mail(to=plain, subject="PT team meet soon")), tokens, ACCEPT)
    assert looks_like.reason == "unknown_tag" and not should_notify_mail("unknown_tag")


def test_a_pasted_key_still_needs_the_sender_check_and_is_not_used_up_by_a_forgery(tokens):
    tag, shown = guest_key(tokens)
    forged = make_mail(to=REQUEST, subject=shown, auth="mx.google.com; spf=fail; dmarc=fail header.from=example.com")
    assert decide_mail(parse(forged), tokens, ACCEPT).reason == "dmarc_fail"
    assert tokens.check(tag) is Verdict.OK
    assert decide_mail(parse(make_mail(to=REQUEST, subject=shown)), tokens, ACCEPT).ok


def test_a_pasted_key_works_once(tokens):
    tag, shown = guest_key(tokens)
    assert decide_mail(parse(make_mail(to=REQUEST, subject=shown)), tokens, ACCEPT).ok
    again = decide_mail(parse(make_mail(to=REQUEST, body=shown)), tokens, ACCEPT)
    assert not again.ok and again.reason == Verdict.RESERVED.value
    tokens.spend(tag)
    assert decide_mail(parse(make_mail(to=REQUEST, subject=shown)), tokens, ACCEPT).reason == "replayed_tag"


def test_a_pasted_key_without_a_file_or_with_a_word_file_costs_nothing(tokens):
    tag, shown = guest_key(tokens)
    assert decide_mail(parse(make_mail(to=REQUEST, subject=shown, attachments=[])), tokens, ACCEPT).reason == "no_attachment"
    word = [("a.docx", "application/zip", b"PK\x03\x04" + b"0" * 50)]
    assert decide_mail(parse(make_mail(to=REQUEST, subject=shown, attachments=word)), tokens, ACCEPT).reason == "unsupported_type"
    assert tokens.check(tag) is Verdict.OK


def test_pasted_keys_are_ignored_when_guest_keys_are_off(tokens):
    tag, shown = guest_key(tokens)
    off = decide_mail(parse(make_mail(to=REQUEST, subject=shown)), tokens, ACCEPT, guest_enabled=False)
    assert not off.ok and off.reason == "no_key" and tokens.check(tag) is Verdict.OK


def test_a_one_time_address_still_wins_over_text(tokens):
    tag, mail = live_mail(tokens, subject="PT-ABCD-EFGH-IJKL")
    decision = decide_mail(mail, tokens, ACCEPT)
    assert decision.ok and decision.tag == tag and not decision.guest


def test_a_body_that_cannot_be_read_is_an_empty_one():
    raw = (b"From: a@example.com\nTo: printbox+print@gmail.com\nSubject: x\n"
           b'Content-Type: text/plain; charset="no-such-charset"\nContent-Transfer-Encoding: base64\n\n'
           b"!!!not base64!!!\n")
    mail = parse(raw)
    assert mail.tags == ["print"] and isinstance(mail.text, str)
