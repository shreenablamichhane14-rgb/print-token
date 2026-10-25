"""Gating test 5 from the plan: does the asyncio + worker split hold?

A fake printer takes SLOW seconds per job. While it works, a probe measures how late the event
loop wakes up, which is how long a Telegram reply would be held back. The control runs the same
slow call directly on the loop, the naive way, to show what the split is protecting against.
"""
import asyncio
import statistics
import time

from conftest import make_pdf

from printtoken.pipeline import Pipeline, PrintJob

SLOW = 2.0
TICK = 0.02


class SlowPrinter:
    is_dryrun = True

    def print_pdf(self, pdf, job_name):
        time.sleep(SLOW)                    # stands in for a slow conversion or a busy spooler
        return {"pages": 1, "output": None}


async def _probe(lags: list[float]) -> None:
    while True:
        start = time.perf_counter()
        await asyncio.sleep(TICK)
        lags.append(time.perf_counter() - start - TICK)


def test_slow_print_does_not_block_the_loop(store, capsys):
    async def scenario():
        pipeline = Pipeline(store, SlowPrinter(), page_cap=20, queue_size=5, workers=2)
        runner = asyncio.create_task(pipeline.run())
        lags: list[float] = []
        probe = asyncio.create_task(_probe(lags))
        started = time.perf_counter()
        for i in range(2):
            assert pipeline.submit(PrintJob("telegram", "tg:1", f"slow{i}.pdf", make_pdf()))
        await asyncio.wait_for(pipeline.queue.join(), timeout=20)
        elapsed = time.perf_counter() - started
        probe.cancel()
        runner.cancel()
        pipeline.pool.shutdown(wait=False)
        return lags, elapsed

    lags, elapsed = asyncio.run(scenario())
    with capsys.disabled():
        print(f"\n[split]   2 jobs x {SLOW}s printer, {elapsed:.2f}s total, {len(lags)} probe ticks, "
              f"loop lag median {1000 * statistics.median(lags):.1f}ms max {1000 * max(lags):.1f}ms")
    assert max(lags) < 0.2
    assert elapsed >= 2 * SLOW - 0.1        # the print lock really does serialise the printer


def test_baseline_idle_loop(capsys):
    """The same probe with nothing running, so the split numbers have something to compare to."""
    async def scenario():
        lags: list[float] = []
        probe = asyncio.create_task(_probe(lags))
        await asyncio.sleep(2 * SLOW)
        probe.cancel()
        return lags

    lags = asyncio.run(scenario())
    with capsys.disabled():
        print(f"\n[idle]    no jobs, {len(lags)} probe ticks, loop lag median "
              f"{1000 * statistics.median(lags):.1f}ms max {1000 * max(lags):.1f}ms")
    assert max(lags) < 0.2


def test_control_blocking_call_on_the_loop_stalls_it(capsys):
    async def scenario():
        lags: list[float] = []
        probe = asyncio.create_task(_probe(lags))
        await asyncio.sleep(0.2)
        SlowPrinter().print_pdf(b"", "naive")   # the mistake: blocking work on the loop
        await asyncio.sleep(0.2)
        probe.cancel()
        return lags

    lags = asyncio.run(scenario())
    with capsys.disabled():
        print(f"[control] same {SLOW}s call on the loop, loop lag max {1000 * max(lags):.0f}ms")
    assert max(lags) > SLOW * 0.9
