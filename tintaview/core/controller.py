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
import time

from .config import ColorsConfig, Config
from .events import STATUS_CONFIRM, STATUS_NONE

log = logging.getLogger(__name__)

#: Black — the "off" half of the confirm blink.
_OFF = (0, 0, 0)

#: `auto` mode only: how long to wait before probing for a lighting engine again after
#: detection came up empty. Synapse, G HUB and OpenRGB are all routinely started *after*
#: the tray (they autostart too, and the user may install one later), and without a
#: re-probe the process stayed status-only for the rest of its life. A cooldown, because
#: a probe sweep is several socket connects and every hook event would otherwise pay it.
AUTO_REDETECT_SECONDS = 30.0


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

    def __init__(self, cfg: Config, engine=None, clock=time.monotonic) -> None:
        self._cfg = cfg
        #: Injectable so the auto re-detect cooldown is testable without sleeping.
        self._clock = clock
        # None until first use: the engines/factory module is being written
        # concurrently elsewhere in this repo, so it is imported lazily (inside
        # _get_engine) rather than at module load time. Tests inject a fake engine
        # here directly and never touch the factory at all.
        self._engine = engine
        self._lock = threading.RLock()

        self._blinking = False
        self._blink_stop = threading.Event()
        self._blink_thread: threading.Thread | None = None
        #: Bumped on every stop/start. A blink thread captures the value it was born
        #: with and exits as soon as it goes stale — see `_start_blink_locked`.
        self._blink_generation = 0

        # "Pause lighting" from the tray menu. Deliberately enforced here rather than
        # in the tray: `apply()` is driven by the HTTP handler and the stall detector,
        # neither of which the tray sits in front of, so a flag anywhere else would let
        # the next hook event repaint the device the user just asked to leave alone.
        # Runtime-only, never persisted — quitting while paused and starting again to
        # find the lights permanently dead is a worse bug than losing the setting.
        self._paused = False
        #: Last status `apply()` was asked for, replayed on unpause so the lights come
        #: back to what is actually happening rather than to whatever they showed last.
        self._wanted = STATUS_NONE

        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None

        #: Earliest monotonic time `auto` mode may probe for an engine again.
        self._next_auto_probe = 0.0

        #: Pre-computed `/state` engine block, replaced (never mutated) by
        #: `_refresh_status_locked` — see `engine_status()` for why it is not read
        #: under `_lock`.
        self._status: dict = {"name": "none", "active": False, "note": None, "paused": False}
        if engine is not None:
            self._refresh_status_locked()

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
                self._engine = self._make_engine()
                self._next_auto_probe = self._clock() + AUTO_REDETECT_SECONDS
            else:
                self._maybe_redetect_locked()
            return self._engine

    def _make_engine(self):
        from tintaview.engines.factory import make_engine  # lazy: see class docstring

        return make_engine(self._cfg)

    def _maybe_redetect_locked(self) -> None:
        """`auto` mode only: re-probe when the cached engine is the status-only fallback.

        A `NullEngine` here means nothing was reachable the first time we looked — a
        Synapse or OpenRGB that starts a few seconds after the tray, or is installed
        later in the day. Without this the process stayed dark until it was restarted.
        An explicitly pinned engine is never re-probed: the user asked for that one (or
        for status-only), and swapping it out from under them would be a surprise.
        """
        if self._cfg.engine.mode != "auto":
            return
        if self._engine is None or getattr(self._engine, "name", "") != "none":
            return
        now = self._clock()
        if now < self._next_auto_probe:
            return
        self._next_auto_probe = now + AUTO_REDETECT_SECONDS
        engine = self._make_engine()
        if getattr(engine, "name", "") == "none":
            return
        log.info("lighting engine %r became available — switching to it", engine.name)
        self._engine = engine
        self._refresh_status_locked()

    def _ensure_open_locked(self) -> None:
        engine = self._get_engine()
        try:
            if not engine.active:
                engine.open()
        except Exception:
            log.exception("engine.open() failed")
        self._refresh_status_locked()

    def _close_locked(self) -> None:
        if self._engine is None:
            return
        try:
            if self._engine.active:
                self._engine.close()
        except Exception:
            log.exception("engine.close() failed")
        self._refresh_status_locked()

    def _set_solid_locked(self, status: str) -> None:
        engine = self._get_engine()
        try:
            # The colour lookup is inside the try with the paint: `device_rgb` raises on
            # a hand-edited non-hex colour, and a status change must not take the whole
            # apply() path down with it.
            r, g, b = self._cfg.colors.device_rgb(status)
            engine.set_color(r, g, b)
        except Exception:
            log.exception("engine.set_color() failed")
        # set_color is where an engine notices its session died (a Chroma 4xx, a dead
        # OpenRGB socket, a G HUB paint refusal), so the /state snapshot is refreshed
        # from its result rather than only on open/close.
        self._refresh_status_locked()

    # --- blink ---------------------------------------------------------------

    def _start_blink_locked(self) -> None:
        if self._blinking:
            return
        self._blinking = True
        self._blink_generation += 1
        # A fresh Event per thread, plus the generation the thread is born with. The
        # previous thread (if any) is still waiting on the *old* Event, which
        # `_stop_blink_locked` left set, so it wakes at once, finds its generation
        # stale and exits — instead of ticking alongside its replacement for the rest
        # of the run. A confirm -> idle -> confirm inside one half-period used to leak
        # a blink thread every time, and two of them paint opposite halves.
        stop = threading.Event()
        self._blink_stop = stop
        self._blink_thread = threading.Thread(
            target=self._blink_loop, args=(self._blink_generation, stop),
            daemon=True, name="tintaview-blink",
        )
        self._blink_thread.start()

    def _stop_blink_locked(self) -> None:
        self._blinking = False
        self._blink_generation += 1  # anything still running is stale from here on
        self._blink_stop.set()

    def _blink_interval(self) -> float:
        """Half-period in seconds, re-read every tick so a `blink_ms` changed in
        Settings takes effect on the next one. The floor is a safety net against a
        near-zero config spinning the engine, not a UX minimum — the test suite relies
        on configuring a genuinely fast blink.
        """
        try:
            ms = int(self._cfg.colors.blink_ms)
        except (TypeError, ValueError):
            ms = ColorsConfig.blink_ms
        return max(ms, 10) / 1000.0

    def _blink_loop(self, generation: int, stop: threading.Event) -> None:
        on = False
        # Event.wait() as the sleep, not time.sleep(): stopping the blink (confirm ->
        # anything else) must take effect immediately, not after the rest of the
        # current half-period — matters most for a fast confirm -> idle transition.
        while not stop.is_set():
            on = not on
            with self._lock:
                # Re-checked under the lock so a `reset_engine()` that has already
                # dropped the engine can't have it rebuilt by this tick; the generation
                # check is what retires a thread whose blink was already restarted.
                if stop.is_set() or generation != self._blink_generation:
                    break
                try:
                    # Colour read per tick rather than cached before the loop: a confirm
                    # colour changed in Settings has to reach the device on the next
                    # tick, and `apply("confirm")` won't restart an already-running
                    # blink. Inside the try because `device_rgb` raises on a hand-edited
                    # non-hex colour — outside it, that killed this thread for good while
                    # `blinking` stayed True and /state kept promising a blink.
                    color = self._cfg.colors.device_rgb(STATUS_CONFIRM) if on else _OFF
                    self._get_engine().set_color(*color)
                except Exception:
                    log.exception("blink tick failed")
                self._refresh_status_locked()
            stop.wait(self._blink_interval())

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
                self._wanted = effective_status
                if self._paused:
                    return
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

    @property
    def paused(self) -> bool:
        return self._paused

    def set_paused(self, paused: bool) -> None:
        """Release the lights and stop driving them (or resume). Never raises.

        Pausing releases the device immediately — the point is to hand the hardware
        back to Synapse/G HUB/OpenRGB while the user records, streams or screenshots,
        not merely to freeze it on whatever colour it happened to be showing.
        """
        paused = bool(paused)
        with self._lock:
            if paused == self._paused:
                return
            self._paused = paused
            wanted = self._wanted
            self._refresh_status_locked()
        if paused:
            with self._lock:
                self._stop_blink_locked()
                self._close_locked()
            log.info("lighting paused — device released")
        else:
            log.info("lighting resumed — restoring %r", wanted)
            self.apply(wanted)

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
                if self._paused:
                    continue  # nothing to keep alive: the session was closed on pause
                engine = self._engine
            if engine is None:
                continue
            try:
                if engine.active:
                    engine.heartbeat()
            except Exception:
                log.exception("engine.heartbeat() failed")
            with self._lock:
                self._refresh_status_locked()

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
            # A dropped engine is "none" again, and `auto` must be allowed to look for a
            # replacement immediately rather than after the re-detect cooldown.
            self._next_auto_probe = 0.0
            self._refresh_status_locked()

    def _refresh_status_locked(self) -> None:
        """Recompute the `/state` engine block. Called by every method that can change
        it, always under ``_lock``; the dict is replaced wholesale, never mutated, so a
        lock-free reader can only ever see a complete one.
        """
        engine = self._engine
        if engine is None:
            self._status = {"name": "none", "active": False, "note": None,
                            "paused": self._paused}
            return
        try:
            active = bool(engine.active)
        except Exception:
            active = False
        note = getattr(engine, "status_note", None)
        if note is not None and not isinstance(note, str):
            note = None
        self._status = {"name": engine.name, "active": active, "note": note,
                        "paused": self._paused}

    def engine_status(self) -> dict:
        """``{"name", "active", "note", "paused"}`` for the ``/state`` payload.

        Deliberately lock-free. The Qt GUI thread polls `/state` every 1.5 s, while
        `_lock` is held across `engine.set_color()` on the blink thread and across
        `open()`/`close()` in `apply()` — a slow vendor SDK call therefore used to
        freeze the tray for as long as it took. The snapshot is kept up to date by the
        methods that change it instead.
        """
        return dict(self._status)

    def shutdown(self) -> None:
        """Stop background threads and release the lights.

        Used when the owning server stops. Without this, a test suite that starts many
        `StatusServer` instances would leave every earlier one's blink/heartbeat thread
        ticking (harmlessly, but noisily) for the rest of the run.
        """
        with self._lock:
            self._stop_blink_locked()
            self._close_locked()
            self._refresh_status_locked()
        self._heartbeat_stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=1.0)
