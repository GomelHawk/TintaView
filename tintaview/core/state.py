"""Which agents are doing what, and what colour that adds up to.

Sessions are keyed by ``(agent, sid)`` so Claude Code, Codex and Cursor can be watched
at the same time without their session ids colliding. The fold to a single colour is
global and priority-based — ``confirm`` beats ``working`` beats ``idle`` — because there
is only one set of lights: if any session needs you, the lights say so.

Each session carries its **own** last-seen timestamp, not just a shared one. The
watchdog is the reason: with a single global clock a chatty session keeps every stale
session alive (so a crashed agent's lights never get released, which is the whole point
of the watchdog), while one quiet-but-alive session drags every *other* session down
with it when the timeout finally fires. Per-session stamps make expiry mean what it
says — this session has gone silent — so the watchdog can retire exactly that one.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .events import (
    STATUS_CONFIRM,
    STATUS_IDLE,
    STATUS_NONE,
    STATUS_PRIORITY,
    STATUS_WORKING,
)

VALID_STATUSES = (STATUS_IDLE, STATUS_WORKING, STATUS_CONFIRM)


@dataclass
class _Session:
    """One live ``(agent, sid)``: what it's doing, with what, and when it last said so."""

    status: str
    #: Name of the tool this session most recently started, "" if unknown. Purely
    #: descriptive — it never affects the effective status or the lighting, it just
    #: lets the flyout say *what* an agent is busy with rather than only that it is.
    tool: str = ""
    seen: float = field(default_factory=time.monotonic)


class StateStore:
    """Thread-safe map of ``(agent, sid) -> _Session``.

    Every mutator returns **the new effective status when it changed**, and ``None`` when
    it didn't, so callers can skip redundant device writes — the blink loop and a chatty
    PostToolUse stream would otherwise hammer the lighting SDK with identical colours.

    Returning the status itself, rather than a bool the caller then re-reads with
    ``effective()``, is what makes the lighting update race-free: two events landing on
    two HTTP worker threads used to mutate under the lock and then each read the fold
    back *outside* it, so the older event could win and leave the lights on a colour
    nothing was in any more.
    """

    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str], _Session] = {}
        self._lock = threading.RLock()
        self._last_event = time.monotonic()

    # --- mutation ---------------------------------------------------------

    def start(self, agent: str, sid: str) -> str | None:
        with self._lock:
            before = self.effective()
            self._sessions[(agent, sid)] = _Session(status=STATUS_IDLE)
            self._touch()
            return self._changed_locked(before)

    def set(self, agent: str, sid: str, status: str, tool: str | None = None) -> str | None:
        """Set a session's status, and optionally the tool it's running.

        ``tool=None`` means "unchanged" and ``tool=""`` means "no longer running a named
        tool" — a tool-end has to be able to clear the name it set, but a plain
        ``working`` ping in between must not.
        """
        if status not in VALID_STATUSES:
            raise ValueError(f"unknown status {status!r}")
        with self._lock:
            before = self.effective()
            session = self._sessions.get((agent, sid))
            if session is None:
                session = _Session(status=status)
                self._sessions[(agent, sid)] = session
            else:
                session.status = status
            if tool is not None:
                session.tool = tool
            session.seen = time.monotonic()
            self._touch()
            return self._changed_locked(before)

    def end(self, agent: str, sid: str) -> str | None:
        with self._lock:
            before = self.effective()
            self._sessions.pop((agent, sid), None)
            self._touch()
            return self._changed_locked(before)

    def end_many(self, keys) -> str | None:
        """Drop several sessions at once, reporting one effective-status change.

        The watchdog's retirement path: expiring three dead sessions must produce a
        single lighting update, not three, and must not report a change at all when the
        surviving sessions still fold to the same colour.
        """
        with self._lock:
            before = self.effective()
            for key in list(keys):
                self._sessions.pop(tuple(key), None)
            self._touch()
            return self._changed_locked(before)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()
            self._touch()

    def _changed_locked(self, before: str) -> str | None:
        """The new effective status, or None when it still folds to `before`.

        Computed while the mutator still holds the store lock, so what the caller applies
        to the hardware is the fold that this very mutation produced.
        """
        after = self.effective()
        return after if after != before else None

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
            present = {s.status for s in self._sessions.values()}
            for status in STATUS_PRIORITY:
                if status in present:
                    return status
            return STATUS_IDLE

    def agent_effective(self, agent: str) -> str:
        with self._lock:
            present = {s.status for (a, _), s in self._sessions.items() if a == agent}
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
        """Seconds since the last hook event on *any* session.

        A whole-store liveness reading, kept for diagnostics and tests. The watchdog
        deliberately does not use it — see `expired()`.
        """
        with self._lock:
            return time.monotonic() - self._last_event

    def expired(self, timeout: float) -> list[tuple[str, str]]:
        """Keys of every session that hasn't been heard from in `timeout` seconds.

        Per-session, so an active Claude session can't vouch for a Cursor session that
        died half an hour ago, and a session that simply sat idle past the timeout takes
        only itself down rather than everything on screen.
        """
        now = time.monotonic()
        with self._lock:
            return [key for key, s in self._sessions.items() if now - s.seen > timeout]

    def snapshot(self) -> dict:
        """The payload behind ``GET /state``.

        Read-only by contract: it must never touch ``_last_event`` or any session's
        ``seen``, or the tray polling it every 1.5 s would keep the watchdog from ever
        releasing the lights.
        """
        with self._lock:
            per_agent: dict[str, dict] = {}
            for (agent, sid), session in self._sessions.items():
                entry = per_agent.setdefault(agent, {"sessions": {}, "tools": {}})
                # `sessions` stays a plain {sid: status} map: `doctor`'s live hook test
                # and the flyout both read it, and neither needs the rest.
                entry["sessions"][sid] = session.status
                if session.tool:
                    entry["tools"][sid] = session.tool
            for entry in per_agent.values():
                present = set(entry["sessions"].values())
                effective = STATUS_IDLE
                for status in STATUS_PRIORITY:
                    if status in present:
                        effective = status
                        break
                entry["effective"] = effective
                entry["count"] = len(entry["sessions"])
                # One tool name for the agent's whole section, since that's all the
                # flyout has room for. Only meaningful while it's actually busy: a
                # finished session's last tool is stale trivia, not status.
                entry["tool"] = (
                    self._headline_tool(entry) if effective == STATUS_WORKING else ""
                )
            return {
                "effective": self.effective(),
                "agents": per_agent,
                "count": len(self._sessions),
            }

    @staticmethod
    def _headline_tool(entry: dict) -> str:
        """The tool to show for an agent running several sessions at once.

        Picked from a session that is actually ``working`` — with two sessions open, the
        idle one's leftover tool name must not be what the section advertises. Sorted by
        sid so the choice is stable across polls rather than flickering between two
        equally busy sessions on dict order.
        """
        working = sorted(
            sid for sid, status in entry["sessions"].items() if status == STATUS_WORKING
        )
        for sid in working:
            tool = entry["tools"].get(sid)
            if tool:
                return tool
        return ""
