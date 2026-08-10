"""The system-tray front end: the icon, its blink loop, the context menu and the
usage flyout. `run_tray(cfg, server)` is `cli.py`'s entry point for the GUI path
(see `_cmd_run`) — `server` is an already-started `StatusServer`.

Ported from claude_code_razer_lights/tray_app.py's `RazerTray`. The one structural
change from that predecessor: its light server ran in a *separate process*, so the
tray had to poll `/state` over HTTP. TintaView runs the broker in the same process
(docs/PLAN.md §2.1), so `TrayApp` reads `server.state_payload()` directly — a plain
in-process dict build under a lock, not I/O — and only falls back to HTTP if that
method isn't there at all (e.g. some other object standing in for a real server).
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from typing import TYPE_CHECKING, Any

from PySide6 import QtCore, QtGui, QtWidgets

from tintaview.core.config import Config
from tintaview.ui import icons
from tintaview.ui.flyout import Flyout

if TYPE_CHECKING:  # pragma: no cover - types only
    from tintaview.stats.model import UsageResult

log = logging.getLogger(__name__)

STATE_POLL_MS = 1500
USAGE_MIN_REFRESH_S = 30.0  # ignore flyout-open refreshes more frequent than this
CLICK_REOPEN_GUARD_S = 0.25  # the predecessor's fix for "the click that just closed it"
ICON_SIZE = 128

_STATUS_LABELS = {
    "idle": "idle",
    "working": "working",
    "confirm": "needs confirmation",
    "none": "no session",
}


def _dim(rgb: tuple[int, int, int], factor: float = 0.3) -> tuple[int, int, int]:
    """A darkened variant of `rgb`, used for the "off" half of the confirm blink.

    Derived from `cfg.colors.confirm` rather than a hardcoded dim colour — icon
    colours must come from config, not be baked into this module.
    """
    return tuple(max(0, min(255, int(c * factor))) for c in rgb)  # type: ignore[return-value]


class StatsWorker(QtCore.QObject):
    """Runs `StatsService.fetch_all()` off the GUI thread — it's real network/disk
    I/O (Claude/Codex JSONL scans, a Cursor RPC call) and must never block painting.
    """

    results_ready = QtCore.Signal(dict)  # dict[str, UsageResult]

    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self._cfg = cfg
        self._svc: Any = None  # built lazily, off the GUI thread, on first use

    def fetch(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="tv-tray-stats").start()

    def _run(self) -> None:
        try:
            # Imported lazily: the stats layer (service.py/cache.py/providers/) may
            # still be under construction by another agent when this module loads,
            # and importing it eagerly would make that a hard dependency at import
            # time instead of at first use.
            from tintaview.stats.service import StatsService

            if self._svc is None:
                self._svc = StatsService(self._cfg)
            results = self._svc.fetch_all()
        except Exception:
            # Never let a stats failure reach the GUI thread as a crash — the tray
            # just keeps showing whatever usage it already had.
            log.exception("stats fetch_all() failed - keeping last known usage")
            return
        self.results_ready.emit(results)


class StateWorker(QtCore.QObject):
    """HTTP fallback path only — see module docstring. Direct `state_payload()`
    reads happen straight on the GUI thread in `TrayApp._poll_state`, since that
    call is documented as an in-process lock + dict build, not I/O.
    """

    state_ready = QtCore.Signal(dict)

    def __init__(self, server: Any) -> None:
        super().__init__()
        self._server = server

    def fetch(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="tv-tray-state-http").start()

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


class TrayApp(QtCore.QObject):
    """Owns the tray icon, the flyout and the polling timers.

    Split out from `run_tray()` so it's constructible — and testable — without a
    running Qt event loop: tests build a `TrayApp` directly against a fake server
    and call `_poll_state()` / `_apply_state()` synchronously.
    """

    def __init__(self, cfg: Config, server: Any, app: QtWidgets.QApplication) -> None:
        super().__init__()
        self._cfg = cfg
        self._server = server
        self._app = app

        self._prev_effective = "none"
        self._blink_on = True
        self._usage_results: dict[str, UsageResult] = {}
        self._last_usage_fetch = 0.0

        # If `server` doesn't expose `state_payload` (some other object standing in
        # for a real StatusServer), fall back to polling its `/state` HTTP endpoint.
        self._has_direct_state = callable(getattr(server, "state_payload", None))
        self._state_worker = StateWorker(server)
        self._state_worker.state_ready.connect(self._apply_state)

        self._stats_worker = StatsWorker(cfg)
        self._stats_worker.results_ready.connect(self._apply_results)

        self.flyout = Flyout()

        self.tray = QtWidgets.QSystemTrayIcon(icons.brand_icon(ICON_SIZE))
        self.tray.setToolTip("TintaView: connecting…")
        self.tray.activated.connect(self._on_activated)
        self.tray.setContextMenu(self._build_menu())
        self.tray.show()

        self.state_timer = QtCore.QTimer(self)
        self.state_timer.setInterval(STATE_POLL_MS)
        self.state_timer.timeout.connect(self._poll_state)
        self.state_timer.start()

        usage_ms = max(1000, int(cfg.stats.poll_seconds * 1000))
        self.usage_timer = QtCore.QTimer(self)
        self.usage_timer.setInterval(usage_ms)
        self.usage_timer.timeout.connect(self._stats_worker.fetch)
        self.usage_timer.start()

        self.blink_timer = QtCore.QTimer(self)
        self.blink_timer.setInterval(cfg.colors.blink_ms)
        self.blink_timer.timeout.connect(self._on_blink)

        self._poll_state()
        self._stats_worker.fetch()

    # --- menu -------------------------------------------------------------

    def _build_menu(self) -> QtWidgets.QMenu:
        menu = QtWidgets.QMenu()
        menu.addAction("Refresh usage", self._stats_worker.fetch)
        sound_action = menu.addAction("Sound on confirm")
        sound_action.setCheckable(True)
        sound_action.setChecked(self._cfg.ui.chime_on_confirm)
        sound_action.toggled.connect(self._set_sound)
        menu.addSeparator()
        menu.addAction("Settings…", self._open_settings)
        menu.addAction("Check for updates", self._check_updates)
        menu.addSeparator()
        menu.addAction("Quit", self._app.quit)
        return menu

    def _set_sound(self, on: bool) -> None:
        self._cfg.ui.chime_on_confirm = on
        try:
            from tintaview.core.config import save

            save(self._cfg)
        except Exception:
            log.exception("could not persist chime_on_confirm")

    def _open_settings(self) -> None:
        # `tintaview.ui.wizard` doesn't exist yet (another agent's milestone) — a
        # message box beats a traceback in the meantime.
        try:
            from tintaview.ui.wizard import run_wizard
        except ImportError:
            QtWidgets.QMessageBox.information(
                None, "TintaView", "Settings aren't available yet — coming soon."
            )
            return
        run_wizard()

    def _check_updates(self) -> None:
        """Check for a newer release and offer to install it.

        Deliberately does not call run_update(), which prints its progress to stdout:
        a windowed build has no console, so that output would go nowhere the user can
        see it. Everything here reports through dialogs instead.
        """
        from tintaview import __version__

        try:
            from tintaview.install import update as update_mod
        except ImportError:
            QtWidgets.QMessageBox.information(
                None, "TintaView", "Update checking isn't available in this build."
            )
            return

        release = update_mod.latest_release()
        if release is None:
            QtWidgets.QMessageBox.information(
                None, "TintaView",
                "Couldn't check for updates just now — no network, a rate limit, or no "
                "releases published yet. Try again later.",
            )
            return

        tag = str(release.get("tag_name") or "").lstrip("vV").strip()
        if not tag or update_mod.compare_versions(__version__, tag) >= 0:
            QtWidgets.QMessageBox.information(
                None, "TintaView", f"You're up to date (version {__version__})."
            )
            return

        answer = QtWidgets.QMessageBox.question(
            None, "TintaView",
            f"Version {tag} is available — you have {__version__}.\n\n"
            "Your settings and your agents' hook configuration are never changed by an "
            "update.\n\nInstall it now?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return

        # The installer replaces files this process is running from, so hand off and let
        # run_update's own platform logic (verify SHA-256, run silently) take over.
        code = update_mod.run_update(check_only=False)
        if code != 0:
            QtWidgets.QMessageBox.warning(
                None, "TintaView",
                "The update didn't complete. Run `tintaview update` from a terminal to "
                "see why.",
            )

    # --- state / icon -------------------------------------------------------------

    def _poll_state(self) -> None:
        if self._has_direct_state:
            try:
                payload = self._server.state_payload()
            except Exception:
                log.exception("state_payload() failed")
                return
            self._apply_state(payload)
            return
        self._state_worker.fetch()

    def _apply_state(self, payload: dict) -> None:
        effective = payload.get("effective", "none")

        if effective == "confirm":
            if not self.blink_timer.isActive():
                self._blink_on = True
                self.blink_timer.start()
                self.tray.setIcon(icons.state_icon(self._cfg.colors.rgb("confirm"), ICON_SIZE))
        else:
            self.blink_timer.stop()
            self.tray.setIcon(self._icon_for_status(effective))

        if effective == "confirm" and self._prev_effective != "confirm":
            self._chime()
        self._prev_effective = effective

        self.tray.setToolTip(self._tooltip_for(payload))

    def _icon_for_status(self, status: str) -> QtGui.QIcon:
        # Every state, "none" included, is the same silhouette in a different hue from
        # the logo's own gradient (see ColorsConfig). Using the multicolour mark for
        # "none" was the earlier design, but a gradient icon among solid ones reads as a
        # different icon rather than a fourth state — and at 16px it just looks muddy.
        return icons.state_icon(self._cfg.colors.rgb(status), ICON_SIZE)

    def _on_blink(self) -> None:
        self._blink_on = not self._blink_on
        confirm_rgb = self._cfg.colors.rgb("confirm")
        rgb = confirm_rgb if self._blink_on else _dim(confirm_rgb)
        self.tray.setIcon(icons.state_icon(rgb, ICON_SIZE))

    def _tooltip_for(self, payload: dict) -> str:
        agents = payload.get("agents", {})
        parts = []
        for key in self._cfg.enabled_agents:
            info = agents.get(key, {})
            status = info.get("effective", "none")
            count = info.get("count", 0)
            label = _STATUS_LABELS.get(status, status)
            suffix = f" ({count} session{'s' if count != 1 else ''})" if count else ""
            parts.append(f"{self._agent_display_name(key)}: {label}{suffix}")
        return " · ".join(parts) if parts else "TintaView"

    def _agent_display_name(self, key: str) -> str:
        try:
            from tintaview.agents.base import get as get_agent

            adapter = get_agent(key)
            if adapter is not None:
                return adapter.display_name
        except Exception:
            pass
        return key.title()

    def _chime(self) -> None:
        if not self._cfg.ui.chime_on_confirm:
            return
        if sys.platform == "win32":
            try:
                import winsound

                winsound.MessageBeep(winsound.MB_ICONASTERISK)
                return
            except Exception:
                pass
        QtWidgets.QApplication.beep()

    # --- usage / flyout -------------------------------------------------------------

    def _apply_results(self, results: dict) -> None:
        # Merge rather than replace: a partial fetch (or a config change dropping an
        # agent) must never make a previously-known agent's section vanish. Bad
        # individual results are already resolved to cached-good ones inside
        # StatsService, so whatever comes back here is the best we currently have.
        self._usage_results.update(results)
        self._last_usage_fetch = time.monotonic()
        self.flyout.set_results(self._usage_results)

    def _on_activated(self, reason: QtWidgets.QSystemTrayIcon.ActivationReason) -> None:
        if reason == QtWidgets.QSystemTrayIcon.Context:
            return  # let the context menu handle right-click
        if reason != QtWidgets.QSystemTrayIcon.Trigger:
            return
        if self.flyout.isVisible():
            self.flyout.hide()
            return
        # If the flyout was just dismissed by this same click (focus-out fired
        # first), don't immediately reopen it — treat the click as "close".
        if time.monotonic() - self.flyout.hidden_at < CLICK_REOPEN_GUARD_S:
            return
        # Show cached data immediately; only re-fetch if it's stale.
        if self._usage_results:
            self.flyout.set_results(self._usage_results)
        if time.monotonic() - self._last_usage_fetch > USAGE_MIN_REFRESH_S:
            self._stats_worker.fetch()
        self._show_flyout_near_cursor()

    def _show_flyout_near_cursor(self) -> None:
        self.flyout.adjustSize()
        screen = (
            QtGui.QGuiApplication.screenAt(QtGui.QCursor.pos())
            or QtGui.QGuiApplication.primaryScreen()
        )
        area = screen.availableGeometry()
        pos = QtGui.QCursor.pos()
        x = min(pos.x(), area.right() - self.flyout.width() - 8)
        y = pos.y() - self.flyout.height() - 12  # above the cursor (tray is usually bottom)
        if y < area.top():
            y = pos.y() + 12
        x = max(area.left() + 8, x)
        self.flyout.move(x, y)
        self.flyout.show()
        self.flyout.raise_()
        self.flyout.activateWindow()


def run_tray(cfg: Config, server: Any) -> int:
    """Build the QApplication, the tray icon and the flyout, and run the event
    loop. Signature matches what `cli.py`'s `_cmd_run` calls: `run_tray(cfg, server)`
    with an already-started `StatusServer`.
    """
    app = QtWidgets.QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # closing the flyout must not quit the tray
    if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
        print("No system tray available on this platform.", file=sys.stderr)
        return 1
    _tray = TrayApp(cfg, server, app)  # noqa: F841 - kept alive by Qt's event loop / parenting
    return app.exec()
