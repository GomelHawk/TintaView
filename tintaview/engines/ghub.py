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
3. **G HUB shows colour N-1 until the next lighting call on a later turn of that
   thread.** A sleep after `set_color` returns does nothing, and a second `SetLighting`
   in the same burst is coalesced. `set_color` therefore paints, then on a second pump
   job pumps Win32 messages (`PeekMessage`) and paints a 1% nudge so `pct` becomes N-1
   and the mouse matches the status now, not on the next event.

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
import time
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
#: `set_color` does two pump turns (paint + 1% commit). Each G HUB IPC can take
#: hundreds of milliseconds; 8 s is enough for both without returning mid-paint.
_COLOR_TIMEOUT = 8.0
#: Win32 message-pump window on the SDK thread between the real RGB and the 1%
#: nudge. G HUB shows colour N-1 until the *next* lighting call on a later turn.
_FLUSH_GAP = 0.3

#: Shown in G HUB's integrations list. `LogiLedInit()` alone registers the process as
#: `python.exe`, which G HUB typically leaves disabled for lighting, so Init succeeds
#: and every later SetLighting is a silent no-op.
_APP_NAME = b"TintaView"

#: Official LED SDK samples sleep a full second after init before any other call;
#: without a pause, the first SetLighting lands on a still-starting SDK and the mouse
#: stays dark until the *next* colour. Skipped when a test injects a fake DLL.
_INIT_SETTLE = 1.0

#: `LogiLed::DeviceType` values for `LogiLedSetLightingForTargetZone`. Mice are zoned:
#: `SetLighting` can return true while leaving them dark (G102/G502 Lightsync). Zone 1
#: is the Lightsync sample's mouse slot; 0 is the logo on most G mice. Headset zones
#: were dropped from the hot path — each extra IPC sits in G HUB's queue for seconds
#: and the mouse shows the *previous* colour until that queue drains.
_ZONE_MOUSE = 0x3
_ZONE_PAINT = ((_ZONE_MOUSE, (0, 1)),)

#: Probe success does not mean G HUB will paint — new integrations default to off,
#: onboard memory ignores the SDK, and Windows Dynamic Lighting fights for the same
#: LEDs. Printed whenever the user pins this engine, not only when a probe fails.
GHUB_TURN_ON = (
    "G HUB itself — leave it running (do not close it)",
    "Settings > Allow games and applications to control illumination "
    '("Game lighting control")',
    "Integrations (or Games) > TintaView — enable lighting. Older builds list "
    "python.exe / pythonw.exe; the Add game screen is the wrong place",
    "Each Logitech device on a G HUB (automatic) profile, not onboard memory",
)
GHUB_TURN_OFF = (
    "Onboard memory mode on the mouse or keyboard — it ignores the SDK",
    "Windows 11 Dynamic Lighting (Settings > Personalization > Dynamic lighting)",
    "OpenRGB, if it is installed — it fights G HUB for the same LEDs",
)


def format_setup_notes(indent: str = "") -> str:
    """ON/OFF checklist for G HUB, one block the wizard, doctor and logs can share."""
    on = "\n".join(f"{indent}- {item}" for item in GHUB_TURN_ON)
    off = "\n".join(f"{indent}- {item}" for item in GHUB_TURN_OFF)
    return (
        f"{indent}In Logitech G HUB, turn these ON:\n{on}\n"
        f"{indent}Turn these OFF:\n{off}"
    )


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
    location (`LGHUB\\sdks\\` first, then the older `LGHUB\\` root), the registry CLSID
    some installers register for games that look it up that way, the older Logitech
    Gaming Software path (pre-G-HUB installs), and finally a bare `LogitechLed.dll` on
    PATH. Exposed at module level — not just used inside the engine — so `doctor` and
    the wizard can report the exact path, or its absence, without instantiating (and
    thereby initialising) an engine.
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
        lghub = Path(base) / "LGHUB"
        # Current G HUB ships the DLL under sdks\; older installs put it in the LGHUB
        # root. sdks first, so a leftover root copy from a previous version doesn't win.
        for candidate in (
            lghub / "sdks" / f"sdk_legacy_led_{bitness}.dll",
            lghub / f"sdk_legacy_led_{bitness}.dll",
        ):
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


def _bind_signatures(dll) -> None:
    """Pin restype/argtypes so ctypes doesn't read a C++ bool as a 4-byte c_int.

    A leftover high byte in EAX makes a failed `LogiLedInit` look like success, which
    is exactly "session opened, lights never move".
    """

    def _bool(name: str, argtypes: tuple[type, ...] = ()) -> None:
        func = getattr(dll, name, None)
        if func is None:
            return
        func.restype = ctypes.c_bool
        if argtypes:
            func.argtypes = list(argtypes)

    _bool("LogiLedInit")
    _bool("LogiLedInitWithName", (ctypes.c_char_p,))
    _bool("LogiLedSetTargetDevice", (ctypes.c_int,))
    _bool("LogiLedSaveCurrentLighting")
    _bool("LogiLedRestoreLighting")
    _bool("LogiLedSetLighting", (ctypes.c_int, ctypes.c_int, ctypes.c_int))
    _bool(
        "LogiLedSetLightingForTargetZone",
        (ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int),
    )
    shutdown = getattr(dll, "LogiLedShutdown", None)
    if shutdown is not None:
        shutdown.restype = None  # void in the header; a bool restype would be a lie


