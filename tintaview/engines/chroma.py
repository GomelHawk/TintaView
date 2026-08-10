"""Razer Chroma REST SDK engine — the default when Synapse is running.

Ported from the old ``claude_code_razer_lights`` server: POST to open a session (the
response body's ``uri`` is the *real* session URI/port — the SDK never actually listens
on 54235 itself, that's just the discovery endpoint), PUT ``<uri>/heartbeat`` to keep the
session alive, PUT ``<uri>/<device>`` per targeted device to set a colour, DELETE
``<uri>`` to hand control back to Synapse. Synapse resumes its own lighting the instant
the session is gone, so close() has nothing else to do.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

from ..core.config import ChromaConfig
from .base import BaseEngine

log = logging.getLogger(__name__)

#: Chroma Connect's fixed discovery endpoint. A module-level default so callers don't
#: need to know it, but overridable per instance — the tests point it at a local
#: http.server instead of a real Synapse install.
CHROMA_URL = "http://localhost:54235/razer/chromasdk"

_OPEN_TIMEOUT = 3.0
_CALL_TIMEOUT = 5.0
#: probe() must stay cheap — a wizard/auto-detect pass over every engine can't afford
#: Chroma's normal open timeout, and a dead Synapse should not make startup feel hung.
_PROBE_TIMEOUT = 1.5
_OPEN_ATTEMPTS = 3
_RETRY_SLEEP = 0.3


class ChromaEngine(BaseEngine):
    """Drives Razer devices through Chroma Connect. BGR colour packing, see set_color."""

    name = "chroma"
    display_name = "Razer Chroma"

    def __init__(self, cfg: ChromaConfig | None = None, url: str = CHROMA_URL) -> None:
        super().__init__()
        self._devices = tuple((cfg or ChromaConfig()).devices)
        self._url = url
        self._session_uri: str | None = None

    @property
    def active(self) -> bool:
        return self._session_uri is not None

    def probe(self) -> bool:
        """Reachability check that never takes control.

        Chroma has no read-only "are you there" call, so the only honest probe is to
        open a throwaway session and immediately delete it — but an orphaned session
        left behind by a crashed probe would keep Chroma from granting *our own* later
        open() (and would fight any other Chroma app), so the delete is unconditional
        and best-effort even if it never got used.
        """
        if self.active:
            return True  # already holding a session; opening a second one would fight it
        if self.in_cooldown():
            return False
        uri = self._open_request(timeout=_PROBE_TIMEOUT)
        if uri is None:
            return False
        try:
            self._delete(uri, timeout=_PROBE_TIMEOUT)
        except Exception as e:
            log.debug("Chroma probe cleanup failed: %r", e)
        return True

    def open(self) -> bool:
        if self.in_cooldown():
            return False
        uri = None
        for attempt in range(_OPEN_ATTEMPTS):
            uri = self._open_request(timeout=_OPEN_TIMEOUT)
            if uri is not None:
                break
            if attempt < _OPEN_ATTEMPTS - 1:
                time.sleep(_RETRY_SLEEP)
        if uri is None:
            self.note_failure("Chroma unavailable (no Razer devices / Synapse not running?)")
            return False
        self._session_uri = uri
        self.clear_cooldown()
        log.info("Chroma session opened: %s", uri)
        return True

    def heartbeat(self) -> None:
        if not self._session_uri:
            return
        try:
            self._put(self._session_uri + "/heartbeat", None, timeout=_CALL_TIMEOUT)
        except Exception as e:
            log.debug("Chroma heartbeat failed: %r", e)

    def set_color(self, r: int, g: int, b: int) -> None:
        """Set every targeted device to one solid colour. Never raises."""
        if not self._session_uri:
            return  # no session (no devices / Chroma unavailable) — status still tracked
        bgr = (b << 16) | (g << 8) | r  # Chroma packs colour as BGR, not RGB
        payload = {"effect": "CHROMA_STATIC", "param": {"color": bgr}}
        for device in self._devices:
            try:
                self._put(self._session_uri + "/" + device, payload, timeout=_CALL_TIMEOUT)
                # DEBUG, not INFO: the blink loop alone calls this twice a second and
                # would otherwise dominate the log with routine, uninteresting lines.
                log.debug("chroma set_color %s -> %s (bgr=%06x)", device, (r, g, b), bgr)
            except Exception as e:
                log.debug("chroma set_color %s FAILED: %r", device, e)

    def close(self) -> None:
        """Release the session. Synapse takes back its own lighting automatically."""
        if not self._session_uri:
            return
        uri = self._session_uri
        self._session_uri = None
        try:
            self._delete(uri, timeout=_CALL_TIMEOUT)
            log.info("Chroma session released: %s", uri)
        except Exception as e:
            # Still log at INFO (not a routine event) but never raise — a lighting
            # failure on shutdown must not stop the rest of teardown from running.
            log.info("Chroma session release failed (may already be gone): %r", e)

    # --- HTTP plumbing (stdlib only — the core stays dependency-free for PyInstaller) --

    def _open_request(self, timeout: float) -> str | None:
        body = json.dumps({
            "title": "TintaView",
            "description": "Agent status lighting",
            "author": {"name": "TintaView", "contact": "https://github.com/"},
            "device_supported": list(self._devices),
            "category": "application",
        }).encode("utf-8")
        req = urllib.request.Request(
            self._url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["uri"]  # the SDK's response carries the *real* session URI
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError, OSError) as e:
            log.debug("Chroma open failed: %r", e)
            return None

    def _put(self, url: str, payload: dict | None, timeout: float) -> None:
        data = json.dumps(payload).encode("utf-8") if payload is not None else b""
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="PUT"
        )
        with urllib.request.urlopen(req, timeout=timeout):
            pass

    def _delete(self, url: str, timeout: float) -> None:
        req = urllib.request.Request(url, method="DELETE")
        with urllib.request.urlopen(req, timeout=timeout):
            pass
