"""The no-op engine — status tracking without touching any hardware.

Always available (no SDK, no network, nothing to fail), so it is both the explicit
``mode = "none"`` choice and the guaranteed floor ``auto`` mode falls back to when
neither Chroma nor OpenRGB can be reached. The tray and ``/state`` keep working
identically either way; only the physical lights are missing.
"""

from __future__ import annotations

from .base import BaseEngine


class NullEngine(BaseEngine):
    """Drives nothing. Exists so "no lighting hardware" is a normal state, not an error."""

    name = "none"
    display_name = "Status only (no lighting)"

    def probe(self) -> bool:
        return True  # nothing to reach, so it's always "usable"

    def open(self) -> bool:
        return True  # "taking control" of nothing always succeeds

    def set_color(self, r: int, g: int, b: int) -> None:
        pass  # status is tracked elsewhere (state store); there is no device to write to

    def close(self) -> None:
        pass

    @property
    def active(self) -> bool:
        # Never "in control" — this must read honestly on /state so the tray can tell
        # the difference between "engine active" and "status-only, no lights".
        return False
