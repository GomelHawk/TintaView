"""Owns the lighting engine, the configured colours, and the blink thread.

This is the only place that talks to a :class:`~tintaview.engines.base.LightingEngine`.
Everything above it (the HTTP server, the stall detector) only ever calls
``apply(effective_status)`` with one of the four values the state model can produce —
it never touches the engine directly. That keeps the vendor SDK calls (which can be
slow, or simply absent on a device-less machine) off the hook-acknowledgement path.
"""

from __future__ import annotations

import logging
import threading

from .config import Config
from .events import STATUS_CONFIRM, STATUS_NONE

log = logging.getLogger(__name__)

#: Black — the "off" half of the confirm blink.
_OFF = (0, 0, 0)


class LightController:
    """Maps an effective status to engine calls, thread-safely and without raising.

    The engine is opened lazily — on the first non-``none`` status — rather than at
    construction time, so a headless `doctor` run or a config with ``engine.mode =
    "none"`` never pays a connect attempt it doesn't need. ``apply()`` re-checks
    ``engine.active`` on every call (rather than trusting a cached flag), which is what
    lets it transparently re-open a session that would otherwise need special-casing: a
    spurious SessionEnd can release control while an agent is still very much alive, and
    the next status event for that session must take the lights back over.
    """

    def __init__(self, cfg: Config, engine=None) -> None:
        self._cfg = cfg
        # None until first use: the engines/factory module is being written
        # concurrently elsewhere in this repo, so it is imported lazily (inside
        # _get_engine) rather than at module load time. Tests inject a fake engine
        # here directly and never touch the factory at all.
        self._engine = engine
        self._lock = threading.RLock()

        self._blinking = False
        self._blink_stop = threading.Event()
        self._blink_thread: threading.Thread | None = None

        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None

    # --- engine lifecycle ---------------------------------------------------

    def _get_engine(self):
        """The engine, built on first use. Takes ``self._lock`` (an RLock, so the
        already-locked callers below re-enter it harmlessly) because the blink thread
        calls this too: without it, a tick racing ``reset_engine()`` could rebuild an
        engine straight after it was dropped, leaving one that is never opened and
        never closed.
        """
        with self._lock:
            if self._engine is None:
                from tintaview.engines.factory import make_engine  # lazy: see class docstring

                self._engine = make_engine(self._cfg)
            return self._engine

    def _ensure_open_locked(self) -> None:
        engine = self._get_engine()
        try:
            if not engine.active:
                engine.open()
        except Exception:
            log.exception("engine.open() failed")

    def _close_locked(self) -> None:
        if self._engine is None:
            return
        try:
            if self._engine.active:
                self._engine.close()
        except Exception:
            log.exception("engine.close() failed")

    def _set_solid_locked(self, status: str) -> None:
        engine = self._get_engine()
        r, g, b = self._cfg.colors.device_rgb(status)
        try:
            engine.set_color(r, g, b)
        except Exception:
            log.exception("engine.set_color() failed")

    # --- blink ---------------------------------------------------------------

    def _start_blink_locked(self) -> None:
        if self._blinking:
            return
        self._blinking = True
        self._blink_stop.clear()
        self._blink_thread = threading.Thread(
            target=self._blink_loop, daemon=True, name="tintaview-blink"
        )
        self._blink_thread.start()

    def _stop_blink_locked(self) -> None:
        self._blinking = False
        self._blink_stop.set()

    def _blink_loop(self) -> None:
        on = False
        # Floor is a safety net against a near-zero config spinning the engine, not a
        # UX minimum — the test suite relies on configuring a genuinely fast blink.
        interval = max(self._cfg.colors.blink_ms, 10) / 1000.0
        # Event.wait() as the sleep, not time.sleep(): stopping the blink (confirm ->
        # anything else) must take effect immediately, not after the rest of the
        # current half-period — matters most for a fast confirm -> idle transition.
        while not self._blink_stop.is_set():
            on = not on
            with self._lock:
                # Re-checked under the lock so a `reset_engine()` that has already
                # dropped the engine can't have it rebuilt by this tick.
                if self._blink_stop.is_set():
                    break
                # Read per tick rather than cached before the loop: a confirm colour
                # changed in Settings has to reach the device on the next tick, and
                # `apply("confirm")` won't restart an already-running blink.
                color = self._cfg.colors.device_rgb(STATUS_CONFIRM) if on else _OFF
                try:
                    self._get_engine().set_color(*color)
                except Exception:
                    log.exception("blink set_color() failed")
            self._blink_stop.wait(interval)

    @property
    def blinking(self) -> bool:
        return self._blinking

    # --- public API ------------------------------------------------------------

    def apply(self, effective_status: str) -> None:
        """Drive the engine to match ``effective_status``. Never raises.

        A hook handler calls this only when ``StateStore``'s mutators report that the
        effective status actually changed, but ``apply`` is defensive regardless —
        nothing about the lighting path may ever propagate an exception back into the
        HTTP handler.
        """
        try:
            with self._lock:
                if effective_status == STATUS_NONE:
                    self._stop_blink_locked()
                    self._close_locked()
                    return
                self._ensure_open_locked()
                if effective_status == STATUS_CONFIRM:
                    self._start_blink_locked()
                else:
                    self._stop_blink_locked()
                    self._set_solid_locked(effective_status)
        except Exception:
            log.exception("controller.apply(%r) failed", effective_status)

    def start_heartbeat(self) -> None:
        """Start the daemon thread that pings ``engine.heartbeat()`` every 4s.

        Idempotent: called once by the server at startup. Keeping a Chroma session
        alive requires this even when nothing else is happening — Synapse expires an
        idle session after some minutes without one.
        """
        if self._heartbeat_thread is not None and self._heartbeat_thread.is_alive():
            return
        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="tintaview-heartbeat"
        )
        self._heartbeat_thread.start()

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(4.0):
            with self._lock:
                engine = self._engine
            if engine is None:
                continue
            try:
                if engine.active:
                    engine.heartbeat()
            except Exception:
                log.exception("engine.heartbeat() failed")

    def reset_engine(self) -> None:
        """Drop the cached engine so the next status change rebuilds it from the
        current config — lets a lighting-engine change made in Settings take effect
        without restarting the tray. Safe mid-blink or mid-session: closes whatever is
        open first, exactly as `shutdown()` does, but leaves the heartbeat thread
        running (it already tolerates ``self._engine`` being briefly ``None``).
        """
        with self._lock:
            self._stop_blink_locked()
            self._close_locked()
            self._engine = None

    def engine_status(self) -> dict:
        """``{"name", "active", "note"}`` for the ``/state`` payload."""
        with self._lock:
            engine = self._engine
        if engine is None:
            return {"name": "none", "active": False, "note": None}
        try:
            active = bool(engine.active)
        except Exception:
            active = False
        note = getattr(engine, "status_note", None)
        if note is not None and not isinstance(note, str):
            note = None
        return {"name": engine.name, "active": active, "note": note}

    def shutdown(self) -> None:
        """Stop background threads and release the lights.

        Used when the owning server stops. Without this, a test suite that starts many
        `StatusServer` instances would leave every earlier one's blink/heartbeat thread
        ticking (harmlessly, but noisily) for the rest of the run.
        """
        with self._lock:
            self._stop_blink_locked()
            self._close_locked()
        self._heartbeat_stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=1.0)
