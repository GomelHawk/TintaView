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
    right: str = ""  # right-aligned text, e.g. "Resets in 3 hr 12 min"
    show_pct: bool = True
    severity: str = "normal"  # normal | warning | critical
    kind: str = "limit"  # limit | credits | info

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class UsageResult:
    agent: str
    rows: list[UsageRow] = field(default_factory=list)
    header: str = ""  # e.g. "Your usage limits · Max"
    source: str = "official"  # official | estimate | activity | cache
    error: str | None = None  # user-facing reason when rows is empty

    @property
    def ok(self) -> bool:
        return bool(self.rows)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "rows": [r.to_dict() for r in self.rows],
            "header": self.header,
            "source": self.source,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UsageResult:
        return cls(
            agent=data.get("agent", ""),
            rows=[UsageRow(**r) for r in data.get("rows", [])],
            header=data.get("header", ""),
            source=data.get("source", "cache"),
            error=data.get("error"),
        )


class UsageProvider(abc.ABC):
    """Fetches usage for one agent. Must never raise — return a UsageResult with
    ``error`` set instead, so one broken provider can't blank the whole flyout."""

    key: str = "agent"

    @abc.abstractmethod
    def fetch(self, agent_config, timeout: float = 15.0) -> UsageResult:
        ...
