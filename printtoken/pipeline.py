"""One bounded queue between the network side and the printing side.

The asyncio loop only does network work: Telegram long polling, the mailbox check, replies.
Anything slow or blocking (opening a PDF, rendering pages, talking to the spooler) runs in a thread
pool. If a conversion ran on the loop, the bot would go quiet for as long as the print took and
look broken while being perfectly healthy. The queue has a size limit so a flood gets turned away
instead of piling up forever.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from . import convert
from .printer import GdiPrinter
from .store import Store

log = logging.getLogger("printtoken.pipeline")


@dataclass
class PrintJob:
    channel: str                        # "telegram" or "email"
    sender: str
    filename: str
    data: bytes
    sent_ts: float | None = None        # sender's clock: Telegram date or the email Date header
    received_ts: float | None = None    # provider's clock: Telegram date or Gmail INTERNALDATE
    picked_ts: float = field(default_factory=time.time)
    reply_chat: int | None = None
    event_id: int | None = None


@dataclass
class JobResult:
    ok: bool
    reason: str
    pages: int = 0
    spooled_ts: float | None = None
    done_ts: float | None = None
    detail: str = ""


Notify = Callable[[PrintJob, JobResult], Awaitable[None]]


class Pipeline:
    def __init__(self, store: Store, printer: GdiPrinter, page_cap: int, queue_size: int = 10,
                 workers: int = 2, notify: Notify | None = None, wait_for_queue: bool = True):
        self.store = store
        self.printer = printer
        self.page_cap = page_cap
        self.queue: asyncio.Queue[PrintJob] = asyncio.Queue(maxsize=queue_size)
        self.workers = workers
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="pt-worker")
        self.notify = notify
        self.wait_for_queue = wait_for_queue
        self._print_lock = threading.Lock()     # converting can overlap, printing cannot

    def submit(self, job: PrintJob) -> bool:
        job.event_id = self.store.log_event(
            channel=job.channel, kind="print", outcome="queued", sender=job.sender,
            filename=job.filename, bytes=len(job.data), sent_ts=job.sent_ts,
            received_ts=job.received_ts, picked_ts=job.picked_ts)
        try:
            self.queue.put_nowait(job)
        except asyncio.QueueFull:
            self.store.update_event(job.event_id, outcome="rejected", reason="queue_full")
            return False
        return True

    async def run(self) -> None:
        await asyncio.gather(*(self._consume() for _ in range(self.workers)))

    async def _consume(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            job = await self.queue.get()
            try:
                result = await loop.run_in_executor(self.pool, self.process, job)
            except Exception as exc:            # one bad job must not stop the consumer
                log.exception("job %s crashed", job.event_id)
                result = JobResult(False, "internal_error", detail=exc.__class__.__name__)
            self._record(job, result)
            self.queue.task_done()
            if self.notify:
                try:
                    await self.notify(job, result)
                except Exception:
                    log.exception("notify failed for job %s", job.event_id)

    def process(self, job: PrintJob) -> JobResult:
        """Runs in a worker thread, never on the event loop."""
        try:
            pdf, pages, _kind = convert.to_pdf(job.data)
        except convert.Unsupported as exc:
            return JobResult(False, "unsupported_type", detail=str(exc))
        if pages > self.page_cap:
            return JobResult(False, "over_page_cap", pages=pages,
                             detail=f"{pages} pages, the limit is {self.page_cap}")
        job_name = f"PT{job.event_id or 0:05d} {job.filename}"[:120]
        with self._print_lock:
            try:
                info = self.printer.print_pdf(pdf, job_name)
            except Exception as exc:
                log.warning("print failed for job %s: %s", job.event_id, exc)
                return JobResult(False, "print_error", pages=pages, detail=str(exc))
            spooled = time.time()
            done: float | None = spooled
            if self.wait_for_queue and not self.printer.is_dryrun:
                waited = self.printer.wait_until_gone(job_name)
                done = None if waited is None else spooled + waited
        return JobResult(True, "printed", pages=info["pages"], spooled_ts=spooled, done_ts=done,
                         detail=info.get("output") or "")

    def _record(self, job: PrintJob, result: JobResult) -> None:
        if job.event_id is None:
            return
        self.store.update_event(
            job.event_id, outcome="printed" if result.ok else "rejected",
            reason=None if result.ok else result.reason, pages=result.pages or None,
            spooled_ts=result.spooled_ts, done_ts=result.done_ts, detail=result.detail or None)
