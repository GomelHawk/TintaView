"""The HTTP status broker — the hook ingress, `/state` for the tray, and the watchdog.

Ports `razer_light_server.py`'s handler, watchdog and blink-loop pattern onto the new
`(agent, sid)` state model. The non-obvious constraints below are carried over from
that server on purpose; see its comments for the incidents that put them there.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
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
    STATUS_NONE,
    STATUS_WORKING,
    TOOL_END,
    TOOL_START,
    WORKING,
)
from .stalldetect import StallDetector
from .state import StateStore

log = logging.getLogger(__name__)

#: The old `hook.sh` and every legacy alias never sent an `agent=` query param —
#: they predate multi-agent support entirely — so they're all folded onto "claude".
DEFAULT_AGENT = "claude"

#: Fallback session id when a request omits `sid` outright (shouldn't happen from a
#: real hook, but a handler must never crash on a malformed request).
DEFAULT_SID = "default"

_V1_PREFIX = "/v1/event/"


def _first(query: dict[str, list[str]], key: str, default: str) -> str:
    values = query.get(key)
    return values[0] if values else default


def _watchdog_poll_seconds(timeout: float) -> float:
    """How often the watchdog thread checks `idle_seconds()`.

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
            # Back-compat: the old claude_code_razer_lights hook.sh only ever knew
            # about `/idle`, `/working`, ... with a bare `sid` — no `agent=`. Defaulting
            # it to "claude" lets that install keep working, unmodified, against this
            # server (docs/PLAN.md §11: migration can be incremental).
            sid = _first(query, "sid", DEFAULT_SID)
            self._write_ack()
            status_server.handle_event(legacy_event, DEFAULT_AGENT, sid, "")
            return

        self._send_json({"error": "not found"}, status=404)

    def log_message(self, *args) -> None:
        # Hooks fire on every tool call in every session — the default access log
        # would be pure noise (and, embedded in the tray, nowhere useful to send it).
        pass


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

        self._httpd: ThreadingHTTPServer | None = None
        self._http_thread: threading.Thread | None = None
        self._watchdog_stop = threading.Event()
        self._watchdog_thread: threading.Thread | None = None

    # --- lifecycle ------------------------------------------------------------

    def start(self) -> bool:
        """Bind and start serving. Returns False (never raises) if the port is taken —
        that means another TintaView instance already owns it, which is a normal
        outcome (e.g. the tray and a stray headless daemon both starting at login),
        not a failure to report loudly.
        """
        try:
            httpd = ThreadingHTTPServer((self._cfg.server.host, self._cfg.server.port), _Handler)
        except OSError as exc:
            log.info(
                "port %s:%s already in use — assuming another instance owns it (%r)",
                self._cfg.server.host,
                self._cfg.server.port,
                exc,
            )
            return False

        httpd.daemon_threads = True
        httpd.status_server = self  # type: ignore[attr-defined]
        self._httpd = httpd

        self._http_thread = threading.Thread(
            target=httpd.serve_forever, daemon=True, name="tintaview-http"
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
        self._watchdog_stop.set()
        self._stall.stop()
        self.controller.shutdown()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._http_thread is not None:
            self._http_thread.join(timeout=2.0)
            self._http_thread = None
        if self._watchdog_thread is not None:
            self._watchdog_thread.join(timeout=2.0)
            self._watchdog_thread = None

    @property
    def url(self) -> str:
        host = self._cfg.server.host
        # Report the actual bound port, not the configured one: tests deliberately
        # request port 0 (ephemeral) so many can run concurrently without clashing.
        port = self._httpd.server_address[1] if self._httpd is not None else self._cfg.server.port
        return f"http://{host}:{port}"

    # --- event handling ------------------------------------------------------------

    def handle_event(self, event: str, agent: str, sid: str, tool: str) -> None:
        """Update the state store (and the stall detector) for one hook event, then
        push a lighting update only if the effective status actually changed —
        `StateStore`'s mutators report that so a chatty PostToolUse stream doesn't
        hammer the lighting SDK with redundant, identical colours.
        """
        del tool  # not needed for status tracking; kept for future per-tool logging
        try:
            # Any event for a session is a sign of life — see StallDetector's
            # docstring for why this must disarm regardless of which event it is.
            self._stall.cancel(agent, sid)

            if event == SESSION_START:
                changed = self.state.start(agent, sid)
            elif event == SESSION_END:
                changed = self.state.end(agent, sid)
            elif event == WORKING:
                changed = self.state.set(agent, sid, STATUS_WORKING)
            elif event == IDLE:
                changed = self.state.set(agent, sid, STATUS_IDLE)
            elif event == CONFIRM:
                changed = self.state.set(agent, sid, STATUS_CONFIRM)
            elif event == TOOL_START:
                stall_seconds = self._stall_seconds_for(agent)
                if stall_seconds is not None:
                    self._stall.tool_start(agent, sid, stall_seconds)
                changed = self.state.set(agent, sid, STATUS_WORKING)
            elif event == TOOL_END:
                changed = self.state.set(agent, sid, STATUS_WORKING)
            else:
                log.warning("unhandled event %r for %s/%s", event, agent, sid)
                return

            if changed:
                self.controller.apply(self.state.effective())
        except Exception:
            log.exception("failed handling event %s for %s/%s", event, agent, sid)

    def _stall_seconds_for(self, agent: str) -> float | None:
        """None unless this agent is configured for the stall heuristic (Cursor) —
        see docs/PLAN.md §5.3. Claude/Codex have a real confirm event and must never
        be armed, or a slow tool call would eventually paint them red for no reason.
        """
        acfg = self._cfg.agent(agent)
        if acfg.confirm_detection == "stall":
            return acfg.stall_seconds
        return None

    def _on_stall(self, agent: str, sid: str) -> None:
        """StallDetector callback: promote a session that looks stuck on an unseen
        approval prompt to `confirm`. Runs on the stall detector's own daemon thread.
        """
        try:
            log.info("stall detected for %s/%s -> confirm", agent, sid)
            changed = self.state.set(agent, sid, STATUS_CONFIRM)
            if changed:
                self.controller.apply(self.state.effective())
        except Exception:
            log.exception("failed applying stall promotion for %s/%s", agent, sid)

    # --- reads for the tray ------------------------------------------------------

    def state_payload(self) -> dict:
        """The `GET /state` body: `StateStore.snapshot()` plus lighting/version info."""
        payload = self.state.snapshot()
        payload["blinking"] = self.controller.blinking
        payload["engine"] = self.controller.engine_status()
        payload["version"] = __version__
        return payload

    # --- watchdog ------------------------------------------------------------------

    def _watchdog_loop(self) -> None:
        """Force-release the lights if no hook has fired in `watchdog_timeout`
        seconds — the case the old server called out explicitly: an agent that
        crashed or was killed without ever sending its SessionEnd hook.
        """
        poll = _watchdog_poll_seconds(self._cfg.server.watchdog_timeout)
        while not self._watchdog_stop.wait(poll):
            try:
                if (
                    not self.state.empty()
                    and self.state.idle_seconds() > self._cfg.server.watchdog_timeout
                ):
                    log.warning(
                        "watchdog: forcing release after %.0fs of silence",
                        self.state.idle_seconds(),
                    )
                    self.state.clear()
                    self.controller.apply(STATUS_NONE)
            except Exception:
                log.exception("watchdog tick failed")
