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
     There is no login flow here; if the re-read still fails we just report it.

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
from pathlib import Path
from typing import Any
from urllib.parse import quote

from tintaview.core.config import AgentConfig, expand

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


def _parse_usage(payload: dict[str, Any]) -> UsageRow | None:
    # GUESSED field names — none of this is documented. Candidates chosen from the
    # plausible shape described in PLAN.md 6.3 ("plan usage in cents, remaining, %").
    # If neither a percent nor a cents figure turns up anywhere in the payload, this
    # is treated as an unrecognised response, not a zero.
    percent = _find_number(payload, ("percent", "pct", "utilization"))
    cents_used = _find_number(payload, ("usedcents", "spendcents", "cents"))
    limit_cents = _find_number(payload, ("limitcents", "hardlimit", "limit"))

    if percent is None and cents_used is None:
        return None

    right = ""
    if cents_used is not None and limit_cents:
        right = f"${cents_used / 100:.2f} of ${limit_cents / 100:.2f}"
    elif cents_used is not None:
        right = f"${cents_used / 100:.2f} used this period"

    return UsageRow(
        label="Current period usage",
        pct=float(percent) if percent is not None else 0.0,
        right=right,
        show_pct=percent is not None,
        severity="normal",
        kind="limit" if percent is not None else "info",
    )


# --------------------------------------------------------------------------- provider


class CursorUsageProvider(UsageProvider):
    key = "cursor"

    def fetch(self, agent_config: AgentConfig, timeout: float = 15.0) -> UsageResult:
        try:
            return self._fetch(agent_config, timeout)
        except Exception as e:  # noqa: BLE001 - contract: a provider must never raise
            log.exception("cursor usage provider failed unexpectedly")
            return UsageResult(agent=self.key, error=f"Cursor usage unavailable: {e!r}")

    def _fetch(self, agent_config: AgentConfig, timeout: float) -> UsageResult:
        db_path = _resolve_state_db(agent_config)
        try:
            token = _read_token(db_path)
        except _TokenError as e:
            log.info("cursor token unavailable: %s", e)  # the exception text, never the token
            if "not found" in str(e) or "not signed in" in str(e):
                return UsageResult(agent=self.key, error="Cursor not signed in.")
            return UsageResult(
                agent=self.key, error="Cursor usage unavailable (state DB locked or unreadable)."
            )

        try:
            data = self._post_with_retry(token, db_path, timeout)
        except urllib.error.HTTPError as e:
            return UsageResult(agent=self.key, error=f"Cursor usage endpoint HTTP {e.code}.")
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
            return UsageResult(agent=self.key, error=f"Cursor usage unavailable: {e!r}")

        log.debug("cursor usage payload shape: %s", _shape(data))
        row = _parse_usage(data)
        if row is None:
            return UsageResult(agent=self.key, error="Cursor usage unavailable (unofficial endpoint changed).")
        return UsageResult(agent=self.key, rows=[row], header="Cursor usage", source="official")

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
