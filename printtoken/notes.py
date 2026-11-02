"""Typed Telegram messages become a clean page.

A .txt file keeps the monospaced path in convert.py, which suits code and logs. A note typed on a
phone is different: Enter gets pressed wherever the screen wrapped, nobody wants Courier for a
letter, and the page should look like it came out of a word processor. So a typed note is tidied
into a title, paragraphs and list items, then laid out by PyMuPDF's Story engine (the HTML and
CSS layout engine inside MuPDF) with a Times-style serif, 1 inch margins and a small footer.

Pitfall for whoever changes the layout loop: Story.place() hands back the same content again until
draw() has been called for that page, so a loop that only calls place() to count the pages first
never finishes. Each page below is placed and drawn in turn, and the footer, which needs the total
page count, goes on afterwards by reopening the finished PDF.
"""
from __future__ import annotations

import html
import io
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime

import pymupdf

from .convert import Unsupported

log = logging.getLogger("printtoken.notes")

PAGE_W, PAGE_H = 612, 792           # Letter, in points
MARGIN = 72                         # 1 inch on every side
FOOTER_Y = PAGE_H - 40              # footer baseline, inside the bottom margin
TITLE_MAX = 60                      # a first line longer than this is a paragraph, not a title
LONG_WORD = 40                      # a run without spaces longer than this gets break points
MAX_PAGES = 500                     # layout guard, far above anything one chat message can make

# "Times New Roman" resolves to Nimbus Roman, MuPDF's Times clone; a bare "serif" would give
# Charis SIL instead. The footer is drawn separately in Helvetica. MuPDF gives body a margin of
# its own, so it is zeroed here and the margins come from the text rectangle alone.
CSS = """
body { font-family: "Times New Roman", Times, serif; font-size: 12pt; line-height: 1.35; margin: 0; }
h1 { font-size: 16pt; font-weight: bold; margin: 0 0 10pt 0; }
p { margin: 0 0 8pt 0; }
ul, ol { margin: 0 0 8pt 24pt; }
li { margin: 0 0 2pt 0; }
"""

ITEM_RE = re.compile(r"^(?:([-*•])|(\d{1,3})[.)])\s+(\S.*)$")
# Control characters and the zero-width marks phones paste in; tab and newline stay.
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u200b\ufeff]")
LONG_RUN_RE = re.compile(rf"\S{{{LONG_WORD + 1},}}")


@dataclass
class Block:
    kind: str           # "title", "paragraph" or "item"
    text: str
    marker: str = ""    # items only: "" for a bullet, the number for a "1." or "2)" line


