"""OpenRGB engine — cross-platform control via the OpenRGB SDK server.

Unlike Chroma, OpenRGB has no concept of a session that hands control back on its own:
closing our TCP connection leaves every device exactly as we last set it. So this engine
has to do, by hand, everything Chroma gives away for free:

1. **Snapshot & restore** — record each targeted device's active mode and per-LED
   colours on open(), put them back on close(). Skip this and the user's rig stays
   whatever colour we last set it to, forever, after the agent exits.
2. **Direct mode only** — most non-Direct modes write straight to the device's flash;
   our confirm-state blink twice a second would grind through flash write-cycles in
   days. Only devices that expose a "Direct" mode are driven; we switch them into it.
3. **Reconnect back-off** — mirrors Chroma's cooldown via BaseEngine.

`openrgb-python` (import name ``openrgb``) is an optional dependency (see
``pyproject.toml``'s ``[project.optional-dependencies] openrgb``). It is imported
lazily, inside methods, never at module scope, so this module — and therefore the whole
``engines`` package — imports fine on a machine that only has Chroma, or neither.
"""

from __future__ import annotations

import logging

from ..core.config import OpenRGBConfig
from .base import BaseEngine

log = logging.getLogger(__name__)

#: probe() must stay cheap. openrgb-python's own socket connect timeout is hardcoded to
#: 1s internally; this is just for our log messages, not an actual override knob.
_PROBE_LABEL = "TintaView-probe"
_OPEN_LABEL = "TintaView"


class OpenRGBEngine(BaseEngine):
    """Drives non-Chroma RGB devices through the OpenRGB SDK. RGB colour order."""

    name = "openrgb"
    display_name = "OpenRGB"

    def __init__(self, cfg: OpenRGBConfig | None = None) -> None:
        super().__init__()
        self._cfg = cfg or OpenRGBConfig()
        self._client = None
        self._targets: list = []  # live Device objects we're actually driving
        # id(device) -> (active_mode, [RGBColor, ...]) as they were before we touched
        # anything. Keyed by identity, not name/index: names collide across devices of
        # the same model and this only needs to live for one open()/close() pair.
        self._snapshot: dict[int, tuple] = {}

    @property
    def active(self) -> bool:
        return self._client is not None and bool(self._targets)

    # --- connection helpers (all openrgb imports happen in here, lazily) --------

    def _connect(self, client_name: str):
        """Import openrgb-python and connect. Returns the client, or None on any failure."""
        try:
            from openrgb import OpenRGBClient
        except ImportError as e:
            log.debug("openrgb-python not installed: %r", e)
            return None
        try:
            return OpenRGBClient(address=self._cfg.host, port=self._cfg.port, name=client_name)
        except OSError as e:
            log.debug("OpenRGB connect to %s:%s failed: %r", self._cfg.host, self._cfg.port, e)
            return None

    @staticmethod
    def _disconnect(client) -> None:
        try:
            client.disconnect()
        except Exception as e:
            log.debug("OpenRGB disconnect failed: %r", e)

    def _target_types(self):
        """DeviceType members for cfg.device_types, or None meaning "every device".

        The configured default is peripherals only (mouse/keyboard/headset) — see
        OpenRGBConfig.device_types for why motherboard and RAM lighting is left alone.
        """
        if not self._cfg.device_types:
            return None
        try:
            from openrgb.utils import DeviceType
        except ImportError:
            return None
        types = set()
        for name in self._cfg.device_types:
            member = getattr(DeviceType, name.upper(), None)
            if member is not None:
                types.add(member)
            else:
                log.info("OpenRGB: unknown device type %r in config, ignoring", name)
        return types

    @staticmethod
    def _has_direct_mode(device) -> bool:
        return any(m.name.lower() == "direct" for m in device.modes)

    def _select_devices(self, client, *, log_skipped: bool = False) -> list:
        """Devices matching `device_types`, further narrowed to Direct-capable ones
        when `direct_mode_only` is set. Shared by probe() and open() so a probe()
        success is an honest prediction of whether open() would also succeed."""
        types = self._target_types()
        devices = list(client.devices)
        if types is not None:
            devices = [d for d in devices if d.type in types]
        if self._cfg.direct_mode_only:
            skipped = [d.name for d in devices if not self._has_direct_mode(d)]
            devices = [d for d in devices if self._has_direct_mode(d)]
            if log_skipped and skipped:
                log.info("OpenRGB: skipping device(s) with no Direct mode: %s",
                          ", ".join(skipped))
        return devices

    # --- LightingEngine ----------------------------------------------------

    def probe(self) -> bool:
        """Connect, count matching devices, disconnect. Never takes control."""
        if self.in_cooldown():
            return False
        client = self._connect(_PROBE_LABEL)
        if client is None:
            return False
        try:
            return bool(self._select_devices(client))
        finally:
            self._disconnect(client)

    def open(self) -> bool:
        if self.in_cooldown():
            return False
        client = self._connect(_OPEN_LABEL)
        if client is None:
            self.note_failure("OpenRGB unreachable or openrgb-python not installed")
            return False

        candidates = self._select_devices(client, log_skipped=True)
        if not candidates:
            log.info("OpenRGB: no controllable devices found")
            self._disconnect(client)
            self.note_failure("no matching OpenRGB devices")
            return False

        # Snapshot BEFORE changing anything, so a failure partway through set_mode
        # below still leaves us with a correct picture to restore from on close().
        snapshot: dict[int, tuple] = {}
        if self._cfg.restore_on_release:
            for device in candidates:
                try:
                    snapshot[id(device)] = (device.active_mode, list(device.colors))
                except Exception as e:
                    log.info("OpenRGB: could not snapshot %s: %r", device.name, e)

        if self._cfg.direct_mode_only:
            for device in candidates:
                try:
                    device.set_mode("Direct")
                except Exception as e:
                    log.info("OpenRGB: failed to set Direct mode on %s: %r", device.name, e)

        self._client = client
        self._targets = candidates
        self._snapshot = snapshot
        self.clear_cooldown()
        log.info("OpenRGB session opened: %d device(s)", len(candidates))
        return True

    def heartbeat(self) -> None:
        pass  # the SDK connection is a plain persistent TCP socket; no keepalive needed

    def set_color(self, r: int, g: int, b: int) -> None:
        """Set every targeted device to one solid colour. Never raises."""
        if not self._client or not self._targets:
            return
        try:
            from openrgb.utils import RGBColor
        except ImportError:
            return
        color = RGBColor(r, g, b)  # RGB order here — do not reuse Chroma's BGR packing
        for device in self._targets:
            try:
                device.set_color(color)
                # DEBUG, not INFO: the blink loop calls this twice a second.
                log.debug("openrgb set_color %s -> %s", device.name, (r, g, b))
            except Exception as e:
                log.debug("openrgb set_color %s FAILED: %r", device.name, e)

    def close(self) -> None:
        """Best-effort restore of whatever open() overwrote, then disconnect."""
        if not self._client:
            return
        client = self._client
        targets = self._targets
        snapshot = self._snapshot
        self._client = None
        self._targets = []
        self._snapshot = {}

        if self._cfg.restore_on_release:
            for device in targets:
                saved = snapshot.get(id(device))
                if saved is None:
                    continue
                mode, colors = saved
                try:
                    device.set_mode(mode)
                    device.set_colors(colors)
                except Exception as e:
                    # Best-effort by contract: a device that vanished or refused the
                    # restore must not stop the rest of teardown (or the other
                    # devices' restores) from happening.
                    log.info("OpenRGB: restore failed for %s: %r", device.name, e)

        self._disconnect(client)
        log.info("OpenRGB session released")
