"""Claude Code usage provider.

Where the numbers come from:

  - Primary source: the same internal endpoint Claude Code's own ``/usage`` slash
    command uses, ``GET https://api.anthropic.com/api/oauth/usage``, authenticated with
    the OAuth access token from ``<home>/.credentials.json``. Returns the OFFICIAL
    5-hour / weekly utilization percentages (+ reset times) and the monthly
    extra-usage / overage credit pool.
  - Fallback: if the endpoint fails (network, non-401/429 HTTP error, unreadable
    credentials, unrecognised shape), reconstruct approximate 5h/7d token + cost
    totals from the transcript JSONL under ``<home>/projects/**/*.jsonl``.
  - 401 means the login itself is dead — no fallback estimate is useful there, so we
    surface the exact message Claude Code's own CLI shows.
  - 429 means the endpoint is rate-limited, not that there's no data — falling back to
    the noisy local estimate would *replace* good cached numbers with a worse guess, so
    this also skips the fallback and just reports the rate limit. ``StatsService``
    is what actually keeps the old cached rows in that case (see ``stats/service.py``).

Stdlib only (``urllib``), so this runs on a bare WSL distro or any other
bundle with nothing extra installed.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tintaview.core.config import AgentConfig, expand

from ..model import UsageProvider, UsageResult, UsageRow

log = logging.getLogger(__name__)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"

# Approx public per-MTok pricing, used only for the JSONL fallback cost estimate.
# (input, output) USD per million tokens. Cache read ~0.1x input, write ~1.25x.
# Best-effort and not kept perfectly in sync with pricing changes — the fallback path
# is explicitly labelled "estimate" in the UI for this reason.
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


# --------------------------------------------------------------------------- paths


def _default_home() -> Path:
    return Path.home() / ".claude"


def _resolve_home(agent_config: AgentConfig) -> Path:
    """AgentConfig.home may be a Windows UNC path in the WSL split
    (``\\\\wsl.localhost\\Ubuntu\\home\\u\\.claude``); empty means "use the adapter
    default" (plain ``~/.claude``)."""
    return expand(agent_config.home) if agent_config.home else _default_home()


# --------------------------------------------------------------------------- official endpoint


def _read_access_token(home: Path) -> tuple[str, bool]:
    """Read the current OAuth access token. Claude Code refreshes this file while it
    runs, so re-reading each call is the simplest freshness strategy."""
    path = home / ".credentials.json"
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    oauth = data.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if not token:
        raise ValueError("no accessToken in .credentials.json")
    expires_at = oauth.get("expiresAt")  # ms epoch
    expired = bool(expires_at) and expires_at / 1000 < datetime.now(UTC).timestamp()
    return token, expired


def _read_tier(home: Path) -> str | None:
    """Plan name for the header (e.g. 'Team'), from subscriptionType. Best-effort:
    any problem here just means a plainer header, never a failed fetch."""
    try:
        with open(home / ".credentials.json", encoding="utf-8") as f:
            subscription = (json.load(f).get("claudeAiOauth") or {}).get("subscriptionType")
            return subscription.title() if subscription else None
    except (OSError, ValueError, AttributeError):
        return None


def _fetch_usage(home: Path, timeout: float) -> dict[str, Any]:
    """Return the parsed /api/oauth/usage JSON, or raise on failure."""
    token, expired = _read_access_token(home)
    if expired:
        # Not fatal — the server often still honors a just-expired token, and a 401
        # is handled by the caller either way.
        log.info("claude access token appears expired; may still be honored")
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "anthropic-beta": OAUTH_BETA,
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _money(m: dict[str, Any] | None) -> tuple[float, str] | None:
    """Scale a {amount_minor, currency, exponent} money object to a real amount.
    Amounts are in MINOR units — e.g. amount_minor 58, exponent 2 -> 0.58."""
    if not m:
        return None
    amt = m.get("amount_minor")
    if amt is None:
        return None
    exp = m.get("exponent", 0) or 0
    return amt / (10**exp), m.get("currency", "USD")


