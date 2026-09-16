"""The tray's background workers — everything `TrayApp` must not do on the GUI thread.

Each one is a `QObject` that runs its job on a daemon thread and hands the result back
through a signal, so the result arrives on the GUI thread by Qt's own queued-connection
rules and no widget is ever touched from a worker. What they have in common, and the
reason they live together rather than beside their callers, is `_GuardedWorker`: **one
run at a time**, later requests dropped rather than queued.

Split out of `tray.py`, which was three things in one file (these workers, the tray
itself, and the dialogs it opens). Nothing here imports the tray, so the dependency runs
one way only — a worker knows what it fetches, never who asked.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from typing import Any

from PySide6 import QtCore

from tintaview.core.config import Config
from tintaview.i18n import t

log = logging.getLogger(__name__)


class _GuardedWorker(QtCore.QObject):
    """Base for the tray's background workers: **one run at a time**, later requests
    dropped rather than queued.

    Everything below is triggered by something the user can repeat freely (the "Refresh
    usage" menu item, "Check for updates", a timer that also fires on demand) and each run
    is seconds of real I/O. Unguarded they stack: several concurrent Cursor RPCs against a
    ~300 MB `state.vscdb`, or — worse — two `doctor` runs whose process-global
    `redirect_stdout` unwinds in the wrong order and leaves `sys.stdout` pointing at a dead
    buffer for the rest of the process's life. A non-blocking lock makes a request that
    arrives mid-run a no-op, which is what "refresh" means to a user anyway.

    `_run()` is deliberately callable directly (the tests do): the lock lives in `_start`,
    not in the body, so a synchronous call is never asked to release a lock it never took.
    """

    #: Thread name for the default `fetch()` entry point.
    _thread_name = "tv-tray-worker"

    def __init__(self) -> None:
        super().__init__()
        self._inflight = threading.Lock()

    def _start(self, fn: Any, name: str) -> bool:
        """Run `fn` on a daemon thread unless one is already in flight. True if started."""
        if not self._inflight.acquire(blocking=False):
            return False

        def run() -> None:
            try:
                fn()
            finally:
                self._inflight.release()

        try:
            threading.Thread(target=run, daemon=True, name=name).start()
        except Exception:
            self._inflight.release()
            raise
        return True

    def fetch(self) -> None:
        self._start(self._run, self._thread_name)

    def _run(self) -> None:  # pragma: no cover - always overridden
        raise NotImplementedError


class StatsWorker(_GuardedWorker):
    """Runs `StatsService.fetch_all()` off the GUI thread — it's real network/disk
    I/O (Claude/Codex JSONL scans, a Cursor RPC call) and must never block painting.
    """

    results_ready = QtCore.Signal(dict)  # dict[str, UsageResult]
    _thread_name = "tv-tray-stats"

    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self._cfg = cfg
        self._svc: Any = None  # built lazily, off the GUI thread, on first use

    def _run(self) -> None:
        try:
            # Imported lazily: the stats layer (service.py/cache.py/providers/) may
            # still be under construction by another agent when this module loads,
            # and importing it eagerly would make that a hard dependency at import
            # time instead of at first use.
            from tintaview.stats.service import StatsService

            # Built here rather than in __init__ and, thanks to `_GuardedWorker`, only
            # ever by one thread at a time — two overlapping fetches used to be able to
            # construct (and cache-open) two services.
            if self._svc is None:
                self._svc = StatsService(self._cfg)
            results = self._svc.fetch_all()
        except Exception:
            # Never let a stats failure reach the GUI thread as a crash — the tray
            # just keeps showing whatever usage it already had.
            log.exception("stats fetch_all() failed - keeping last known usage")
            return
        self.results_ready.emit(results)


class StateWorker(_GuardedWorker):
    """HTTP fallback path only — see module docstring. Direct `state_payload()`
    reads happen straight on the GUI thread in `TrayApp._poll_state`, since that
    call is documented as an in-process lock + dict build, not I/O.
    """

    state_ready = QtCore.Signal(dict)
    _thread_name = "tv-tray-state-http"

    def __init__(self, server: Any) -> None:
        super().__init__()
        self._server = server

    def _run(self) -> None:
        import json
        import urllib.request

        try:
            with urllib.request.urlopen(f"{self._server.url}/state", timeout=2) as r:
                payload = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            log.warning("state poll (HTTP fallback) failed: %r", e)
            payload = {"effective": "none", "agents": {}, "count": 0}
        self.state_ready.emit(payload)


class UpdateCheckWorker(_GuardedWorker):
    """One-shot background check against the GitHub Releases API, off the GUI thread —
    same reasoning as `StatsWorker`: real network I/O must never block painting.

    Only emits when a strictly newer release actually exists; "up to date" and "the
    check failed" (no network, rate-limited, no releases yet) are silent, since this
    runs unattended on every start and neither is something the user needs to see.
    """

    update_available = QtCore.Signal(str, str)  # (latest_tag, current_version)
    _thread_name = "tv-tray-update-check"

    def _run(self) -> None:
        from tintaview import __version__

        try:
            from tintaview.install import update as update_mod
        except ImportError:
            return

        try:
            release = update_mod.latest_release()
            if release is None:
                return
            tag = str(release.get("tag_name") or "").lstrip("vV").strip()
            if not tag or update_mod.compare_versions(__version__, tag) >= 0:
                return
        except Exception:
            log.exception("startup update check failed")
            return
        self.update_available.emit(tag, __version__)


class DoctorWorker(_GuardedWorker):
    """Runs `tintaview doctor` off the GUI thread and hands back its report as text.

    `doctor` writes a human report to stdout and returns an exit code; a windowed build
    has no stdout anyone can read, so it is captured and shown in a dialog instead.
    `run_doctor` has no "write to this stream" parameter, so the capture stays
    `redirect_stdout` — which is process-global, and therefore only safe because
    `_GuardedWorker` serialises runs: two overlapping ones unwind their redirects in the
    wrong order and leave `sys.stdout` bound to a StringIO nobody reads again. It is worth
    it: the alternative is spawning a console the user has to keep open, on a platform
    where the tray runs as pythonw precisely so that no console ever appears.
    """

    report_ready = QtCore.Signal(str)
    _thread_name = "tv-tray-doctor"

    def _run(self) -> None:
        import io
        import traceback

        buffer = io.StringIO()
        try:
            from tintaview.install.doctor import run_doctor

            with contextlib.redirect_stdout(buffer):
                # interactive=False, not just paint=False: `doctor -v` also offers a
                # live hook test, and *both* prompts are unanswerable here. A windowed
                # process has no stdin at all (`sys.stdin` is None under pythonw), and a
                # tray started from a terminal has one the user cannot see — so this
                # would either raise or hang on "Running diagnostics…" forever.
                run_doctor(verbose=True, paint=False, interactive=False)
        except Exception:
            log.exception("doctor run failed")
            # Emit what actually went wrong, plus whatever the report managed before it
            # broke. A generic "couldn't run it" tells the user nothing and sends them
            # to a log they have to find first — which is the same problem the logs menu
            # item exists to solve.
            partial = buffer.getvalue().strip()
            detail = t("tray.diagnostics.crashed") + "\n\n" + traceback.format_exc()
            self.report_ready.emit(f"{partial}\n\n{detail}" if partial else detail)
            return
        self.report_ready.emit(buffer.getvalue().strip())


class ManualUpdateWorker(_GuardedWorker):
    """The "Check for updates" menu item's background half — the *check* and the
    *install*, both off the GUI thread.

    Neither is anything a GUI thread may run. `latest_release()` is an HTTPS call with a
    10 s timeout, and `run_update()` on Linux/macOS is a blocking `sh install.sh` that
    tears down and rebuilds the private venv — minutes, not seconds. Run inline they froze
    the tray icon, the flyout and every one of the broker's own Qt callbacks, with no
    window on screen to say why. (On Windows `run_update` detaches the installer and
    returns immediately; a thread is harmless there and keeps one code path.)

    The check and the install share one in-flight lock: they are two halves of the same
    user action, and starting a second check while an install is running makes no sense.
    """

    #: `outcome` values carried by `check_ready`. Deliberately not translated strings —
    #: the wording lives in the catalogue, this is the state the GUI slot switches on.
    OUTCOME_UNSUPPORTED = "unsupported"
    OUTCOME_FAILED = "failed"
    OUTCOME_CURRENT = "current"
    OUTCOME_AVAILABLE = "available"

    check_ready = QtCore.Signal(str, str, str)  # (outcome, latest_tag, release_notes)
    install_done = QtCore.Signal(int)  # run_update()'s exit code

    #: Release notes are the project's own published text, quoted as written (the rule
    #: every usage provider follows) — just not all of it in a message box.
    NOTES_LIMIT = 500

    def check(self) -> bool:
        """Start a check. False if a check or an install is already running."""
        return self._start(self._check, "tv-tray-update-manual")

    def install(self) -> bool:
        """Start the install. False if a check or an install is already running."""
        return self._start(self._install, "tv-tray-update-install")

    def _check(self) -> None:
        from tintaview import __version__

        try:
            from tintaview.install import update as update_mod
        except ImportError:
            self.check_ready.emit(self.OUTCOME_UNSUPPORTED, "", "")
            return

        try:
            release = update_mod.latest_release()
            if release is None:
                self.check_ready.emit(self.OUTCOME_FAILED, "", "")
                return
            tag = str(release.get("tag_name") or "").lstrip("vV").strip()
            if not tag or update_mod.compare_versions(__version__, tag) >= 0:
                self.check_ready.emit(self.OUTCOME_CURRENT, "", "")
                return
            notes = str(release.get("body") or "").strip()
        except Exception:
            log.exception("manual update check failed")
            self.check_ready.emit(self.OUTCOME_FAILED, "", "")
            return

        if len(notes) > self.NOTES_LIMIT:
            notes = notes[: self.NOTES_LIMIT].rstrip() + "…"
        self.check_ready.emit(self.OUTCOME_AVAILABLE, tag, notes)

    def _install(self) -> None:
        try:
            from tintaview.install import update as update_mod

            code = update_mod.run_update(check_only=False)
        except Exception:
            log.exception("update install failed")
            code = 1
        self.install_done.emit(int(code))


class HookCheckWorker(_GuardedWorker):
    """One hook check at startup, off the GUI thread — no timer, no menu item.

    Agents rewrite their config on upgrade, so hooks that were installed can disappear
    and TintaView simply stops hearing about sessions, which reads as "the lights are
    broken". The whole check is `install.wsl.missing_hooks`, the same WSL-aware resolution
    `doctor` uses: in a split install it compares against the distro's `tv-hook.sh` and
    reads the agent configs behind their UNC paths, because measured against the
    Windows-side paths every agent on a working machine is "stale". It runs off the GUI
    thread since that resolution can wait on `wsl.exe`.
    """

    #: Display names of the agents whose hooks need (re)installing. Never emitted when
    #: the state could not be determined (an unreachable distro): unknown is not missing.
    missing_ready = QtCore.Signal(list)
    _thread_name = "tv-tray-hooks"

    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self._cfg = cfg

    def _run(self) -> None:
        try:
            from tintaview.install import wsl
        except ImportError:
            return
        try:
            missing = wsl.missing_hooks(self._cfg)
        except Exception:
            log.exception("startup hook check failed")
            return
        if missing is not None:
            self.missing_ready.emit(missing)
