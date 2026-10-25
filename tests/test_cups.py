"""The Linux (Raspberry Pi) printing backend, tested with a fake subprocess so it runs anywhere."""
from types import SimpleNamespace

import pymupdf
import pytest
from conftest import make_pdf

from printtoken import printer
from printtoken.printer import DRYRUN, CupsPrinter, GdiPrinter, PrinterError, make_printer


class FakeRun:
    def __init__(self, *responses):
        self.calls = []
        self.responses = list(responses)

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        return self.responses.pop(0)


def done(stdout=b"", stderr=b"", code=0):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


def test_lp_command_and_job_id():
    run = FakeRun(done(b"request id is SmartTank-42 (1 file(s))\n"))
    p = CupsPrinter("SmartTank", run=run)
    info = p.print_pdf(make_pdf(2), "PT00001 a.pdf")
    cmd, kwargs = run.calls[0]
    assert cmd == ["lp", "-d", "SmartTank", "-t", "PT00001 a.pdf", "-o", "fit-to-page"]
    assert kwargs["input"].startswith(b"%PDF")          # the PDF goes in on standard input
    assert info == {"pages": 2, "output": None}


def test_wait_until_the_job_leaves_the_queue():
    run = FakeRun(done(b"request id is SmartTank-42 (1 file(s))\n"),
                  done("SmartTank-42 shree 1024 Sun 25 Oct 2026\n"),
                  done(""))
    p = CupsPrinter("SmartTank", run=run)
    p.print_pdf(make_pdf(1), "job")
    waited = p.wait_until_gone("job", timeout=5, interval=0.01)
    assert waited is not None and waited < 5
    assert run.calls[1][0] == ["lpstat", "-o", "SmartTank"]


def test_lp_failure_is_reported():
    run = FakeRun(done(stderr=b"lp: The printer or class does not exist.", code=1))
    with pytest.raises(PrinterError, match="does not exist"):
        CupsPrinter("Nope", run=run).print_pdf(make_pdf(1), "job")


def test_dryrun_saves_the_pdf(tmp_path):
    info = CupsPrinter(DRYRUN, tmp_path).print_pdf(make_pdf(3), "dry job")
    with pymupdf.open(info["output"]) as doc:
        assert doc.page_count == 3


def test_check_needs_cups(monkeypatch):
    monkeypatch.setattr(printer.shutil, "which", lambda name: None)
    with pytest.raises(PrinterError, match="CUPS is not installed"):
        CupsPrinter("SmartTank").check()


def test_check_finds_the_queue(monkeypatch):
    monkeypatch.setattr(printer.shutil, "which", lambda name: f"/usr/bin/{name}")
    CupsPrinter("SmartTank", run=FakeRun(done("SmartTank\nOffice\n"))).check()
    with pytest.raises(PrinterError, match="no CUPS printer"):
        CupsPrinter("Missing", run=FakeRun(done("SmartTank\n"))).check()


def test_backend_follows_the_operating_system(monkeypatch):
    monkeypatch.setattr(printer.sys, "platform", "linux")
    assert isinstance(make_printer("SmartTank"), CupsPrinter)
    monkeypatch.setattr(printer.sys, "platform", "win32")
    assert isinstance(make_printer("SmartTank"), GdiPrinter)
