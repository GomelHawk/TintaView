"""The lighting-engine contract.

Everything above this line (state model, HTTP API, hooks, tray) is vendor- and
OS-agnostic; everything vendor-specific lives behind this interface. An engine owns the
whole lifecycle of "take control of the lights, drive them, give them back".
"""

from __future__ import annotations

import abc
import logging
import time

log = logging.getLogger(__name__)

#: How long to stop retrying after the vendor SDK turns out to be unavailable. Without
#: this a device-less machine would pay seconds of connect timeouts on *every* hook.
INIT_COOLDOWN = 60.0


class LightingEngine(abc.ABC):
    """One lighting backend (Chroma, OpenRGB, or the no-op)."""

    #: Stable identifier used in config (`engine.mode`) and reported by /state.
    name: str = "base"
    #: Shown in the wizard.
    display_name: str = "Lighting engine"

    @abc.abstractmethod
    def probe(self) -> bool:
        """Is this engine usable *right now*? Must be fast and must not take control.

        Used by the wizard to show detected/not-running, and by ``auto`` mode to pick.
        """

    @abc.abstractmethod
    def open(self) -> bool:
        """Connect and take control of the lights. True on success.

        Implementations that cannot restore the user's previous lighting automatically
        (OpenRGB) must snapshot it here.
        """

    @abc.abstractmethod
    def set_color(self, r: int, g: int, b: int) -> None:
        """Set every targeted device to one solid colour. Never raises."""

    @abc.abstractmethod
    def close(self) -> None:
        """Release control and restore whatever the user's lighting was before."""

    def heartbeat(self) -> None:  # noqa: B027 — optional by design, not an oversight
        """Keep the session alive.

        Concrete and empty on purpose: only Chroma needs a keepalive, so making this
        abstract would force every other engine to write a no-op override.
        """

    @property
    @abc.abstractmethod
    def active(self) -> bool:
        """Whether control is currently held — feeds ``/state``."""


class BaseEngine(LightingEngine):
    """Shared failure back-off, so a missing SDK degrades to 'status only' quietly."""

    def __init__(self) -> None:
        self._cooldown_until = 0.0

    def in_cooldown(self) -> bool:
        return time.monotonic() < self._cooldown_until

    def note_failure(self, why: str, cooldown: float = INIT_COOLDOWN) -> None:
        self._cooldown_until = time.monotonic() + cooldown
        log.info("%s unavailable (%s) — status still tracked; retrying in %.0fs",
                 self.display_name, why, cooldown)

    def clear_cooldown(self) -> None:
        self._cooldown_until = 0.0
