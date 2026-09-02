"""Codex CLI usage provider — local only, no network.

Codex keeps no server-side "my usage" endpoint we can call; everything comes from
``<home>/sessions/**/rollout-*.jsonl``, where each line is one JSON record. The ones we
care about look like::

    {"timestamp": "2026-07-24T16:08:28.664Z", "type": "event_msg",
     "payload": {"type": "token_count",
                 "info": {"total_token_usage": {...}, "last_token_usage": {...},
                          "model_context_window": 258400},
                 "rate_limits": {"limit_id": "codex", "primary": null,
                                  "secondary": null, "credits": null, "plan_type": null}}}

``rate_limits.primary``/``secondary`` carry OFFICIAL percentages on ChatGPT-plan
sessions (fields seen in the wild: ``used_percent``, ``resets_at`` /
``resets_in_seconds``, ``window_minutes`` — none of this is documented, so every access
below is a defensive ``.get()``). They are ``null`` on API-key sessions (verified on
this machine); we fall back to token totals over the shared 5h/7d windows there,
labelled as informational (``show_pct=False``) rather than a real percentage.

Performance: this may run over a slow Windows UNC path (the WSL split). Three rules to
keep a poll fast:
  1. Only look at files whose mtime is within the last 7 days, and prune whole
     ``YYYY/MM/DD`` directories that cannot hold one before descending into them —
     ``sessions/`` accumulates a directory per day forever, and walking every one of
     them was most of the poll on a machine that had been using Codex for a year.
  2. Read each candidate file from the END, not front-to-back — these files are
     append-only, so the records that matter are at the tail. ``_scan_tail`` grows the
     read geometrically only as far as it needs.
  3. Memoise the parse per file on ``(mtime_ns, size)``, so an unchanged session costs
     one ``stat`` instead of a read.

``info.total_token_usage`` is **cumulative for the whole session**, which is the trap in
the 5-hour row: a week-long session touched ten minutes ago belongs in that window, but
only for what it spent *inside* it. Adding its running total made "last 5 hours" larger
than "last 7 days". So the tail scan keeps every ``token_count`` record back to 5 hours
before the file's own newest one, and the 5h row accumulates ``latest - the newest
snapshot from before the cutoff``.

The schema is undocumented and has changed across Codex versions; this module must
never raise — an unrecognised shape degrades to an ``UsageResult`` with a clear
``error`` instead.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tintaview.core.config import AgentConfig, expand
from tintaview.i18n import t

from .. import _scan
from .. import format as fmt
from ..model import UsageProvider, UsageResult, UsageRow

log = logging.getLogger(__name__)

_MAX_AGE_DAYS = 7
# Give up on a single file past this many tail bytes rather than read the whole thing —
# this is the fallback informational path, not worth an unbounded scan of a huge log.
_MAX_TAIL_BYTES = 16 * 1024 * 1024
_TAIL_CHUNK = 64 * 1024

#: The 5-hour window, plus a few minutes of slack so the baseline record is strictly
#: older than any cutoff a later poll can produce. The tail scan reads back until it has
#: a record this far behind the file's newest one — anchored to the *file*, not to `now`,
#: which is what lets the result be memoised across polls.
_WINDOW_5H_SPAN_S = 5 * 3600 + 300

#: How long after its date a `YYYY/MM/DD` session directory may still gain writes. Codex
#: names the directory for when the session *started*, and a session left open overnight
#: keeps appending — so pruning strictly on the directory's own date would drop files
#: whose mtime is inside the window. Two days is generous on purpose: pruning is only an
#: optimisation, and being wrong about it loses real usage.
_DIR_GRACE_S = 2 * 86400

#: The token fields Codex reports, in the order `_empty_totals` keys them.
_TOKEN_FIELDS = (
    ("input", "input_tokens"),
    ("cached_input", "cached_input_tokens"),
    ("cache_write", "cache_write_input_tokens"),
    ("output", "output_tokens"),
    ("reasoning_output", "reasoning_output_tokens"),
    ("total", "total_tokens"),
)


# --------------------------------------------------------------------------- paths


def _default_home() -> Path:
    return Path.home() / ".codex"


def _resolve_home(agent_config: AgentConfig) -> Path:
    return expand(agent_config.home) if agent_config.home else _default_home()


def _date_dir_end(parts: list[str]) -> float | None:
    """When a `YYYY`, `YYYY/MM` or `YYYY/MM/DD` directory's date range ends (exclusive).

    None for anything that is not one of those shapes — a name we do not recognise as a
    date is never pruned, because it might be anything (a user's own folder, a future
    Codex layout) and losing real sessions is far worse than walking a few directories.
    """
    if not 1 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        return None
    if len(parts[0]) != 4:
        return None
    try:
        year = int(parts[0])
        if len(parts) == 1:
            end = datetime(year + 1, 1, 1)
        elif len(parts) == 2:
            month = int(parts[1])
            end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
        else:
            end = datetime(year, int(parts[1]), int(parts[2])) + timedelta(days=1)
        return end.timestamp()  # naive: these directory names are local dates
    except (ValueError, OverflowError, OSError):
        return None  # month 13, day 32, a year outside the platform's range — don't prune


def _make_skip_dir(sessions: Path, cutoff: float) -> Callable[[str, str], bool]:
    """A `_scan.recent_files` pruner for Codex's `sessions/YYYY/MM/DD` tree.

    `sessions/` gains a directory per day and never loses one, so the old
    `glob("**/rollout-*.jsonl")` re-walked every day the user had ever run Codex on every
    5-minute poll — over a UNC path that is a round trip per directory. Whole years and
    months are dropped here without ever being listed.
    """
    root = str(sessions)

    def skip(dirpath: str, dirname: str) -> bool:
        try:
            rel = os.path.relpath(dirpath, root)
        except ValueError:  # different drive on Windows — not our tree, don't prune
            return False
        parts = [] if rel in (".", "") else rel.split(os.sep)
        parts.append(dirname)
        end = _date_dir_end(parts)
        if end is None:
            return False
        return end + _DIR_GRACE_S <= cutoff

    return skip


def _recent_session_files(home: Path, max_age_days: int = _MAX_AGE_DAYS,
                          now: float | None = None) -> list[Path]:
    now = time.time() if now is None else now
    sessions = home / "sessions"
    cutoff = now - max_age_days * 86400
    return _scan.recent_files(
        sessions, "rollout-*.jsonl", max_age_days * 86400,
        now=now, skip_dir=_make_skip_dir(sessions, cutoff),
    )


# --------------------------------------------------------------------------- tail scan


@dataclass(frozen=True)
class _TailScan:
    """What one rollout file's tail says about its session.

    `latest` is the whole newest `token_count` record (the rate-limit percentages live on
    it); `history` is `(timestamp, total_token_usage)` for every `token_count` record the
    scan read, newest first, reaching back far enough to answer the 5-hour window.
    """

    latest: dict[str, Any] | None = None
    latest_ts: datetime | None = None
    history: tuple[tuple[datetime, dict[str, Any]], ...] = ()


#: Per-file parse cache. `total_token_usage` never changes for a record already written,
#: so an unchanged file's scan is still valid; `_fetch` prunes this to the recent set on
#: every poll so it cannot grow for the life of the tray.
_TAIL_MEMO: _scan.FileMemo[_TailScan] = _scan.FileMemo()


def _is_token_count_record(rec: Any) -> bool:
    return (
        isinstance(rec, dict)
        and rec.get("type") == "event_msg"
        and isinstance(rec.get("payload"), dict)
        and rec["payload"].get("type") == "token_count"
    )


def _record_totals(rec: dict[str, Any]) -> dict[str, Any]:
    info = (rec.get("payload") or {}).get("info") or {}
    totals = info.get("total_token_usage")
    return totals if isinstance(totals, dict) else {}


def _scan_tail(path: Path) -> _TailScan:
    """Every `token_count` record in the tail of one rollout file, newest first.

    Reads from the END in geometrically growing chunks, because these files are
    append-only and can get large. It stops as soon as it holds a record at least
    `_WINDOW_5H_SPAN_S` older than the newest one — that is all the 5-hour delta needs,
    and anchoring the stop condition to the *file's* own newest timestamp (rather than to
    `now`) is what makes the result safe to memoise across polls: every later poll's
    cutoff is later still, so the span already covers it.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return _TailScan()
    if size == 0:
        return _TailScan()

    chunk = _TAIL_CHUNK
    with open(path, "rb") as f:
        while True:
            read = min(chunk, size)
            f.seek(size - read)
            text = f.read(read).decode("utf-8", errors="ignore")
            lines = text.split("\n")
            if read < size:
                # The first line of a partial tail is very likely cut mid-record —
                # drop it rather than risk json.loads on a truncated line.
                lines = lines[1:]

            history: list[tuple[datetime, dict[str, Any]]] = []
            latest: dict[str, Any] | None = None
            for line in reversed(lines):
                line = line.strip()
                if not line or '"token_count"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not _is_token_count_record(rec):
                    continue
                ts = _parse_ts(rec.get("timestamp"))
                if ts is None:
                    continue
                if latest is None:
                    latest = rec
                history.append((ts, _record_totals(rec)))

            spans = bool(history) and (
                (history[0][0] - history[-1][0]).total_seconds() >= _WINDOW_5H_SPAN_S
            )
            if spans or read >= size or read >= _MAX_TAIL_BYTES:
                if not history:
                    return _TailScan()
                return _TailScan(latest=latest, latest_ts=history[0][0], history=tuple(history))
            chunk *= 2


