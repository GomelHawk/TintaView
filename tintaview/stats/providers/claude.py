"""Claude Code usage provider.

Where the numbers come from:

  - Primary source: the same internal endpoint Claude Code's own ``/usage`` slash
    command uses, ``GET https://api.anthropic.com/api/oauth/usage``, authenticated with
    the OAuth access token from ``<home>/.credentials.json``. Returns the OFFICIAL
    5-hour / weekly utilization percentages (+ reset times) and the monthly
    extra-usage / overage credit pool.
  - Fallback: if the endpoint fails (network, non-401/429 HTTP error, unreadable
    credentials, unrecognised shape), reconstruct approximate 5h/7d token + cost
    totals from the transcript JSONL under ``<home>/projects/**/*.jsonl`` — reading
    only files modified in the last week, from a per-file memo (``stats/_scan.py``),
    and counting each streamed message once (see ``_parse_transcript``).
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

import json
import logging
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

from tintaview.core.config import AgentConfig, expand
from tintaview.i18n import t

from .. import format as fmt
from .._scan import WEEK_S, FileMemo, recent_files
from ..model import UsageProvider, UsageResult, UsageRow

log = logging.getLogger(__name__)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"

# Approx public per-MTok pricing, used only for the JSONL fallback cost estimate.
# (input, output) USD per million tokens. Cache read ~0.1x input, write ~1.25x.
# Best-effort and not kept perfectly in sync with pricing changes — the fallback path
# is explicitly labelled "estimate" in the UI for this reason.
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-fable-5-1": (10.0, 50.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5-1": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

#: Cache-read USD per MTok where it is *not* the usual 0.1x of the input rate. Fable 5.1
#: reads its cache at a flat $0.25/MTok; at 0.1x it would be billed here at $1.00, a 4x
#: overstatement on the token class that dominates a long agentic session.
CACHE_READ_PER_MTOK: dict[str, float] = {
    "claude-fable-5-1": 0.25,
}

#: Claude Code writes placeholder assistant messages under this model id (a cancelled
#: turn, an interrupted stream). They carry no billable usage and would otherwise count
#: as an "unknown model" and flip the header to the unpriced wording.
SYNTHETIC_MODEL = "<synthetic>"

#: What an unrecognised model costs. NOT (0.0, 0.0): a model released after this build
#: would then contribute exactly nothing to the estimate, which reads as a plausible
#: (just quietly low) number rather than as a gap — the failure mode nobody notices.
#: The flagship rate is the safer guess, since a model this table has never heard of is
#: far more likely to be a new top-tier one than a new cheap one. The estimate says so:
#: any unpriced token switches the section header to `header_estimate_unpriced`.
DEFAULT_PRICING: tuple[float, float] = (5.0, 25.0)

#: Model ids already reported as unpriced, so a 30k-line transcript sweep logs each new
#: model once instead of once per usage line.
_warned_models: set[str] = set()


def _rates_for(model: str | None) -> tuple[tuple[float, float], bool]:
    """``((input_rate, output_rate), priced)`` for a transcript's model id."""
    if model in PRICING:
        return PRICING[model], True
    if model and model not in _warned_models:
        _warned_models.add(model)
        log.info(
            "no price for model %r; estimating it at the default $%.2f/$%.2f per MTok. "
            "Add it to stats.providers.claude.PRICING to make the estimate exact.",
            model, *DEFAULT_PRICING,
        )
    return DEFAULT_PRICING, False


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
    absolute weekday+time otherwise ('Resets Fri 3:59 PM') — worded by `stats.format`,
    which builds both forms without `strftime` (`%-I` is Linux-only and `%a`/`%p` follow
    the C locale, not the language the user picked)."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso  # an unparseable value from the API, quoted as it arrived
    return fmt.reset_text(dt)


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

    window("session", "five_hour", t("usage.claude.session"))
    window("weekly_all", "seven_day", t("usage.claude.weekly_all"))

    # Per-model weekly buckets, if the plan exposes them. These do NOT have a
    # stable top-level key (the API hands out an obfuscated/rotating key per model,
    # e.g. one observed as "nimbus_quill" for Fable) — the only stable signal is a
    # `limits[]` entry with kind "weekly_scoped" carrying `scope.model.display_name`.
    # There can be more than one (e.g. Opus and Fable both scoped), so scan the raw
    # list rather than the kind-deduped `limits` dict above.
    for lim in data.get("limits") or []:
        if lim.get("kind") != "weekly_scoped":
            continue
        model_name = ((lim.get("scope") or {}).get("model") or {}).get("display_name")
        pct = lim.get("percent")
        if not model_name or pct is None:
            continue
        resets = lim.get("resets_at")
        # A model with no usage yet has no reset time (mirrors the in-app
        # "You haven't used Fable yet" wording) — fall back to a plain label.
        right = _fmt_reset(resets) if resets else t("usage.claude.not_used_yet")
        rows.append(
            # `model_name` is the API's own display name ("Opus", "Fable") — never
            # translated, same as every other value an agent hands back.
            UsageRow(label=t("usage.claude.weekly_model", model=model_name), pct=float(pct), right=right,
                      show_pct=True, severity=lim.get("severity") or "normal", kind="limit")
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
            UsageRow(label=t("usage.claude.credits"), pct=float(pct),
                      right=t("usage.claude.credits_right",
                              used=f"{sym}{used[0]:.2f}", limit=f"{sym}{limit[0]:.2f}"),
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


#: Fallback window key -> catalogue key for its row label. The dict keys stay internal
#: identifiers rather than the label itself, so accumulating a window and naming it are
#: separate concerns and the label can be translated at render time.
_WINDOW_LABELS = {"5h": "usage.claude.window_5h", "week": "usage.claude.window_week"}

_WINDOWS_S = {"5h": 5 * 3600, "week": WEEK_S}


class _UsageRec(NamedTuple):
    """One billable assistant message, as parsed out of a transcript."""

    ts: float  # epoch seconds
    model: str | None  # normalised (date snapshot stripped)
    input: int
    output: int
    cache_read: int
    cache_write: int


#: Parsed transcripts, re-read only when a file's (mtime, size) changes. Module-level on
#: purpose: the provider object is rebuilt per poll, the memo must outlive it.
_MEMO: FileMemo[list[_UsageRec]] = FileMemo()


def _parse_transcript(path: Path) -> list[_UsageRec]:
    """Every billable message in one transcript, each counted once.

    Claude Code writes a streamed assistant message as one JSONL line *per content
    block* (text, tool_use, ...), and every one of those lines carries the message's
    `usage`. Input and cache counts repeat identically across them; `output_tokens`
    grows, the last line holding the final figure. Summing lines naively therefore
    double-counts roughly half of all usage (measured: 3592 usage lines for 1737
    distinct messages in one week). Lines are collapsed on `(message.id, requestId)`,
    keeping the largest counts seen; a line with neither id (an older transcript shape)
    is kept as its own record.
    """
    recs: list[_UsageRec] = []
    index: dict[tuple[str | None, str | None], int] = {}
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
            if not ts or not isinstance(usage, dict):
                continue
            try:
                when = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except (ValueError, TypeError):
                continue
            raw_model = msg.get("model")
            if raw_model == SYNTHETIC_MODEL:
                continue
            new = _UsageRec(
                ts=when,
                model=_norm_model(raw_model),
                input=int(usage.get("input_tokens") or 0),
                output=int(usage.get("output_tokens") or 0),
                cache_read=int(usage.get("cache_read_input_tokens") or 0),
                cache_write=int(usage.get("cache_creation_input_tokens") or 0),
            )
            key = (msg.get("id"), rec.get("requestId"))
            if key == (None, None):
                recs.append(new)
                continue
            at = index.get(key)
            if at is None:
                index[key] = len(recs)
                recs.append(new)
                continue
            old = recs[at]
            recs[at] = old._replace(
                input=max(old.input, new.input),
                output=max(old.output, new.output),
                cache_read=max(old.cache_read, new.cache_read),
                cache_write=max(old.cache_write, new.cache_write),
            )
    return recs


def _cost_of(rec: _UsageRec) -> tuple[float, bool]:
    """``(usd, priced)`` for one message at the current `PRICING`."""
    (in_r, out_r), priced = _rates_for(rec.model)
    cache_r = CACHE_READ_PER_MTOK.get(rec.model or "", in_r * 0.1)
    usd = (
        rec.input * in_r
        + rec.output * out_r
        + rec.cache_read * cache_r
        + rec.cache_write * in_r * 1.25
    ) / 1_000_000
    return usd, priced


def _reconstruct_from_jsonl(home: Path, now: float | None = None) -> dict[str, dict[str, float]]:
    """Approximate 5h and 7d token totals + cost from transcript usage lines.

    Only files modified inside the 7-day window are opened (a file untouched for a week
    cannot hold a line from it), and unchanged files are served from `_MEMO` — the two
    halves of AGENTS.md's rule for transcript scans over a WSL-split UNC path. Cost is
    priced at aggregation time, so a `PRICING` change never needs the memo invalidated.
    """
    now = time.time() if now is None else now
    cutoffs = {k: now - secs for k, secs in _WINDOWS_S.items()}
    acc = {
        k: {"in": 0, "out": 0, "cache_r": 0, "cache_w": 0, "cost": 0.0, "unpriced": 0}
        for k in cutoffs
    }

    files = recent_files(home / "projects", "*.jsonl", WEEK_S, now=now)
    _MEMO.prune(files)
    for path in files:
        try:
            recs = _MEMO.get(path, _parse_transcript)
        except OSError:
            continue
        for rec in recs:
            usd, priced = _cost_of(rec)
            for k, cut in cutoffs.items():
                if rec.ts >= cut:
                    a = acc[k]
                    a["in"] += rec.input
                    a["out"] += rec.output
                    a["cache_r"] += rec.cache_read
                    a["cache_w"] += rec.cache_write
                    a["cost"] += usd
                    if not priced:
                        a["unpriced"] += rec.input + rec.output + rec.cache_read + rec.cache_write
    return acc


def _has_unpriced(acc: dict[str, dict[str, float]]) -> bool:
    """Did any window include tokens from a model `PRICING` doesn't know?

    Drives the header wording — the cost is still shown (a guessed rate beats a silent
    zero), but the panel says out loud that part of it was guessed.
    """
    return any(a.get("unpriced", 0) for a in acc.values())


def _fallback_rows(acc: dict[str, dict[str, float]]) -> list[UsageRow]:
    rows: list[UsageRow] = []
    for window, a in acc.items():
        total = a["in"] + a["out"] + a["cache_r"] + a["cache_w"]
        if total == 0 and a["cost"] == 0:
            continue
        right = t("usage.claude.estimate_right",
                  tokens=f"{total / 1e6:.2f}", cost=f"${a['cost']:.2f}")
        label = t(_WINDOW_LABELS.get(window, window))
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
            return UsageResult(agent=self.key,
                                error=t("usage.claude.error.unavailable", detail=repr(e)))

    def _fetch(self, agent_config: AgentConfig, timeout: float) -> UsageResult:
        home = _resolve_home(agent_config)
        # `tier` is the plan name straight out of the credentials file ("Max", "Team") —
        # a product name, so the surrounding words are translated and it is not.
        tier = _read_tier(home)
        header = t("usage.claude.header_tier", tier=tier) if tier else t("usage.claude.header")

        try:
            data = _fetch_usage(home, timeout)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                return UsageResult(
                    agent=self.key,
                    header=header,
                    source="official",
                    error=t("usage.claude.error.login_expired"),
                )
            if e.code == 429:
                # Rate-limited is not "no data" — don't let a noisier local estimate
                # clobber whatever good numbers are already cached (StatsService
                # enforces this by only overwriting the cache on `.ok` results).
                return UsageResult(
                    agent=self.key,
                    header=header,
                    source="official",
                    error=t("usage.claude.error.rate_limited"),
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
        header = t("usage.claude.header_estimate")
        try:
            acc = _reconstruct_from_jsonl(home)
        except OSError as e:
            return UsageResult(agent=self.key, header=header, source="estimate",
                                error=t("usage.claude.error.unavailable", detail=repr(e)))
        if _has_unpriced(acc):
            header = t("usage.claude.header_estimate_unpriced")
        rows = _fallback_rows(acc)
        if not rows:
            return UsageResult(agent=self.key, header=header, source="estimate",
                                error=t("usage.claude.error.no_transcripts"))
        return UsageResult(agent=self.key, rows=rows, header=header, source="estimate")
