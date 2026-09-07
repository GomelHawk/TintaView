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
            result = provider.fetch(agent_cfg, timeout=timeout)
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

        Deliberately does not write: `fetch_all` persists the whole pass in one go once
        every provider has answered.
        """
        if result.ok:
            return result
        cached = self._cache.get(result.agent)
        if cached is None or not cached.ok:
            return result
        if result.error_kind == "auth" and not self._cache_is_still_fresh(cached):
            log.info(
                "stats provider %s failed to authenticate and its cached rows are stale "
                "— reporting the auth failure instead of usage nobody can refresh",
                result.agent,
            )
            return result
        return replace(cached, source="cache")

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
