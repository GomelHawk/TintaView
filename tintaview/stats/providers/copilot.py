"""GitHub Copilot CLI usage provider — local only, no network, no hooks.

Copilot CLI (`@github/copilot`) is a real agentic CLI like Claude Code/Codex/Cursor,
but it has no hook-based lighting integration here: its hook system exists (a rich
`preToolUse`/`postToolUse`/`sessionStart`/... event vocabulary), but it is dispatched
over an internal "SDK callback transport" aimed at programs embedding
`@github/copilot-sdk`, not a documented external shell-command hook the way Claude's
`settings.json`, Codex's `hooks.json` or Cursor's `hooks.json` are. So — like
JetBrains AI Assistant — this is stats-only, and there is no adapter for it in
`agents/`.

There is also no local quota percentage, deliberately: GitHub's real "X% of your
plan used, resets in Nd" figure comes from an internal, undocumented endpoint
(`copilot_internal/user`) that needs a token GitHub stores in the OS credential
store (Windows Credential Manager here, under a target like
`<uuid>.github-copilot-app`) via a two-step OAuth exchange
(`copilot_internal/v2/token` first) — reverse-engineerable in principle, but not
attempted here without a captured real response to verify field names against
(the same bar Cursor's and JetBrains's providers were held to).

What *is* solid: every model call is logged locally to
`<home>/session-store.db`'s `assistant_usage_events` table (confirmed against a real
database on a live machine, not guessed):

    SELECT model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
           reasoning_tokens, created_at
    FROM assistant_usage_events

`reasoning_tokens` is a subset of `output_tokens` (observed: a row with
`output_tokens=1002, reasoning_tokens=448`, matching OpenAI's convention of billing
reasoning as output), so it is not added separately. Cache tokens are tracked but
excluded from the headline total, same reasoning as Codex's `cached_input` handling:
a cache read/write is much cheaper than a fresh token and would inflate "how much did
I use" if folded in.

This mirrors Codex's own local fallback: plain, informational token totals
(`show_pct=False`, `kind="info"`) rather than a percentage nobody can verify.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from tintaview.core.config import AgentConfig, expand

from ..model import UsageProvider, UsageResult, UsageRow

DB_FILENAME = "session-store.db"
_WINDOW_DAYS = 7
_MAX_MODEL_ROWS = 5


class _DbError(Exception):
    """No usable `assistant_usage_events` data could be read."""


def _default_home() -> Path:
    return Path.home() / ".copilot"


def _resolve_db_path(agent_config: AgentConfig) -> Path:
    home = expand(agent_config.home) if agent_config.home else _default_home()
    return home / DB_FILENAME


def detect() -> bool:
    """Is a Copilot CLI usage database found at the default home? Mirrors
    `AgentAdapter.detect()` / `jetbrains.detect()`, used to pre-tick the wizard's
    opt-in — this integration has no adapter of its own since it has no hooks."""
    return _default_home().joinpath(DB_FILENAME).exists()


def _read_totals_by_model(db_path: Path, since: datetime) -> dict[str, int]:
    """Total (input + output) tokens per model, for events at or after `since`.

    Opened read-only, like Cursor's `state.vscdb` — Copilot CLI holds this file open
    in WAL mode while running, so a plain read/write connection can collide with it.
    """
    if not db_path.exists():
        raise _DbError(f"{db_path} not found")
    uri = f"file:{quote(db_path.as_posix(), safe='/:')}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            cur = con.execute(
                "SELECT model, input_tokens, output_tokens, created_at "
                "FROM assistant_usage_events WHERE created_at >= ?",
                (since.strftime("%Y-%m-%dT%H:%M:%S.000Z"),),
            )
            rows = cur.fetchall()
        finally:
            con.close()
    except sqlite3.Error as e:
        raise _DbError(f"could not read {db_path}: {e}") from e

    totals: dict[str, int] = {}
    for model, input_tokens, output_tokens, _created_at in rows:
        if not model:
            continue
        totals[model] = totals.get(model, 0) + int(input_tokens or 0) + int(output_tokens or 0)
    return totals


def _fmt_tokens(total: int) -> str:
    if total >= 1_000_000:
        return f"{total / 1e6:.2f}M tokens"
    return f"{total / 1e3:.1f}k tokens" if total >= 1000 else f"{total} tokens"


def _rows_from_totals(totals: dict[str, int]) -> list[UsageRow]:
    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    return [
        UsageRow(label=model, pct=0.0, right=_fmt_tokens(total),
                  show_pct=False, severity="normal", kind="info")
        for model, total in ranked[:_MAX_MODEL_ROWS]
        if total > 0
    ]


class CopilotUsageProvider(UsageProvider):
    key = "copilot"

    def fetch(self, agent_config: AgentConfig, timeout: float = 15.0) -> UsageResult:
        try:
            return self._fetch(agent_config)
        except Exception as e:  # noqa: BLE001 - contract: a provider must never raise
            return UsageResult(agent=self.key, error=f"GitHub Copilot usage unavailable: {e!r}")

    def _fetch(self, agent_config: AgentConfig) -> UsageResult:
        db_path = _resolve_db_path(agent_config)
        since = datetime.now(UTC) - timedelta(days=_WINDOW_DAYS)
        try:
            totals = _read_totals_by_model(db_path, since)
        except _DbError:
            return UsageResult(agent=self.key, error="GitHub Copilot usage data not found.")

        rows = _rows_from_totals(totals)
        if not rows:
            return UsageResult(agent=self.key, error="No recent GitHub Copilot activity found.")
        return UsageResult(
            agent=self.key, rows=rows,
            header=f"GitHub Copilot — token totals ({_WINDOW_DAYS}d)", source="activity",
        )
