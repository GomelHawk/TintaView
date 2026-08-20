"""GitHub Copilot CLI usage provider — local only, no network, no hooks (except the
one verified, read-only call to GitHub's own quota endpoint described below).

Copilot CLI (`@github/copilot`) is a real agentic CLI like Claude Code/Codex/Cursor,
but it has no hook-based lighting integration here: its hook system exists (a rich
`preToolUse`/`postToolUse`/`sessionStart`/... event vocabulary), but it is dispatched
over an internal "SDK callback transport" aimed at programs embedding
`@github/copilot-sdk`, not a documented external shell-command hook the way Claude's
`settings.json`, Codex's `hooks.json` or Cursor's `hooks.json` are. So — like
JetBrains AI Assistant — this is stats-only, and there is no adapter for it in
`agents/`.

GitHub's real "X% of your plan used, resets in Nd" figure — what the account's own
Copilot usage page shows — comes from an internal, undocumented endpoint
(`GET https://api.github.com/copilot_internal/user`), gated behind the OAuth token
GitHub stores in the OS credential store. Confirmed against a real response on a
live Windows machine (`Authorization: token <token>` on the raw `gho_...` OAuth
token was enough — no separate `copilot_internal/v2/token` exchange needed to read
quota, that exchange is only for the completions/chat API itself), not guessed (the
same bar Cursor's and JetBrains's providers were held to):

    {"access_type_sku": "free_limited_copilot", "copilot_plan": "individual",
     "quota_reset_date_utc": "2026-09-01T00:00:00.000Z",
     "quota_snapshots": {
       "chat": {"has_quota": true, "unlimited": false, "percent_remaining": 97.4,
                 "entitlement": 200, "remaining": 194, ...},
       "completions": {"has_quota": true, ...},
       "premium_interactions": {"has_quota": false, "entitlement": 0, ...}}}

`has_quota: false` (seen on the free plan's `premium_interactions`) means "not part
of this plan", not "zero left" — such quotas are skipped rather than shown as an
alarming 100%-used row.

The token itself lives in Windows Credential Manager under a target named
`<install-uuid>.github-copilot-app` (confirmed via a real `cmdkey /list` on a live
machine) — the UUID is per-install, so it is discovered via `CredEnumerateW` rather
than guessed, then the one matching credential is fetched with `CredReadW`. This is
Windows-only: Copilot CLI's token storage on macOS/Linux has not been captured, so
there this degrades straight to the local fallback below rather than guessing a
Keychain/libsecret path.

Non-negotiable rules (this is a real, live OAuth credential):
  - Never log, persist, or cache the token — it is read fresh into a local variable
    on every fetch and only ever attached to the one request to
    `api.github.com/copilot_internal/user`.
  - `CredEnumerateW` is used only to discover the *target name*; the credential
    blob itself is fetched once via a separate, exact-name `CredReadW` call, not by
    reading blobs off the full enumeration (which may include unrelated
    credentials).

If the token can't be read (non-Windows, not signed in, endpoint shape changes) or
the account has no reachable quota, this falls back to what was already here:
every model call is logged locally to `<home>/session-store.db`'s
`assistant_usage_events` table (confirmed against a real database on a live
machine, not guessed):

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
(`show_pct=False`, `kind="info"`) rather than a percentage nobody can verify — the
same two-tier shape Codex itself uses (official rate-limit percentages when
available, informational totals otherwise).
"""

from __future__ import annotations

import ctypes
import json
import logging
import math
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from tintaview.core.config import AgentConfig, expand
from tintaview.i18n import t

from ..model import UsageProvider, UsageResult, UsageRow

log = logging.getLogger(__name__)

DB_FILENAME = "session-store.db"
_WINDOW_DAYS = 7
_MAX_MODEL_ROWS = 5

USER_URL = "https://api.github.com/copilot_internal/user"
_CRED_TARGET_SUFFIX = ".github-copilot-app"
_CRED_TYPE_GENERIC = 1  # CRED_TYPE_GENERIC, from wincred.h

#: Quota ids GitHub's endpoint reports, in the order its own usage page shows them. The
#: labels are translated (`usage.copilot.quota.*`); a quota id this build doesn't know
#: falls back to the id itself, title-cased — see `_quota_label`.
_QUOTA_ORDER = ("chat", "completions", "premium_interactions")
#: Plan SKUs whose marketing name isn't derivable from the SKU string.
_PLAN_LABELS = {"free_limited_copilot": "usage.copilot.plan.free_limited_copilot"}


class _DbError(Exception):
    """No usable `assistant_usage_events` data could be read."""


