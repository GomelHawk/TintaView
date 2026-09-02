"""The HTTP status broker — the hook ingress, `/state` for the tray, and the watchdog.

The handler, watchdog and blink loop all hang off one `(agent, sid)` state model. The
non-obvious constraints commented below are deliberate — each one is there because of a
real failure mode, not as defensive habit; don't "simplify" them away.

Routes (all GET, loopback only — see AGENTS.md, "Core contracts"):

    /v1/event/{event}?agent=&sid=&tool=   hook ingress
    /{session-start,session-end,working,idle,confirm}?sid=
                                          agent-less aliases, agent defaults to claude
    /state                                read-only status for the tray and `doctor`
    /healthz                              liveness for `doctor`
    /show                                 surface the running instance's usage panel
    /quit                                 ask the running instance to shut down cleanly
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .. import __version__
from .config import Config
from .controller import LightController
from .events import (
    CONFIRM,
    EVENTS,
    IDLE,
    LEGACY_EVENTS,
    SESSION_END,
    SESSION_START,
    STATUS_CONFIRM,
    STATUS_IDLE,
    STATUS_WORKING,
    TOOL_END,
    TOOL_START,
    WORKING,
)
from .stalldetect import StallDetector
from .state import StateStore

log = logging.getLogger(__name__)

#: The agent-less aliases carry no `agent=` query param, so they all fold onto the
#: agent most people are running: a bare `curl .../working?sid=x` means Claude Code.
DEFAULT_AGENT = "claude"

#: Fallback session id when a request omits `sid` outright (shouldn't happen from a
#: real hook, but a handler must never crash on a malformed request).
DEFAULT_SID = "default"

_V1_PREFIX = "/v1/event/"

#: How often `serve_forever` wakes to notice `shutdown()`. The stdlib default is 0.5 s,
#: which every `stop()` — including one per test in tests/test_server.py — paid in full
#: as teardown latency for nothing.
HTTP_POLL_INTERVAL_S = 0.1

#: How long `stop()` waits for an in-flight `controller.apply()` before giving up on the
#: applier thread. It is a daemon thread, so a genuinely wedged vendor SDK call delays
#: shutdown by this much rather than preventing it.
APPLIER_JOIN_TIMEOUT_S = 2.0


def _first(query: dict[str, list[str]], key: str, default: str) -> str:
    values = query.get(key)
    return values[0] if values else default


def _watchdog_poll_seconds(timeout: float) -> float:
    """How often the watchdog thread re-checks sessions against the clock.

    Scales with the configured timeout so a short test timeout is still caught
    promptly, without polling a real (minutes-long) timeout needlessly often.
    """
    return max(0.05, min(5.0, timeout / 5.0))


class _Handler(BaseHTTPRequestHandler):
    """Stateless per-request handler; all state lives on `self.server.status_server`
    (the owning `StatusServer`), which `http.server` hands every handler as `self.server`.
    """

    server_version = "TintaView/1"

    def _write_ack(self) -> None:
        """Acknowledge a hook request IMMEDIATELY — 200, Content-Length: 0, flushed —
        BEFORE any lighting I/O runs. A hook's `curl -m 1/2` must never time out
        waiting on a slow Chroma/OpenRGB call, or the agent it's guarding would stall.
        """
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()
        with contextlib.suppress(OSError):
            self.wfile.flush()

    def _send_json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (http.server's naming convention)
        try:
            self._route()
        except Exception:
            log.exception("unhandled error handling %s", self.path)

    def _route(self) -> None:
        status_server: StatusServer = self.server.status_server  # type: ignore[attr-defined]
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/healthz":
            self._send_json({"ok": True, "version": __version__})
            return

        if path == "/show":
            # "Someone launched TintaView again" — a second process found the port
            # taken, so it asks the instance that owns it to surface its usage panel
            # instead of both of them exiting silently. Loopback only, and it can do
            # nothing but pop a window that a tray click already opens.
            self._send_json({"ok": True, "shown": status_server.request_show()})
            return

        if path == "/quit":
            # The graceful-shutdown channel: `install/restart.py` and the tray's Quit
            # both need the *running* instance to stop, and killing a pid would skip
            # `server.stop()` and leave the lights on whatever colour they last showed.
            # Answered before the callback runs — it tears this server down, so a reply
            # written afterwards would never reach the caller.
            self._send_json({"ok": True, "quitting": status_server.on_quit is not None})
            status_server.request_quit()
            return

        if path == "/state":
            # Best-effort read that must NOT touch the watchdog's last-event timestamp
            # — see StateStore.snapshot()'s docstring. The tray can poll this as
            # often as it likes without ever keeping a dead agent's lights alive.
            self._send_json(status_server.state_payload())
            return

        if path.startswith(_V1_PREFIX):
            event = path[len(_V1_PREFIX) :]
            if event not in EVENTS:
                self._send_json({"error": f"unknown event {event!r}"}, status=404)
                return
            agent = _first(query, "agent", DEFAULT_AGENT)
            sid = _first(query, "sid", DEFAULT_SID)
            tool = _first(query, "tool", "")
            self._write_ack()
            status_server.handle_event(event, agent, sid, tool)
            return

        legacy_event = path.lstrip("/")
        if legacy_event in LEGACY_EVENTS:
            # A caller that hits `/idle`, `/working`, ... with a bare `sid` and no
            # `agent=` gets defaulted to "claude", so a hand-written hook script stays
            # a one-line curl without having to know about the multi-agent query API.
            sid = _first(query, "sid", DEFAULT_SID)
            self._write_ack()
            status_server.handle_event(legacy_event, DEFAULT_AGENT, sid, "")
            return

        self._send_json({"error": "not found"}, status=404)

    def log_message(self, *args) -> None:
        # Hooks fire on every tool call in every session — the default access log
        # would be pure noise (and, embedded in the tray, nowhere useful to send it).
        pass


class _StatusHTTPServer(ThreadingHTTPServer):
    # HTTPServer defaults this on, but on Windows SO_REUSEADDR lets bind() succeed
    # against a port another process is still actively listening on — unlike POSIX,
    # where it only affects sockets stuck in TIME_WAIT. That would silently defeat
    # start()'s "is another instance already running" check below.
    allow_reuse_address = sys.platform != "win32"


def _is_tintaview_listening(host: str, port: int, timeout: float = 1.5) -> bool:
    """Is the program holding `host:port` a TintaView daemon?

    Same test `doctor` applies to the DAEMON check, kept deliberately narrow: only a
    parseable JSON body carrying an ``ok`` key counts. Anything else — a plain-text
    reply, an HTML error page, a connection that opens and says nothing — is some other
    program, and start() must say so rather than exit quietly. Never raises; an
    unreachable or unparseable port simply means "not TintaView".
    """
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=timeout) as resp:
            raw = resp.read()
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return False
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return isinstance(payload, dict) and "ok" in payload


def request_show(host: str, port: int, timeout: float = 1.5) -> bool:
    """Ask the TintaView already listening on `host:port` to show its usage panel.

    True only if that instance confirms it actually showed something — a headless
    daemon owning the port has no panel to show, and the caller needs to be able to say
    so rather than exiting with no window and no message.
    """
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://{host}:{port}/show", timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return False
    return isinstance(payload, dict) and bool(payload.get("shown"))


class _StatusApplier:
    """Serialises `controller.apply()` onto one thread, keeping only the newest status.

    Two problems, one mechanism:

    - **The ack must not wait on lighting I/O.** The handler used to call `apply()`
      itself, so a slow Chroma/OpenRGB/G HUB call held an HTTP worker thread — and with
      several sessions firing hooks, a pile of them, each queued on the controller lock
      behind the one before it.
    - **The newest status is the only one that matters.** A queue would replay a whole
      backlog of colours onto the device after a slow call finally returned; a single
      slot means a status superseded while an older one was still being applied is
      simply dropped, which is what the lights should do anyway.

    The watchdog and the stall detector route through here too, so every writer to the
    engine is the same thread and the fold the store computed under its own lock is the
    one that reaches the hardware.
    """

    def __init__(self, apply: Callable[[str], None]) -> None:
        self._apply = apply
        self._cond = threading.Condition()
        self._wanted: str | None = None
        self._applying = False
        self._stopping = False
        self._thread: threading.Thread | None = None

    def request(self, status: str) -> None:
        """Ask for `status` to be applied, replacing any status not applied yet."""
        with self._cond:
            if self._stopping:
                return
            self._wanted = status
            self._ensure_started_locked()
            self._cond.notify()

    def _ensure_started_locked(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="tintaview-apply"
            )
            self._thread.start()

    def _run(self) -> None:
        while True:
            with self._cond:
                while self._wanted is None and not self._stopping:
                    self._cond.wait()
                if self._wanted is None:
                    return  # stopping, nothing left to do
                status = self._wanted
                self._wanted = None
                self._applying = True
            try:
                self._apply(status)
            except Exception:
                # apply() is already defensive; this is the last net, because an
                # exception here would kill the only thread that drives the lights.
                log.exception("applying status %r failed", status)
            finally:
                with self._cond:
                    self._applying = False
                    self._cond.notify_all()

    def wait_idle(self, timeout: float = 2.0) -> bool:
        """Block until nothing is pending and nothing is being applied.

        For tests and for `stop()`: the HTTP response now returns before the lighting
        call runs, so "has this reached the engine yet?" needs an answer that isn't a
        sleep.
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._wanted is not None or self._applying:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return True

    def stop(self, timeout: float = APPLIER_JOIN_TIMEOUT_S) -> None:
        """Stop accepting statuses and join the thread.

        Called before `controller.shutdown()`, so an event still in flight cannot
        re-open the engine after it was closed and released.
        """
        with self._cond:
            self._stopping = True
            thread = self._thread
            self._thread = None
            self._cond.notify_all()
        if thread is not None:
            thread.join(timeout)


