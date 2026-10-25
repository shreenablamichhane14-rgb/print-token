"""The numbers for the Results section, straight from the event log."""
from __future__ import annotations

import csv
import statistics
import time
from datetime import datetime
from pathlib import Path

from .messages import describe
from .store import EVENT_FIELDS, Store


def _fmt(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    return f"{seconds:.1f}s" if seconds < 120 else f"{seconds / 60:.1f}min"


def availability(store: Store, since: float | None = None, now: float | None = None) -> tuple[int, int]:
    """(minutes with a heartbeat, minutes in the window). The window starts at `since`, or at the
    first heartbeat ever recorded."""
    now = time.time() if now is None else now
    first = store.scalar("SELECT MIN(minute) FROM heartbeats")
    if first is None:
        return 0, 0
    start = max(first, int(since // 60)) if since else first
    end = int(now // 60)
    seen = store.scalar("SELECT COUNT(*) FROM heartbeats WHERE minute BETWEEN ? AND ?", (start, end))
    return seen, end - start + 1


def summary(store: Store, since: float | None = None) -> str:
    since = since or 0
    lines = []
    seen, window = availability(store, since or None)
    if window:
        lines.append(f"Availability: {seen}/{window} minutes = {100 * seen / window:.1f}%")
    rows = store.query("SELECT channel, kind, outcome, reason, COUNT(*) FROM events WHERE ts >= ? "
                       "AND channel != 'system' GROUP BY channel, kind, outcome, reason "
                       "ORDER BY channel, kind, outcome, COUNT(*) DESC", (since,))
    lines.append("Events:")
    for channel, kind, outcome, reason, count in rows:
        why = f" ({describe(reason)})" if reason and outcome == "rejected" else ""
        lines.append(f"  {channel:8} {kind:6} {outcome:9} {count:4}{why}")
    printed = store.query("SELECT channel, sent_ts, received_ts, picked_ts, spooled_ts, done_ts, pages "
                          "FROM events WHERE outcome = 'printed' AND ts >= ?", (since,))
    for channel in ("telegram", "email"):
        mine = [r for r in printed if r[0] == channel]
        if not mine:
            continue
        def med(values):
            values = [v for v in values if v is not None]
            return statistics.median(values) if values else None
        end_to_end = med([(r[5] or r[4]) - r[1] for r in mine if r[1] and (r[5] or r[4])])
        pickup = med([r[3] - r[2] for r in mine if r[2] and r[3]])
        spool = med([r[4] - r[3] for r in mine if r[3] and r[4]])
        drain = med([r[5] - r[4] for r in mine if r[4] and r[5]])
        lines.append(f"{channel}: {len(mine)} printed, {sum(r[6] or 0 for r in mine)} pages, median "
                     f"send-to-done {_fmt(end_to_end)} (pickup {_fmt(pickup)}, convert+spool {_fmt(spool)}, "
                     f"printer queue {_fmt(drain)})")
    gaps = store.query("SELECT detail FROM events WHERE kind = 'gap' AND ts >= ?", (since,))
    if gaps:
        lines.append(f"Gaps (sleep/freeze): {len(gaps)}")
    return "\n".join(lines)


def export_csv(store: Store, path: Path) -> int:
    rows = store.query(f"SELECT {', '.join(EVENT_FIELDS)} FROM events ORDER BY id")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(EVENT_FIELDS)
        for row in rows:
            writer.writerow([datetime.fromtimestamp(v).isoformat(timespec="seconds")
                             if k.endswith("ts") and isinstance(v, float) else v
                             for k, v in zip(EVENT_FIELDS, row)])
    return len(rows)