class _TokenError(Exception):
    """No usable GitHub Copilot OAuth token could be read from Windows Credential
    Manager (or this isn't Windows)."""


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


# --------------------------------------------------------------------------- Windows credential


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


class _CREDENTIAL(ctypes.Structure):
    """Mirrors Win32's `CREDENTIAL` struct (wincred.h) field-for-field — this exact
    layout was proven against a live credential (see module docstring) via the
    equivalent P/Invoke declaration before being ported to ctypes here."""


_PCREDENTIAL = ctypes.POINTER(_CREDENTIAL)
_CREDENTIAL._fields_ = [
    ("Flags", ctypes.c_uint32),
    ("Type", ctypes.c_uint32),
    ("TargetName", ctypes.c_wchar_p),
    ("Comment", ctypes.c_wchar_p),
    ("LastWritten", _FILETIME),
    ("CredentialBlobSize", ctypes.c_uint32),
    ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
    ("Persist", ctypes.c_uint32),
    ("AttributeCount", ctypes.c_uint32),
    ("Attributes", ctypes.c_void_p),
    ("TargetAlias", ctypes.c_wchar_p),
    ("UserName", ctypes.c_wchar_p),
]


def _find_copilot_credential_target(advapi32: ctypes.WinDLL) -> str | None:
    """The credential's target name is `<install-uuid>.github-copilot-app` — the UUID
    is per-install, not guessable, so every stored generic credential is enumerated
    and the first target ending in the known suffix is returned. `CredEnumerateW`'s
    own filter parameter only supports a *prefix* wildcard, not a suffix one, hence
    the manual scan instead of passing a filter.
    """
    count = ctypes.c_uint32(0)
    creds_ptr = ctypes.POINTER(_PCREDENTIAL)()
    ok = advapi32.CredEnumerateW(None, 0, ctypes.byref(count), ctypes.byref(creds_ptr))
    if not ok:
        return None
    try:
        for i in range(count.value):
            name = creds_ptr[i].contents.TargetName or ""
            if name.endswith(_CRED_TARGET_SUFFIX):
                return name
        return None
    finally:
        advapi32.CredFree(creds_ptr)


def _read_windows_credential(advapi32: ctypes.WinDLL, target: str) -> str:
    """Reads one credential's secret by its exact target name — deliberately not
    reused from the enumeration above, which the caller only used to discover the
    name, so unrelated credentials' blobs are never touched."""
    cred_ptr = _PCREDENTIAL()
    ok = advapi32.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(cred_ptr))
    if not ok:
        raise _TokenError(f"CredReadW failed for {target}")
    try:
        cred = cred_ptr.contents
        blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        return blob.decode("utf-16-le", errors="ignore").rstrip("\x00")
    finally:
        advapi32.CredFree(cred_ptr)


def _read_copilot_token() -> str:
    """Read the Copilot CLI OAuth token fresh from Windows Credential Manager — see
    the module docstring's non-negotiable rules; the returned value is never logged,
    persisted, or cached by any caller."""
    if sys.platform != "win32":
        raise _TokenError("GitHub Copilot's OAuth token store is only handled on Windows")
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    target = _find_copilot_credential_target(advapi32)
    if not target:
        raise _TokenError("no GitHub Copilot credential found in Windows Credential Manager")
    token = _read_windows_credential(advapi32, target)
    if not token:
        raise _TokenError(f"empty credential blob for {target}")
    return token


# --------------------------------------------------------------------------- quota endpoint