def tidy(text: str) -> list[Block]:
    """Raw message text to blocks. A blank line ends a block and single newlines inside one are
    joined with spaces (phones wrap at odd places). Lines starting with "- ", "* " or "1." are
    items. A short first line that stands on its own, with more text after it, is the title."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4)
    text = CONTROL_RE.sub("", text)
    lines = [line.strip() for line in text.split("\n")]
    title = _take_title(lines)
    blocks: list[Block] = []
    current: list[str] = []
    kind, marker = "paragraph", ""

    def flush() -> None:
        if current:
            blocks.append(Block(kind, " ".join(current), marker))
            current.clear()

    for line in lines:
        if not line:
            flush()
            continue
        item = ITEM_RE.match(line)
        if item:
            flush()
            kind, marker = "item", item.group(2) or ""
            current.append(item.group(3).strip())
        else:
            if not current:
                kind, marker = "paragraph", ""
            current.append(line)
    flush()
    return ([title] if title else []) + blocks


def _take_title(lines: list[str]) -> Block | None:
    """Pop the first line off `lines` as the title when it is short, not a sentence or an item,
    stands alone (a blank line or a list follows it) and more text comes after it."""
    first = next((i for i, line in enumerate(lines) if line), None)
    if first is None:
        return None
    candidate, rest = lines[first], lines[first + 1:]
    stands_alone = not rest or not rest[0] or bool(ITEM_RE.match(rest[0]))
    looks_like_title = (len(candidate) <= TITLE_MAX and not candidate.endswith((".", ",", ";", ":"))
                        and not ITEM_RE.match(candidate))
    if not (stands_alone and any(rest) and looks_like_title):
        return None
    del lines[:first + 1]
    return Block("title", candidate)


def to_html(blocks: list[Block]) -> str:
    """Every piece of user text goes through html.escape, so a note containing <b> prints as <b>.
    Consecutive items form one list, numbered when every item carried a number."""
    parts: list[str] = []
    i = 0
    while i < len(blocks):
        block = blocks[i]
        if block.kind != "item":
            tag = "h1" if block.kind == "title" else "p"
            parts.append(f"<{tag}>{_escape(block.text)}</{tag}>")
            i += 1
            continue
        j = i
        while j < len(blocks) and blocks[j].kind == "item":
            j += 1
        run = blocks[i:j]
        tag = "ol" if all(b.marker for b in run) else "ul"
        parts.append(f"<{tag}>" + "".join(f"<li>{_escape(b.text)}</li>" for b in run) + f"</{tag}>")
        i = j
    return "<html><body>" + "".join(parts) + "</body></html>"


def _escape(text: str) -> str:
    # MuPDF only breaks lines at spaces, so a pasted link or hash would run off the right edge.
    # A zero-width space every LONG_WORD characters gives it somewhere to wrap. It is invisible on
    # the page; text extraction shows it as a space.
    def split(match: re.Match) -> str:
        run = match.group(0)
        return "\u200b".join(run[k:k + LONG_WORD] for k in range(0, len(run), LONG_WORD))
    return html.escape(LONG_RUN_RE.sub(split, text))


def to_pdf(text: str, now: float | None = None, max_pages: int = MAX_PAGES) -> tuple[bytes, int]:
    """Lay the note out on Letter pages and return (pdf_bytes, page_count). Raises Unsupported for
    an empty note or one that needs more than `max_pages` pages."""
    blocks = tidy(text)
    if not blocks:
        raise Unsupported("the note is empty")
    story = pymupdf.Story(html=to_html(blocks), user_css=CSS)
    page_rect = pymupdf.Rect(0, 0, PAGE_W, PAGE_H)
    text_rect = pymupdf.Rect(MARGIN, MARGIN, PAGE_W - MARGIN, PAGE_H - MARGIN)
    buf = io.BytesIO()
    writer = pymupdf.DocumentWriter(buf)
    more, pages = 1, 0
    try:
        while more:
            device = writer.begin_page(page_rect)
            more, _filled = story.place(text_rect)
            story.draw(device)
            writer.end_page()
            pages += 1
            if more and pages >= max_pages:
                raise Unsupported(f"the note is too long (more than {max_pages} pages)")
    finally:
        writer.close()
    stamp = datetime.fromtimestamp(time.time() if now is None else now).strftime("%Y-%m-%d %H:%M")
    with pymupdf.open(stream=buf.getvalue(), filetype="pdf") as doc:
        for number, page in enumerate(doc, start=1):
            page.insert_text((MARGIN, FOOTER_Y), f"Note from Telegram, {stamp}, page {number} of {pages}",
                             fontsize=8, fontname="helv", color=(0.5, 0.5, 0.5))
        # DocumentWriter embeds every font it touched whole (120 KB for a Latin note, 3.5 MB once
        # one CJK character pulls in Droid Sans Fallback), so the fonts are cut down to the glyphs
        # used before the bytes are kept, logged and spooled. PyMuPDF calls its subsetter new, so
        # a failure there keeps the full fonts rather than losing the note.
        try:
            doc.subset_fonts()
        except Exception as exc:
            log.warning("font subsetting failed, keeping the full fonts: %s", exc.__class__.__name__)
        return doc.tobytes(garbage=3, deflate=True), pages
