"""Claude Code usage provider.

Where the numbers come from:

  - Primary source: the same internal endpoint Claude Code's own ``/usage`` slash
    command uses, ``GET https://api.anthropic.com/api/oauth/usage``, authenticated with
    the OAuth access token from ``<home>/.credentials.json``. Returns the OFFICIAL
    5-hour / weekly utilization percentages (+ reset times) and the monthly
    extra-usage / overage credit pool.
  - Alongside it, **always**: approximate 5h/7d token + cost totals reconstructed from
    the transcript JSONL under ``<home>/projects/**/*.jsonl`` — reading only files
    modified in the last week, from a per-file memo (``stats/_scan.py``), and counting
    each streamed message once (see ``_parse_transcript``). These go in
    ``UsageResult.estimate``, never in ``rows``, and the flyout draws them under the
    official rows (or under the error line).

    This used to be a *fallback*: it replaced the official rows when the endpoint
    failed, under a ``header`` reading "estimate (official % unavailable)". The flyout
    never drew that header (it draws the agent's display name instead), so a local
    guess appeared on screen indistinguishable from official data, and the only clue
    was a "~" in front of the cost. Making it a second, permanently visible block
    removes the ambiguity and gives the token/cost view — which the percentages don't
    provide at all — a home of its own.
  - 401/403, or a credentials file that is missing, unreadable or has no
    ``accessToken``, all mean the login itself is dead. Each is tagged
    ``error_kind="auth"`` so ``StatsService`` reports it rather than hiding it behind
    stale cached rows (``stats/service.py``). Only the 401 was classified this way
    before; the file-level failures raised ``OSError``/``ValueError`` into the generic
    handler and quietly became an estimate, so a signed-out Claude showed token totals
    where Cursor, in the same flyout, says "Sign in again to see it".
  - 429 means the endpoint is rate-limited, not that there's no data, so it is left
    ``transient`` and ``StatsService`` keeps the previous cached rows (see
    ``stats/service.py``). The estimate rides along either way.

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

# Approx public per-MTok pricing, used only for the local JSONL cost estimate.
# (input, output) USD per million tokens. Cache read ~0.1x input, write ~1.25x.
# Best-effort and not kept perfectly in sync with pricing changes — which is why every
# figure it produces is drawn under an "Est." label with a "~" on the cost.
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
#: as an "unknown model" and drag the estimate onto the default rate.
SYNTHETIC_MODEL = "<synthetic>"

#: What an unrecognised model costs. NOT (0.0, 0.0): a model released after this build
#: would then contribute exactly nothing to the estimate, which reads as a plausible
#: (just quietly low) number rather than as a gap — the failure mode nobody notices.
#: The flagship rate is the safer guess, since a model this table has never heard of is
#: far more likely to be a new top-tier one than a new cheap one. `_rates_for` logs the
#: model once so the gap is findable; the row itself only ever claims to be a "~" figure.
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


class _NotSignedIn(Exception):
    """No usable OAuth token in ``.credentials.json``.

    Its own exception type, rather than the ``OSError``/``ValueError`` these failures
    raise naturally, because those are indistinguishable from the network and parsing
    errors the generic handler treats as transient — which is exactly how a signed-out
    Claude used to fall through to a local estimate instead of saying so. Mirrors
    ``providers/cursor._TokenError``.
    """


def _read_access_token(home: Path) -> tuple[str, bool]:
    """Read the current OAuth access token. Claude Code refreshes this file while it
    runs, so re-reading each call is the simplest freshness strategy.

    Every way this can fail is a way of not being signed in, so they all raise
    `_NotSignedIn` — a missing file (never logged in, or a different `home`), an
    unparseable one (a half-written refresh), and a well-formed one with no
    `accessToken` (logged out).
    """
    path = home / ".credentials.json"
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        oauth = data.get("claudeAiOauth") or {}
        token = oauth.get("accessToken")
    except OSError as e:
        raise _NotSignedIn(f"cannot read {path}: {e}") from e
    except (ValueError, AttributeError) as e:
        raise _NotSignedIn(f"{path} is not a readable credentials file: {e}") from e
    if not token:
        raise _NotSignedIn(f"no accessToken in {path}")
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


def _reset_of(iso: str | None) -> tuple[float, str]:
    """``(reset_at, right)`` for a row that resets at `iso`.

    The *instant*, not the wording: `stats.format.reset_row_text` turns it into "Resets
    in 3 hr 23 min" / "Resets Fri 3:59 PM" at render time, which is what keeps the
    countdown honest between polls and stops a cached row freezing one (see
    `UsageRow.reset_at`). An unparseable value keeps the old behaviour of being quoted
    in `right` exactly as it arrived.
    """
    if not iso:
        return 0.0, ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 0.0, iso  # an unparseable value from the API, quoted as it arrived
    return dt.timestamp(), ""


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
        reset_at, right = _reset_of(resets)
        rows.append(
            UsageRow(label=label, pct=float(pct), right=right, reset_at=reset_at,
                      reset_style=fmt.RESET_RELATIVE,
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
        # "You haven't used Fable yet" wording) — fall back to a plain label. That
        # label is static text, so it belongs in `right`, not in a reset instant.
        reset_at, right = _reset_of(resets)
        if not reset_at and not right:
            right = t("usage.claude.not_used_yet")
        rows.append(
            # `model_name` is the API's own display name ("Opus", "Fable") — never
            # translated, same as every other value an agent hands back.
            UsageRow(label=t("usage.claude.weekly_model", model=model_name), pct=float(pct), right=right,
                      reset_at=reset_at, reset_style=fmt.RESET_RELATIVE,
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


#: Estimate window key -> catalogue key for its row label. The dict keys stay internal
#: identifiers rather than the label itself, so accumulating a window and naming it are
#: separate concerns and the label can be translated at render time. The catalogue keys
#: are shared with `providers/codex`, which shows the same two windows: one flyout
#: should not call the same seven days "This week" under one agent and "Last 7 days"
#: under the next.
_WINDOW_LABELS = {"5h": "usage.estimate.5h", "week": "usage.estimate.week"}

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


def _estimate_rows(acc: dict[str, dict[str, float]]) -> list[UsageRow]:
    """One `kind="info"` row per window — no bar, because there is no limit to be a
    percentage of (see `ui/flyout._draw_rows`).

    A window with nothing in it still gets a row. When these were the *fallback* rows
    an empty window was skipped, because a section consisting of one "0.00M tokens"
    line says nothing; now that they are a fixed pair sitting under the official rows,
    the zero is the answer — "you have used nothing in this window" — and dropping the
    row instead makes the block change height as usage crosses in and out of the
    5-hour window.
    """
    rows: list[UsageRow] = []
    for window, key in _WINDOW_LABELS.items():
        a = acc.get(window)
        if a is None:
            continue
        total = a["in"] + a["out"] + a["cache_r"] + a["cache_w"]
        right = t("usage.claude.estimate_right",
                  tokens=f"{total / 1e6:.2f}", cost=f"${a['cost']:.2f}")
        rows.append(UsageRow(label=t(key), pct=0.0, right=right, show_pct=False,
                              severity="normal", kind="info"))
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
        # Built before the request, and attached to every return below — success and
        # failure alike. That is the whole point of the field: the local numbers are
        # never the consolation prize for a failed fetch, so there is no path through
        # this method that has official rows but no estimate, or an error but no
        # estimate.
        estimate = self._estimate(home)

        def failed(error: str, kind: str = "transient") -> UsageResult:
            return UsageResult(agent=self.key, header=header, source="official",
                                error=error, error_kind=kind, estimate=estimate)

        try:
            data = _fetch_usage(home, timeout)
        except _NotSignedIn as e:
            # Not a blip: nothing refreshes until the user runs `claude` again, so
            # `error_kind="auth"` stops `StatsService` covering it with cached rows.
            log.info("claude usage: %s", e)
            return failed(t("usage.claude.error.not_signed_in"), "auth")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                # 403 alongside 401: a token the server refuses is a token the user has
                # to replace either way, and the remedy sentence is the same.
                return failed(t("usage.claude.error.login_expired"), "auth")
            if e.code == 429:
                # Rate-limited is not "no data" — left transient so `StatsService` keeps
                # whatever good rows are already cached.
                return failed(t("usage.claude.error.rate_limited"))
            return failed(t("usage.claude.error.endpoint_http", code=e.code))
        except (urllib.error.URLError, OSError, ValueError, KeyError, TimeoutError) as e:
            return failed(t("usage.claude.error.unavailable", detail=repr(e)))

        rows = _parse_usage(data)
        if not rows:
            # A 200 whose shape we no longer recognise. Reported rather than silently
            # swallowed: this is the signal that the undocumented endpoint has changed.
            return failed(t("usage.claude.error.payload"))
        return UsageResult(agent=self.key, rows=rows, header=header, source="official",
                            estimate=estimate)

    def _estimate(self, home: Path) -> list[UsageRow]:
        """The local transcript estimate, or `[]` if it cannot be built.

        Swallows its own failures on purpose. This runs on the success path now, so a
        sleeping WSL distro or an unreadable projects directory must not be able to
        turn a perfectly good official result into an error — the estimate is the
        supplementary block, and the worst it may do when it fails is not be there.
        """
        try:
            acc = _reconstruct_from_jsonl(home)
        except OSError as e:
            log.info("claude local estimate unavailable: %r", e)
            return []
        if _has_unpriced(acc):
            # Nowhere to say this on screen without inventing a caption the estimate
            # block deliberately doesn't have; `_rates_for` has already logged which
            # model it was, and the row's "~" never claimed to be exact.
            log.info("claude local estimate includes tokens from a model with no price")
        return _estimate_rows(acc)