def _fetch_user(token: str, timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(
        USER_URL,
        headers={
            "Authorization": f"token {token}",
            "User-Agent": "GithubCopilot/1.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https host
        return json.loads(resp.read().decode("utf-8"))


def _shape(data: Any) -> Any:
    """Top-level keys only, for the debug log line — never the token, and never the
    full payload (it carries the account's GitHub login)."""
    if isinstance(data, dict):
        return sorted(data.keys())
    return type(data).__name__


def _plan_label(payload: dict[str, Any]) -> str:
    sku = payload.get("access_type_sku")
    if isinstance(sku, str) and sku in _PLAN_LABELS:
        return t(_PLAN_LABELS[sku])
    plan = payload.get("copilot_plan")
    if isinstance(plan, str) and plan:
        # The plan name itself is GitHub's ("business", "individual") — only the word
        # around it is ours, and "Copilot" is the product name in every language.
        return t("usage.copilot.plan.named", plan=plan.replace("_", " ").title())
    return t("usage.copilot.plan.generic")


def _fmt_days_until(iso_ts: Any) -> str:
    if not isinstance(iso_ts, str) or not iso_ts:
        return ""
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return ""
    seconds = (dt - datetime.now(UTC)).total_seconds()
    if seconds <= 0:
        return t("usage.reset.today")
    return t("usage.reset.in_days", days=math.ceil(seconds / 86400))


def _quota_label(quota_id: str) -> str:
    key = f"usage.copilot.quota.{quota_id}"
    label = t(key)
    return quota_id.replace("_", " ").title() if label == key else label


def _quota_snapshot_row(quota_id: str, snapshot: Any, reset_text: str) -> UsageRow | None:
    if not isinstance(snapshot, dict) or not snapshot.get("has_quota"):
        return None  # `has_quota: false` means "not part of this plan", not "0 left"
    label = _quota_label(quota_id)
    if snapshot.get("unlimited"):
        return UsageRow(label=label, pct=0.0, right=t("usage.copilot.unlimited"), show_pct=False,
                         severity="normal", kind="info")
    remaining = snapshot.get("percent_remaining")
    if not isinstance(remaining, int | float) or isinstance(remaining, bool):
        return None
    pct = max(0.0, min(100.0, 100.0 - float(remaining)))
    severity = "critical" if pct >= 90 else "warning" if pct >= 75 else "normal"
    return UsageRow(label=label, pct=pct, right=reset_text, show_pct=True, severity=severity, kind="limit")


def _parse_user_payload(payload: dict[str, Any]) -> list[UsageRow]:
    """The quotas GitHub's own usage page shows: a percentage bar plus a shared
    "Resets in Nd" (the whole account resets on one date, not per quota)."""
    snapshots = payload.get("quota_snapshots")
    if not isinstance(snapshots, dict):
        return []
    reset_text = _fmt_days_until(payload.get("quota_reset_date_utc"))

    rows: list[UsageRow] = []
    seen: set[str] = set()
    for quota_id in _QUOTA_ORDER:
        seen.add(quota_id)
        row = _quota_snapshot_row(quota_id, snapshots.get(quota_id), reset_text)
        if row is not None:
            rows.append(row)
    for quota_id, snapshot in snapshots.items():
        if quota_id in seen:
            continue
        row = _quota_snapshot_row(quota_id, snapshot, reset_text)
        if row is not None:
            rows.append(row)
    return rows


# --------------------------------------------------------------------------- local fallback


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
        return t("usage.tokens.millions", value=f"{total / 1e6:.2f}")
    if total >= 1000:
        return t("usage.tokens.thousands", value=f"{total / 1e3:.1f}")
    return t("usage.tokens.plain", value=total)


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
            return self._fetch(agent_config, timeout)
        except Exception as e:  # noqa: BLE001 - contract: a provider must never raise
            log.exception("copilot usage provider failed unexpectedly")
            return UsageResult(agent=self.key,
                                error=t("usage.copilot.error.unavailable", detail=repr(e)))

    def _fetch(self, agent_config: AgentConfig, timeout: float) -> UsageResult:
        quota = self._fetch_quota(timeout)
        if quota is not None:
            return quota
        return self._fetch_token_totals(agent_config)

    def _fetch_quota(self, timeout: float) -> UsageResult | None:
        """The real "X% used, resets in Nd" figure, straight from GitHub's own quota
        endpoint. Returns None (never an error) for anything short of a full,
        parseable response, so the caller falls back to local token totals instead of
        surfacing a half-broken quota card."""
        try:
            token = _read_copilot_token()
        except _TokenError as e:
            log.info("copilot token unavailable: %s", e)  # the exception text, never the token
            return None
        try:
            payload = _fetch_user(token, timeout)
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
            log.info("copilot quota endpoint unavailable: %s", e)
            return None

        log.debug("copilot quota payload shape: %s", _shape(payload))
        rows = _parse_user_payload(payload)
        if not rows:
            return None
        return UsageResult(
            agent=self.key, rows=rows,
            header=t("usage.copilot.header.official", plan=_plan_label(payload)),
            source="official",
        )

    def _fetch_token_totals(self, agent_config: AgentConfig) -> UsageResult:
        db_path = _resolve_db_path(agent_config)
        since = datetime.now(UTC) - timedelta(days=_WINDOW_DAYS)
        try:
            totals = _read_totals_by_model(db_path, since)
        except _DbError:
            return UsageResult(agent=self.key, error=t("usage.copilot.error.no_data"))

        rows = _rows_from_totals(totals)
        if not rows:
            return UsageResult(agent=self.key, error=t("usage.copilot.error.no_activity"))
        return UsageResult(
            agent=self.key, rows=rows,
            header=t("usage.copilot.header.totals", days=_WINDOW_DAYS), source="activity",
        )
