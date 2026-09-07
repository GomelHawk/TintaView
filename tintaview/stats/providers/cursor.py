"""Cursor usage provider — UNOFFICIAL. No personal-usage API is published; this talks
to the same Connect RPC the Cursor app itself uses, authenticated with the token
Cursor already has sitting in its local state DB. Handle with care: it can break on
any Cursor release, and the token is a live credential.

Flow:
  1. Read `cursorAuth/accessToken` from the `ItemTable` of Cursor's `state.vscdb`
     (path is platform-specific; `AgentConfig.state_db` overrides, empty = auto-detect).
  2. `POST https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage`
     with that token → current-period usage (plan usage in cents, remaining, percent).
  3. On 401/403, re-read the token from disk once and retry — Cursor rotates it while
     signed in, so a stale in-memory copy looks like an auth failure but usually isn't.
     There is no login flow here; if the re-read still fails we report it as
     ``error_kind="auth"``, which stops ``StatsService`` masking a signed-out Cursor
     with rows from the last period it could read (``stats/service.py``).

Non-negotiable rules (this is a real credential, and the DB is huge and someone else's):
  - Open the DB read-only (`file:...?mode=ro`) and never copy it — it has been observed
    at 300+ MB on a real machine and Cursor holds it open while running.
  - Read the token fresh on every fetch; never persist, cache, or log it. This module
    never passes the token variable to a logger anywhere, by construction — the debug
    line below logs only the response's top-level shape, never the request that carried
    the token.
  - Only ever sent to https://api2.cursor.sh.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from tintaview.core.config import AgentConfig, expand
from tintaview.i18n import t

from .. import format as fmt
from ..model import UsageProvider, UsageResult, UsageRow

log = logging.getLogger(__name__)

USAGE_URL = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
TOKEN_KEY = "cursorAuth/accessToken"


class _TokenError(Exception):
    """No usable Cursor access token could be read from disk."""


# --------------------------------------------------------------------------- state DB


def _default_state_db() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    if sys.platform == "darwin":
        return (
            Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
        )
    return Path.home() / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"


def _resolve_state_db(agent_config: AgentConfig) -> Path:
    override = getattr(agent_config, "state_db", "") or ""
    return expand(override) if override else _default_state_db()


def _read_token(db_path: Path) -> str:
    """Read the access token fresh from Cursor's state DB.

    Opened read-only and never copied — see module docstring. `mode=ro` still requires
    the file to exist; sqlite raises OperationalError for "unable to open database
    file", a locked WAL, or (via the SELECT) a missing ItemTable, all of which fold
    into `_TokenError` here so the caller has exactly one thing to catch.
    """
    if not db_path.exists():
        raise _TokenError(f"state DB not found at {db_path}")
    # quote() escapes spaces/`?`/`#` etc. that would otherwise be parsed as URI query
    # syntax; safe="/" keeps path separators (and a Windows drive colon) intact.
    uri = f"file:{quote(db_path.as_posix(), safe='/:')}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            cur = con.execute("SELECT value FROM ItemTable WHERE key = ?", (TOKEN_KEY,))
            row = cur.fetchone()
        finally:
            con.close()
    except sqlite3.Error as e:
        raise _TokenError(f"could not read Cursor state DB: {e}") from e
    if not row or not row[0]:
        raise _TokenError("no Cursor access token in state DB — not signed in")
    value = row[0]
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    # Observed stored both as a bare string and as a JSON-quoted string across Cursor
    # versions — try JSON first, fall back to the raw value either way.
    try:
        decoded = json.loads(value)
        if isinstance(decoded, str) and decoded:
            return decoded
    except (json.JSONDecodeError, TypeError):
        pass
    return value


# --------------------------------------------------------------------------- RPC


def _post_usage(token: str, timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(
        USAGE_URL,
        data=b"{}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _shape(data: Any) -> Any:
    """Top-level keys only, for the debug log line — never the token, never the full
    (potentially large) payload."""
    if isinstance(data, dict):
        return sorted(data.keys())
    return type(data).__name__


def _find_number(obj: Any, names: tuple[str, ...]) -> float | None:
    """Depth-first search for the first numeric value under a key matching `names`
    (case-insensitive substring match).

    The Dashboard RPC's response shape is not published; this degrades to "not found"
    rather than assuming a fixed path so a schema change surfaces as a clear error
    instead of a wrong number or a crash.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and isinstance(v, (int, float)) and any(n in k.lower() for n in names):
                return float(v)
        for v in obj.values():
            found = _find_number(v, names)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_number(item, names)
            if found is not None:
                return found
    return None


def _cycle_end_epoch(value: Any) -> float:
    """Billing-cycle end as epoch seconds, from an epoch-milliseconds string; 0.0 if it
    can't be read.

    Observed as a 13-character digit string (``"1789125077000"``). Seconds are accepted
    too, so a change of unit degrades to a wrong-but-harmless label rather than a crash.

    The instant, not "Resets 14 Sep": `fmt.RESET_DATE` words it at render time, so a
    cycle end that has since passed is a comparison the renderer can still make instead
    of a dead date frozen into a string. That was the second half of the incident behind
    `UsageRow.reset_at` — this row read "Resets 6 Sep" on the 7th.
    """
    if value is None:
        return 0.0
    text = str(value).strip()
    if not text.lstrip("+-").isdigit():
        return 0.0
    try:
        number = float(text)
    except ValueError:
        return 0.0
    if abs(number) > 1e11:  # 1e11 seconds is year 5138 — this is milliseconds
        number /= 1000.0
    try:
        datetime.fromtimestamp(number, UTC)  # reject anything `fmt` couldn't render
    except (OverflowError, OSError, ValueError):
        return 0.0
    return number


