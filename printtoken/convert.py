"""Everything becomes a PDF, then the PDF gets printed.

The file type is decided from the first bytes of the file, never from its name or the Content-Type
the sender wrote, because mail clients routinely label PDFs application/octet-stream and a sender
can label anything as anything.
"""
from __future__ import annotations

import io
import textwrap

import pymupdf
from PIL import Image, ImageOps, UnidentifiedImageError

LETTER_W, LETTER_H = 612, 792       # points
MARGIN = 54                         # 0.75 inch
FONT_SIZE, LINE_H = 10, 12
CHARS_PER_LINE = 84                 # Courier 10pt is 6pt wide: 84 * 6 = 504 = 612 - 2 * 54
LINES_PER_PAGE = (LETTER_H - 2 * MARGIN) // LINE_H
MAX_TEXT_BYTES = 1024 * 1024        # plain text above this is thousands of pages, turned away unread


class Unsupported(Exception):
    """The file cannot be printed (type not handled yet, damaged, empty, or locked)."""


class TooLong(Unsupported):
    """More pages than the cap, known before a single page is drawn. A 20 MB text file would
    otherwise be laid out in full (hundreds of thousands of pages) before the cap was checked."""


def sniff(data: bytes) -> str:
    if not data:
        return "empty"
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypheif"):
        return "heic"               # an iPhone photo sent as a file instead of as a photo
    if data.startswith(b"PK\x03\x04"):
        return "zip"                # .docx and friends are zip files; not handled yet
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "unknown"
    return "unknown" if "\x00" in text else "text"


def to_pdf(data: bytes, page_cap: int | None = None) -> tuple[bytes, int, str]:
    """Return (pdf_bytes, page_count, detected_kind) or raise Unsupported. With `page_cap`, plain
    text that would run past the cap is refused before it is laid out."""
    kind = sniff(data)
    if kind == "pdf":
        return _check_pdf(data), _page_count(data), kind
    if kind in ("png", "jpeg"):
        return _image_to_pdf(data), 1, kind
    if kind == "text":
        if len(data) > MAX_TEXT_BYTES:
            raise TooLong(f"the text file is {len(data) // 1024} KB, far more than the page limit holds")
        pdf = _text_to_pdf(data.decode("utf-8"), page_cap)
        return pdf, _page_count(pdf), kind
    if kind == "zip":
        raise Unsupported("Word and Office files are not supported yet, export it as a PDF and send that")
    if kind == "heic":
        # Read by the owner, who is asked for files rather than photos, and on the email path, so
        # the advice is the export. A guest, who may send photos, gets that advice from the bot.
        raise Unsupported("iPhone photos sent as files are HEIC, which is not supported yet. Export the photo "
                          "as a JPEG or PDF first")
    if kind == "empty":
        raise Unsupported("the file is empty")
    raise Unsupported("this file type is not supported (PDF, PNG, JPEG and plain text are)")


def _check_pdf(data: bytes) -> bytes:
    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            if doc.needs_pass:
                raise Unsupported("the PDF is password protected")
            if doc.page_count == 0:
                raise Unsupported("the PDF has no pages")
    except Unsupported:
        raise
    except Exception as exc:
        raise Unsupported(f"the PDF could not be opened ({exc.__class__.__name__})") from exc
    return data


def _page_count(pdf: bytes) -> int:
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        return doc.page_count


def _image_to_pdf(data: bytes) -> bytes:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise Unsupported("the image could not be opened") from exc
    # Phone cameras store the picture sideways and add an EXIF note saying "rotate me".
    # Without exif_transpose a portrait photo prints on its side.
    img = ImageOps.exif_transpose(img).convert("RGB")
    out = io.BytesIO()
    img.save(out, "PDF", resolution=150)
    return out.getvalue()


def _text_to_pdf(text: str, page_cap: int | None = None) -> bytes:
    limit = None if page_cap is None else page_cap * LINES_PER_PAGE
    lines: list[str] = []
    for raw_line in text.expandtabs(4).splitlines() or [""]:
        lines += textwrap.wrap(raw_line, CHARS_PER_LINE, replace_whitespace=False,
                               drop_whitespace=False) or [""]
        if limit is not None and len(lines) > limit:
            raise TooLong(f"more than {page_cap} pages of text")
    with pymupdf.open() as doc:
        for start in range(0, len(lines), LINES_PER_PAGE):
            page = doc.new_page(width=LETTER_W, height=LETTER_H)
            y = MARGIN + FONT_SIZE
            for line in lines[start:start + LINES_PER_PAGE]:
                page.insert_text((MARGIN, y), line, fontname="cour", fontsize=FONT_SIZE)
                y += LINE_H
        return doc.tobytes()
