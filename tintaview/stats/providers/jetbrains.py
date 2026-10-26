"""JetBrains AI Assistant usage provider.

There is no personal-usage API and no CLI: JetBrains AI Assistant is an IDE plugin,
not a scriptable agent, so (unlike Claude/Codex/Cursor) there is no hook layer for it
at all — this provider is TintaView's only integration point for it. It reads the
same local quota cache the IDE's own status-bar widget is populated from:

    <IDE data dir>/options/AIAssistantQuotaManager2.xml

  Windows: %APPDATA%\\JetBrains\\<Product><Version>\\options\\...
  macOS:   ~/Library/Application Support/JetBrains/<Product><Version>/options/...
  Linux:   ~/.config/JetBrains/<Product><Version>/options/...

It is a standard JVM `Storage` XML: one `<option name="..." value="...">` per field,
the value itself a JSON string. Two options matter, confirmed against real files on
a live machine (six of them: PhpStorm/PyCharm/WebStorm x two installed versions
each) rather than guessed:

    quotaInfo   {"current": "161185.755", "maximum": "5498808.015", "until": "...",
                 "tariffQuota": {"current": ..., "maximum": ..., "available": ...},
                 "topUpQuota":  {"current": ..., "maximum": ..., "available": ...}}
    nextRefill  {"type": "Known", "next": "2026-08-16T10:00:11.989Z",
                 "tariff": {"amount": "1000000", "duration": "PT720H"}}

The top-level `current`/`maximum` are always exactly the sum of `tariffQuota` and
`topUpQuota`'s — `tariffQuota` is the recurring allowance that refills every
`nextRefill.tariff.duration` (PT720H = 30 days on every file observed), `topUpQuota`
is a purchased balance that carries over and is drawn down only once the tariff
quota is exhausted. The tariff quota's own percentage, not the combined total, is
what tells you whether you're about to run dry — the top-up pool is typically many
times larger and would make a combined percentage look artificially healthy.

`CREDIT_SCALE` (raw units per "credit") was reverse-engineered, not documented: the
IDE's own AI Assistant widget showed "8.27 / 10.00 monthly credits left" and "44.99
top-up credits" at the exact moment a quota file read `tariffQuota.maximum
"1000000"` and `topUpQuota.available "4498808.015"` — both divide out to those same
numbers by 100,000 (`10.00 == 1000000 / 100000`, `44.99 ≈ 4498808.015 / 100000`), and
the same "1000000" tariff maximum was constant across every file on this machine
regardless of plan usage. Purely a display nicety if this drifts on another plan
tier — it only affects the formatted `right` text, never the percentage/severity,
which are computed straight from the raw current/maximum.

Quota is account-wide, but each installed IDE only syncs its own copy of this file
when *that* IDE last talked to the JetBrains AI service — a rarely opened IDE's copy
can be weeks stale even though the number is the same account balance. With no way
to force a sync, the freshest file on disk (by mtime) is the best available signal,
so every `<Product><Version>` directory under the JetBrains config root is scanned
and the newest wins. `AgentConfig.quota_path` overrides this with a specific file or
IDE directory when auto-detection picks the wrong one.

**PhpStorm 2026.3 stopped refreshing that file** (measured 2026-10-26: its copy was
frozen at the day the IDE was installed and said 0.13 credits used and a refill on 15 Oct,
while the IDE itself showed 9.36 of 10.00 left and a renewal on 14 Nov). The live balance
is in `<IDE data dir>/options/other.xml` instead, rewritten as the IDE runs, in two keys
that hold the *available* amount in the same raw units::

    AIHubNotificationManager.lastTariffQuotaAvailable   "935729.9515"   -> 9.36 credits left
    AIHubNotificationManager.lastTopUpQuotaAvailable    "4498808.015"   -> 44.99 top-up

Both match the IDE's widget exactly. That file has no maximum and no renewal date, so
those come from the newest quota file (the maximum is a plan constant, the date rolls
forward by `nextRefill.tariff.duration` — Oct 15 + 30 days is the Nov 14 the IDE showed).
Whichever source was written most recently wins; figures older than the current period's
start get a notice instead of passing as current.

Junie CLI bills to the same account, so its spend is counted **in the same figure**, not on
a line of its own: the IDE's balance already includes everything it had synced, so only the
Junie spend recorded *after* the figure was written (`stats/junie_usage`, summed over its
Windows and WSL session folders) is added, and the numbers carry a `~` while it is.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from tintaview.core.config import AgentConfig, expand
from tintaview.i18n import t

from .. import format as fmt
from .. import junie_usage
from ..model import UsageProvider, UsageResult, UsageRow

log = logging.getLogger(__name__)

QUOTA_FILENAME = "AIAssistantQuotaManager2.xml"
COMPONENT_NAME = "AIAssistantQuotaManager2"
#: Raw quota units per "credit", as JetBrains's own status-bar widget displays them —
#: see the module docstring for how this was derived.
CREDIT_SCALE = 100_000.0

LIVE_FILENAME = "other.xml"
#: One key's value in the IDE's `other.xml`, which the JVM writer entity-escapes
#: (`&quot;`) inside a JSON blob — so both spellings of the quote are accepted.
_LIVE_VALUE = r'AIHubNotificationManager\.{key}(?:&quot;|")\s*:\s*(?:&quot;|")([0-9]+(?:\.[0-9]+)?)'
_LIVE_TARIFF_RE = re.compile(_LIVE_VALUE.format(key="lastTariffQuotaAvailable"))
_LIVE_TOP_UP_RE = re.compile(_LIVE_VALUE.format(key="lastTopUpQuotaAvailable"))
#: A plan's allowance period when the file doesn't say (PT720H on every file observed).
DEFAULT_PERIOD_S = 30 * 86400.0


def _now() -> float:
    """The clock, in one place so tests can pin the period a fixture belongs to."""
    return time.time()


class _QuotaFileError(Exception):
    """No usable JetBrains AI Assistant quota file could be found or parsed."""


# --------------------------------------------------------------------------- discovery


def _default_jetbrains_root() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "JetBrains"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "JetBrains"
    return Path.home() / ".config" / "JetBrains"


def _find_newest(root: Path) -> Path:
    if not root.is_dir():
        raise _QuotaFileError(f"JetBrains config root not found at {root}")
    candidates = list(root.glob(f"*/options/{QUOTA_FILENAME}"))
    if not candidates:
        raise _QuotaFileError(f"no {QUOTA_FILENAME} found under {root}")
    # The stat has to be tolerant: the IDE rewrites this file on its own sync schedule,
    # so one of the candidates can vanish between the glob and the stat. A raw OSError
    # here escapes the `_QuotaFileError` handler and takes the whole provider down —
    # treating an unreadable candidate as infinitely old just picks another one.
    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    return max(candidates, key=_mtime)


@dataclass(frozen=True)
class _Live:
    """The balance an IDE keeps current in `other.xml` (raw units, amounts *available*)."""

    tariff_available: float
    top_up_available: float | None
    mtime: float


def _find_live(root: Path) -> _Live | None:
    """The newest `other.xml` under `root` that carries the live balance, else None.

    Candidates are tried newest first and the first match wins, so only as many files
    are read as it takes — they can be large, and most IDE folders have none of the keys.
    """
    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    try:
        candidates = sorted(root.glob(f"*/options/{LIVE_FILENAME}"), key=_mtime, reverse=True)
    except OSError:
        return None
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        tariff = _LIVE_TARIFF_RE.search(text)
        if tariff is None:
            continue
        top_up = _LIVE_TOP_UP_RE.search(text)
        return _Live(float(tariff.group(1)),
                     float(top_up.group(1)) if top_up is not None else None, _mtime(path))
    return None


def detect() -> bool:
    """Is JetBrains AI Assistant quota data found anywhere on this machine?

    Mirrors `AgentAdapter.detect()` for the hook-based agents, used the same way to
    pre-tick the wizard's opt-in — but this integration has no adapter of its own
    since it has no hooks at all (see module docstring).
    """
    root = _default_jetbrains_root()
    try:
        _find_newest(root)
        return True
    except _QuotaFileError:
        return _find_live(root) is not None


def _resolve_quota_path(agent_config: AgentConfig) -> Path:
    override = getattr(agent_config, "quota_path", "") or ""
    if not override:
        return _find_newest(_default_jetbrains_root())
    path = expand(override)
    if path.is_file():
        return path
    candidate = path / "options" / QUOTA_FILENAME
    if candidate.exists():
        return candidate
    raise _QuotaFileError(f"no quota file found at or under {path}")


# --------------------------------------------------------------------------- parsing


def _parse_file(path: Path) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The `quotaInfo` (required) and `nextRefill` (best-effort) JSON payloads.

    XML parsing already unescapes the embedded `&quot;`/`&#10;` entities the JVM
    writer produces, so `option.get("value")` is plain JSON text with no further
    unescaping needed.
    """
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as e:
        raise _QuotaFileError(f"could not parse {path}: {e}") from e

    component = root.find(f".//component[@name='{COMPONENT_NAME}']")
    if component is None:
        raise _QuotaFileError(f"{COMPONENT_NAME} component not found in {path}")

    def option_json(name: str) -> dict[str, Any] | None:
        option = component.find(f"./option[@name='{name}']")
        value = option.get("value") if option is not None else None
        if not value:
            return None
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return None
        return decoded if isinstance(decoded, dict) else None

    quota = option_json("quotaInfo")
    if quota is None:
        raise _QuotaFileError(f"quotaInfo missing or invalid in {path}")
    return quota, option_json("nextRefill")


