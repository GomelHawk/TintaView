"""A short rolling history of each usage window, and the one number it is kept for:
*will this limit run out before it resets?*

The cache next door (`cache.py`) keeps only the latest reading per agent, which is all
the flyout needs to draw a bar — and exactly nothing of what a reader actually wants
from a quota panel. "62%" answers a question nobody asked; "at this pace the window
empties in ~40 min" is the one they did.

So a handful of `(timestamp, percent)` samples per window, appended from the poll that
already happens every `stats.poll_seconds`, and a straight-line projection over them.
Deliberately crude, and deliberately quiet:

- only *fresh* readings are recorded. A cached result re-records numbers that have not
  moved, which would flatten the rate towards zero and keep it there for as long as the
  outage lasted;
- a projection needs `MIN_SPAN_S` of history, so a freshly started tray says nothing
  rather than extrapolating a whole window from two polls four minutes apart;
- a window that is flat or falling has no ETA at all (a reset *lowers* the percentage,
  which is a negative rate, not a forecast);
- and an ETA that lands after the window's own reset is dropped: the limit is not going
  to run out, it is going to reset, and the reset time is already on the row.

Stdlib only, like everything else under `stats/` — this runs on a worker thread in a
headless install too.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Iterable
from pathlib import Path

from tintaview.core.config import config_dir

from .model import UsageResult, UsageRow

log = logging.getLogger(__name__)

TREND_FILENAME = "usage_trend.json"

#: Samples older than this are dropped on every write. Two hours is long enough to
#: survive a lunch break without the rate being computed against yesterday's session,
#: and short enough that the projection follows what you are doing *now*.
SAMPLE_TTL_S = 2 * 3600
#: Nothing is projected from less history than this. At the default 300 s poll that is
#: three samples — enough that one anomalous reading cannot carry the slope on its own.
MIN_SPAN_S = 900
#: Per series. At the default poll interval `SAMPLE_TTL_S` expires them long before
#: this; it is a ceiling for a hand-lowered `poll_seconds`, so the file cannot grow
#: without bound.
MAX_SAMPLES = 120
#: An ETA further out than this is not news. Nothing that empties "in 19 hours" is worth
#: a line on a card someone glances at.
MAX_ETA_S = 6 * 3600


def trend_path() -> Path:
    return config_dir() / TREND_FILENAME


class TrendStore:
    """`(timestamp, percent)` samples per agent and row label, persisted as JSON.

    Keyed by row *label* rather than by index: providers reorder their rows (Claude's
    weekly rows appear only once a week has data), and an index would quietly start
    charting the 5-hour window's history against the weekly window's percentage.

    Same locking and atomic-write story as `UsageCache`: `StatsService` records from
    whichever thread finished the poll, and a torn file must never be possible.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or trend_path()
        self._lock = threading.Lock()
        self._series: dict[str, dict[str, list[list[float]]]] = self._load()

    # --- storage ---------------------------------------------------------------

    def _load(self) -> dict[str, dict[str, list[list[float]]]]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}  # missing or corrupt — start empty, exactly like the usage cache
        if not isinstance(raw, dict):
            return {}
        out: dict[str, dict[str, list[list[float]]]] = {}
        for agent, rows in raw.items():
            if not isinstance(rows, dict):
                continue
            for label, samples in rows.items():
                clean = [
                    [float(s[0]), float(s[1])]
                    for s in samples
                    if isinstance(s, list) and len(s) == 2
                ]
                if clean:
                    out.setdefault(str(agent), {})[str(label)] = clean
        return out

    def _save(self) -> None:
        # Called with `self._lock` already held.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(self._series), encoding="utf-8")
        os.replace(tmp, self._path)

    # --- recording -------------------------------------------------------------

    def record(self, results: Iterable[UsageResult], now: float | None = None) -> None:
        """Append this poll's percentages. Never raises — a trend is a nicety.

        Skips anything that is not a *fresh, successful* reading: a result served from
        the cache (`source == "cache"`) is the same numbers again, and recording it
        would drag the computed rate towards zero for the whole outage.
        """
        stamp = time.time() if now is None else now
        try:
            with self._lock:
                changed = False
                for result in results:
                    if not result.ok or result.source == "cache":
                        continue
                    for row in result.rows:
                        if row.kind != "limit" or not row.show_pct:
                            continue
                        series = self._series.setdefault(result.agent, {}).setdefault(row.label, [])
                        series.append([stamp, float(row.pct)])
                        changed = True
                if changed:
                    self._prune_locked(stamp)
                    self._save()
        except Exception:
            log.debug("could not record usage trend", exc_info=True)

    def _prune_locked(self, now: float) -> None:
        for agent, rows in list(self._series.items()):
            for label, samples in list(rows.items()):
                kept = [s for s in samples if now - s[0] <= SAMPLE_TTL_S][-MAX_SAMPLES:]
                if kept:
                    rows[label] = kept
                else:
                    del rows[label]
            if not rows:
                del self._series[agent]

    # --- projection ------------------------------------------------------------

    def eta(self, agent: str, row: UsageRow, now: float | None = None) -> float | None:
        """Seconds until `row` hits 100% at its recent pace, or None for "don't say".

        None covers every case the module docstring lists — too little history, a flat
        or falling window, an ETA past the horizon, and an ETA later than the row's own
        reset.
        """
        stamp = time.time() if now is None else now
        with self._lock:
            samples = list(self._series.get(agent, {}).get(row.label, ()))
        samples = [s for s in samples if stamp - s[0] <= SAMPLE_TTL_S]
        if len(samples) < 2:
            return None
        oldest, newest = samples[0], samples[-1]
        span = newest[0] - oldest[0]
        if span < MIN_SPAN_S:
            return None
        rate = (newest[1] - oldest[1]) / span  # percent per second
        if rate <= 0:
            return None
        remaining = 100.0 - float(row.pct)
        if remaining <= 0:
            return None  # already full: the row's own bar says so
        eta = remaining / rate
        if eta > MAX_ETA_S:
            return None
        if row.reset_at and eta > row.reset_at - stamp:
            return None  # it resets before it empties, and the row already says when
        return eta

    def soonest(self, result: UsageResult, now: float | None = None) -> tuple[UsageRow, float] | None:
        """The row of `result` that runs out first, with its ETA — or None.

        One line per agent section, not one per row: a card with three projections on it
        is a table, and the only one that matters is whichever stops your work first.
        """
        best: tuple[UsageRow, float] | None = None
        for row in result.rows:
            if row.kind != "limit" or not row.show_pct:
                continue
            eta = self.eta(result.agent, row, now)
            if eta is not None and (best is None or eta < best[1]):
                best = (row, eta)
        return best
