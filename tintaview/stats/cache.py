"""Last-good usage results, persisted to disk.

Two jobs:
  - Survive a restart: the tray flyout would otherwise be blank until the first poll
    completes, which can take a while (three network/local providers, one poll cycle
    every ``stats.poll_seconds``).
  - Survive a bad poll: a rate limit or a transient network error must not blank out
    (or overwrite with a noisy estimate) numbers that were good a minute ago —
    ``StatsService`` reads this back whenever a fetch comes back empty.

One JSON file for every agent, atomically replaced on write (temp file + ``os.replace``,
same pattern as ``core.config.save``) so a crash mid-write can never leave a truncated
file behind. A corrupt or missing file is treated as "no cache yet", never raised.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterable
from pathlib import Path

from tintaview.core.config import config_dir

from .model import UsageResult

CACHE_FILENAME = "usage_cache.json"


def cache_path() -> Path:
    return config_dir() / CACHE_FILENAME


class UsageCache:
    """Last-good :class:`UsageResult` per agent key.

    ``StatsService`` fetches every agent in its own thread and calls ``update()`` from
    whichever thread finishes, so this needs its own lock — without one, two
    concurrent saves race on the same ``*.tmp`` path and one's ``os.replace`` loses
    the file the other just consumed.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or cache_path()
        self._lock = threading.Lock()
        self._data: dict[str, UsageResult] = self._load()

    def _load(self) -> dict[str, UsageResult]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}  # missing or corrupt — start empty rather than raising
        if not isinstance(raw, dict):
            return {}
        out: dict[str, UsageResult] = {}
        for key, value in raw.items():
            try:
                out[key] = UsageResult.from_dict(value)
            except (TypeError, ValueError, AttributeError):
                continue  # one malformed entry must not blank the whole cache
        return out

    def get(self, agent: str) -> UsageResult | None:
        with self._lock:
            return self._data.get(agent)

    def update(self, result: UsageResult) -> None:
        """Remember `result` as the last-good result for its agent and persist it.

        Callers decide the freshness policy (see ``StatsService``); this only ever
        stores what it's told, keyed by ``result.agent``.
        """
        with self._lock:
            self._data[result.agent] = result
            self._save()

    def update_many(self, results: Iterable[UsageResult]) -> None:
        """Remember several results and persist them in **one** write.

        `StatsService` used to call `update()` per provider, so a five-provider poll
        rewrote this whole file five times — five temp files, five `os.replace`, five
        chances for a reader to see a half-finished set. The atomicity is unchanged; only
        the number of writes is.
        """
        with self._lock:
            changed = False
            for result in results:
                self._data[result.agent] = result
                changed = True
            if changed:
                self._save()

    def _save(self) -> None:
        # Called with `self._lock` already held.
        payload = {key: value.to_dict() for key, value in self._data.items()}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Unique per writer even though the lock already serialises same-process
        # callers — cheap insurance against a second process (e.g. a stale daemon)
        # touching the same file.
        tmp = self._path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)
