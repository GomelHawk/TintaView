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

Performance: this may run over a slow Windows UNC path (the WSL split). Two rules to
keep a poll fast:
  1. Only look at files whose mtime is within the last 7 days.
  2. Read each candidate file from the END, not front-to-back — the newest
     ``token_count`` record is always near the tail because these files are
     append-only. ``_latest_record_in_file`` grows the tail read geometrically only
     as far as it needs to find one.

The schema is undocumented and has changed across Codex versions; this module must
never raise — an unrecognised shape degrades to an ``UsageResult`` with a clear
``error`` instead.
"""

from __future__ import annotations

import glob
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tintaview.core.config import AgentConfig, expand
from tintaview.i18n import t

from .. import format as fmt
from ..model import UsageProvider, UsageResult, UsageRow

log = logging.getLogger(__name__)

_MAX_AGE_DAYS = 7
# Give up on a single file past this many tail bytes rather than read the whole thing —
# this is the fallback informational path, not worth an unbounded scan of a huge log.
_MAX_TAIL_BYTES = 16 * 1024 * 1024
_TAIL_CHUNK = 64 * 1024


# --------------------------------------------------------------------------- paths


def _default_home() -> Path:
    return Path.home() / ".codex"


def _resolve_home(agent_config: AgentConfig) -> Path:
    return expand(agent_config.home) if agent_config.home else _default_home()


def _iter_recent_session_files(home: Path, max_age_days: int = _MAX_AGE_DAYS) -> list[Path]:
    pattern = str(home / "sessions" / "**" / "rollout-*.jsonl")
    cutoff = time.time() - max_age_days * 86400
    out: list[Path] = []
    for raw in glob.glob(pattern, recursive=True):
        p = Path(raw)
        try:
            if p.stat().st_mtime >= cutoff:
                out.append(p)
        except OSError:
            continue  # vanished between glob and stat — not fatal, just skip it
    return out


# --------------------------------------------------------------------------- tail scan


def _is_token_count_record(rec: Any) -> bool:
    return (
        isinstance(rec, dict)
        and rec.get("type") == "event_msg"
        and isinstance(rec.get("payload"), dict)
        and rec["payload"].get("type") == "token_count"
    )


def _latest_record_in_file(path: Path) -> dict[str, Any] | None:
    """Return the most recent `token_count` event_msg record in one rollout file,
    reading from the end in growing chunks instead of parsing the whole file.

    A session can run long enough to produce a large file, so a single small tail read
    is not always enough (the last few lines might be unrelated housekeeping events) —
    this doubles the read size until it finds a match, hits the whole file, or hits
    `_MAX_TAIL_BYTES`.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size == 0:
        return None

    chunk = _TAIL_CHUNK
    with open(path, "rb") as f:
        while True:
            read = min(chunk, size)
            f.seek(size - read)
            data = f.read(read)
            text = data.decode("utf-8", errors="ignore")
            lines = text.split("\n")
            if read < size:
                # The first line of a partial tail is very likely cut mid-record —
                # drop it rather than risk json.loads on a truncated line.
                lines = lines[1:]
            for line in reversed(lines):
                line = line.strip()
                if not line or '"token_count"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if _is_token_count_record(rec):
                    return rec
            if read >= size or read >= _MAX_TAIL_BYTES:
                return None
            chunk *= 2


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
    return {"input": 0, "cached_input": 0, "cache_write": 0, "output": 0, "reasoning_output": 0, "total": 0}


def _accumulate(acc: dict[str, int], totals: dict[str, Any]) -> None:
    acc["input"] += int(totals.get("input_tokens") or 0)
    acc["cached_input"] += int(totals.get("cached_input_tokens") or 0)
    acc["cache_write"] += int(totals.get("cache_write_input_tokens") or 0)
    acc["output"] += int(totals.get("output_tokens") or 0)
    acc["reasoning_output"] += int(totals.get("reasoning_output_tokens") or 0)
    acc["total"] += int(totals.get("total_tokens") or 0)


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
        files = _iter_recent_session_files(home)
        if not files:
            return UsageResult(agent=self.key, error=t("usage.codex.error.no_sessions"))

        now = datetime.now(UTC)
        cutoffs = {"5h": now - timedelta(hours=5), "7d": now - timedelta(days=7)}
        window_totals = {"5h": _empty_totals(), "7d": _empty_totals()}

        latest_record: dict[str, Any] | None = None
        latest_ts: datetime | None = None

        for path in files:
            record = _latest_record_in_file(path)
            if record is None:
                continue
            ts = _parse_ts(record.get("timestamp"))
            if ts is None:
                continue
            if latest_ts is None or ts > latest_ts:
                latest_ts = ts
                latest_record = record
            # Codex's total_token_usage is cumulative for the whole session, so the
            # latest record per file already IS that session's running total — bucket
            # it into a window if the session was active within it.
            payload = record.get("payload") or {}
            info = payload.get("info") or {}
            totals = info.get("total_token_usage") or {}
            for label, cutoff in cutoffs.items():
                if ts >= cutoff:
                    _accumulate(window_totals[label], totals)

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
