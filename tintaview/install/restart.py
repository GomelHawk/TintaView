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
_POLL = 0.2


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


def _stop(pid: int) -> bool:
    """Ask `pid` to exit, escalating to a kill. True once it is actually gone."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True  # already gone, which is the state we wanted
    except (OSError, PermissionError) as exc:
        log.warning("could not signal TintaView (pid %s): %s", pid, exc)
        return False

    deadline = time.monotonic() + _SHUTDOWN_GRACE
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)  # signal 0 only tests for existence
        except ProcessLookupError:
            return True
        except OSError:
            return True
        time.sleep(_POLL)

    # A windowed tray has no console to receive a Ctrl-C-like signal, and on Windows
    # SIGTERM is emulated rather than delivered, so a stuck process is normal enough to
    # plan for rather than treat as an error.
    log.info("TintaView (pid %s) did not exit in %.0fs — killing it", pid, _SHUTDOWN_GRACE)
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
        if not _stop(pid):
            return False
        return _launch()
    except Exception:  # a failed restart must never fail the wizard
        log.exception("restart after settings change failed")
        return False
