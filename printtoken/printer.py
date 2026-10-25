"""Printing a PDF with no dialog.

On Windows (the laptop) PyMuPDF renders each page to a bitmap and Windows GDI hands it to the
spooler, so nothing extra has to be installed. On Linux (the Raspberry Pi 5 the gateway is moving
to) the PDF goes to CUPS through the lp command instead. DRYRUN never uses paper: on Windows it
sends the same GDI job to "Microsoft Print to PDF", on Linux it saves the PDF that would have
been sent to CUPS.

The printer is always named explicitly. Windows 10/11 can move the default printer to whatever was
used last ("Let Windows manage my default printer"), so the default is not something to rely on.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pymupdf

DRYRUN = "DRYRUN"
MS_PDF = "Microsoft Print to PDF"
MAX_RENDER_DPI = 300


class PrinterError(Exception):
    pass


def make_printer(name: str, dryrun_dir: Path | None = None) -> "GdiPrinter | CupsPrinter":
    """The backend for the operating system this is running on."""
    if sys.platform == "win32":
        return GdiPrinter(name, dryrun_dir)
    return CupsPrinter(name, dryrun_dir)


def installed_printers() -> list[str]:
    """Windows printer names. Empty on other systems."""
    if sys.platform != "win32":
        return []
    import win32print
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    return [p[2] for p in win32print.EnumPrinters(flags)]


def _safe_name(job_name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in job_name)[:80]


def _page_count(pdf: bytes) -> int:
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        return doc.page_count


class GdiPrinter:
    """Windows backend."""

    def __init__(self, name: str, dryrun_dir: Path | None = None):
        self.name = name
        self.dryrun_dir = Path(dryrun_dir) if dryrun_dir else None

    @property
    def is_dryrun(self) -> bool:
        return self.name == DRYRUN

    @property
    def target(self) -> str:
        return MS_PDF if self.is_dryrun else self.name

    def check(self) -> None:
        if self.target not in installed_printers():
            raise PrinterError(f"printer is not installed on this PC: {self.target}")

    def print_pdf(self, pdf: bytes, job_name: str) -> dict:
        import win32con
        import win32ui
        output = None
        if self.is_dryrun:
            if self.dryrun_dir is None:
                raise PrinterError("DRYRUN needs a folder for its output")
            self.dryrun_dir.mkdir(parents=True, exist_ok=True)
            output = str(self.dryrun_dir / f"{_safe_name(job_name)}.pdf")
        dc = win32ui.CreateDC()
        try:
            dc.CreatePrinterDC(self.target)
        except win32ui.error as exc:
            raise PrinterError(f"cannot open printer {self.target!r}: {exc}") from exc
        pages = 0
        try:
            horz = dc.GetDeviceCaps(win32con.HORZRES)
            vert = dc.GetDeviceCaps(win32con.VERTRES)
            dpi = dc.GetDeviceCaps(win32con.LOGPIXELSX)
            if output:
                dc.StartDoc(job_name, output)
            else:
                dc.StartDoc(job_name)
            try:
                with pymupdf.open(stream=pdf, filetype="pdf") as doc:
                    for page in doc:
                        dc.StartPage()
                        _draw_page(dc, page, horz, vert, dpi)
                        dc.EndPage()
                        pages += 1
                dc.EndDoc()
            except Exception:
                dc.AbortDoc()
                raise
        finally:
            dc.DeleteDC()
        return {"pages": pages, "output": output}

    def wait_until_gone(self, job_name: str, timeout: float = 180, interval: float = 1.0) -> float | None:
        """Seconds until the job left the Windows queue, or None if it was still there at the timeout.
        Leaving the queue means the printer accepted all of it, which is the closest a PC can get to
        knowing the paper came out."""
        import win32print
        start = time.time()
        handle = win32print.OpenPrinter(self.target)
        try:
            while time.time() - start < timeout:
                jobs = win32print.EnumJobs(handle, 0, 999, 1)
                if not any(j.get("pDocument") == job_name for j in jobs):
                    return time.time() - start
                time.sleep(interval)
        finally:
            win32print.ClosePrinter(handle)
        return None


def _draw_page(dc, page, horz: int, vert: int, dpi: int) -> None:
    """Shrink the page to fit the printable area (never enlarge it), turning wide pages a quarter
    turn counter-clockwise onto tall paper the way printer drivers do for landscape."""
    from PIL import Image, ImageWin
    rect = page.rect
    rotate = -90 if (rect.width > rect.height) != (horz > vert) else 0
    pw, ph = (rect.height, rect.width) if rotate else (rect.width, rect.height)
    natural_w, natural_h = pw / 72 * dpi, ph / 72 * dpi           # device pixels at 100%
    fit = min(horz / natural_w, vert / natural_h, 1.0)
    dest_w, dest_h = int(natural_w * fit), int(natural_h * fit)
    render_dpi = min(dpi, MAX_RENDER_DPI)
    zoom = render_dpi / 72 * fit
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom).prerotate(rotate), alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    x, y = (horz - dest_w) // 2, (vert - dest_h) // 2
    ImageWin.Dib(img).draw(dc.GetHandleOutput(), (x, y, x + dest_w, y + dest_h))


class CupsPrinter:
    """Linux backend for the Raspberry Pi. CUPS does the scaling (fit-to-page) and the driver work.
    Written ahead of the move; the commands it builds are covered by tests, but it has not run on
    the Pi yet."""

    def __init__(self, name: str, dryrun_dir: Path | None = None, run=subprocess.run):
        self.name = name
        self.dryrun_dir = Path(dryrun_dir) if dryrun_dir else None
        self._run = run
        self._jobs: dict[str, str] = {}

    @property
    def is_dryrun(self) -> bool:
        return self.name == DRYRUN

    @property
    def target(self) -> str:
        return self.name

    def check(self) -> None:
        if self.is_dryrun:
            return
        if shutil.which("lp") is None or shutil.which("lpstat") is None:
            raise PrinterError("CUPS is not installed (on the Pi: sudo apt install cups)")
        result = self._run(["lpstat", "-e"], capture_output=True, text=True, timeout=15)
        queues = result.stdout.split()
        if self.name not in queues:
            raise PrinterError(f"no CUPS printer called {self.name!r}, lpstat -e lists {queues}")

    def print_pdf(self, pdf: bytes, job_name: str) -> dict:
        pages = _page_count(pdf)
        if self.is_dryrun:
            if self.dryrun_dir is None:
                raise PrinterError("DRYRUN needs a folder for its output")
            self.dryrun_dir.mkdir(parents=True, exist_ok=True)
            out = self.dryrun_dir / f"{_safe_name(job_name)}.pdf"
            out.write_bytes(pdf)
            return {"pages": pages, "output": str(out)}
        # No file argument: lp reads the document from standard input.
        cmd = ["lp", "-d", self.name, "-t", job_name, "-o", "fit-to-page"]
        result = self._run(cmd, input=pdf, capture_output=True, timeout=60)
        if result.returncode != 0:
            raise PrinterError(f"lp failed: {result.stderr.decode(errors='replace').strip()}")
        match = re.search(r"request id is (\S+)", result.stdout.decode(errors="replace"))
        if match:
            self._jobs[job_name] = match.group(1)
        return {"pages": pages, "output": None}

    def wait_until_gone(self, job_name: str, timeout: float = 180, interval: float = 1.0) -> float | None:
        job = self._jobs.pop(job_name, None)
        if job is None:
            return None
        start = time.time()
        while time.time() - start < timeout:
            result = self._run(["lpstat", "-o", self.name], capture_output=True, text=True, timeout=15)
            if job not in result.stdout.split():
                return time.time() - start
            time.sleep(interval)
        return None
