"""Infers Cursor's missing "waiting for your approval" event.

Cursor has no `Notification`/`PermissionRequest` equivalent (see AGENTS.md, "Cursor stall heuristic"):
when it stops to ask the user to approve a tool call, no hook fires at all. The only
observable symptom is that `tool-start` fired and nothing — no `tool-end`, no other
event for that session — followed within a threshold. That silence is the signal.

This has to be conservative: a long-running test suite or build is *also* a tool-start
followed by silence for a while, and it must never turn the lights red. Two properties
keep it safe:

* Only sessions explicitly armed by :meth:`tool_start` are ever candidates — nothing
  is inferred from state that wasn't reported to us.
* *Any* subsequent event for that session — not just its matching `tool-end` —
  disarms it. A long-running tool is still "working", not "stalled", right up until
  something suggests otherwise.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

#: How often the daemon thread re-checks armed sessions against the clock. Independent
#: of `stall_seconds` (which is per-agent and configurable) — this just bounds how late
#: a stall can be noticed.
_TICK_INTERVAL = 1.0


class StallDetector:
    """Arms a `(agent, sid)` on `tool_start`; fires `on_stall(agent, sid)` if nothing
    else is heard for it within `stall_seconds`.

    `clock` is injectable so tests can drive expiry with a fake clock and manual
    `tick()` calls instead of real sleeps.
    """

    def __init__(
        self,
        on_stall: Callable[[str, str], None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._on_stall = on_stall
        self._clock = clock
        self._lock = threading.Lock()
        # (agent, sid) -> deadline (in `clock()` units) at which to fire on_stall.
        self._armed: dict[tuple[str, str], float] = {}
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # --- arming -------------------------------------------------------------

    def tool_start(self, agent: str, sid: str, stall_seconds: float) -> None:
        """Arm a session: if nothing else happens for it within `stall_seconds`, it is
        promoted to `confirm`."""
        with self._lock:
            self._armed[(agent, sid)] = self._clock() + stall_seconds

    def tool_end(self, agent: str, sid: str) -> None:
        """A matching tool-end is the common case: the tool just finished normally."""
        self.cancel(agent, sid)

    def cancel(self, agent: str, sid: str) -> None:
        """Disarm a session. Called for *every* event on it, not only tool-end — any
        sign of life means it wasn't actually stalled."""
        with self._lock:
            self._armed.pop((agent, sid), None)

    # --- evaluation -----------------------------------------------------------

    def tick(self) -> None:
        """Fire `on_stall` for every session whose deadline has passed.

        Pops expired entries before calling back, so `on_stall` raising or being slow
        can't leave a session permanently armed, and can't fire twice for the same
        expiry.
        """
        now = self._clock()
        expired: list[tuple[str, str]] = []
        with self._lock:
            for key, deadline in self._armed.items():
                if now >= deadline:
                    expired.append(key)
            for key in expired:
                del self._armed[key]
        for agent, sid in expired:
            try:
                self._on_stall(agent, sid)
            except Exception:
                log.exception("stall callback failed for %s/%s", agent, sid)

    # --- daemon thread --------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="tintaview-stall")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:
        while not self._stop_event.wait(_TICK_INTERVAL):
            self.tick()