def _window_totals(scan: _TailScan, cutoff: datetime) -> dict[str, Any]:
    """This session's usage *inside* the window, not its whole history.

    `total_token_usage` is cumulative for the session, so adding the latest record's
    total to the 5-hour bucket charged that window with every token the session had ever
    spent — a session opened last Tuesday and touched ten minutes ago made "last 5 hours"
    bigger than "last 7 days". Subtract the newest snapshot taken *before* the cutoff.

    With no such snapshot in the tail — a session that began inside the window, or one
    whose tail we could not read far enough back — the running total IS the window's
    usage, which is the right answer in the first case and the old behaviour in the
    second.
    """
    latest = scan.history[0][1]
    for ts, totals in scan.history[1:]:
        if ts < cutoff:
            return _subtract_totals(latest, totals)
    return latest


def _subtract_totals(latest: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for _acc_key, field in _TOKEN_FIELDS:
        # Floored at zero: a counter that appears to go backwards (a resumed session
        # re-reporting from scratch) must not subtract from another session's usage.
        out[field] = max(_as_int(latest.get(field)) - _as_int(baseline.get(field)), 0)
    return out


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _parse_ts(ts: Any) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


# --------------------------------------------------------------------------- rows


def _parse_reset_moment(value: Any) -> datetime | None:
    """Interpret an undocumented `resets_at` as an absolute moment, or None.

    Codex has been observed sending this as a **Unix epoch integer** (`1789125077`) as
    well as an ISO-8601 string, and `datetime.fromisoformat` rejects the former outright.
    Epoch values are also seen in milliseconds by some producers, so anything far beyond
    the plausible seconds range is rescaled rather than landing the reset time ~50,000
    years in the future.
    """
    if isinstance(value, bool):  # bool is an int subclass; a flag is not a timestamp
        return None

    number: float | None = None
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, str) and value.strip().lstrip("+-").isdigit():
        number = float(value.strip())

    if number is not None:
        # 1e11 seconds is year 5138; anything above it is milliseconds, not seconds.
        if abs(number) > 1e11:
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, UTC)
        except (OverflowError, OSError, ValueError):
            return None

    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    # A naive ISO string would blow up the `dt - now` subtraction below against an
    # aware `now`; the API reports UTC, so assume it when no offset is given.
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _fmt_reset(resets_at: Any = None, resets_in_seconds: Any = None) -> str:
    """Same wording as the Claude provider's reset formatting, but Codex's rate-limit
    windows have been observed to carry either an absolute `resets_at` or a relative
    `resets_in_seconds` — accept either."""
    now = datetime.now(UTC)
    if resets_at:
        dt = _parse_reset_moment(resets_at)
        if dt is None:
            # Deliberately blank rather than echoing the raw value. This lands in the
            # flyout row's right-hand slot, where a value we could not interpret reads as
            # a usage figure — an unparsed epoch once showed up next to the 5-hour limit
            # as a bare "1789125077", which looks like a token count, not a clock.
            log.debug("codex: unparseable resets_at %r", resets_at)
            return ""
    elif resets_in_seconds is not None:
        try:
            dt = now + timedelta(seconds=float(resets_in_seconds))
        except (TypeError, ValueError):
            return ""
    else:
        return ""
    # Beyond a week a weekday name is ambiguous at best — "Resets Fri" for a monthly
    # budget four weeks out reads as *this* Friday. Codex's own UI shows a date there,
    # so `date_after_days` makes `stats.format` match it.
    return fmt.reset_text(dt, date_after_days=6)


