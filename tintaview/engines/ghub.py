"""Logitech G HUB engine — control via the LED Illumination SDK DLL G HUB installs.

G HUB ships `sdk_legacy_led_x64.dll`, the same interface games have always used to take
over lighting on Logitech G devices — and, unlike OpenRGB, it's designed to be driven
*while G HUB itself keeps running*: the "close the vendor app first" story that forces
itself onto the OpenRGB path (see engines/openrgb.py) simply doesn't apply here. Loading
the DLL with ctypes needs no new dependency and no bundled binary — it already sits on
the machine, put there by G HUB's own installer.

Hazards from the vendor SDK that drive this module's shape:

1. **`RestoreLighting` does not hand the mouse back.** Measured on live G HUB with the
   checkout verify: paint works; `LogiLedRestoreLighting` (and per-zone restore) leave
   the device on our last colour; only `LogiLedShutdown` returns control to G HUB's own
   profile. `LightController` opens/closes an engine on every session start/end, so
   `close()` must `Shutdown`, and the next `open()` must `Init` again (`InitWithName` +
   settle + retries). Deferring Shutdown to process exit left devices stuck until the
   tray quit.
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
   and the mouse matches the status now, not on the next event. The commit is posted
   (not waited on) and shares a coalesce key with paints, so a blink storm drops stale
   colours instead of queuing them.
4. **`probe()` must not call `LogiLedInit`.** Init registers us in G HUB's Integrations
   list; probing from `auto` mode used to pay that cost even when Chroma won.
   Reachability is "DLL on disk and G HUB not known-stopped".

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
#: `set_color` waits only for the real paint; the 1% commit is posted and may be
#: superseded by a newer colour. Each G HUB IPC can take hundreds of milliseconds.
_COLOR_TIMEOUT = 8.0
#: Win32 message-pump window on the SDK thread between the real RGB and the 1%
#: nudge. G HUB shows colour N-1 until the *next* lighting call on a later turn.
_FLUSH_GAP = 0.3
#: Coalesce key for colour paints on the pump. Lifecycle calls (init/save/restore/
#: shutdown) intentionally omit a key so a blink storm can never drop a restore.
_COLOR_KEY = "color"
#: Commit (1% nudge) jobs. A new *paint* drops pending commits; a new commit must
#: **not** drop a pending paint — otherwise a colour that arrived while we were still
#: painting gets cancelled by our own commit of the previous colour.
_COMMIT_KEY = "color_commit"
#: Consecutive `LogiLedSetLighting` failures before we surface a status_note. One
#: false is noise (G HUB briefly busy); three in a row from the blink loop is a real
#: "integration off / onboard memory / SDK dead" signal.
_SET_FAILURE_LIMIT = 3

#: Shown in G HUB's integrations list. `LogiLedInit()` alone registers the process as
#: `python.exe`, which G HUB typically leaves disabled for lighting, so Init succeeds
#: and every later SetLighting is a silent no-op.
_APP_NAME = b"TintaView"

#: Official LED SDK samples sleep a full second after init before any other call;
#: without a pause, the first SetLighting lands on a still-starting SDK and the mouse
#: stays dark until the *next* colour. Skipped when a test injects a fake DLL.
_INIT_SETTLE = 1.0
#: How many times to retry `LogiLedInitWithName` after a `close()`/`Shutdown`. Re-init
#: after Shutdown is the cost of handing the mouse back (see module docstring); a single
#: failure must not leave the next agent session dark for the whole cooldown.
_INIT_ATTEMPTS = 3
_INIT_RETRY_SLEEP = 0.5

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
    _bool("LogiLedSaveLightingForTargetZone", (ctypes.c_int, ctypes.c_int))
    _bool("LogiLedRestoreLightingForTargetZone", (ctypes.c_int, ctypes.c_int))
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


def _ensure_thread_message_queue() -> None:
    """Create a Win32 message queue on the *calling* thread if it has none yet.

    The LED SDK initialises per-thread and delivers on that thread's queue. The G HUB
    pump worker is a bare ``threading.Thread`` — under ``pythonw.exe`` it has never
    called User32, so it has no queue and ``LogiLedInitWithName`` hangs (or the
    process dies) before any paint. A console ``python.exe`` often appears to work
    without this because the process already touched User32. ``PeekMessage`` with
    ``PM_NOREMOVE`` is the usual force-create; it does not dispatch.
    """
    if sys.platform != "win32":
        return
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    msg = wintypes.MSG()
    pm_noremove = 0x0000
    user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, pm_noremove)


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

    __slots__ = ("done", "error", "fn", "key", "result", "started", "superseded")

    def __init__(self, fn, key: str | None = None) -> None:
        self.fn = fn
        self.key = key
        self.done = threading.Event()
        self.result = None
        self.error: Exception | None = None
        self.started = False
        self.superseded = False


class _CallPump:
    """Runs every SDK call on one persistent worker thread.

    See the module docstring's per-thread-initialisation hazard for why this exists at
    all — without it, `LogiLedInit` and a later `LogiLedSetLighting` could easily land
    on two different threads and the SDK would treat the second as never having
    initialised.

    Colour jobs share `_COLOR_KEY`: a newer paint drops any not-yet-started paint or
    commit with the same key, so a slow G HUB IPC under a blink storm shows the *current*
    status rather than a colour from several seconds ago. Lifecycle calls omit the key
    and are never dropped — collapsing a `RestoreLighting` into a blink would leave the
    user's profile unrestored on session end.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._pending: list[_Call] = []
        self._in_flight = False
        self._stopped = False
        self._thread: threading.Thread | None = None

    def _ensure_started_locked(self) -> None:
        """Start (or restart) the pump thread. Caller holds `_cond`."""
        self._stopped = False
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, daemon=True, name="tintaview-ghub")
            self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        """Retire the pump thread, waking it and joining briefly.

        The pump used to run until the process exited, so every `GHubEngine` ever built
        kept a `tintaview-ghub` thread alive — and the sidecar worker built a new engine
        on every `open`, leaking one per reconnect. `GHubEngine.close()` calls this; a
        later `open()` starts a fresh thread through `_ensure_started_locked` (the SDK
        is re-initialised there anyway, since `close()` had to `LogiLedShutdown`).
        """
        with self._cond:
            self._stopped = True
            thread = self._thread
            self._thread = None
            # Anything still queued can never run now; release its waiter rather than
            # leaving a `call()` parked until its timeout.
            for pending in self._pending:
                pending.superseded = True
                pending.done.set()
            self._pending = []
            self._cond.notify_all()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._pending and not self._stopped:
                    self._cond.wait()
                if self._stopped:
                    return
                call = self._pending.pop(0)
                call.started = True
                self._in_flight = True
            try:
                if call.superseded:
                    call.done.set()
                    continue
                try:
                    call.result = call.fn()
                except Exception as e:  # noqa: BLE001 - reported back to the caller
                    call.error = e
                call.done.set()
            finally:
                with self._cond:
                    self._in_flight = False
                    self._cond.notify_all()

    def wait_idle(self, timeout: float = _COLOR_TIMEOUT) -> bool:
        """Block until the queue is empty and no call is running. Used by tests."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._pending or self._in_flight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return True

    def drop_pending(self, keys: set[str]) -> None:
        """Supersede not-yet-started jobs whose key is in `keys`.

        `close()` uses this so a posted 1% commit cannot paint *after*
        `LogiLedRestoreLighting` and leave the mouse on our last status colour until
        process exit (when `atexit` finally calls `LogiLedShutdown`).
        """
        with self._cond:
            survivors: list[_Call] = []
            for old in self._pending:
                if old.key in keys and not old.started:
                    old.superseded = True
                    old.done.set()
                else:
                    survivors.append(old)
            self._pending = survivors
            self._cond.notify_all()

    def _enqueue(self, call: _Call) -> None:
        with self._cond:
            self._ensure_started_locked()
            if call.key is not None:
                # A new paint drops stale paints *and* stale commits; a new commit only
                # drops other commits — see `_COMMIT_KEY`.
                drop = (
                    {_COLOR_KEY, _COMMIT_KEY} if call.key == _COLOR_KEY else {call.key}
                )
                survivors: list[_Call] = []
                for old in self._pending:
                    if old.key in drop and not old.started:
                        old.superseded = True
                        old.done.set()
                    else:
                        survivors.append(old)
                self._pending = survivors
            self._pending.append(call)
            self._cond.notify()

    def post(self, fn, key: str | None = None) -> None:
        """Queue `fn` on the pump thread and return immediately.

        Used for the 1% commit pass whose result nobody waits on — and which a newer
        colour is free to supersede.
        """
        self._enqueue(_Call(fn, key=key))

    def call(self, fn, timeout: float = _CALL_TIMEOUT, key: str | None = None):
        """Run `fn` on the pump thread and return its result, or None on timeout/supersede.

        Re-raises whatever `fn` raised, on the calling thread, so callers keep their
        normal try/except shape instead of having to know the pump exists at all.
        """
        call = _Call(fn, key=key)
        self._enqueue(call)
        if not call.done.wait(timeout):
            log.debug("ghub: SDK call timed out after %.1fs", timeout)
            return None
        if call.superseded:
            return None
        if call.error is not None:
            raise call.error
        return call.result


def _save_lighting(dll) -> bool:
    """Snapshot global + mouse-zone lighting before we take over.

    We paint mice with `SetLightingForTargetZone`; a bare `SaveCurrentLighting` does
    not reliably cover those zones, so `RestoreLighting` alone leaves the mouse on our
    last colour until `LogiLedShutdown` at process exit.
    """
    ok = bool(dll.LogiLedSaveCurrentLighting())
    save_z = getattr(dll, "LogiLedSaveLightingForTargetZone", None)
    if callable(save_z):
        for device_type, zones in _ZONE_PAINT:
            for zone in zones:
                save_z(device_type, zone)
    return ok


def _restore_lighting(dll) -> bool:
    """Best-effort snapshot restore before `LogiLedShutdown`.

    Zone restores first, then global; message-pump + a second pass for the N-1 hazard.
    Measured on live G HUB: this alone does **not** return the mouse to G HUB's profile —
    `close()` must still `Shutdown`. Kept because it is cheap and may help keyboards.
    """
    restore_z = getattr(dll, "LogiLedRestoreLightingForTargetZone", None)

    def _zones() -> None:
        if not callable(restore_z):
            return
        for device_type, zones in _ZONE_PAINT:
            for zone in zones:
                restore_z(device_type, zone)

    _zones()
    ok = bool(dll.LogiLedRestoreLighting())
    if isinstance(dll, ctypes.CDLL):
        _drain_windows_messages(_FLUSH_GAP)
        _zones()
        dll.LogiLedRestoreLighting()
        _drain_windows_messages(_FLUSH_GAP)
    return ok


def _shutdown_sdk(dll) -> None:
    """Release SDK control so G HUB's own profile can drive the device again."""
    shutdown = getattr(dll, "LogiLedShutdown", None)
    if callable(shutdown):
        shutdown()
    if isinstance(dll, ctypes.CDLL):
        _drain_windows_messages(_FLUSH_GAP)


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
        # True between a successful Init and the matching Shutdown (close or atexit).
        self._initialized = False
        self._atexit_registered = False
        # True once open() has taken control; cleared in close(). Feeds `active`.
        self._saved = False
        # First SetLighting failure is worth an INFO line; the blink loop would
        # otherwise repeat it twice a second.
        self._logged_set_failure = False
        self._logged_setup_notes = False
        self._set_failures = 0
        # Surfaced via LightController.engine_status() → /state → tray. If G HUB
        # restarts under us the in-process session is orphaned — restart TintaView.
        self.status_note: str | None = None
        # Under pythonw the LED SDK silently no-ops; paint via a python.exe child.
        from .ghub_sidecar import should_use_ghub_sidecar

        self._use_sidecar = should_use_ghub_sidecar(dll_override=dll)
        self._sidecar = None

    @property
    def active(self) -> bool:
        if not self._saved:
            return False
        if self._use_sidecar:
            # A dead or discarded worker is not an open session. `_saved` alone used to
            # answer here, so once the sidecar went away the engine still reported
            # active, `LightController._ensure_open_locked` never called `open()` again,
            # and every later paint failed silently for the rest of the run.
            sc = self._sidecar
            return sc is not None and sc.alive
        return True

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
        """`LogiLedInitWithName` on the pump thread; allowed again after `close()`.

        `close()` must `Shutdown` to hand the mouse back (measured), so every later
        `open()` re-inits. Retries absorb the flaky post-Shutdown Init the old design
        tried to avoid by never shutting down mid-process.
        """
        if self._initialized:
            return True
        dll = self._ensure_dll()
        if dll is None:
            return False
        attempts = 1 if self._dll_override is not None else _INIT_ATTEMPTS
        ok = False
        for attempt in range(attempts):
            ok = bool(
                self._pump.call(
                    self._init_on_pump, timeout=_CALL_TIMEOUT + _INIT_SETTLE,
                )
            )
            if ok:
                break
            if attempt + 1 < attempts:
                time.sleep(_INIT_RETRY_SLEEP)
        if ok:
            self._initialized = True
            if not self._atexit_registered:
                atexit.register(self._shutdown_at_exit)
                self._atexit_registered = True
        return ok

    def _init_on_pump(self) -> bool:
        """Must run on the pump thread — see the per-thread-init hazard.

        Prefers `LogiLedInitWithName("TintaView")` so G HUB's integrations list shows
        a recognizable entry rather than `python.exe`. Older DLLs without that export
        fall back to `LogiLedInit`.
        """
        # Before any LogiLed* call: this worker thread needs a message queue under
        # pythonw or Init never returns (see `_ensure_thread_message_queue`).
        _ensure_thread_message_queue()
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
        """Last-resort release if the process exits while a session is still open."""
        if not self._initialized or self._dll is None:
            return
        try:
            self._pump.drop_pending({_COLOR_KEY, _COMMIT_KEY})
            self._pump.call(lambda: _shutdown_sdk(self._dll), timeout=1.0)
            self._initialized = False
        except Exception as e:
            log.debug("ghub: shutdown at exit failed: %r", e)

    # --- LightingEngine --------------------------------------------------------

    def probe(self) -> bool:
        """Reachability without taking control or touching the SDK.

        Used to call `LogiLedInitWithName`, which registered TintaView in G HUB's
        Integrations list even when `auto` mode then picked Chroma, and burned the
        one-shot init door on a probe that was never going to paint. Now: DLL on disk
        plus G HUB not known-stopped. Init moves entirely into `open()`.
        """
        if self.in_cooldown():
            return False
        if self._dll_override is not None:
            # Injected fakes have no process to look for; presence of the override is
            # itself the reachability signal tests rely on.
            return True
        if discover_dll_path(self._cfg) is None:
            return False
        from .ghub_env import ghub_running

        return ghub_running() is not False

    def open(self) -> bool:
        if self.in_cooldown():
            return False
        if self._use_sidecar:
            return self._open_sidecar()
        if not self._ensure_initialized():
            self.note_failure("Logitech LED SDK unavailable (G HUB not installed, or not running?)")
            return False

        dll = self._dll
        self._pump.call(lambda: dll.LogiLedSetTargetDevice(self._device_mask))
        saved = bool(self._pump.call(lambda: _save_lighting(dll)))
        if not saved:
            log.info("G HUB: could not save current lighting before taking over")
        self._saved = True
        self._set_failures = 0
        self.status_note = None
        self.clear_cooldown()
        log.info("G HUB session opened (%s)", self._resolved_path or "injected DLL")
        if self._dll_override is None and not self._logged_setup_notes:
            self._logged_setup_notes = True
            log.info("G HUB lighting checklist:\n%s", format_setup_notes())
        return True

    def _open_sidecar(self) -> bool:
        from .ghub_sidecar import GHubSidecar

        try:
            if self._sidecar is None:
                self._sidecar = GHubSidecar(self._cfg)
                if not self._atexit_registered:
                    atexit.register(self._stop_sidecar_at_exit)
                    self._atexit_registered = True
            ok = self._sidecar.open()
        except Exception as e:
            log.info("G HUB sidecar open failed: %r", e)
            self._saved = False
            self.note_failure(f"G HUB sidecar unavailable ({e!r})")
            return False
        if not ok:
            self._saved = False
            self.note_failure("Logitech LED SDK unavailable (G HUB not installed, or not running?)")
            return False
        self._saved = True
        self._set_failures = 0
        self.status_note = None
        self.clear_cooldown()
        log.info("G HUB session opened via python.exe sidecar")
        if not self._logged_setup_notes:
            self._logged_setup_notes = True
            log.info("G HUB lighting checklist:\n%s", format_setup_notes())
        return True

    def _stop_sidecar_at_exit(self) -> None:
        sc = self._sidecar
        self._sidecar = None
        if sc is not None:
            try:
                sc.stop()
            except Exception as e:
                log.debug("ghub sidecar atexit stop failed: %r", e)

    def set_color(self, r: int, g: int, b: int) -> None:
        """Set every targeted device to one solid colour. Never raises."""
        if not self._saved:
            return
        if self._use_sidecar:
            self._set_color_sidecar(r, g, b)
            return
        if self._dll is None:
            return
        dll = self._dll
        pct = tuple(round(v * 100 / 255) for v in (r, g, b))
        try:
            painted = self._pump.call(
                lambda: _paint(dll, pct), timeout=_COLOR_TIMEOUT, key=_COLOR_KEY,
            )
            # Only commit a colour that actually painted. A superseded/timed-out paint
            # must not post a nudge, and must not count as a SetLighting refusal.
            if painted is None:
                return
            self._pump.post(lambda: _commit_paint(dll, pct), key=_COMMIT_KEY)
            ok = bool(painted)
            log.debug("ghub set_color %s -> pct=%s ok=%s", (r, g, b), pct, ok)
            self._note_paint_result(ok)
        except Exception as e:
            log.debug("ghub set_color FAILED: %r", e)
            self._note_paint_result(False)

    def _set_color_sidecar(self, r: int, g: int, b: int) -> None:
        sc = self._sidecar
        if sc is None:
            return
        try:
            ok = sc.set_color(r, g, b)
            self._note_paint_result(ok)
        except Exception as e:
            log.debug("ghub sidecar set_color FAILED: %r", e)
            self.status_note = "G HUB lighting helper stopped responding — reconnecting"
            self._note_paint_result(False)

    def _note_paint_result(self, ok: bool) -> None:
        """Accumulate silent SetLighting failures into a tray-visible status_note.

        Do **not** respond to a dead SDK by calling `LogiLedInit` again — that is the
        re-init hazard this module exists to avoid. If G HUB restarted under us the only
        honest recovery is restarting TintaView.
        """
        if ok:
            self._set_failures = 0
            if self.status_note is not None:
                self.status_note = None
            return
        self._set_failures += 1
        if not self._logged_set_failure:
            self._logged_set_failure = True
            log.info(
                "G HUB SetLighting returned false — enable Settings > Game lighting "
                "control, allow TintaView under Integrations, and take the device "
                "off onboard memory mode"
            )
        if self._set_failures < _SET_FAILURE_LIMIT:
            return
        if self.status_note is not None:
            return  # already surfaced; don't re-extend the cooldown every blink tick
        # G HUB still in the process list while paints fail usually means the agent
        # restarted under us and our in-process SDK session is permanently orphaned.
        restarted = False
        if self._dll_override is None:
            try:
                from .ghub_env import ghub_running

                restarted = ghub_running() is True
            except Exception as e:
                log.debug("ghub: running check after paint failure failed: %r", e)
        if restarted:
            self.status_note = (
                "G HUB restarted; restart TintaView to reclaim lighting"
            )
        else:
            self.status_note = (
                "G HUB is ignoring lighting commands — check Integrations and "
                "Game lighting control"
            )
        self.note_failure(self.status_note)

    def close(self) -> None:
        """Hand lighting back to G HUB and tear down the SDK session.

        Measured: `RestoreLighting` (even per-zone) leaves the mouse on our last colour;
        only `LogiLedShutdown` returns G HUB's profile. So we restore best-effort, then
        always Shutdown, and clear `_initialized` so the next `open()` re-inits on a
        fresh pump thread.

        Drops pending colour commits first so a posted 1% nudge cannot run after restore,
        and always retires the pump thread on the way out: the SDK has to be
        re-initialised by the next `open()` regardless, and a thread that outlives its
        engine is a real leak in the sidecar worker, which built one engine per `open`.
        """
        try:
            self._close_session()
        finally:
            self._pump.stop()

    def _close_session(self) -> None:
        if not self._saved:
            return
        self._saved = False
        if self._use_sidecar:
            sc = self._sidecar
            if sc is not None:
                try:
                    sc.close()
                    log.info("G HUB session closed via sidecar (Shutdown — control returned to G HUB)")
                except Exception as e:
                    log.info("G HUB: sidecar close failed: %r", e)
            return
        if self._dll is None:
            return
        dll = self._dll
        try:
            self._pump.drop_pending({_COLOR_KEY, _COMMIT_KEY})
            self._pump.wait_idle(timeout=_COLOR_TIMEOUT)
            if self._cfg.restore_on_release:
                try:
                    self._pump.call(lambda: _restore_lighting(dll), timeout=_COLOR_TIMEOUT)
                except Exception as e:
                    log.info("G HUB: restore before shutdown failed: %r", e)
            self._pump.call(lambda: _shutdown_sdk(dll), timeout=_CALL_TIMEOUT)
            self._initialized = False
            log.info("G HUB session closed (Shutdown — control returned to G HUB)")
        except Exception as e:
            self._initialized = False
            log.info("G HUB: close failed: %r", e)