def _fmt_reset(iso: str | None) -> str:
    """Mirror the in-app wording: relative within a day ('Resets in 3 hr 23 min'),
    absolute weekday+time otherwise ('Resets Fri 3:59 PM')."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    delta = dt - datetime.now(UTC)
    secs = int(delta.total_seconds())
    if secs <= 0:
        return "Resets now"
    if secs < 86400:
        h, rem = divmod(secs, 3600)
        m, _ = divmod(rem, 60)
        if h:
            return f"Resets in {h} hr {m} min"
        return f"Resets in {m} min"
    # Build the absolute form manually — strftime's %-I is Linux-only (Windows
    # rejects it) and %a/%p are locale-dependent. Keep it portable + English.
    local = dt.astimezone()
    weekday = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][local.weekday()]
    hour12 = local.hour % 12 or 12
    ampm = "AM" if local.hour < 12 else "PM"
    return f"Resets {weekday} {hour12}:{local.minute:02d} {ampm}"


def _parse_usage(data: dict[str, Any]) -> list[UsageRow]:
    """Normalise the /api/oauth/usage payload into UsageRows."""
    rows: list[UsageRow] = []
    limits = {lm.get("kind"): lm for lm in (data.get("limits") or [])}

    def window(kind: str, obj_key: str, label: str) -> None:
        lim = limits.get(kind)
        obj = data.get(obj_key) or {}
        pct = lim.get("percent") if lim else obj.get("utilization")
        resets = (lim.get("resets_at") if lim else None) or obj.get("resets_at")
        sev = (lim.get("severity") if lim else "normal") or "normal"
        if pct is None:
            return
        rows.append(
            UsageRow(label=label, pct=float(pct), right=_fmt_reset(resets),
                      show_pct=True, severity=sev, kind="limit")
        )

    window("session", "five_hour", "5-hour limit")
    window("weekly_all", "seven_day", "Weekly · all models")

    # Per-model weekly buckets, if the plan exposes them — a key like
    # "seven_day_fable" only appears once that model is enabled on the account, so
    # this naturally shows/hides the row per user rather than needing a separate
    # entitlement check.
    for key, label in (
        ("seven_day_opus", "Weekly · Opus"),
        ("seven_day_sonnet", "Weekly · Sonnet"),
        ("seven_day_fable", "Weekly · Fable"),
    ):
        obj = data.get(key)
        if obj and obj.get("utilization") is not None:
            resets = obj.get("resets_at")
            # A model with no usage yet has no reset time (mirrors the in-app
            # "You haven't used Fable yet" wording) — fall back to a plain label.
            right = _fmt_reset(resets) if resets else "Not used yet"
            rows.append(
                UsageRow(label=label, pct=float(obj["utilization"]), right=right,
                          show_pct=True, severity="normal", kind="limit")
            )

    # Usage credits / overage pool (subscription overage, NOT the dev API).
    # Amounts are in MINOR units — scale by 10**exponent to get the real figure
    # (amount_minor 58, exponent 2 -> $0.58), matching the in-app display.
    spend = data.get("spend") or {}
    ex = data.get("extra_usage") or {}
    used = limit = None
    pct = None
    if spend.get("enabled"):
        used = _money(spend.get("used"))
        limit = _money(spend.get("limit"))
        pct = spend.get("percent")
    elif ex.get("is_enabled"):
        dp = ex.get("decimal_places", 2)
        cur = ex.get("currency", "USD")
        if ex.get("used_credits") is not None:
            used = (ex["used_credits"] / (10**dp), cur)
        if ex.get("monthly_limit") is not None:
            limit = (ex["monthly_limit"] / (10**dp), cur)
        pct = ex.get("utilization")

    if used and limit and pct is not None:
        sym = "$" if used[1] == "USD" else ""
        rows.append(
            UsageRow(label="Usage credits", pct=float(pct),
                      right=f"{sym}{used[0]:.2f} of {sym}{limit[0]:.2f}",
                      show_pct=False, severity="normal", kind="credits")
        )
    return rows


# --------------------------------------------------------------------------- fallback


def _norm_model(m: str | None) -> str | None:
    if not m:
        return m
    # strip a trailing date snapshot, e.g. claude-haiku-4-5-20251001
    parts = m.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 8:
        return parts[0]
    return m


def _reconstruct_from_jsonl(home: Path) -> dict[str, dict[str, float]]:
    """Approximate 5h and 7d token totals + cost from transcript usage lines."""
    now = datetime.now(UTC)
    cutoffs = {"5-hour": now - timedelta(hours=5), "This week": now - timedelta(days=7)}
    acc = {k: {"in": 0, "out": 0, "cache_r": 0, "cache_w": 0, "cost": 0.0} for k in cutoffs}

    pattern = os.path.join(home, "projects", "**", "*.jsonl")
    for path in glob.glob(pattern, recursive=True):
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if '"usage"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = rec.get("timestamp")
                    msg = rec.get("message") or {}
                    usage = msg.get("usage")
                    if not ts or not usage:
                        continue
                    try:
                        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    model = _norm_model(msg.get("model"))
                    in_r, out_r = PRICING.get(model, (0.0, 0.0))
                    itok = usage.get("input_tokens", 0) or 0
                    otok = usage.get("output_tokens", 0) or 0
                    crd = usage.get("cache_read_input_tokens", 0) or 0
                    cwr = usage.get("cache_creation_input_tokens", 0) or 0
                    cost = (itok * in_r + otok * out_r + crd * in_r * 0.1 + cwr * in_r * 1.25) / 1_000_000
                    for k, cut in cutoffs.items():
                        if t >= cut:
                            a = acc[k]
                            a["in"] += itok
                            a["out"] += otok
                            a["cache_r"] += crd
                            a["cache_w"] += cwr
                            a["cost"] += cost
        except OSError:
            continue
    return acc


def _fallback_rows(acc: dict[str, dict[str, float]]) -> list[UsageRow]:
    rows: list[UsageRow] = []
    for label, a in acc.items():
        total = a["in"] + a["out"] + a["cache_r"] + a["cache_w"]
        if total == 0 and a["cost"] == 0:
            continue
        right = f"{total / 1e6:.2f}M tokens · ~${a['cost']:.2f}"
        rows.append(UsageRow(label=label, pct=0.0, right=right, show_pct=False, severity="normal", kind="info"))
    return rows


# --------------------------------------------------------------------------- provider


class ClaudeUsageProvider(UsageProvider):
    key = "claude"

    def fetch(self, agent_config: AgentConfig, timeout: float = 15.0) -> UsageResult:
        try:
            return self._fetch(agent_config, timeout)
        except Exception as e:  # noqa: BLE001 - contract: a provider must never raise
            log.exception("claude usage provider failed unexpectedly")
            return UsageResult(agent=self.key, error=f"Claude usage unavailable: {e!r}")

    def _fetch(self, agent_config: AgentConfig, timeout: float) -> UsageResult:
        home = _resolve_home(agent_config)
        tier = _read_tier(home)
        header = "Your usage limits" + (f" · {tier}" if tier else "")

        try:
            data = _fetch_usage(home, timeout)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                return UsageResult(
                    agent=self.key,
                    header=header,
                    source="official",
                    error=(
                        "Your Claude Code login has expired. Run `claude` (or restart "
                        "Claude Code) to sign in again, then try this again."
                    ),
                )
            if e.code == 429:
                # Rate-limited is not "no data" — don't let a noisier local estimate
                # clobber whatever good numbers are already cached (StatsService
                # enforces this by only overwriting the cache on `.ok` results).
                return UsageResult(
                    agent=self.key,
                    header=header,
                    source="official",
                    error="Claude usage endpoint is rate-limited (HTTP 429) — try again shortly.",
                )
            return self._fallback(home, note=f"endpoint HTTP {e.code} ({e.reason})")
        except (urllib.error.URLError, OSError, ValueError, KeyError, TimeoutError) as e:
            return self._fallback(home, note=f"endpoint failed: {e!r}")

        rows = _parse_usage(data)
        if not rows:
            return self._fallback(home, note="endpoint returned no recognizable limits")
        return UsageResult(agent=self.key, rows=rows, header=header, source="official")

    def _fallback(self, home: Path, note: str) -> UsageResult:
        log.info("claude usage: %s — falling back to local transcript estimate", note)
        header = "Claude usage — estimate (official % unavailable)"
        try:
            acc = _reconstruct_from_jsonl(home)
        except OSError as e:
            return UsageResult(agent=self.key, header=header, source="estimate",
                                error=f"Claude usage unavailable: {e!r}")
        rows = _fallback_rows(acc)
        if not rows:
            return UsageResult(agent=self.key, header=header, source="estimate",
                                error="No local Claude transcripts found to estimate usage.")
        return UsageResult(agent=self.key, rows=rows, header=header, source="estimate")