def _window_label(window: dict[str, Any], fallback: str) -> str:
    """Name the limit from its own `window_minutes` rather than assuming what it is.

    `primary`/`secondary` are positions in the payload, not fixed durations: they were
    a 5-hour and a weekly window on the plans this was first written against, but a free
    plan reports a single `primary` with ``window_minutes: 43200`` — 30 days. Labelling
    that "5-hour limit" contradicts Codex's own UI, which calls it Monthly, and makes a
    92%-consumed monthly budget look like a 5-hour one that will clear over lunch.
    """
    minutes = window.get("window_minutes")
    if not isinstance(minutes, int | float) or isinstance(minutes, bool) or minutes <= 0:
        return fallback
    minutes = int(minutes)
    if minutes % 43200 == 0:  # 30-day months, as Codex counts them
        months = minutes // 43200
        return t("usage.codex.limit.monthly") if months == 1 else t("usage.codex.limit.months", count=months)
    if minutes % 10080 == 0:
        weeks = minutes // 10080
        return t("usage.codex.limit.weekly") if weeks == 1 else t("usage.codex.limit.weeks", count=weeks)
    if minutes % 1440 == 0:
        days = minutes // 1440
        return t("usage.codex.limit.daily") if days == 1 else t("usage.codex.limit.days", count=days)
    if minutes % 60 == 0:
        hours = minutes // 60
        return t("usage.codex.limit.hourly") if hours == 1 else t("usage.codex.limit.hours", count=hours)
    return t("usage.codex.limit.minutes", count=minutes)