def _nudge_pct(pct: tuple[int, int, int]) -> tuple[int, int, int]:
    """1% off `pct` — invisible on a mouse, different enough to be a new lighting call."""
    r, g, b = pct
    if r:
        return (r - 1, g, b)
    if g:
        return (r, g - 1, b)
    if b:
        return (r, g, b - 1)
    return (1, 0, 0)


def _drain_windows_messages(seconds: float) -> None:
    """Run the calling thread's Win32 queue. The LED SDK delivers on this thread.

    `time.sleep` does not pump messages, so G HUB would sit on the previous colour
    until the next `set_color` entered the DLL — exactly the one-behind mouse.
    """
    if sys.platform != "win32":
        return
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.PeekMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG), wintypes.HWND,
        wintypes.UINT, wintypes.UINT, wintypes.UINT,
    ]
    user32.PeekMessageW.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    msg = wintypes.MSG()
    pm_remove = 0x0001
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, pm_remove):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.01)


def _paint(dll, pct: tuple[int, int, int]) -> bool:
    """One SetLighting plus mouse zones. No flush — the caller does a second turn."""
    ok = bool(dll.LogiLedSetLighting(*pct))
    paint = getattr(dll, "LogiLedSetLightingForTargetZone", None)
    if paint is not None:
        for device_type, zones in _ZONE_PAINT:
            for zone in zones:
                paint(device_type, zone, *pct)
    return ok


def _commit_paint(dll, pct: tuple[int, int, int]) -> bool:
    """Second pump-thread turn: message-pump, then 1% nudge so G HUB commits `pct`."""
    if isinstance(dll, ctypes.CDLL):
        _drain_windows_messages(_FLUSH_GAP)
    return _paint(dll, _nudge_pct(pct))


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
    and `ctypes.CDLL` loading are skipped entirely and every call goes straight to the
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
        # First SetLighting failure is worth an INFO line; the blink loop would
        # otherwise repeat it twice a second.
        self._logged_set_failure = False
        self._logged_setup_notes = False

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
            # cdecl, matching Logitech's own logiPy and Aurora. WinDLL (stdcall) is the
            # same ABI on x64, so a 64-bit machine wouldn't show the bug — a 32-bit
            # Python would silently mis-call every function with arguments.
            self._dll = ctypes.CDLL(str(path))
        except OSError as e:
            log.debug("ghub: failed to load %s: %r", path, e)
            return None
        _bind_signatures(self._dll)
        self._resolved_path = path
        return self._dll

    def _ensure_initialized(self) -> bool:
        """Call `LogiLedInitWithName` (falling back to `LogiLedInit`) once per process.

        Never re-initialises after a `close()` — see the module docstring's re-init
        hazard. `atexit` registers the real `LogiLedShutdown` on the first successful
        init, so the SDK still gets released cleanly when the process actually exits.
        """
        if self._initialized:
            return True
        dll = self._ensure_dll()
        if dll is None:
            return False
        ok = bool(self._pump.call(self._init_on_pump, timeout=_CALL_TIMEOUT + _INIT_SETTLE))
        if ok:
            self._initialized = True
            atexit.register(self._shutdown_at_exit)
        return ok

    def _init_on_pump(self) -> bool:
        """Must run on the pump thread — see the per-thread-init hazard.

        Prefers `LogiLedInitWithName("TintaView")` so G HUB's integrations list shows
        a recognizable entry rather than `python.exe`. Older DLLs without that export
        fall back to `LogiLedInit`.
        """
        dll = self._dll
        init_with_name = getattr(dll, "LogiLedInitWithName", None)
        if callable(init_with_name):
            ok = bool(init_with_name(_APP_NAME))
        else:
            ok = bool(dll.LogiLedInit())
        if ok and self._dll_override is None:
            time.sleep(_INIT_SETTLE)
            _drain_windows_messages(_FLUSH_GAP)
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
        if self._dll_override is None and not self._logged_setup_notes:
            self._logged_setup_notes = True
            log.info("G HUB lighting checklist:\n%s", format_setup_notes())
        return True

    def set_color(self, r: int, g: int, b: int) -> None:
        """Set every targeted device to one solid colour. Never raises."""
        if not self._saved or self._dll is None:
            return
        dll = self._dll
        pct = tuple(round(v * 100 / 255) for v in (r, g, b))
        try:
            painted = self._pump.call(lambda: _paint(dll, pct), timeout=_COLOR_TIMEOUT)
            # Second turn of the pump thread: G HUB shows colour N-1 until a later
            # lighting call on a later turn. A 1% nudge commits `pct`. Win32 messages
            # are pumped on that job — sleep without PeekMessage does not.
            self._pump.call(lambda: _commit_paint(dll, pct), timeout=_COLOR_TIMEOUT)
            ok = bool(painted)
            log.debug("ghub set_color %s -> pct=%s ok=%s", (r, g, b), pct, ok)
            if not ok and not self._logged_set_failure:
                self._logged_set_failure = True
                log.info(
                    "G HUB SetLighting returned false — enable Settings > Game lighting "
                    "control, allow TintaView under Integrations, and take the device "
                    "off onboard memory mode"
                )
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
