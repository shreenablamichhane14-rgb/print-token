"""Typed notes: the tidy / html / pdf renderer, and the draft, /go and /cancel flow in the bot."""
import asyncio
import re
import time

import pymupdf
import pytest
from test_bot import OWNER, make_bot, msg

from printtoken import convert, telegram
from printtoken.notes import Block, tidy, to_html, to_pdf
from printtoken.policy import RateLimiter


# tidy

def test_line_endings_tabs_and_trailing_spaces_are_normalised():
    text = "A first line long enough that it cannot be mistaken for the title of the note.  \r\n\tsecond\tline  \r\n"
    blocks = tidy(text)
    assert len(blocks) == 1 and blocks[0].kind == "paragraph"
    assert "\t" not in blocks[0].text and "\r" not in blocks[0].text
    assert blocks[0].text.split()[-2:] == ["second", "line"]
    assert not blocks[0].text.endswith(" ")


def test_single_newlines_join_and_blank_lines_split():
    blocks = tidy("This sentence got cut\nby the phone keyboard, as they do.\n\n\n\nA second paragraph.")
    assert [b.kind for b in blocks] == ["paragraph", "paragraph"]
    assert blocks[0].text == "This sentence got cut by the phone keyboard, as they do."
    assert blocks[1].text == "A second paragraph."


def test_short_first_line_on_its_own_is_the_title():
    blocks = tidy("Parcel for number 12\n\nIt is on the shelf by the door.")
    assert blocks == [Block("title", "Parcel for number 12"), Block("paragraph", "It is on the shelf by the door.")]
    assert tidy("Shopping\n- milk\n- eggs")[0].kind == "title"          # a list after it counts as standing alone


def test_first_line_is_not_a_title_when_long_a_sentence_or_alone():
    assert tidy("x" * 61 + "\n\nmore text")[0].kind == "paragraph"
    assert tidy("Ends with a period.\n\nmore text")[0].kind == "paragraph"
    assert tidy("Hi neighbour,\n\nmore text")[0].kind == "paragraph"
    assert tidy("Only line") == [Block("paragraph", "Only line")]
    assert tidy("Cut by the phone\nso it carries on here.") == [Block("paragraph", "Cut by the phone so it carries on here.")]


def test_list_lines_become_items():
    blocks = tidy("Things to do\n\n- milk\n* eggs\n1. call the office\n2) email back\n\nthen a paragraph")
    assert [b.kind for b in blocks] == ["title", "item", "item", "item", "item", "paragraph"]
    assert [b.text for b in blocks[1:5]] == ["milk", "eggs", "call the office", "email back"]
    assert [b.marker for b in blocks[1:5]] == ["", "", "1", "2"]


def test_item_continuation_lines_are_joined():
    blocks = tidy("- a long item that the phone\nsplit in two\n- next")
    assert blocks == [Block("item", "a long item that the phone split in two"), Block("item", "next")]


def test_control_characters_and_zero_width_spaces_are_dropped():
    assert tidy("\u200b\ufeff\x00") == []
    assert tidy("a\x07b.") == [Block("paragraph", "ab.")]


# to_html

def test_html_escapes_every_piece_of_user_text():
    out = to_html([Block("title", "<b>bold?</b>"), Block("paragraph", "a & b"), Block("item", "<i>x</i>")])
    assert "&lt;b&gt;bold?&lt;/b&gt;" in out and "a &amp; b" in out and "&lt;i&gt;x&lt;/i&gt;" in out
    assert "<b>" not in out and "<i>" not in out
    assert out.startswith("<html><body><h1>") and "<ul><li>" in out


def test_numbered_items_make_an_ordered_list_and_mixed_ones_a_bulleted_one():
    assert "<ol><li>a</li><li>b</li></ol>" in to_html([Block("item", "a", "1"), Block("item", "b", "2")])
    assert "<ul>" in to_html([Block("item", "a"), Block("item", "b", "2")])


def test_long_runs_get_invisible_break_points():
    out = to_html([Block("paragraph", "x" * 100)])
    assert out.count("\u200b") == 2 and "x" * 41 not in out


# to_pdf

