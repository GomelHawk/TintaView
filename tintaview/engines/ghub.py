"""Logitech G HUB engine — control via the LED Illumination SDK DLL G HUB installs.

G HUB ships `sdk_legacy_led_x64.dll`, the same interface games have always used to take
over lighting on Logitech G devices — and, unlike OpenRGB, it's designed to be driven
*while G HUB itself keeps running*: the "close the vendor app first" story that forces
itself onto the OpenRGB path (see engines/openrgb.py) simply doesn't apply here. Loading
the DLL with ctypes needs no new dependency and no bundled binary — it already sits on
the machine, put there by G HUB's own installer.

Two hazards from the vendor SDK drive this module's shape:

1. **Re-initialising after shutdown is unreliable.** Calling `LogiLedInit` again after a
   prior `LogiLedShutdown`, in the same process, is documented in the wild to misbehave —
   Logitech's own SDK guidance is to not shut down until you are completely finished with
   the hardware. But `LightController` opens and closes an engine on every session
   start/end, potentially many times over one process's lifetime. So `close()` here only
   ever restores the saved lighting; the one real `LogiLedShutdown` call is deferred to
   process exit via `atexit`.
2. **The SDK initialises per calling thread**, not per process ("initializes the sdk for
   the current thread", per Logitech's own docs). Our calls would otherwise arrive from
   whichever `ThreadingHTTPServer` handler thread served the hook, plus the independent
   blink and heartbeat threads — never reliably the same thread twice. So every SDK call
   is funnelled through one dedicated worker thread (`_CallPump` below), started lazily
   and never restarted, so `LogiLedInit` and everything after it always run on the same
   OS thread.

The SDK also has no concept of addressing a single device (mouse vs. keyboard) — only a
capability bitmask (`LOGI_DEVICETYPE_*`), so unlike OpenRGB's `device_types` this targets
classes of lighting, not device instances, and a solid colour lands on every matching
device at once. Colour arguments are **0-100 percentages**, not 0-255.
"""

from __future__ import annotations

import atexit
import ctypes
import logging
import os
import queue
import shutil
import sys
import threading
from pathlib import Path

from ..core.config import GHubConfig, expand
from .base import BaseEngine

log = logging.getLogger(__name__)

#: Registered by some (not all) G HUB/LGS installers so games can find the DLL without
#: knowing the install path — see henninglive/logitech-led and VRocker/LogiLed2Corsair.
#: Best-effort: absence is normal, not a sign anything is broken.
_LED_SDK_CLSID_KEY = r"SOFTWARE\Classes\CLSID\{a6519e67-7632-4375-afdf-caa889744403}\ServerBinary"

#: SDK capability bitmask (`LOGI_DEVICETYPE_*` in the C header) — see the module
#: docstring for why this isn't the same shape as OpenRGB's `device_types`.
_DEVICE_TYPE_BITS = {"monochrome": 1 << 0, "rgb": 1 << 1, "perkey": 1 << 2}
_DEVICE_TYPE_ALL = sum(_DEVICE_TYPE_BITS.values())

#: How long to wait for a reply from the SDK call pump before giving up. Generous
#: compared to Chroma's HTTP timeouts because LogiLedInit can briefly block while G HUB
#: itself is still starting up.
_CALL_TIMEOUT = 3.0


def _device_mask(names: list[str]) -> int:
    mask = 0
    for name in names:
        bit = _DEVICE_TYPE_BITS.get(name.lower())
        if bit is None:
            log.info("G HUB: unknown device type %r in config, ignoring", name)
            continue
        mask |= bit
    return mask or _DEVICE_TYPE_ALL  # an empty/unrecognised list must still light something


def _dll_path_from_registry() -> Path | None:
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _LED_SDK_CLSID_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, "")
    except OSError:
        return None
    return Path(value) if value else None


