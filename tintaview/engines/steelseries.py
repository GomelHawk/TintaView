"""SteelSeries GameSense engine — the one lighting backend that works on macOS.

GameSense is SteelSeries GG's local HTTP API, and it is the cheapest engine in this
directory to support: no SDK, no DLL, no compiled dependency, no pip package. GG writes
its ephemeral port into a `coreProps.json` on disk; everything after that is JSON POSTs
to `http://<address>/…` with the stdlib, which is why this fits a core that must keep
working on a bare distro.

The dance, in the order `open()`/`set_color()`/`close()` perform it:

1. `POST /game_metadata` registers the "game" (any app may be one) and, with
   `deinitialize_timer_length_ms`, says how long GG should wait before deciding we are
   gone. TintaView asks for the longest it may: that plus `heartbeat()` is what keeps a
   colour on screen while an agent sits idle for an hour.
2. `POST /bind_game_event` declares an event and the handlers that paint it — one per
   configured device class, each a flat `"mode": "color"` fill of every zone.
3. `POST /game_event` fires it, which is the moment the lights actually change.
4. `POST /remove_game` hands the devices back; GG resumes the user's own lighting.

A **colour** is part of the *handler*, not of the event's payload — GameSense has no
"paint this RGB now" call — so changing colour means re-binding the event. That is why
`set_color` posts twice (bind, then fire), and it is not as wasteful as it looks: both
are loopback POSTs to a local service, the same shape and rate as the Chroma engine's
per-device PUTs.

Windows and macOS only, because GG is. There is no Linux build of SteelSeries GG at all,
so `supports_steelseries` gates it rather than letting Linux users watch it fail to
detect forever.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from ..core.config import SteelSeriesConfig, expand
from .base import BaseEngine

log = logging.getLogger(__name__)

#: Our identity to GG. Uppercase with no spaces is the documented constraint on a game
#: id (`[A-Z0-9_-]`), and it is what shows up in GG's own "Games" list.
GAME_ID = "TINTAVIEW"
GAME_DISPLAY_NAME = "TintaView"
DEVELOPER = "TintaView"
#: One event for every status: the colour lives in the handler that this re-binds, so a
#: second event would only be a second thing to keep in step.
EVENT_ID = "STATUS"

#: GG drops a game that has not been heard from in this long, and 60 s is the documented
#: maximum. `LightController`'s 4 s heartbeat keeps us far inside it; the long timer is
#: what covers a tray that was suspended, not a tray that is merely quiet.
DEINITIALIZE_MS = 60_000

_CALL_TIMEOUT = 3.0
#: probe() runs for every engine on a wizard/auto-detect pass, so it must not be able to
#: make startup feel hung — same budget as the Chroma probe.
_PROBE_TIMEOUT = 1.5
#: Consecutive failures before the session is dropped so `LightController` reopens it.
#: One is noise (GG busy mid-update); three means GG restarted or was quit.
_SESSION_FAILURE_LIMIT = 3

#: Where GG publishes the address of its local server. Not configurable by the user in
#: GG itself — these are the two fixed locations, one per platform it ships on.
_CORE_PROPS = {
    "win32": r"%PROGRAMDATA%\SteelSeries\SteelSeries Engine 3\coreProps.json",
    "darwin": "/Library/Application Support/SteelSeries Engine 3/coreProps.json",
}

#: Device classes a handler may target, as GameSense spells them. The config takes the
#: same names as the OpenRGB engine's `device_types` so one mental model covers both;
#: anything unknown is passed through untouched, since GG's vocabulary is longer than
#: this list (`rgb-per-key-zones`, `indicator`, …) and a user may want one of those.
_DEFAULT_ZONE = "all"


def core_props_path(configured: str = "") -> Path | None:
    """Where to read GG's address from, or None on a platform GG doesn't ship on."""
    if configured:
        return expand(configured)
    template = _CORE_PROPS.get(sys.platform)
    if template is None:
        return None
    return Path(os.path.expandvars(template))


def read_address(configured: str = "") -> str | None:
    """`"127.0.0.1:49786"` from `coreProps.json`, or None if GG isn't installed/running.

    Never raises: a missing file is the normal case on a machine without GG, and a
    half-written one (GG rewrites it on every start, with a fresh port) must degrade to
    "not available" like any other absent vendor service.
    """
    path = core_props_path(configured)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.debug("GameSense coreProps unreadable (%s): %r", path, e)
        return None
    address = data.get("address")
    return address if isinstance(address, str) and address else None


class SteelSeriesEngine(BaseEngine):
    """Drives SteelSeries devices through GG's local GameSense API."""

    name = "steelseries"
    display_name = "SteelSeries GameSense"

    def __init__(self, cfg: SteelSeriesConfig | None = None) -> None:
        super().__init__()
        cfg = cfg or SteelSeriesConfig()
        self._core_props = cfg.core_props
        self._devices = tuple(cfg.device_types)
        #: Set once `open()` has registered the game; None means "not holding control",
        #: which is what `active` reports and what makes the controller reopen.
        self._address: str | None = None
        self._failures = 0
        #: The last colour bound, so a heartbeat can re-assert it. GG forgets a game's
        #: handlers when it restarts, and a heartbeat that only says "still here" would
        #: leave the devices on GG's own lighting with `active` still True.
        self._last_color: tuple[int, int, int] | None = None

    @property
    def active(self) -> bool:
        return self._address is not None

    # --- lifecycle ---------------------------------------------------------------

    def probe(self) -> bool:
        """Is GG running and answering? Registers metadata but takes *no* control.

        Registering a game is not the same as taking the lights: nothing changes on a
        device until an event is bound and fired. That makes this probe honest *and*
        side-effect-free in a way Chroma's cannot be (there, the only probe is to open a
        session and delete it again).
        """
        if self.active:
            return True
        if self.in_cooldown():
            return False
        address = read_address(self._core_props)
        if address is None:
            return False
        return self._post(address, "/game_metadata", self._metadata(), _PROBE_TIMEOUT)

    def open(self) -> bool:
        if self.in_cooldown():
            return False
        address = read_address(self._core_props)
        if address is None:
            self.note_failure("SteelSeries GG not installed or never started (no coreProps.json)")
            return False
        if not self._post(address, "/game_metadata", self._metadata(), _CALL_TIMEOUT):
            self.note_failure(f"SteelSeries GG is not answering at {address}")
            return False
        self._address = address
        self._failures = 0
        self._last_color = None
        self.clear_cooldown()
        log.info("GameSense session opened: %s", address)
        return True

    def close(self) -> None:
        """Hand the devices back. GG resumes the user's own lighting immediately."""
        address = self._address
        if address is None:
            return
        self._address = None
        self._failures = 0
        self._last_color = None
        if not self._post(address, "/remove_game", {"game": GAME_ID}, _CALL_TIMEOUT):
            # INFO, not a raise: a lighting failure during shutdown must never stop the
            # rest of teardown, and GG dropping us first is a perfectly ordinary reason.
            log.info("GameSense release failed (GG may already be gone): %s", address)
        else:
            log.info("GameSense session released: %s", address)

    def heartbeat(self) -> None:
        """Keep GG from deinitialising us while a status sits unchanged for an hour."""
        if self._address is None:
            return
        self._note_call_result(
            self._post(self._address, "/game_heartbeat", {"game": GAME_ID}, _CALL_TIMEOUT)
        )

    # --- painting ----------------------------------------------------------------

    def set_color(self, r: int, g: int, b: int) -> None:
        """Set every targeted device class to one solid colour. Never raises."""
        if self._address is None:
            return  # no session — status is still tracked, the lights just stay put
        bound = self._post(
            self._address, "/bind_game_event", self._binding(r, g, b), _CALL_TIMEOUT
        )
        if not bound:
            self._note_call_result(False)
            return
        fired = self._post(
            self._address,
            "/game_event",
            {"game": GAME_ID, "event": EVENT_ID, "data": {"value": 100}},
            _CALL_TIMEOUT,
        )
        # DEBUG: the confirm blink alone calls this twice a second.
        log.debug("steelseries set_color %s ok=%s", (r, g, b), fired)
        if fired:
            self._last_color = (r, g, b)
        self._note_call_result(fired)

    def _metadata(self) -> dict:
        return {
            "game": GAME_ID,
            "game_display_name": GAME_DISPLAY_NAME,
            "developer": DEVELOPER,
            "deinitialize_timer_length_ms": DEINITIALIZE_MS,
        }

    def _binding(self, r: int, g: int, b: int) -> dict:
        """The event plus one handler per configured device class.

        `min_value`/`max_value` exist because GameSense events are *numeric*; a flat
        `"mode": "color"` handler ignores the value entirely, which is exactly what a
        status colour wants — the number carries no meaning here beyond "fire".
        """
        color = {"red": r, "green": g, "blue": b}
        handlers = [
            {
                "device-type": device,
                "zone": _DEFAULT_ZONE,
                "color": color,
                "mode": "color",
            }
            for device in self._devices
        ]
        return {
            "game": GAME_ID,
            "event": EVENT_ID,
            "min_value": 0,
            "max_value": 100,
            "handlers": handlers,
        }

    # --- HTTP plumbing (stdlib only — the core stays dependency-free) ---------------

    def _post(self, address: str, path: str, payload: dict, timeout: float) -> bool:
        """POST JSON to GG. True on a 2xx; every failure is False, never an exception."""
        url = f"http://{address}{path}"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout):
                return True
        except urllib.error.HTTPError as e:
            # GG answers 4xx with a JSON body naming the offending field, which is the
            # difference between "a device class you asked for does not exist" and "GG
            # is gone" — worth having in the log when someone reports dead lights.
            log.debug("GameSense %s rejected (%s): %r", path, e.code, _error_body(e))
            return False
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log.debug("GameSense %s failed: %r", path, e)
            return False

    def _note_call_result(self, ok: bool) -> None:
        """Drop the session once consecutive failures say GG is no longer there.

        Same contract as the Chroma engine's: without it `active` stays True after GG
        quits, `LightController` never reopens, and every later paint goes nowhere while
        `/state` keeps claiming the lights are ours.
        """
        if ok:
            self._failures = 0
            return
        self._failures += 1
        if self._failures < _SESSION_FAILURE_LIMIT:
            return
        address = self._address
        self._address = None
        self._failures = 0
        self._last_color = None
        log.info("GameSense at %s is no longer answering — will reopen on the next "
                 "status change", address)


def _error_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", "replace")[:200]
    except Exception:
        return ""