def test_short_note_is_one_page_with_a_footer():
    pdf, pages = to_pdf("Parcel\n\nIt is on the shelf by the door.", now=1_800_000_000)
    assert pdf.startswith(b"%PDF-") and pages == 1
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        assert doc.page_count == 1
        text = doc[0].get_text()
        assert "Parcel" in text and "It is on the shelf by the door." in text
        assert "Note from Telegram" in text and "page 1 of 1" in text
        fonts = [f[3] for f in doc[0].get_fonts()]
        assert any("Nimbus Roman" in name for name in fonts)     # what "Times New Roman" resolves to in MuPDF
        assert any("Helvetica" in name for name in fonts)        # the footer


def test_text_stays_inside_the_one_inch_margins():
    pdf, _ = to_pdf("Margins\n\n" + "word " * 400)
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        blocks = doc[0].get_text("blocks")
        assert min(b[0] for b in blocks) >= 72 - 2 and max(b[2] for b in blocks) <= 540 + 2
        assert min(b[1] for b in blocks) >= 72 - 2


def test_long_note_paginates_and_numbers_every_page():
    paragraphs: list[str] = []
    pdf, pages = b"", 0
    for i in range(80):
        paragraphs.append(f"Paragraph {i + 1}. " + "word " * 60)
        pdf, pages = to_pdf("\n\n".join(paragraphs))
        if pages == 2:
            break
    assert pages == 2
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        assert doc.page_count == 2
        assert "page 1 of 2" in doc[0].get_text() and "page 2 of 2" in doc[1].get_text()


def test_empty_note_is_refused_before_any_page_is_made():
    for text in ("", "   \n\n\t", "\u200b\ufeff"):
        with pytest.raises(convert.Unsupported, match="empty"):
            to_pdf(text)


def test_accented_and_non_latin_text_does_not_raise():
    text = "Café résumé\n\nnaïve ünï 日本語 नमस्ते"
    pdf, pages = to_pdf(text)
    assert pages == 1
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        assert "Café" in doc[0].get_text()


def test_fonts_are_subset_so_a_note_stays_small():
    # DocumentWriter embeds whole fonts (Droid Sans Fallback alone is 3.5 MB once a CJK character
    # appears); to_pdf cuts them down to the glyphs used, so a note is kept and spooled in kilobytes.
    for text in ("Parcel\n\nIt is on the shelf by the door.", "Parcel\n\nIt is on the shelf. 日本語 नमस्ते"):
        pdf, pages = to_pdf(text, now=1_800_000_000)
        assert pages == 1 and len(pdf) < 100_000, len(pdf)
        with pymupdf.open(stream=pdf, filetype="pdf") as doc:
            assert "It is on the shelf" in doc[0].get_text() and "page 1 of 1" in doc[0].get_text()


def test_long_word_wraps_inside_the_text_rectangle():
    pdf, pages = to_pdf("A pasted link follows.\n\n" + "x" * 300)
    assert pages == 1
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        assert all(b[2] <= 540 + 2 for b in doc[0].get_text("blocks"))
        # extraction shows a break point as a space; every character is still there
        assert "x" * 300 in re.sub(r"\s", "", doc[0].get_text())


def test_page_guard_stops_a_runaway_layout():
    with pytest.raises(convert.Unsupported, match="too long"):
        to_pdf("\n\n".join("p" for _ in range(400)), max_pages=2)


# the bot

def test_typed_text_is_saved_as_a_draft_and_not_printed(tmp_path, store, tokens):
    bot, tg, pipeline = make_bot(tmp_path, store, tokens)
    text = "Parcel for number 12\n\nIt arrived this afternoon. Knock any time after six."
    asyncio.run(bot.handle(msg(text=text)))
    assert tg.sent == [(OWNER, f"Note saved: {len(text.split())} words, 1 page. Send /go to print it, "
                               "/cancel to drop it, or send more text to replace it.")]
    assert pipeline.queue.items == []
    assert bot.drafts[OWNER].pages == 1 and bot.drafts[OWNER].pdf.startswith(b"%PDF-")
    assert store.scalar("SELECT outcome FROM events WHERE kind = 'note'") == "drafted"
    assert "/go" in bot.help


