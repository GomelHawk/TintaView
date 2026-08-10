"""Which agents are doing what, and what colour that adds up to.

Sessions are keyed by ``(agent, sid)`` so Claude Code, Codex and Cursor can be watched
at the same time without their session ids colliding. The fold to a single colour is
global and priority-based — ``confirm`` beats ``working`` beats ``idle`` — because there
is only one set of lights: if any session needs you, the lights say so.
"""

from __future__ import annotations

import threading
import time

from .events import (
    STATUS_CONFIRM,
    STATUS_IDLE,
    STATUS_NONE,
    STATUS_PRIORITY,
    STATUS_WORKING,
)

VALID_STATUSES = (STATUS_IDLE, STATUS_WORKING, STATUS_CONFIRM)


class StateStore:
    """Thread-safe map of ``(agent, sid) -> status``.

    Every mutator returns whether the *effective* status changed, so callers can skip
    redundant device writes — the blink loop and a chatty PostToolUse stream would
    otherwise hammer the lighting SDK with identical colours.
    """

    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str], str] = {}
        self._lock = threading.RLock()
        self._last_event = time.monotonic()

    # --- mutation ---------------------------------------------------------

    def start(self, agent: str, sid: str) -> bool:
        with self._lock:
            before = self.effective()
            self._sessions[(agent, sid)] = STATUS_IDLE
            self._touch()
            return self.effective() != before

    def set(self, agent: str, sid: str, status: str) -> bool:
        if status not in VALID_STATUSES:
            raise ValueError(f"unknown status {status!r}")
        with self._lock:
            before = self.effective()
            self._sessions[(agent, sid)] = status
            self._touch()
            return self.effective() != before

    def end(self, agent: str, sid: str) -> bool:
        with self._lock:
            before = self.effective()
            self._sessions.pop((agent, sid), None)
            self._touch()
            return self.effective() != before

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()
            self._touch()

    def _touch(self) -> None:
        self._last_event = time.monotonic()

    # --- reads ------------------------------------------------------------

    def empty(self) -> bool:
        with self._lock:
            return not self._sessions

    def effective(self) -> str:
        """Global status: the highest-priority status across every live session."""
        with self._lock:
            if not self._sessions:
                return STATUS_NONE
            present = set(self._sessions.values())
            for status in STATUS_PRIORITY:
                if status in present:
                    return status
            return STATUS_IDLE

    def agent_effective(self, agent: str) -> str:
        with self._lock:
            present = {s for (a, _), s in self._sessions.items() if a == agent}
            if not present:
                return STATUS_NONE
            for status in STATUS_PRIORITY:
                if status in present:
                    return status
            return STATUS_IDLE

    def agents(self) -> list[str]:
        with self._lock:
            return sorted({a for a, _ in self._sessions})

    def idle_seconds(self) -> float:
        """Seconds since the last hook event — the watchdog's crash-safety input."""
        with self._lock:
            return time.monotonic() - self._last_event

    def snapshot(self) -> dict:
        """The payload behind ``GET /state``.

        Read-only by contract: it must never touch ``_last_event``, or the tray polling
        it every 1.5 s would keep the watchdog from ever releasing the lights.
        """
        with self._lock:
            per_agent: dict[str, dict] = {}
            for (agent, sid), status in self._sessions.items():
                entry = per_agent.setdefault(agent, {"sessions": {}})
                entry["sessions"][sid] = status
            for entry in per_agent.values():
                present = set(entry["sessions"].values())
                effective = STATUS_IDLE
                for status in STATUS_PRIORITY:
                    if status in present:
                        effective = status
                        break
                entry["effective"] = effective
                entry["count"] = len(entry["sessions"])
            return {
                "effective": self.effective(),
                "agents": per_agent,
                "count": len(self._sessions),
            }