class StatusServer:
    """Owns the HTTP server, the state store, the controller, the stall detector and
    the watchdog thread. Embeddable in-process by the tray/CLI, or run headless.
    """

    def __init__(
        self,
        cfg: Config,
        state: StateStore | None = None,
        controller: LightController | None = None,
    ) -> None:
        self._cfg = cfg
        self.state = state if state is not None else StateStore()
        self.controller = controller if controller is not None else LightController(cfg)
        self._stall = StallDetector(self._on_stall)

        #: Set by the tray to its "show the flyout" slot. Left None headless, which is
        #: what makes `/show` answer `shown: false` there instead of pretending.
        #: Called on an HTTP worker thread — a GUI implementation has to marshal to its
        #: own thread (the tray does, via a Qt signal).
        self.on_show: Callable[[], None] | None = None

        #: Set by whoever owns the process lifetime to its "shut down cleanly" slot —
        #: the tray to its quit action, headless to the park event `_cmd_run` waits on.
        #: Left None means `/quit` answers `quitting: false` and does nothing, exactly
        #: as `/show` does. Also called on an HTTP worker thread.
        self.on_quit: Callable[[], None] | None = None

        #: Every `controller.apply()` goes through here — see `_StatusApplier`.
        self._applier = _StatusApplier(self.controller.apply)

        self._httpd: _StatusHTTPServer | None = None
        self._http_thread: threading.Thread | None = None
        self._watchdog_stop = threading.Event()
        self._watchdog_thread: threading.Thread | None = None

    # --- lifecycle ------------------------------------------------------------

    def start(self) -> bool:
        """Bind and start serving. Returns False (never raises) if the port is taken.

        A taken port has two very different causes and they must not be reported the
        same way. Another TintaView owning it is routine — the tray and a stray headless
        daemon both starting at login — and deserves nothing louder than an INFO. A
        *different* program squatting on the port is a dead end: TintaView exits, no tray
        icon ever appears, and the user is left with no explanation at all. So the port
        is probed before assuming anything, and a stranger is logged as an ERROR that
        names the fix.
        """
        try:
            httpd = _StatusHTTPServer((self._cfg.server.host, self._cfg.server.port), _Handler)
        except OSError as exc:
            host, port = self._cfg.server.host, self._cfg.server.port
            if _is_tintaview_listening(host, port):
                log.info("port %s:%s already in use by another TintaView instance — "
                         "leaving it to serve (%r)", host, port, exc)
            else:
                log.error(
                    "port %s:%s is held by something that is not TintaView, so it cannot "
                    "start and no tray icon will appear. Free that port, or set "
                    "`server.port` in config.toml to an unused one and start TintaView "
                    "again. Run `tintaview doctor` to confirm. (%r)",
                    host, port, exc,
                )
            return False

        httpd.daemon_threads = True
        httpd.status_server = self  # type: ignore[attr-defined]
        self._httpd = httpd

        self._http_thread = threading.Thread(
            target=lambda: httpd.serve_forever(poll_interval=HTTP_POLL_INTERVAL_S),
            daemon=True, name="tintaview-http",
        )
        self._http_thread.start()

        self.controller.start_heartbeat()

        self._stall.start()

        self._watchdog_stop.clear()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, daemon=True, name="tintaview-watchdog"
        )
        self._watchdog_thread.start()

        log.info("TintaView status broker listening on %s", self.url)
        return True

    def stop(self) -> None:
        """Tear down in strict ingress-first order.

        The HTTP server goes down first, then the two background threads that can also
        produce a status, then the applier, and only then the controller. Closing the
        engine first (which is what this used to do) left a window in which an in-flight
        hook could re-open it, and the process then exited with the device still held
        and the lights stuck on TintaView's last colour.
        """
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._http_thread is not None:
            self._http_thread.join(timeout=2.0)
            self._http_thread = None
        self._watchdog_stop.set()
        self._stall.stop()
        if self._watchdog_thread is not None:
            self._watchdog_thread.join(timeout=2.0)
            self._watchdog_thread = None
        self._applier.stop()
        self.controller.shutdown()

    @property
    def url(self) -> str:
        host = self._cfg.server.host
        # Report the actual bound port, not the configured one: tests deliberately
        # request port 0 (ephemeral) so many can run concurrently without clashing.
        port = self._httpd.server_address[1] if self._httpd is not None else self._cfg.server.port
        return f"http://{host}:{port}"

    # --- event handling ------------------------------------------------------------

    def handle_event(self, event: str, agent: str, sid: str, tool: str) -> None:
        """Update the state store (and the stall detector) for one hook event, then hand
        the new effective status to the applier — but only when it actually changed.
        `StateStore`'s mutators report that (and what it changed *to*, computed under the
        store's own lock) so a chatty PostToolUse stream doesn't hammer the lighting SDK
        with redundant, identical colours, and two events on two HTTP worker threads
        can't apply each other's stale fold.

        Returns as soon as the store is updated: the actual `controller.apply()` runs on
        the applier thread, so the ack this request already sent is never followed by a
        handler thread sitting on a slow vendor SDK call.
        """
        # `agents.enabled` has to be enforced *here*, not only where hooks are installed.
        # Unticking an agent in the wizard stops TintaView managing its hooks, but any
        # entry already sitting in that agent's config file keeps firing — installed by an
        # earlier run, by hand, or in a project-scoped file the user forgot about. Without
        # this guard a "disabled" agent still drives the lighting, which is precisely the
        # thing the user turned off. Dropping the event here also keeps it out of /state,
        # so the tray and the flyout agree with the config too.
        if not self._cfg.is_enabled(agent):
            log.debug("ignoring %s event for disabled agent %r (session %s)", event, agent, sid)
            return

        try:
            # Any event for a session is a sign of life — see StallDetector's
            # docstring for why this must disarm regardless of which event it is.
            self._stall.cancel(agent, sid)

            if event == SESSION_START:
                effective = self.state.start(agent, sid)
            elif event == SESSION_END:
                effective = self.state.end(agent, sid)
            elif event == WORKING:
                effective = self.state.set(agent, sid, STATUS_WORKING)
            elif event == IDLE:
                effective = self.state.set(agent, sid, STATUS_IDLE)
            elif event == CONFIRM:
                effective = self.state.set(agent, sid, STATUS_CONFIRM)
            elif event == TOOL_START:
                stall_seconds = self._stall_seconds_for(agent)
                if stall_seconds is not None:
                    self._stall.tool_start(agent, sid, stall_seconds)
                # The tool name rides along for display only (the flyout shows what an
                # agent is busy *with*); it never influences the status or the lights.
                effective = self.state.set(agent, sid, STATUS_WORKING, tool=tool)
            elif event == TOOL_END:
                # "" rather than leaving it: the tool has finished, so the session is
                # still working but no longer on anything we can name.
                effective = self.state.set(agent, sid, STATUS_WORKING, tool="")
            else:
                log.warning("unhandled event %r for %s/%s", event, agent, sid)
                return

            if effective is not None:
                self._applier.request(effective)
        except Exception:
            log.exception("failed handling event %s for %s/%s", event, agent, sid)

    def _stall_seconds_for(self, agent: str) -> float | None:
        """None unless this agent is configured for the stall heuristic (Cursor) —
        see AGENTS.md, "Cursor stall heuristic". Claude/Codex have a real confirm event and must never
        be armed, or a slow tool call would eventually paint them red for no reason.
        """
        # `agent_config`, not `agent`: this runs on an HTTP worker thread for every
        # tool call, and `agent()` inserts into `cfg.agents` — which `dumps()` iterates
        # on the GUI thread while saving settings.
        acfg = self._cfg.agent_config(agent)
        if acfg.confirm_detection == "stall":
            return acfg.stall_seconds
        return None

    def _on_stall(self, agent: str, sid: str) -> None:
        """StallDetector callback: promote a session that looks stuck on an unseen
        approval prompt to `confirm`. Runs on the stall detector's own daemon thread.
        """
        try:
            log.info("stall detected for %s/%s -> confirm", agent, sid)
            effective = self.state.set(agent, sid, STATUS_CONFIRM)
            if effective is not None:
                self._applier.request(effective)
        except Exception:
            log.exception("failed applying stall promotion for %s/%s", agent, sid)

    def request_show(self) -> bool:
        """Invoke the registered show-the-panel callback. Never raises: this runs on an
        HTTP worker thread, where an exception would be logged and lost anyway."""
        callback = self.on_show
        if callback is None:
            return False
        try:
            callback()
        except Exception:
            log.exception("on_show callback failed")
            return False
        return True

    def request_quit(self) -> bool:
        """Invoke the registered shut-down callback. Never raises — same contract as
        `request_show()`: it runs on an HTTP worker thread, where an exception would be
        logged and lost."""
        callback = self.on_quit
        if callback is None:
            log.info("/quit ignored: no shutdown callback registered")
            return False
        try:
            callback()
        except Exception:
            log.exception("on_quit callback failed")
            return False
        return True

    def apply_status(self, status: str | None = None) -> None:
        """Re-send `status` (default: the store's current effective status) to the
        controller through the single-slot applier. The tray uses this after a settings
        change; calling `controller.apply()` directly would run the SDK call on the GUI
        thread and bypass the newest-wins coalescing every other caller gets."""
        if status is None:
            status = self.state.effective()
        self._applier.request(status)

    # --- reads for the tray ------------------------------------------------------

    def state_payload(self) -> dict:
        """The `GET /state` body: `StateStore.snapshot()` plus lighting/version info."""
        payload = self.state.snapshot()
        payload["blinking"] = self.controller.blinking
        payload["engine"] = self.controller.engine_status()
        payload["version"] = __version__
        # So the wizard can restart *this* instance after writing a new config, without
        # guessing which of the machine's python processes is the tray. Only ever served
        # on the loopback interface, and a local process could drive the lights through
        # the hook endpoints anyway, so this exposes nothing new.
        payload["pid"] = os.getpid()
        return payload

    # --- watchdog ------------------------------------------------------------------

    def _watchdog_loop(self) -> None:
        """Retire sessions that have gone silent for `watchdog_timeout` seconds — the
        case this exists for: an agent that crashed or was killed without ever sending
        its SessionEnd hook.

        Expiry is per session, not per store (see `StateStore.expired`). Retiring only
        the silent sessions is what makes the watchdog correct with more than one agent
        running: a busy Claude session no longer keeps a long-dead Cursor session's
        colour on the lights, and an idle-but-alive session that trips the timeout no
        longer takes every other session down with it.
        """
        timeout = self._cfg.server.watchdog_timeout
        poll = _watchdog_poll_seconds(timeout)
        while not self._watchdog_stop.wait(poll):
            try:
                stale = self.state.expired(timeout)
                if not stale:
                    continue
                log.warning(
                    "watchdog: releasing %d session(s) after %.0fs of silence: %s",
                    len(stale), timeout,
                    ", ".join(f"{agent}/{sid}" for agent, sid in stale),
                )
                effective = self.state.end_many(stale)
                if effective is not None:
                    self._applier.request(effective)
            except Exception:
                log.exception("watchdog tick failed")
