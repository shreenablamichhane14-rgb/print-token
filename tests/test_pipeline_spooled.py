"""A job the printer already has is a print, even when its queue cannot be watched afterwards."""
import subprocess

from conftest import make_pdf

from printtoken.pipeline import Pipeline, PrintJob


def test_losing_sight_of_a_spooled_job_is_not_a_failed_print(store):
    class Spooled:
        is_dryrun = False               # a stand-in: nothing here talks to a real printer
        spooled = 0

        def print_pdf(self, pdf, job_name):
            self.spooled += 1
            return {"pages": 1, "output": None}

        def wait_until_gone(self, job_name):
            raise subprocess.TimeoutExpired(["lpstat", "-o"], 15)      # CUPS busy

    printer = Spooled()
    pipeline = Pipeline(store, printer, page_cap=20)
    r = pipeline.process(PrintJob("telegram", "guest:tg:9", "a.pdf", make_pdf(1), page_cap=5, guest_tag="k"))
    assert r.ok and r.reason == "printed" and r.done_ts is None and printer.spooled == 1