def _pct_row(label: str, window: dict[str, Any]) -> UsageRow:
    # GUESSED field names (undocumented API): `used_percent`, `resets_at`,
    # `resets_in_seconds`. `window_minutes` is also seen but unused here. Severity
    # thresholds (75% / 90%) are our own choice — Codex's payload doesn't provide one.
    pct = window.get("used_percent")
    right = _fmt_reset(window.get("resets_at"), window.get("resets_in_seconds"))
    if pct is None:
        return UsageRow(label=label, pct=0.0, right=right, show_pct=False, severity="normal", kind="info")
    severity = "critical" if pct >= 90 else "warning" if pct >= 75 else "normal"
    return UsageRow(label=label, pct=float(pct), right=right, show_pct=True, severity=severity, kind="limit")


def _empty_totals() -> dict[str, int]:
    return {acc_key: 0 for acc_key, _field in _TOKEN_FIELDS}


def _accumulate(acc: dict[str, int], totals: dict[str, Any]) -> None:
    for acc_key, field in _TOKEN_FIELDS:
        acc[acc_key] += _as_int(totals.get(field))


def _total_row(label: str, acc: dict[str, int]) -> UsageRow:
    total = acc["total"] or (acc["input"] + acc["output"])
    right = (t("usage.tokens.millions", value=f"{total / 1e6:.2f}") if total >= 1_000_000
             else t("usage.tokens.thousands", value=f"{total / 1e3:.0f}"))
    return UsageRow(label=label, pct=0.0, right=right, show_pct=False, severity="normal", kind="info")


