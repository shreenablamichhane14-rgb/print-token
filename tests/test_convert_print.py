import io
import time
from pathlib import Path

import pymupdf
import pytest
from conftest import make_pdf, make_png
from PIL import Image

from printtoken import convert
from printtoken.pins import PinGuard, make_hash, matches
from printtoken.printer import DRYRUN, MS_PDF, GdiPrinter, installed_printers


def test_sniff_ignores_names_and_labels():
    assert convert.sniff(make_pdf()) == "pdf"
    assert convert.sniff(make_png()) == "png"
    assert convert.sniff(b"\xff\xd8\xff\xe0rest") == "jpeg"
    assert convert.sniff(b"PK\x03\x04rest") == "zip"
    assert convert.sniff("hello\nworld".encode()) == "text"
    assert convert.sniff(b"\x00\x01\x02\xff\xfe") == "unknown"
    assert convert.sniff(b"") == "empty"


def test_pdf_passthrough_counts_pages():
    pdf, pages, kind = convert.to_pdf(make_pdf(3))
    assert (pages, kind) == (3, "pdf")


def test_image_becomes_one_page():
    pdf, pages, kind = convert.to_pdf(make_png())
    assert (pages, kind) == (1, "png")
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        assert doc.page_count == 1


def _red_mark_side(pdf: bytes) -> str:
    """Which edge of page 1 the red mark is nearest to."""
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        pix = doc[0].get_pixmap(dpi=20)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    reds = [(x, y) for x in range(img.width) for y in range(img.height)
            if img.getpixel((x, y))[0] > 180 and img.getpixel((x, y))[1] < 80]
    cx = sum(x for x, _ in reds) / len(reds) / img.width
    cy = sum(y for _, y in reds) / len(reds) / img.height
    return min((cy, "top"), (1 - cy, "bottom"), (cx, "left"), (1 - cx, "right"))[1]


def test_exif_rotation_is_applied():
    # Draw a portrait picture with a red bar at the top, then store it the way a phone does:
    # pixels turned 90 degrees counter-clockwise plus EXIF orientation 6 ("turn me clockwise").
    upright = Image.new("RGB", (300, 400), (255, 255, 255))
    upright.paste((220, 0, 0), (0, 0, 300, 60))
    stored = upright.rotate(90, expand=True)
    exif = stored.getexif()
    exif[0x0112] = 6
    buf = io.BytesIO()
    stored.save(buf, "JPEG", exif=exif)
    pdf, _, _ = convert.to_pdf(buf.getvalue())
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        rect = doc[0].rect
        assert rect.height > rect.width
    assert _red_mark_side(pdf) == "top"


def test_text_paginates():
    text = "\n".join(f"line {i}" for i in range(150))
    pdf, pages, kind = convert.to_pdf(text.encode())
    assert kind == "text" and pages == 3            # 56 lines a page


def test_rejections():
    for data in (b"", b"PK\x03\x04docx", b"\x00\x00\xff\xfe", b"%PDF-1.7 not really a pdf"):
        with pytest.raises(convert.Unsupported):
            convert.to_pdf(data)


def test_encrypted_pdf_rejected():
    with pymupdf.open() as doc:
        doc.new_page()
        locked = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u")
    with pytest.raises(convert.Unsupported, match="password"):
        convert.to_pdf(locked)


def test_pin_hash_roundtrip_and_lockout():
    stored = make_hash("482913")
    assert matches("482913", stored) and not matches("482914", stored)
    assert "482913" not in stored
    guard = PinGuard(stored, max_failures_per_hour=3)
    for _ in range(3):
        assert not guard.verify(7, "000000", now=100)
    assert guard.locked(7, now=100)
    assert not guard.verify(7, "482913", now=101)   # locked even with the right PIN
    assert guard.verify(7, "482913", now=100 + 3601)
    assert guard.verify(8, "482913", now=101)       # other users are not affected


def _wait_for(path: Path, timeout: float = 30) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if path.exists() and path.stat().st_size > 0:
            try:
                with pymupdf.open(path):
                    return True
            except Exception:
                pass
        time.sleep(0.25)
    return False


@pytest.mark.skipif(MS_PDF not in installed_printers(), reason="needs Microsoft Print to PDF")
def test_dryrun_goes_through_the_real_gdi_path(tmp_path):
    info = GdiPrinter(DRYRUN, tmp_path).print_pdf(make_pdf(2), "pytest two pages")
    out = Path(info["output"])
    assert info["pages"] == 2
    assert _wait_for(out), "spooler never wrote the output file"
    with pymupdf.open(out) as doc:
        assert doc.page_count == 2


@pytest.mark.skipif(MS_PDF not in installed_printers(), reason="needs Microsoft Print to PDF")
def test_dryrun_landscape_turns_counter_clockwise(tmp_path):
    with pymupdf.open() as doc:
        page = doc.new_page(width=792, height=612)
        page.draw_rect(pymupdf.Rect(0, 0, 792, 80), color=(0.9, 0, 0), fill=(0.9, 0, 0))
        landscape = doc.tobytes()
    info = GdiPrinter(DRYRUN, tmp_path).print_pdf(landscape, "pytest landscape")
    out = Path(info["output"])
    assert _wait_for(out)
    printed = out.read_bytes()
    with pymupdf.open(stream=printed, filetype="pdf") as doc:
        assert doc[0].rect.height > doc[0].rect.width   # portrait paper
    assert _red_mark_side(printed) == "left"            # the top of the page faces left


@pytest.mark.skipif(MS_PDF not in installed_printers(), reason="needs Microsoft Print to PDF")
def test_dryrun_small_image_is_not_blown_up(tmp_path):
    pdf, _, _ = convert.to_pdf(make_png(size=(300, 200)))      # 2 x 1.3 inches at 150 dpi
    info = GdiPrinter(DRYRUN, tmp_path).print_pdf(pdf, "pytest small image")
    out = Path(info["output"])
    assert _wait_for(out)
    with pymupdf.open(out) as doc:
        pix = doc[0].get_pixmap(dpi=20)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    corner = img.getpixel((2, 2))
    assert corner == (255, 255, 255)                              # the page edges stay blank
