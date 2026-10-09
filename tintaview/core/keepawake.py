"""Keep the machine awake — the tray's "Keep awake" menu toggle, a built-in Caffeine.

While held, the *system* must not sleep, including after the user locks the screen.
The display is deliberately left alone: it may still turn off and the screensaver may
still start, to save power — an agent's work only needs the system running, and the
lighting keeps showing its status with the screen dark. Driven only by the menu toggle
(`ui.keep_awake`), never by session activity.

One backend per platform, stdlib only:

- **Windows** — a power request (`PowerCreateRequest` + `PowerSetRequest`) for the
  system and, where the OS has it, execution — never the display. Unlike
  `SetThreadExecutionState` it is not tied to the calling thread, and it is listed by
  `powercfg /requests` under its reason string, which is how a user (or `doctor`) can see
  who is keeping the machine up. The kernel drops it when the process dies.
- **macOS** — a `caffeinate -i -s -w <our pid>` child (no `-d`), which exits with us.
- **Linux** — a `gnome-session-inhibit` (GNOME) or `systemd-inhibit` child wrapping
  `cat` on a pipe we hold: if the tray dies, the pipe closes, `cat` exits and the
  inhibitor goes with it, so a crash can never leave the machine unable to sleep.

The lid is not inhibited on purpose, but on a Modern Standby (S0) laptop Windows itself
holds off standby for an outstanding power request after the lid closes: indefinitely
on AC power, up to five minutes on battery (Microsoft's "Prepare software for modern
standby"). So with the charger in, a closed lid may keep running.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time

from tintaview.core.proc import no_window

log = logging.getLogger(__name__)

REASON = "TintaView: Keep awake is on"

#: How long a freshly spawned inhibitor child gets to fail before it counts as holding.
_SPAWN_SETTLE_S = 0.3


class KeepAwake:
    """Holds (or not) one platform keep-awake request. Not thread-safe: the tray calls
    it from the GUI thread only."""

    def __init__(self) -> None:
        self._handle: int | None = None  # Windows power request
        self._child: subprocess.Popen[bytes] | None = None  # macOS / Linux inhibitor

    @property
    def active(self) -> bool:
        if self._child is not None and self._child.poll() is not None:
            log.warning("keep-awake inhibitor exited (code %s)", self._child.returncode)
            self._child = None
        return self._handle is not None or self._child is not None

    def set(self, on: bool) -> bool:
        """Acquire or release; returns whether the request is now held."""
        if on:
            return self.acquire()
        self.release()
        return False

    def acquire(self) -> bool:
        if self.active:
            return True
        try:
            if sys.platform == "win32":
                self._handle = _win_acquire()
            elif sys.platform == "darwin":
                self._child = _spawn(["caffeinate", "-i", "-s", "-w", str(os.getpid())])
            else:
                for cmd in _linux_commands():
                    self._child = _spawn(cmd)
                    if self._child is not None:
                        break
        except Exception:
            log.exception("could not acquire keep-awake")
        if self.active:
            log.info("keep-awake acquired")
            return True
        log.warning("keep-awake is not available on this system")
        return False

    def release(self) -> None:
        if self._handle is not None:
            handle, self._handle = self._handle, None
            try:
                _win_release(handle)
            except Exception:
                log.exception("could not release the keep-awake power request")
            log.info("keep-awake released")
        if self._child is not None:
            child, self._child = self._child, None
            try:
                if child.stdin is not None:
                    child.stdin.close()
                child.terminate()
                child.wait(timeout=2)
            except Exception:
                log.debug("keep-awake child did not exit cleanly", exc_info=True)
                child.kill()
            log.info("keep-awake released")


def _linux_commands() -> list[list[str]]:
    """Inhibitor commands to try, best first. Each wraps `cat` so the inhibitor ends
    when our end of its stdin pipe closes — see the module docstring."""
    commands = []
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    # GNOME's automatic suspend is driven by its own session manager, not by logind's
    # inhibitors, so on GNOME this is the one that stops it. `suspend` only — `idle`
    # would also stop the screen blanking, which is meant to keep working.
    if "GNOME" in desktop and shutil.which("gnome-session-inhibit"):
        commands.append(
            ["gnome-session-inhibit", "--inhibit", "suspend", "--reason", REASON, "cat"]
        )
    if shutil.which("systemd-inhibit"):
        commands.append([
            "systemd-inhibit", "--what=sleep", "--who=TintaView",
            f"--why={REASON}", "--mode=block", "cat",
        ])
    return commands


def _spawn(cmd: list[str]) -> subprocess.Popen[bytes] | None:
    """Start an inhibitor child; None if it is missing or exits straight away."""
    try:
        child = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **no_window(),
        )
    except OSError as exc:
        log.debug("keep-awake: %s unavailable: %s", cmd[0], exc)
        return None
    time.sleep(_SPAWN_SETTLE_S)
    if child.poll() is not None:
        log.debug("keep-awake: %s exited with %s", cmd[0], child.returncode)
        return None
    return child


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _POWER_REQUEST_CONTEXT_VERSION = 0
    _POWER_REQUEST_CONTEXT_SIMPLE_STRING = 0x1
    _PowerRequestSystemRequired = 1
    _PowerRequestExecutionRequired = 3  # Windows 8+; keeps us running under Modern Standby
    _INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    class _Detailed(ctypes.Structure):
        _fields_ = [
            ("LocalizedReasonModule", wintypes.HMODULE),
            ("LocalizedReasonId", wintypes.ULONG),
            ("ReasonStringCount", wintypes.ULONG),
            ("ReasonStrings", ctypes.POINTER(wintypes.LPWSTR)),
        ]

    class _Reason(ctypes.Union):
        _fields_ = [("Detailed", _Detailed), ("SimpleReasonString", wintypes.LPWSTR)]

    class _ReasonContext(ctypes.Structure):
        _fields_ = [("Version", wintypes.ULONG), ("Flags", wintypes.DWORD), ("Reason", _Reason)]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.PowerCreateRequest.argtypes = [ctypes.POINTER(_ReasonContext)]
    _kernel32.PowerCreateRequest.restype = wintypes.HANDLE
    _kernel32.PowerSetRequest.argtypes = [wintypes.HANDLE, ctypes.c_int]
    _kernel32.PowerSetRequest.restype = wintypes.BOOL
    _kernel32.PowerClearRequest.argtypes = [wintypes.HANDLE, ctypes.c_int]
    _kernel32.PowerClearRequest.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    #: Kept alive for as long as any request built from it might be read.
    _reason_buffer = ctypes.create_unicode_buffer(REASON)

    def _win_acquire() -> int | None:
        ctx = _ReasonContext()
        ctx.Version = _POWER_REQUEST_CONTEXT_VERSION
        ctx.Flags = _POWER_REQUEST_CONTEXT_SIMPLE_STRING
        ctx.Reason.SimpleReasonString = ctypes.cast(_reason_buffer, wintypes.LPWSTR)
        handle = _kernel32.PowerCreateRequest(ctypes.byref(ctx))
        if not handle or handle == _INVALID_HANDLE_VALUE:
            log.warning("PowerCreateRequest failed: %d", ctypes.get_last_error())
            return None
        # No display request, on purpose — the screen may turn off; see the docstring.
        if not _kernel32.PowerSetRequest(handle, _PowerRequestSystemRequired):
            log.warning("PowerSetRequest(system) failed: %d", ctypes.get_last_error())
            _kernel32.CloseHandle(handle)
            return None
        # Best effort: absent before Windows 8, and not needed for the system one to hold.
        if not _kernel32.PowerSetRequest(handle, _PowerRequestExecutionRequired):
            log.debug("PowerSetRequest(execution) failed: %d", ctypes.get_last_error())
        return int(handle)

    def _win_release(handle: int) -> None:
        for kind in (_PowerRequestSystemRequired, _PowerRequestExecutionRequired):
            _kernel32.PowerClearRequest(handle, kind)
        _kernel32.CloseHandle(handle)

else:

    def _win_acquire() -> int | None:
        raise OSError("not Windows")

    def _win_release(handle: int) -> None:
        raise OSError("not Windows")