def _num(value: Any) -> float | None:
    """Quota numbers are stored as JSON strings (e.g. `"161185.755"`), not numbers."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct(current: float, maximum: float) -> float | None:
    if maximum <= 0:
        return None
    return max(0.0, min(100.0, current / maximum * 100))


def _severity(pct: float) -> str:
    return "critical" if pct >= 90 else "warning" if pct >= 75 else "normal"


def _date_text(timestamp: float) -> str:
    """A renewal instant as a bare "14 Nov".

    Bare on purpose: the two places it is used need it worded differently ("Renews 14
    Nov" on its own, just the date when it shares the row with a credit figure), and the
    previous version built the long form and then stripped the "Renews " prefix back off
    with `removeprefix` — which silently stops working the moment that word is a
    translation rather than a literal.
    """
    return fmt.date_text(datetime.fromtimestamp(timestamp).astimezone())


def _duration_seconds(text: Any) -> float | None:
    """An ISO-8601 duration such as ``PT720H`` or ``P30D`` in seconds, else None."""
    match = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", text) \
        if isinstance(text, str) else None
    if match is None:
        return None
    days, hours, minutes, seconds = (int(g or 0) for g in match.groups())
    total = days * 86400 + hours * 3600 + minutes * 60 + seconds
    return float(total) if total > 0 else None


def _period(next_refill: dict[str, Any] | None, now: float) -> tuple[float, float] | None:
    """`(start, end)` of the allowance period `now` falls in, from `nextRefill`, else None.

    The file names *one* refill instant, which goes stale the moment it passes — the
    allowance simply recurs every `tariff.duration`, so a past instant is rolled forward by
    whole periods (Oct 15 10:00Z + 30 days is the Nov 14 the IDE itself showed).
    """
    if not isinstance(next_refill, dict) or next_refill.get("type") != "Known":
        return None
    try:
        end = datetime.fromisoformat(str(next_refill.get("next")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    tariff = next_refill.get("tariff")
    duration = _duration_seconds(tariff.get("duration") if isinstance(tariff, dict) else None) \
        or DEFAULT_PERIOD_S
    if end <= now:
        end += (int((now - end) // duration) + 1) * duration
    return end - duration, end


def _with_live(quota: dict[str, Any] | None, live: _Live) -> dict[str, Any]:
    """`quotaInfo`-shaped figures from the live balance.

    The maximum is the plan constant from the newest quota file when there is one, which
    is what turns "available" into a used amount and a bar. Without it the rows carry the
    available amount alone — saying nothing about a limit the file never gave.
    """
    def sub(name: str, available: float | None) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if available is None:
            return out
        out["available"] = str(available)
        known = (quota or {}).get(name)
        maximum = _num(known.get("maximum")) if isinstance(known, dict) else None
        if maximum is not None and maximum > 0:
            out["maximum"] = str(maximum)
            out["current"] = str(max(0.0, maximum - available))
        return out

    return {"tariffQuota": sub("tariffQuota", live.tariff_available),
            "topUpQuota": sub("topUpQuota", live.top_up_available)}


def _with_spend(figures: dict[str, Any], credits: float) -> dict[str, Any]:
    """`figures` with `credits` more spent from the monthly allowance (it is drawn first)."""
    tariff = figures.get("tariffQuota")
    if not isinstance(tariff, dict) or credits <= 0:
        return figures
    raw = credits * CREDIT_SCALE
    adjusted = dict(tariff)
    current, available = _num(tariff.get("current")), _num(tariff.get("available"))
    if current is not None:
        adjusted["current"] = str(current + raw)
    if available is not None:
        adjusted["available"] = str(max(0.0, available - raw))
    return {**figures, "tariffQuota": adjusted}


def _quota_row(label: str, sub_quota: Any, right: str, kind: str) -> UsageRow | None:
    if not isinstance(sub_quota, dict):
        return None
    current = _num(sub_quota.get("current"))
    maximum = _num(sub_quota.get("maximum"))
    if maximum is None and _num(sub_quota.get("available")) is not None:
        # An amount with no limit to measure it against: text only, no bar.
        return UsageRow(label=label, pct=0.0, right=right, show_pct=False, kind="info")
    if current is None or maximum is None:
        return None
    pct = _pct(current, maximum)
    if pct is None:
        return None
    # The bar still fills and colours by `pct` — only the raw "NN%" text is dropped, in
    # favour of the credit points the IDE's own widget shows (see module docstring),
    # matching the "Usage credits" row Claude's provider already uses the same way.
    return UsageRow(label=label, pct=pct, right=right, show_pct=False,
                     severity=_severity(pct), kind=kind)


def _parse_usage(quota: dict[str, Any], date: str, approx: bool = False) -> list[UsageRow]:
    """The two rows the IDE's own widget is built from: monthly credits (a bar plus
    how much of it is *used*, matching every other provider's percentage-of-usage
    convention — Claude's "Usage credits" row and Copilot's quota rows both read as
    "how much have I consumed", not "how much is left") and, only once any have been
    purchased, a top-up balance in credits."""
    rows: list[UsageRow] = []

    tariff = quota.get("tariffQuota")
    right = t("usage.renews", date=date) if date else ""
    if isinstance(tariff, dict):
        current = _num(tariff.get("current"))
        maximum = _num(tariff.get("maximum"))
        left = _num(tariff.get("available"))
        if current is None and maximum is None and left is not None:
            credits = t("usage.jetbrains.credits_left",
                        left=f"{'~' if approx else ''}{left / CREDIT_SCALE:.2f}")
            right = t("usage.jetbrains.credits_and_date", credits=credits, date=date) if date else credits
        elif current is not None and maximum is not None:
            # `current` is the amount already used (this is also what `_pct`/severity
            # are computed from, so the text must match: a higher `current` means more
            # consumed and a redder bar, not more remaining).
            credits = t("usage.jetbrains.credits_used",
                        used=f"{'~' if approx else ''}{current / CREDIT_SCALE:.2f}",
                        maximum=f"{maximum / CREDIT_SCALE:.2f}")
            # The bare date, not the "Renews DD Mon" form, is what keeps this row inside
            # the flyout's 380px card alongside the "Monthly Credits" label — the full
            # combination measures wider than the card.
            right = t("usage.jetbrains.credits_and_date", credits=credits, date=date) if date else credits
    included = _quota_row(t("usage.jetbrains.monthly_credits"), tariff, right, "limit")
    if included is not None:
        rows.append(included)

    top_up = quota.get("topUpQuota")
    if isinstance(top_up, dict) and ((_num(top_up.get("maximum")) or 0) > 0
                                     or (_num(top_up.get("available")) or 0) > 0):
        available = _num(top_up.get("available"))
        right = (t("usage.jetbrains.credits_available", available=f"{available / CREDIT_SCALE:.2f}")
                 if available is not None else "")
        top_up_row = _quota_row(t("usage.jetbrains.top_up_credits"), top_up, right, "credits")
        if top_up_row is not None:
            rows.append(top_up_row)

    return rows


# --------------------------------------------------------------------------- provider


class JetBrainsUsageProvider(UsageProvider):
    key = "jetbrains"

    def __init__(self, junie_roots: Callable[[], list[Path]] | None = None) -> None:
        #: The Junie CLI homes to read spend from; None (the default) reads none, which is
        #: what keeps a bare `JetBrainsUsageProvider()` off the real machine's `~/.junie`.
        self._junie_roots = junie_roots

    def fetch(self, agent_config: AgentConfig, timeout: float = 15.0, *,
              with_estimate: bool = True) -> UsageResult:
        try:
            return self._fetch(agent_config, with_estimate)
        except Exception as e:  # noqa: BLE001 - contract: a provider must never raise
            log.exception("jetbrains usage provider failed unexpectedly")
            return UsageResult(agent=self.key,
                                error=t("usage.jetbrains.error.unavailable", detail=repr(e)))

    def _junie_spend(self, since: float, now: float) -> float:
        """Credits Junie CLI spent after `since` on the JetBrains account, 0 when unknown."""
        if self._junie_roots is None:
            return 0.0
        return junie_usage.credits_since(self._junie_roots(), since, now=now)[0]

    def _fetch(self, agent_config: AgentConfig, with_estimate: bool = True) -> UsageResult:
        now = _now()
        override = getattr(agent_config, "quota_path", "") or ""
        root = _default_jetbrains_root()

        path: Path | None = None
        try:
            path = _resolve_quota_path(agent_config)
        except _QuotaFileError as e:
            log.info("jetbrains quota file unavailable: %s", e)
        quota: dict[str, Any] | None = None
        next_refill: dict[str, Any] | None = None
        quota_mtime = 0.0
        unreadable = False
        if path is not None:
            try:
                quota, next_refill = _parse_file(path)
                quota_mtime = path.stat().st_mtime
            except _QuotaFileError as e:
                log.info("jetbrains quota file unreadable: %s", e)
                unreadable = True
            except OSError:
                quota_mtime = 0.0
        # A pinned `quota_path` means "this file, not what you find" — no live balance then.
        live = None if override else _find_live(root)

        period = _period(next_refill, now)

        if quota is None and live is None:
            error = (t("usage.jetbrains.error.unreadable") if unreadable
                     else t("usage.jetbrains.error.not_found"))
            return UsageResult(agent=self.key, error=error)

        if live is not None and (quota is None or live.mtime >= quota_mtime):
            figures, source_mtime = _with_live(quota, live), live.mtime
        else:
            figures, source_mtime = quota or {}, quota_mtime
        # Figures written before this period began describe the last one. They are still
        # the best official numbers there are, so they stay — under a line saying so, and
        # without Junie's spend added: that spend belongs to a period they do not cover.
        stale = period is not None and source_mtime < period[0]
        spend = 0.0
        if with_estimate and not stale and source_mtime > 0:
            # What the IDE has not seen yet: the balance already includes everything Junie
            # had synced by the time it was written, so only later spend is new.
            spend = self._junie_spend(source_mtime, now)
        rows = _parse_usage(_with_spend(figures, spend), _date_text(period[1]) if period else "",
                            approx=spend > 0)
        if not rows:
            return UsageResult(agent=self.key, error=t("usage.jetbrains.error.no_quota"))
        return UsageResult(agent=self.key, rows=rows, header=t("usage.jetbrains.header"),
                           source="official",
                           notice=t("usage.jetbrains.stale_period") if stale else None)