# --------------------------------------------------------------------------- provider


class CodexUsageProvider(UsageProvider):
    key = "codex"

    def fetch(self, agent_config: AgentConfig, timeout: float = 15.0) -> UsageResult:
        try:
            return self._fetch(agent_config)
        except Exception as e:  # noqa: BLE001 - contract: a provider must never raise
            log.exception("codex usage provider failed unexpectedly")
            return UsageResult(agent=self.key,
                                error=t("usage.codex.error.unavailable", detail=repr(e)))

    def _fetch(self, agent_config: AgentConfig) -> UsageResult:
        home = _resolve_home(agent_config)
        files = _recent_session_files(home)
        if not files:
            return UsageResult(agent=self.key, error=t("usage.codex.error.no_sessions"))
        # Forget files that dropped out of the 7-day window, so the memo tracks the
        # working set instead of every session this process has ever seen.
        _TAIL_MEMO.prune(files)

        now = datetime.now(UTC)
        cutoff_5h = now - timedelta(hours=5)
        cutoff_7d = now - timedelta(days=7)
        window_totals = {"5h": _empty_totals(), "7d": _empty_totals()}

        latest_record: dict[str, Any] | None = None
        latest_ts: datetime | None = None

        for path in files:
            try:
                scan = _TAIL_MEMO.get(path, _scan_tail)
            except OSError:
                continue  # vanished between the walk and the read — skip, not fatal
            if scan.latest is None or scan.latest_ts is None:
                continue
            ts = scan.latest_ts
            if latest_ts is None or ts > latest_ts:
                latest_ts = ts
                latest_record = scan.latest

            session_total = scan.history[0][1]
            # 7d gets the session's whole running total, which is what a session touched
            # inside the week has spent (these files do not outlive a week's worth of
            # relevance in practice). 5h gets only what was spent inside those 5 hours —
            # see `_window_totals` for why the cumulative figure is wrong there.
            if ts >= cutoff_7d:
                _accumulate(window_totals["7d"], session_total)
            if ts >= cutoff_5h:
                _accumulate(window_totals["5h"], _window_totals(scan, cutoff_5h))

        if latest_record is None:
            return UsageResult(agent=self.key, error=t("usage.codex.error.no_records"))

        payload = latest_record.get("payload") or {}
        rate_limits = payload.get("rate_limits") or {}
        primary = rate_limits.get("primary")
        secondary = rate_limits.get("secondary")

        if primary or secondary:
            rows = []
            if primary:
                rows.append(_pct_row(_window_label(primary, t("usage.codex.limit.primary")), primary))
            if secondary:
                rows.append(_pct_row(_window_label(secondary, t("usage.codex.limit.secondary")), secondary))
            if rows:
                return UsageResult(agent=self.key, rows=rows,
                                    header=t("usage.codex.header.limits"), source="official")

        # No official percentages available on this session (typically API-key auth,
        # verified null on this machine) — informational token totals instead.
        rows = [_total_row(t("usage.codex.last_5h"), window_totals["5h"]),
                _total_row(t("usage.codex.last_7d"), window_totals["7d"])]
        return UsageResult(agent=self.key, rows=rows,
                            header=t("usage.codex.header.totals"), source="activity")