def _usage_row(label: str, pct: Any, right: str = "", reset_at: float = 0.0) -> UsageRow | None:
    if not isinstance(pct, int | float) or isinstance(pct, bool):
        return None
    severity = "critical" if pct >= 90 else "warning" if pct >= 75 else "normal"
    return UsageRow(label=label, pct=float(pct), right=right, reset_at=reset_at,
                    reset_style=fmt.RESET_DATE, show_pct=True,
                    severity=severity, kind="limit")


def _parse_usage(payload: dict[str, Any]) -> list[UsageRow]:
    """The two quotas a Pro plan actually has, matching Cursor's own usage screen.

    Field names confirmed against a live `GetCurrentPeriodUsage` response rather than
    guessed (an earlier version searched for any key containing "percent" and so
    collapsed both quotas into one row showing whichever it hit first):

        planUsage.autoPercentUsed  -> "Cursor Models" (Composer, Cursor Grok, …)
        planUsage.apiPercentUsed   -> "Other Models"  (third-party models, on-demand spend)
        planUsage.includedSpend    -> cents of API usage the plan includes
        billingCycleEnd            -> epoch millis, shared by both quotas

    Returns [] for a response carrying neither percentage, which the caller reports as an
    unrecognised payload — deliberately not a row of zeroes, which would look like real
    "you have used nothing" data.
    """
    plan = payload.get("planUsage")
    if not isinstance(plan, dict):
        return []

    resets = _cycle_end_epoch(payload.get("billingCycleEnd"))
    rows: list[UsageRow] = []

    auto = _usage_row(t("usage.cursor.models"), plan.get("autoPercentUsed"), reset_at=resets)
    if auto is not None:
        rows.append(auto)

    # The included API allowance is what makes "Other Models" legible — Cursor's own UI
    # says "your plan includes at least $20 of API usage" next to this bar. Only shown
    # when the cycle-reset text isn't already occupying the first row's slot.
    api_right = ""
    api_reset = 0.0
    included = plan.get("includedSpend")
    if isinstance(included, int | float) and not isinstance(included, bool) and included > 0:
        api_right = t("usage.cursor.included", amount=f"${included / 100:,.2f}")
    elif not rows:
        api_reset = resets
    api = _usage_row(t("usage.cursor.other_models"), plan.get("apiPercentUsed"),
                      right=api_right, reset_at=api_reset)
    if api is not None:
        rows.append(api)

    return rows


# --------------------------------------------------------------------------- provider


class CursorUsageProvider(UsageProvider):
    key = "cursor"

    def fetch(self, agent_config: AgentConfig, timeout: float = 15.0) -> UsageResult:
        try:
            return self._fetch(agent_config, timeout)
        except Exception as e:  # noqa: BLE001 - contract: a provider must never raise
            log.exception("cursor usage provider failed unexpectedly")
            return UsageResult(agent=self.key,
                                error=t("usage.cursor.error.unavailable", detail=repr(e)))

    def _fetch(self, agent_config: AgentConfig, timeout: float) -> UsageResult:
        db_path = _resolve_state_db(agent_config)
        try:
            token = _read_token(db_path)
        except _TokenError as e:
            log.info("cursor token unavailable: %s", e)  # the exception text, never the token
            if "not found" in str(e) or "not signed in" in str(e):
                # No token at all: an auth failure, not a blip. See `error_kind`.
                return UsageResult(agent=self.key, error=t("usage.cursor.error.not_signed_in"),
                                    error_kind="auth")
            # A locked or unreadable state DB *is* transient — Cursor holds the lock
            # while it writes — so this one keeps falling back to cached rows.
            return UsageResult(agent=self.key, error=t("usage.cursor.error.db_unreadable"))

        try:
            data = self._post_with_retry(token, db_path, timeout)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                # `_post_with_retry` already re-read the token and tried once more, so a
                # second rejection means the stored session is dead rather than merely
                # rotated — the same "sign in again" state as having no token at all.
                return UsageResult(agent=self.key, error=t("usage.cursor.error.not_signed_in"),
                                    error_kind="auth")
            return UsageResult(agent=self.key, error=t("usage.cursor.error.http", code=e.code))
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
            return UsageResult(agent=self.key,
                                error=t("usage.cursor.error.unavailable", detail=repr(e)))

        log.debug("cursor usage payload shape: %s", _shape(data))
        rows = _parse_usage(data)
        if not rows:
            return UsageResult(agent=self.key, error=t("usage.cursor.error.payload"))
        return UsageResult(agent=self.key, rows=rows, header=t("usage.cursor.header"),
                            source="official")

    def _post_with_retry(self, token: str, db_path: Path, timeout: float) -> dict[str, Any]:
        try:
            return _post_usage(token, timeout)
        except urllib.error.HTTPError as e:
            if e.code not in (401, 403):
                raise
            try:
                token = _read_token(db_path)
            except _TokenError:
                raise e from None
            return _post_usage(token, timeout)
