"""Normalised usage rows.

Every agent reports usage differently — Claude has official percentages, Codex has
rate-limit percentages buried in local session logs, Cursor has a spend figure behind an
unofficial RPC. They all normalise to these rows so the tray flyout renders any agent
with one painter.
"""

from __future__ import annotations

import abc
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class UsageRow:
    label: str  # "5-hour limit"
    pct: float  # 0-100; 0 when the row is informational only
    right: str = ""  # right-aligned text, e.g. "$20.00 included"
    show_pct: bool = True
    severity: str = "normal"  # normal | warning | critical
    kind: str = "limit"  # limit | credits | info
    #: When this limit next resets, epoch seconds; 0.0 for "no reset instant known".
    #: When set, the renderer words it **on every repaint** through
    #: `stats.format.reset_row_text` and ignores `right` — so a provider must store the
    #: instant here rather than a sentence in `right`. Wording it at fetch time is how
    #: "Resets in 2 hr 59 min" ended up in `usage_cache.json` still counting down to a
    #: window that had closed three days earlier, and it also left every live row up to
    #: one poll interval behind between polls.
    reset_at: float = 0.0
    #: Which `stats.format.RESET_*` wording `reset_at` gets. Meaningless when
    #: `reset_at` is 0.0.
    reset_style: str = "relative"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class UsageResult:
    agent: str
    rows: list[UsageRow] = field(default_factory=list)
    header: str = ""  # e.g. "Your usage limits · Max"
    source: str = "official"  # official | estimate | activity | cache
    error: str | None = None  # user-facing reason when rows is empty
    #: What kind of failure `error` is, and therefore how far `StatsService` may go to
    #: hide it. **transient** — a network blip, an HTTP 429, a locked state DB: nobody
    #: is signed out, the next poll will probably work, so cached rows may stand in for
    #: as long as it lasts. That is what the cache is for, and it stays the default so
    #: an unclassified failure keeps today's behaviour. **auth** — a 401, an agent that
    #: is signed out: no poll will refresh those numbers until the user signs in again,
    #: so cached rows are only trusted while they are still fresh and otherwise give
    #: way to `error` (see `StatsService._apply_cache_policy`).
    #:
    #: Never persisted: only a failed result carries it, and failures are never cached.
    error_kind: str = "transient"  # transient | auth
    #: Locally reconstructed token/cost rows, drawn **underneath** `rows` (or underneath
    #: the error line) rather than instead of them — see `stats/providers/claude`.
    #:
    #: Deliberately not part of `rows`, and deliberately excluded from `ok`. Both would
    #: be one-line changes and both are wrong: `ok` is what `StatsService` reads to
    #: decide whether a poll succeeded, so an estimate in `rows` would make a dead login
    #: look like a good fetch — the auth message would never fire and the estimate would
    #: be written to `usage_cache.json` as last-good data. Keeping it beside `rows`
    #: means a section can show "sign in again" *and* the local numbers at once.
    #:
    #: Never persisted either (see `to_dict`): it is rebuilt from local transcripts on
    #: every poll in single-digit milliseconds, so a cached copy could only ever be a
    #: staler version of something already free. `StatsService._apply_cache_policy`
    #: carries the *live* estimate onto a cached result for exactly that reason.
    estimate: list[UsageRow] = field(default_factory=list)
    #: An advisory sentence drawn above `rows`, in the same muted slot as `error` — for
    #: a result that is not a failure but should not be read as live either. Today that
    #: means exactly one thing: rows served from the cache, old enough that their age is
    #: part of what they mean (`StatsService._staleness_notice`).
    #:
    #: Separate from `error` because it is not one. A section with a notice still has
    #: real rows and `ok` is True; overloading `error` would make "did this poll fail?"
    #: unanswerable from the result alone, and `error` is what `to_dict` persists.
    #: Never persisted either: it is recomputed from `fetched_at` on every substitution,
    #: and a stored one would go on claiming an age that stopped being true.
    notice: str | None = None
    #: When these rows were fetched, epoch seconds — 0.0 meaning "unknown", which is
    #: what a cache file written before this field existed reads back as. Stamped by
    #: `StatsService` so no provider has to remember to, and deliberately carried
    #: through a cache substitution unchanged: the age that matters is the age of the
    #: numbers, not of the poll that failed to replace them.
    fetched_at: float = 0.0

    @property
    def ok(self) -> bool:
        """Did the *authoritative* fetch produce rows?

        `estimate` is not consulted on purpose — see its docstring. A result carrying
        only local estimate rows is not a successful poll, and must not be cached as
        one or allowed to mask an auth failure.
        """
        return bool(self.rows)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "rows": [r.to_dict() for r in self.rows],
            "header": self.header,
            "source": self.source,
            "error": self.error,
            "fetched_at": self.fetched_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UsageResult:
        return cls(
            agent=data.get("agent", ""),
            rows=[UsageRow(**r) for r in data.get("rows", [])],
            header=data.get("header", ""),
            source=data.get("source", "cache"),
            error=data.get("error"),
            # A non-numeric value raises here, which `UsageCache._load` catches and
            # treats as one malformed entry to skip — the same as any other bad field.
            fetched_at=float(data.get("fetched_at") or 0.0),
        )


class UsageProvider(abc.ABC):
    """Fetches usage for one agent. Must never raise — return a UsageResult with
    ``error`` set instead, so one broken provider can't blank the whole flyout."""

    key: str = "agent"

    @abc.abstractmethod
    def fetch(self, agent_config, timeout: float = 15.0) -> UsageResult:
        ...