def discover_dll_path(cfg: GHubConfig) -> Path | None:
    """Locate the LED Illumination SDK DLL, or None if it can't be found anywhere.

    Checked in order: an explicit `cfg.dll_path` override, G HUB's current install
    location, the registry CLSID some installers register for games that look it up
    that way, the older Logitech Gaming Software path (pre-G-HUB installs), and finally
    a bare `LogitechLed.dll` on PATH. Exposed at module level — not just used inside the
    engine — so `doctor` and the wizard can report the exact path, or its absence,
    without instantiating (and thereby initialising) an engine.
    """
    if cfg.dll_path:
        path = expand(cfg.dll_path)
        return path if path.is_file() else None
    if sys.platform != "win32":
        return None

    bitness = "x64" if sys.maxsize > 2**32 else "x86"
    # Windows env var lookups are case-insensitive, so the all-caps spelling below finds
    # the same "ProgramFiles"/"ProgramW6432" values Windows itself documents them as.
    program_dirs = [
        d for d in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMW6432")) if d
    ]

    for base in program_dirs:
        candidate = Path(base) / "LGHUB" / f"sdk_legacy_led_{bitness}.dll"
        if candidate.is_file():
            return candidate

    registry_path = _dll_path_from_registry()
    if registry_path is not None and registry_path.is_file():
        return registry_path

    for base in program_dirs:
        candidate = (
            Path(base) / "Logitech Gaming Software" / "SDK" / "LED" / bitness / "LogitechLed.dll"
        )
        if candidate.is_file():
            return candidate

    found = shutil.which("LogitechLed.dll")
    return Path(found) if found else None


class _Call:
    """One queued SDK call: the pump thread runs `fn` and reports back through `done`."""

    __slots__ = ("done", "error", "fn", "result")

    def __init__(self, fn) -> None:
        self.fn = fn
        self.done = threading.Event()
        self.result = None
        self.error: Exception | None = None


