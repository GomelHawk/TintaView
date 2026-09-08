"""Fetches every enabled agent's usage in parallel and applies the cache policy.

No Qt, no UI imports here by design — the tray layer wraps this class on a timer;
this module only knows how to run one fetch pass and remember the result.

Each agent's provider runs in its own thread (``ThreadPoolExecutor``) so one slow or
hung provider (a stalled network call, a slow UNC path) can't delay the others — the
tray flyout renders whatever has come back so far rather than waiting on the worst
case.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace

from tintaview.core.config import Config

from . import format as fmt
from .cache import UsageCache
from .model import UsageProvider, UsageResult
from .providers.claude import ClaudeUsageProvider
from .providers.codex import CodexUsageProvider
from .providers.copilot import CopilotUsageProvider
from .providers.cursor import CursorUsageProvider
from .providers.jetbrains import JetBrainsUsageProvider

log = logging.getLogger(__name__)

#: How stale cached rows may be, in poll intervals, before an **auth** failure means
#: they can no longer be trusted (see `UsageResult.error_kind`). Two rather than a fixed
#: number of minutes so the window scales with `stats.poll_seconds`: rows from the last
#: cycle or two are rows a poll fetched successfully and a token expiry has not yet had
#: time to make wrong, while anything older is data that polls have repeatedly failed to
#: refresh — and it is showing *that* which put Friday's numbers on screen on Monday.
AUTH_CACHE_GRACE_POLLS = 2

#: Hard ceiling on how old cached rows may be, whatever their windows say. 48 h covers a
#: weekend — the realistic gap between a token dying on Friday evening and anyone
#: noticing — and stops a figure that only ever moves upward (a monthly spend total)
#: being presented as current indefinitely. Before this, only the **auth** path had any
#: age limit at all: a transient failure served cached rows forever, so a provider
#: erroring for a week showed week-old numbers with nothing on screen saying so.
MAX_CACHE_GRACE_S = 48 * 3600

#: Built-in providers, keyed the same way as `Config.enabled_agents` / `Config.agents`.
#: "jetbrains" and "copilot" have no entry in `tintaview.agents` — neither has a hook
#: API TintaView can drive lighting from (see each provider's module docstring), so
#: both are stats-only and never appear in the hook-install wizard.
DEFAULT_PROVIDERS: dict[str, type[UsageProvider]] = {
    "claude": ClaudeUsageProvider,
    "codex": CodexUsageProvider,
    "cursor": CursorUsageProvider,
    "jetbrains": JetBrainsUsageProvider,
    "copilot": CopilotUsageProvider,
}


def _has_closed_window(cached: UsageResult, now: float) -> bool:
    """Has any dated row's window already ended?

    A row carrying a `reset_at` states when its own numbers stop being true, so the rows
    calibrate how long the cache may stand in and no per-provider constant has to be
    kept in step with each API's windows. Claude's 5-hour row expires in hours; Cursor's
    are a monthly billing cycle (`RESET_DATE` off `billingCycleEnd`) and stay true for
    weeks — which is why one 2-poll grace was wrong for both.

    **Any** closed row disqualifies the whole set, not just itself. A Claude result holds
    a 5-hour row and a weekly row together; six hours on, the weekly row is still fine
    while the 5-hour row is precisely the "Friday's window counting down on Monday"
    figure this policy exists to keep off the screen — and they are drawn as one section.
    """
    return any(0 < row.reset_at <= now for row in cached.rows)


def _has_open_window(cached: UsageResult, now: float) -> bool:
    """Does any row date itself and say its window is still running?

    This is what a row needs to earn the long grace, and it is deliberately not the
    negation of `_has_closed_window`: rows with no `reset_at` at all (0.0) say nothing
    about their own shelf life, so they can never vouch for themselves against a dead
    login. Reading "no closed window" as "still valid" would have handed exactly the
    rows from the original incident — pre-`reset_at` rows carrying a frozen
    "Resets in 2 hr 59 min" string — a 48-hour licence to keep being drawn.
    """
    return any(row.reset_at > now for row in cached.rows)


class StatsService:
    """Fetches usage for every enabled agent and exposes results by agent key."""

    def __init__(
        self,
        cfg: Config,
        cache: UsageCache | None = None,
        providers: Mapping[str, UsageProvider] | None = None,
    ) -> None:
        self._cfg = cfg
        self._cache = cache or UsageCache()
        self._providers: dict[str, UsageProvider] = (
            dict(providers) if providers is not None else {key: cls() for key, cls in DEFAULT_PROVIDERS.items()}
        )
        self._lock = threading.Lock()
        self._latest: dict[str, UsageResult] = {}

    def fetch_all(self, timeout: float = 15.0) -> dict[str, UsageResult]:
        """Fetch every enabled+known agent, one thread each, and return the results.

        Also updates the cache and the ``latest()`` snapshot as a side effect.
        """
        keys = [k for k in self._cfg.enabled_agents if k in self._providers]
        if not keys:
            return {}

        completed: dict[str, UsageResult] = {}
        with ThreadPoolExecutor(max_workers=len(keys), thread_name_prefix="tv-stats") as pool:
            future_to_key = {pool.submit(self._fetch_one, key, timeout): key for key in keys}
            for future in as_completed(future_to_key):
                key = future_to_key[future]
                try:
                    completed[key] = future.result()
                except Exception as e:  # noqa: BLE001 - a provider bug must not sink the whole poll
                    log.exception("stats provider %s raised unexpectedly", key)
                    completed[key] = self._apply_cache_policy(
                        UsageResult(agent=key, error=f"internal error: {e!r}")
                    )

        # Rebuilt in `keys` order rather than returned as `completed` directly: threads
        # finish in whatever order the network/disk I/O happens to resolve, and the tray
        # (flyout sections, tooltip lines) renders dict order as display order. Without
        # this, the agent order configured in the wizard would only hold by luck.
        results = {key: completed[key] for key in keys if key in completed}

        # One cache write for the whole poll, not one per provider. `source == "cache"`
        # means `_apply_cache_policy` already substituted the stored copy, so persisting
        # it again would only rewrite the file with what is already in it.
        fresh = [r for r in results.values() if r.ok and r.source != "cache"]
        if fresh:
            self._cache.update_many(fresh)

        with self._lock:
            self._latest.update(results)
        return results

    def latest(self, agent: str) -> UsageResult | None:
        """The most recent result for `agent` from the last `fetch_all()` call, if any."""
        with self._lock:
            return self._latest.get(agent)

    def _fetch_one(self, key: str, timeout: float) -> UsageResult:
        provider = self._providers[key]
        # `agent_config()`, not `agent()`: this is the 5-minute poll, and `agent()`
        # *creates* the agent's table as a side effect of being asked about it.
        agent_cfg = self._cfg.agent_config(key)
        try:
            result = provider.fetch(agent_cfg, timeout=timeout,
                                    with_estimate=self._cfg.stats.show_estimate)
        except Exception as e:  # noqa: BLE001 - belt-and-braces; providers must not raise, but don't trust it
            log.exception("stats provider %s raised despite its contract", key)
            result = UsageResult(agent=key, error=f"internal error: {e!r}")
        if result.ok and not result.fetched_at:
            # Stamped here rather than in each provider: five providers would be five
            # chances to forget, and an unstamped result reads as "age unknown", which
            # `_cache_is_still_fresh` refuses to trust.
            result = replace(result, fetched_at=time.time())
        return self._apply_cache_policy(result)

    def _apply_cache_policy(self, result: UsageResult) -> UsageResult:
        """Good data is kept; bad data (empty rows — a rate limit, an outage, a login
        failure) falls back to the last cached good result instead of blanking the flyout
        or replacing good numbers with a worse guess.

        With one exception, and it is the whole reason `error_kind` exists: an **auth**
        failure is not a blip the next poll fixes. Nothing will refresh those numbers
        until the user signs in again, so masking it with the cache leaves the flyout
        quietly showing figures from the last time the login worked — on the maintainer's
        machine, Friday's 5-hour window still counting down on Monday morning, complete
        with a "Resets in 2 hr 59 min" that had been frozen in the cache file for three
        days. Past the grace window the rows give way to `error`, which says the login
        expired and how to renew it; inside it they still stand, because a token that
        died seconds after a good poll hasn't made those rows wrong.

        A substituted result keeps the **live** `estimate` rather than the cached one.
        The estimate is rebuilt from local files on every poll and is not persisted at
        all, so the copy on `cached` is whatever happened to be in memory when it was
        stored — possibly nothing, if it came off disk. Carrying `result`'s across is
        what lets a section show a stale-but-labelled official block above a set of
        local numbers that are correct as of this second, which is the same reason
        `UsageRow.reset_at` is worded at render time instead of fetch time.

        Deliberately does not write: `fetch_all` persists the whole pass in one go once
        every provider has answered.
        """
        if result.ok:
            return result
        cached = self._cache.get(result.agent)
        if cached is None or not cached.ok:
            return result
        if not self._cached_rows_may_stand_in(cached, result.error_kind):
            log.info(
                "stats provider %s failed (%s) and its cached rows can no longer stand "
                "in — reporting the failure instead of usage nobody can refresh",
                result.agent, result.error_kind,
            )
            return result
        return replace(cached, source="cache", estimate=result.estimate,
                        notice=self._staleness_notice(cached))

    def _cached_rows_may_stand_in(self, cached: UsageResult, error_kind: str) -> bool:
        """May `cached` be shown in place of a failed poll?

        Three allowances, narrowest first:

        - An **auth** failure inside `AUTH_CACHE_GRACE_POLLS` — a token that died
          seconds after a good poll has not made those rows wrong yet.
        - Any failure, while no row's window has closed and the whole result is under
          `MAX_CACHE_GRACE_S` old. A transient failure needs nothing more than that.
        - An **auth** failure additionally needs the rows to vouch for themselves — at
          least one carries a `reset_at` that is still in the future. This is what lets
          Cursor's monthly figures survive a dead session for a day or two while
          Claude's 5-hour row gives way within hours, with no per-provider constant to
          keep in step; and it is what stops rows that carry no reset instant at all
          from inheriting that licence.
        - An unknown age keeps the split it has always had — never trusted against a
          dead login, still better than a blank flyout for a transient blip.
        """
        if cached.fetched_at <= 0:
            return error_kind != "auth"
        now = time.time()
        if (now - cached.fetched_at) > MAX_CACHE_GRACE_S:
            return False
        if _has_closed_window(cached, now):
            # Wrong at any age and for either kind of failure: the window these numbers
            # describe has ended, so they are last window's numbers whatever stopped the
            # refresh.
            return False
        if error_kind != "auth":
            # Nobody is signed out; the next poll will probably work. The ceiling above
            # is the only limit — before it existed this path had none at all.
            return True
        # An auth failure has to be earned past the short grace: either the rows are so
        # recent that a token dying seconds after a good poll cannot have made them
        # wrong, or they date themselves and their window is still running.
        return self._cache_is_still_fresh(cached) or _has_open_window(cached, now)

    def _staleness_notice(self, cached: UsageResult) -> str | None:
        """The muted line drawn above substituted rows, once they are old enough to
        mislead without it.

        Silent inside the short grace: a five-minute-old substitution during a network
        blip is the cache doing exactly its job, and saying so every time would put a
        line on screen for something nobody has to act on. Past that, the age is part of
        what the numbers mean — a monthly spend total only ever moves upward, so a stale
        one always reads low, and `source == "cache"` has never been visible anywhere
        (the flyout draws the agent's display name, not the provider's `header`).
        """
        if cached.fetched_at <= 0 or self._cache_is_still_fresh(cached):
            return None
        return fmt.cache_age_text(time.time() - cached.fetched_at)

    def _cache_is_still_fresh(self, cached: UsageResult) -> bool:
        """Are `cached`'s rows recent enough to survive an auth failure?

        `AUTH_CACHE_GRACE_POLLS` poll intervals wide. Two is what makes the window
        reachable at all: the poll that just failed is itself one interval on from the
        last good one, so a one-interval window would only ever be met by an
        out-of-cadence fetch (the flyout refreshes when it opens) landing in the same
        cycle. A result with no `fetched_at` — a cache file written before that field
        existed — has an unknown age and is never fresh: on an auth failure the safe
        direction is to admit we don't know.
        """
        if cached.fetched_at <= 0:
            return False
        grace = AUTH_CACHE_GRACE_POLLS * float(self._cfg.stats.poll_seconds)
        return (time.time() - cached.fetched_at) <= grace
