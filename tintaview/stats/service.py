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
        return self._apply_cache_policy(result)

    def _apply_cache_policy(self, result: UsageResult) -> UsageResult:
        """Good data is kept; bad data (empty rows — a rate limit, an outage, a login
        failure) falls back to the last cached good result instead of blanking the flyout
        or replacing good numbers with a worse guess.

        Deliberately does not write: `fetch_all` persists the whole pass in one go once
        every provider has answered.
        """
        if result.ok:
            return result
        cached = self._cache.get(result.agent)
        if cached is not None and cached.ok:
            return replace(cached, source="cache")
        return result
