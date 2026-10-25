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


class Unsupported(Exception):
    """The file cannot be printed (type not handled yet, damaged, empty, or locked)."""


def sniff(data: bytes) -> str:
    if not data:
        return "empty"
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data.startswith(b"PK\x03\x04"):
        return "zip"                # .docx and friends are zip files; not handled yet
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "unknown"
    return "unknown" if "\x00" in text else "text"


def to_pdf(data: bytes) -> tuple[bytes, int, str]:
    """Return (pdf_bytes, page_count, detected_kind) or raise Unsupported."""
    kind = sniff(data)
    if kind == "pdf":
        return _check_pdf(data), _page_count(data), kind
    if kind in ("png", "jpeg"):
        return _image_to_pdf(data), 1, kind
    if kind == "text":
        pdf = _text_to_pdf(data.decode("utf-8"))
        return pdf, _page_count(pdf), kind
    if kind == "zip":
        raise Unsupported("Word and Office files are not supported yet, send a PDF")
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


def _text_to_pdf(text: str) -> bytes:
    lines: list[str] = []
    for raw_line in text.expandtabs(4).splitlines() or [""]:
        lines += textwrap.wrap(raw_line, CHARS_PER_LINE, replace_whitespace=False,
                               drop_whitespace=False) or [""]
    with pymupdf.open() as doc:
        for start in range(0, len(lines), LINES_PER_PAGE):
            page = doc.new_page(width=LETTER_W, height=LETTER_H)
            y = MARGIN + FONT_SIZE
            for line in lines[start:start + LINES_PER_PAGE]:
                page.insert_text((MARGIN, y), line, fontname="cour", fontsize=FONT_SIZE)
                y += LINE_H
        return doc.tobytes()