def test_go_queues_the_draft_as_a_telegram_job(tmp_path, store, tokens):
    bot, tg, pipeline = make_bot(tmp_path, store, tokens)
    asyncio.run(bot.handle(msg(text="Hello\n\nWorld.")))
    asyncio.run(bot.handle(msg(text="/go")))
    job = pipeline.queue.items[0]
    assert job.data.startswith(b"%PDF-") and re.fullmatch(r"note-\d{4}-\d{2}-\d{2}-\d{4}\.pdf", job.filename)
    assert (job.channel, job.sender, job.reply_chat) == ("telegram", f"tg:{OWNER}", OWNER)
    assert tg.sent[-1] == (OWNER, "Printing your note (1 page).")
    assert OWNER not in bot.drafts
    asyncio.run(bot.handle(msg(text="/go")))                   # the draft was used up
    assert tg.sent[-1][1] == "No note waiting. Send the text again." and len(pipeline.queue.items) == 1


def test_go_with_nothing_waiting(tmp_path, store, tokens):
    bot, tg, pipeline = make_bot(tmp_path, store, tokens)
    asyncio.run(bot.handle(msg(text="/go")))
    assert tg.sent == [(OWNER, "No note waiting. Send the text again.")] and pipeline.queue.items == []


def test_cancel_drops_the_draft(tmp_path, store, tokens):
    bot, tg, pipeline = make_bot(tmp_path, store, tokens)
    asyncio.run(bot.handle(msg(text="Hello\n\nWorld.")))
    asyncio.run(bot.handle(msg(text="/cancel")))
    assert tg.sent[-1][1] == "Dropped the note." and OWNER not in bot.drafts
    asyncio.run(bot.handle(msg(text="/go")))
    assert tg.sent[-1][1].startswith("No note waiting") and pipeline.queue.items == []
    asyncio.run(bot.handle(msg(text="/cancel")))
    assert tg.sent[-1][1] == "No note waiting."


def test_expired_draft_is_forgotten(tmp_path, store, tokens, monkeypatch):
    bot, tg, pipeline = make_bot(tmp_path, store, tokens)
    asyncio.run(bot.handle(msg(text="Hello\n\nWorld.")))
    later = time.time() + telegram.DRAFT_TTL + 1
    monkeypatch.setattr(telegram.time, "time", lambda: later)   # asyncio keeps its own monotonic clock
    asyncio.run(bot.handle(msg(text="/go")))
    assert tg.sent[-1][1].startswith("No note waiting") and pipeline.queue.items == []
    assert OWNER not in bot.drafts


def test_new_text_replaces_the_draft(tmp_path, store, tokens):
    bot, tg, _ = make_bot(tmp_path, store, tokens)
    asyncio.run(bot.handle(msg(text="first version of the note.")))
    asyncio.run(bot.handle(msg(text="the second version, which is longer.")))
    assert bot.drafts[OWNER].words == 6 and tg.sent[-1][1].startswith("Note saved: 6 words")


def test_go_respects_the_hourly_print_limit(tmp_path, store, tokens):
    bot, tg, pipeline = make_bot(tmp_path, store, tokens)
    bot.limiter = RateLimiter(0)
    asyncio.run(bot.handle(msg(text="Hello\n\nWorld.")))
    asyncio.run(bot.handle(msg(text="/go")))
    assert "Hourly print limit" in tg.sent[-1][1] and pipeline.queue.items == []
    assert OWNER in bot.drafts                                # kept for a later /go


def test_unknown_commands_still_get_help_and_never_print(tmp_path, store, tokens):
    bot, tg, pipeline = make_bot(tmp_path, store, tokens)
    asyncio.run(bot.handle(msg(text="Hello\n\nWorld.")))
    for text in ("/foo", "/gone", "/help"):
        asyncio.run(bot.handle(msg(text=text)))
        assert tg.sent[-1] == (OWNER, bot.help)
    assert pipeline.queue.items == [] and OWNER in bot.drafts


def test_unprintable_text_is_refused_politely(tmp_path, store, tokens):
    bot, tg, _ = make_bot(tmp_path, store, tokens)
    asyncio.run(bot.handle(msg(text="\u200b")))
    assert tg.sent == [(OWNER, "Nothing saved: the note is empty.")] and OWNER not in bot.drafts
    assert store.scalar("SELECT reason FROM events WHERE kind = 'note'") == "bad_note"
    asyncio.run(bot.handle(msg(text="p\n\n" * 1365)))         # 4096 characters, far over the page cap
    assert tg.sent[-1] == (OWNER, "Nothing saved: the note is too long (more than 20 pages).")
    assert OWNER not in bot.drafts
