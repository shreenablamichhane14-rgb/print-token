import io
import sys
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

import pymupdf
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from printtoken.store import Store  # noqa: E402
from printtoken.tokens import Tokens  # noqa: E402

BASE = "printbox"
DOMAIN = "gmail.com"
GOOD_AUTH = ("mx.google.com; dkim=pass header.i=@example.com header.s=s1; "
             "spf=pass (google.com: domain of alice@example.com designates 203.0.113.5 as permitted sender) "
             "smtp.mailfrom=alice@example.com; dmarc=pass (p=REJECT sp=REJECT dis=NONE) header.from=example.com")


def make_pdf(pages: int = 1, landscape: bool = False) -> bytes:
    with pymupdf.open() as doc:
        for i in range(pages):
            w, h = (792, 612) if landscape else (612, 792)
            page = doc.new_page(width=w, height=h)
            page.insert_text((72, 72), f"page {i + 1}", fontsize=24)
        return doc.tobytes()


def make_png(size=(400, 300), color=(200, 30, 30)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "PNG")
    return out.getvalue()


def make_mail(to: str = f"{BASE}+abcdefgh@{DOMAIN}", frm: str = "Alice <alice@example.com>",
              auth: str | None = GOOD_AUTH, lower_auth: str | None = None,
              attachments=None, cc: str | None = None, delivered_to: str | None = None,
              subject: str = "print this", body: str = "see attached", html: str | None = None,
              inline=None) -> bytes:
    msg = EmailMessage()
    if auth is not None:
        msg["Authentication-Results"] = auth          # Gmail puts its own header on top
    if lower_auth is not None:
        msg["Authentication-Results"] = lower_auth    # anything below came from the sender
    if delivered_to:
        msg["Delivered-To"] = delivered_to
    msg["From"] = frm
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = "<test@example.com>"
    if inline:
        # Pictures the text itself shows (a logo in a signature, a pasted screenshot), laid out the
        # way Outlook and Gmail do it: multipart/related around the words, each picture an inline
        # part with a Content-ID. A real attachment then goes outside of that, as below.
        words = EmailMessage()
        if html is not None:
            words.set_content(html, subtype="html")
        else:
            words.set_content(body)
            shown = "".join(f'<img src="cid:pic{i}">' for i in range(len(inline)))
            words.add_alternative(f"<p>{body}</p>{shown}", subtype="html")
        msg["MIME-Version"] = "1.0"
        msg.make_related()
        msg.attach(words)
        for i, (name, ctype, data) in enumerate(inline):
            main, sub = ctype.split("/")
            msg.add_related(data, maintype=main, subtype=sub, filename=name, disposition="inline",
                            cid=f"<pic{i}>")
    elif html is not None:
        msg.set_content(html, subtype="html")        # a mail program that sends no plain text part
    else:
        msg.set_content(body)
    if attachments is None:
        attachments = [("doc.pdf", "application/pdf", make_pdf())]
    for name, ctype, data in attachments:
        main, sub = ctype.split("/")
        msg.add_attachment(data, maintype=main, subtype=sub, filename=name)
    return msg.as_bytes()


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def tokens(store):
    return Tokens(store, ttl_min=30, max_outstanding=3, mints_per_hour=3)
