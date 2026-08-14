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
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from tintaview.core.config import AgentConfig, expand

from ..model import UsageProvider, UsageResult, UsageRow

log = logging.getLogger(__name__)

QUOTA_FILENAME = "AIAssistantQuotaManager2.xml"
COMPONENT_NAME = "AIAssistantQuotaManager2"
#: Raw quota units per "credit", as JetBrains's own status-bar widget displays them —
#: see the module docstring for how this was derived.
CREDIT_SCALE = 100_000.0

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


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
    return max(candidates, key=lambda p: p.stat().st_mtime)


def detect() -> bool:
    """Is a JetBrains AI Assistant quota file found anywhere on this machine?

    Mirrors `AgentAdapter.detect()` for the hook-based agents, used the same way to
    pre-tick the wizard's opt-in — but this integration has no adapter of its own
    since it has no hooks at all (see module docstring).
    """
    try:
        _find_newest(_default_jetbrains_root())
        return True
    except _QuotaFileError:
        return False


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


def _fmt_date(value: Any, prefix: str) -> str:
    if not isinstance(value, str) or not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return ""
    return f"{prefix} {dt.day} {_MONTHS[dt.month - 1]}"


def _quota_row(label: str, sub_quota: Any, right: str, kind: str) -> UsageRow | None:
    if not isinstance(sub_quota, dict):
        return None
    current = _num(sub_quota.get("current"))
    maximum = _num(sub_quota.get("maximum"))
    if current is None or maximum is None:
        return None
    pct = _pct(current, maximum)
    if pct is None:
        return None
    return UsageRow(label=label, pct=pct, right=right, show_pct=True,
                     severity=_severity(pct), kind=kind)


def _parse_usage(quota: dict[str, Any], next_refill: dict[str, Any] | None) -> list[UsageRow]:
    """The two rows the IDE's own widget shows: monthly credits (a bar + "Renews ..."
    date) and, only once any have been purchased, a top-up balance in credits."""
    rows: list[UsageRow] = []

    reset = ""
    if isinstance(next_refill, dict) and next_refill.get("type") == "Known":
        reset = _fmt_date(next_refill.get("next"), "Renews")
    included = _quota_row("Monthly Credits", quota.get("tariffQuota"), reset, "limit")
    if included is not None:
        rows.append(included)

    top_up = quota.get("topUpQuota")
    if isinstance(top_up, dict) and (_num(top_up.get("maximum")) or 0) > 0:
        available = _num(top_up.get("available"))
        right = f"{available / CREDIT_SCALE:.2f} credits available" if available is not None else ""
        top_up_row = _quota_row("Top-up Credits", top_up, right, "credits")
        if top_up_row is not None:
            rows.append(top_up_row)

    return rows


# --------------------------------------------------------------------------- provider


class JetBrainsUsageProvider(UsageProvider):
    key = "jetbrains"

    def fetch(self, agent_config: AgentConfig, timeout: float = 15.0) -> UsageResult:
        try:
            return self._fetch(agent_config)
        except Exception as e:  # noqa: BLE001 - contract: a provider must never raise
            log.exception("jetbrains usage provider failed unexpectedly")
            return UsageResult(agent=self.key, error=f"JetBrains AI usage unavailable: {e!r}")

    def _fetch(self, agent_config: AgentConfig) -> UsageResult:
        try:
            path = _resolve_quota_path(agent_config)
        except _QuotaFileError as e:
            log.info("jetbrains quota file unavailable: %s", e)
            return UsageResult(
                agent=self.key,
                error="JetBrains AI Assistant not found (no IDE has synced a quota yet).",
            )

        try:
            quota, next_refill = _parse_file(path)
        except _QuotaFileError as e:
            log.info("jetbrains quota file unreadable: %s", e)
            return UsageResult(agent=self.key, error="JetBrains AI usage unavailable (quota file unreadable).")

        rows = _parse_usage(quota, next_refill)
        if not rows:
            return UsageResult(agent=self.key, error="JetBrains AI usage unavailable (no active quota).")
        return UsageResult(agent=self.key, rows=rows, header="AI Assistant", source="official")
