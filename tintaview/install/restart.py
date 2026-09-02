"""Restart a running TintaView so new settings take effect immediately.

The wizard writes `config.toml`, but a tray that is already running holds its config in
memory — colours, enabled agents, engine choice and poll intervals are all read at
startup. Without this, finishing the wizard appears to do nothing until the user works
out for themselves that they have to quit and relaunch, which is exactly the kind of
invisible step that makes software feel broken.

Restarting rather than live-reloading is deliberate: a reload would have to rebuild the
lighting engine, the stall detector, every timer and the tray icon itself, and get the
teardown right in each case. A relaunch is one code path that is always correct.

The instance is identified by the PID it reports on `/state`, not by scanning the process
list for something that looks like TintaView — on a developer's machine "a python process
running tintaview" can easily match a checkout, a test run, or a second install.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

from ..core.config import Config

log = logging.getLogger(__name__)

_PROBE_TIMEOUT = 2.0
_SHUTDOWN_GRACE = 8.0
#: How long to wait after SIGTERM before escalating. Shorter than the /quit grace on
#: purpose: a process that ignored a polite shutdown request is not going to tidy up.
_SIGNAL_GRACE = 3.0
_POLL = 0.2

#: Windows `OpenProcess` access right + the "not finished yet" exit code, so liveness can
#: be probed without touching the process. See `_is_alive`.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


def running_pid(cfg: Config) -> int | None:
    """PID of the TintaView serving this config's port, or None if nothing is."""
    url = f"http://{cfg.server.host}:{cfg.server.port}/state"
    try:
        with urllib.request.urlopen(url, timeout=_PROBE_TIMEOUT) as resp:
            payload = json.loads(resp.read())
    except (urllib.error.URLError, OSError, TimeoutError, ValueError,
            json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    pid = payload.get("pid")
    # A version predating the `pid` field, or something else on the port entirely.
    return pid if isinstance(pid, int) and pid > 0 else None


def _request_quit(cfg: Config) -> bool:
    """Ask the broker to shut down gracefully. True if it accepted.

    This is the only shutdown that closes the lighting engine, so it is the only one that
    hands the rig back: OpenRGB restores its snapshot, G HUB gets `LogiLedShutdown`,
    Chroma gets its DELETE. A signal skips all of that and leaves the devices stuck on
    whatever colour TintaView last painted.

    False for anything at all — connection refused, a timeout, or an older build that has
    no `/quit` endpoint and answers 404 — and the caller escalates from there.
    """
    url = f"http://{cfg.server.host}:{cfg.server.port}/quit"
    try:
        with urllib.request.urlopen(url, timeout=_PROBE_TIMEOUT) as resp:
            return 200 <= int(getattr(resp, "status", 0) or 0) < 300
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return False


def _is_alive(pid: int) -> bool:
    """Is `pid` still running?

    **Never `os.kill(pid, 0)` on Windows.** There are no signals there: CPython emulates
    `os.kill` with `TerminateProcess`, so the "harmless existence probe" every POSIX
    example uses is itself a kill — this loop used to shoot the tray in the head once per
    poll instead of waiting for it to finish restoring the lights. `OpenProcess` +
    `GetExitCodeProcess` is the read-only equivalent.
    """
    if sys.platform == "win32":
        return _windows_is_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists; we just may not signal it
    except OSError:
        return False
    return True


def _windows_is_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # Declared explicitly: without a HANDLE restype ctypes assumes a C int and truncates
    # the handle on 64-bit, so CloseHandle would be handed a bogus value.
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False  # gone, or not ours to look at — either way we can't wait on it
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _wait_for_exit(pid: int, grace: float) -> bool:
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _is_alive(pid):
            return True
        time.sleep(_POLL)
    return not _is_alive(pid)


def _stop(pid: int, cfg: Config | None = None) -> bool:
    """Ask `pid` to exit, escalating to a kill. True once it is actually gone.

    Three steps, in this order and for this reason:

    1. `GET /quit`, when we know where the broker is. It is the only exit that closes the
       lighting engine, so it is the only one that gives the user their own lighting back.
    2. Wait out the grace period — closing an engine means real device I/O (an OpenRGB
       per-LED restore over a socket, a G HUB SDK shutdown on its own worker thread).
    3. Only then signal, and only then escalate to SIGKILL. A windowed tray has no console
       to receive a Ctrl-C-like signal and on Windows SIGTERM is emulated rather than
       delivered, so a stuck process is normal enough to plan for rather than treat as
       an error.
    """
    if not _is_alive(pid):
        return True  # already gone, which is the state we wanted

    if cfg is not None and _request_quit(cfg) and _wait_for_exit(pid, _SHUTDOWN_GRACE):
        return True

    log.info("TintaView (pid %s) did not shut down on request — signalling it", pid)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except (OSError, PermissionError) as exc:
        log.warning("could not signal TintaView (pid %s): %s", pid, exc)
        return False

    if _wait_for_exit(pid, _SIGNAL_GRACE):
        return True

    log.info("TintaView (pid %s) did not exit in %.0fs — killing it", pid, _SIGNAL_GRACE)
    try:
        os.kill(pid, signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return True
    time.sleep(_POLL)
    return True


def _launch() -> bool:
    """Start TintaView detached, the same way autostart would."""
    from .autostart import _executable_command

    command = _executable_command()
    kwargs: dict[str, object] = {}
    if sys.platform == "win32":
        # DETACHED_PROCESS: the new tray must outlive the wizard that spawned it, and
        # must not inherit its console window.
        kwargs["creationflags"] = 0x00000008 | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(command, **kwargs)  # type: ignore[arg-type]
    except OSError as exc:
        log.warning("could not relaunch TintaView: %s", exc)
        return False
    return True


def restart_if_running(cfg: Config) -> bool:
    """Restart TintaView if it is running, so `cfg` takes effect. Never raises.

    Returns True only when an instance was actually stopped and relaunched. False means
    there was nothing to restart — the normal case during a fresh install, where the
    installer starts the tray itself once the wizard finishes.
    """
    try:
        pid = running_pid(cfg)
        if pid is None:
            return False
        if pid == os.getpid():
            # `tintaview setup --reconfigure` invoked inside the running tray's own
            # process would be asking us to kill ourselves mid-wizard.
            log.debug("skipping restart: this process is the running instance")
            return False
        if not _stop(pid, cfg):
            return False
        return _launch()
    except Exception:  # a failed restart must never fail the wizard
        log.exception("restart after settings change failed")
        return False