class _CallPump:
    """Runs every SDK call on one persistent worker thread.

    See the module docstring's per-thread-initialisation hazard for why this exists at
    all — without it, `LogiLedInit` and a later `LogiLedSetLighting` could easily land
    on two different threads and the SDK would treat the second as never having
    initialised.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue[_Call] = queue.Queue()
        self._thread: threading.Thread | None = None

    def _ensure_started(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, daemon=True, name="tintaview-ghub")
            self._thread.start()

    def _run(self) -> None:
        while True:
            call = self._queue.get()
            try:
                call.result = call.fn()
            except Exception as e:  # noqa: BLE001 - reported back to the caller, not swallowed
                call.error = e
            call.done.set()

    def call(self, fn, timeout: float = _CALL_TIMEOUT):
        """Run `fn` on the pump thread and return its result, or None on timeout.

        Re-raises whatever `fn` raised, on the calling thread, so callers keep their
        normal try/except shape instead of having to know the pump exists at all.
        """
        self._ensure_started()
        call = _Call(fn)
        self._queue.put(call)
        if not call.done.wait(timeout):
            log.debug("ghub: SDK call timed out after %.1fs", timeout)
            return None
        if call.error is not None:
            raise call.error
        return call.result


class GHubEngine(BaseEngine):
    """Drives Logitech G devices through G HUB's LED Illumination SDK.

    Percentage colour scale (0-100, not 0-255) — see the module docstring. `dll` is a
    test-injection point (mirrors `ChromaEngine`'s `url=` parameter): when set, discovery
    and `ctypes.WinDLL` loading are skipped entirely and every call goes straight to the
    given object, so the full lifecycle is exercisable on Linux/macOS CI with no real
    G HUB installed.
    """

    name = "ghub"
    display_name = "Logitech G HUB"

    def __init__(self, cfg: GHubConfig | None = None, dll=None) -> None:
        super().__init__()
        self._cfg = cfg or GHubConfig()
        self._dll_override = dll
        self._dll = None
        self._resolved_path: Path | None = None
        self._pump = _CallPump()
        self._device_mask = _device_mask(self._cfg.device_types)
        # LogiLedInit is a one-way door for this process — see the module docstring.
        self._initialized = False
        # True once LogiLedSaveCurrentLighting has succeeded and needs a matching
        # restore; doubles as "are we in control" for `active`.
        self._saved = False

    @property
    def active(self) -> bool:
        return self._saved

    # --- DLL / init lifecycle ------------------------------------------------

    def _ensure_dll(self):
        if self._dll is not None:
            return self._dll
        if self._dll_override is not None:
            self._dll = self._dll_override
            return self._dll
        if sys.platform != "win32":
            return None
        path = discover_dll_path(self._cfg)
        if path is None:
            return None
        try:
            self._dll = ctypes.WinDLL(str(path))
        except OSError as e:
            log.debug("ghub: failed to load %s: %r", path, e)
            return None
        self._resolved_path = path
        return self._dll

    def _ensure_initialized(self) -> bool:
        """Call `LogiLedInit` exactly once for the life of this process.

        Never re-initialises after a `close()` — see the module docstring's re-init
        hazard. `atexit` registers the real `LogiLedShutdown` on the first successful
        init, so the SDK still gets released cleanly when the process actually exits.
        """
        if self._initialized:
            return True
        dll = self._ensure_dll()
        if dll is None:
            return False
        ok = bool(self._pump.call(lambda: dll.LogiLedInit()))
        if ok:
            self._initialized = True
            atexit.register(self._shutdown_at_exit)
        return ok

    def _shutdown_at_exit(self) -> None:
        dll = self._dll
        if dll is None:
            return
        try:
            self._pump.call(lambda: dll.LogiLedShutdown(), timeout=1.0)
        except Exception as e:
            log.debug("ghub: shutdown at exit failed: %r", e)

    # --- LightingEngine --------------------------------------------------------

    def probe(self) -> bool:
        """Reachability check that never releases anything — see the module docstring
        for why `LogiLedInit` itself can't be un-done, only never attempted again once
        it fails within this process's cooldown window.
        """
        if self.in_cooldown():
            return False
        return self._ensure_initialized()

    def open(self) -> bool:
        if self.in_cooldown():
            return False
        if not self._ensure_initialized():
            self.note_failure("Logitech LED SDK unavailable (G HUB not installed, or not running?)")
            return False

        dll = self._dll
        self._pump.call(lambda: dll.LogiLedSetTargetDevice(self._device_mask))
        saved = bool(self._pump.call(lambda: dll.LogiLedSaveCurrentLighting()))
        if not saved:
            log.info("G HUB: could not save current lighting before taking over")
        self._saved = True
        self.clear_cooldown()
        log.info("G HUB session opened (%s)", self._resolved_path or "injected DLL")
        return True

    def set_color(self, r: int, g: int, b: int) -> None:
        """Set every targeted device to one solid colour. Never raises."""
        if not self._saved or self._dll is None:
            return
        dll = self._dll
        pct = tuple(round(v * 100 / 255) for v in (r, g, b))
        try:
            self._pump.call(lambda: dll.LogiLedSetLighting(*pct))
            # DEBUG, not INFO: the blink loop alone calls this twice a second.
            log.debug("ghub set_color %s -> pct=%s", (r, g, b), pct)
        except Exception as e:
            log.debug("ghub set_color FAILED: %r", e)

    def close(self) -> None:
        """Restore the lighting G HUB had before `open()`.

        Deliberately does **not** call `LogiLedShutdown` — see the module docstring's
        re-init hazard. The SDK stays initialised for the rest of the process, so the
        next `open()` costs a save/restore round trip, not a re-init.
        """
        if not self._saved:
            return
        self._saved = False
        if self._dll is None or not self._cfg.restore_on_release:
            return
        dll = self._dll
        try:
            self._pump.call(lambda: dll.LogiLedRestoreLighting())
            log.info("G HUB lighting restored")
        except Exception as e:
            # Best-effort by contract: a restore that fails must not stop the rest of
            # teardown from happening.
            log.info("G HUB: restore failed: %r", e)
