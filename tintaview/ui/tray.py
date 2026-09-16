"""The system-tray front end: the icon, its blink loop, the context menu and the
usage flyout. `run_tray(cfg, server)` is `cli.py`'s entry point for the GUI path
(see `_cmd_run`) — `server` is an already-started `StatusServer`.

The broker runs in this same process (one process, per AGENTS.md) rather than behind an HTTP
port of its own, so `TrayApp` reads `server.state_payload()` directly — a plain
in-process dict build under a lock, not I/O — and only falls back to HTTP if that
method isn't there at all (e.g. some other object standing in for a real server).

**Balloon vs dialog.** A tray balloon is for something the user did *not* ask for and is
not watching for: the startup update check finding a release, the startup hook check
finding an agent with no hooks, the lighting engine refusing a command mid-session — and
the progress of a long job already under way (installing an update, which returns minutes
later, after the user has moved on). Anything the user just clicked answers in a dialog,
where they are already looking, and does **not** also balloon: two notifications for one
click is noise, and on Windows toasts are queued while dialogs are not, so the toast
routinely arrives after the answer it was meant to precede. The update balloons are all
titled "TintaView": one title covering "a release exists", "installing" and "installed"
cannot say more than the app's name without being wrong about two of the three. The hook
and engine balloons say one thing each, so they keep a title that says what it is.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6 import QtCore, QtGui, QtWidgets

from tintaview.core.config import Config
from tintaview.core.events import STATUS_NONE
from tintaview.i18n import set_language, t
from tintaview.stats import format as fmt
from tintaview.ui import icons
from tintaview.ui.dialogs import DoctorReportDialog, show_about
from tintaview.ui.flyout import Flyout
from tintaview.ui.workers import (
    DoctorWorker,
    HookCheckWorker,
    ManualUpdateWorker,
    StateWorker,
    StatsWorker,
    UpdateCheckWorker,
)

if TYPE_CHECKING:  # pragma: no cover - types only
    from tintaview.stats.model import UsageResult

log = logging.getLogger(__name__)

STATE_POLL_MS = 1500
# Working-pulse redraw rate. Was 100 ms: the breathe is quantised to icons.PULSE_STEPS
# levels, so a faster tick mostly re-set an icon the shell had already drawn.
ANIM_TICK_MS = 200
USAGE_MIN_REFRESH_S = 30.0  # ignore flyout-open refreshes more frequent than this
CLICK_REOPEN_GUARD_S = 0.25  # guards against "the click that just closed it" reopening it
ICON_SIZE = 128


def _dim(rgb: tuple[int, int, int], factor: float = 0.3) -> tuple[int, int, int]:
    """A darkened variant of `rgb`, used for the "off" half of the confirm blink.

    Derived from `cfg.colors.confirm` rather than a hardcoded dim colour — icon
    colours must come from config, not be baked into this module.
    """
    return tuple(max(0, min(255, int(c * factor))) for c in rgb)  # type: ignore[return-value]



def _agent_label(key: str) -> str:
    """Human name for an agent key, for the escalation balloon.

    Lazy import plus a fallback, the same shape as `flyout._display_name`: the registry
    is the one place a key becomes a label, and a cosmetic lookup must never be able to
    break the thing it is labelling.
    """
    try:
        from tintaview.agents.base import display_name

        return display_name(key)
    except Exception:
        return key.replace("_", " ").title() or key


def _stdin_is_interactive() -> bool:
    """Can this process actually prompt the user where they are looking?

    False under `pythonw.exe` (no console at all) and when stdin is a pipe or closed.
    Only a real terminal makes the in-process, text-mode wizard a sane thing to run.
    """
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except (AttributeError, ValueError, OSError):
        return False


def _console_command() -> list[str] | None:
    """Argv prefix that runs TintaView under a *console* interpreter, or None.

    `sys.executable` is `pythonw.exe` for the tray, and pythonw can never host a text
    prompt no matter what console it is given — so the sibling `python.exe` is what has
    to be launched. Returns None when no console interpreter can be located, leaving the
    caller to tell the user to run the command themselves.
    """
    exe = Path(sys.executable)
    if sys.platform == "win32" and exe.name.lower() == "pythonw.exe":
        console = exe.with_name("python.exe")
        if not console.exists():
            return None
        exe = console
    if not exe.exists():
        return None
    return [str(exe), "-m", "tintaview"]


def run_console_setup() -> None:
    """Open the full setup wizard — in a console of its own, not in this process.

    The wizard is a deliberately text-mode `print`/`input` flow (see
    `tintaview.ui.wizard`), and the tray runs windowed: at login it is launched by
    `pythonw.exe`, which has no console at all. Calling `run_wizard()` in-process
    therefore hits `input()` with no stdin, and the exception escapes the Qt slot and
    takes the whole tray down — the menu item just made the app vanish. It would also
    block the GUI thread for as long as the user took to answer.

    So: run it from a terminal if this process has one (a dev run from a shell), and
    otherwise spawn the *console* interpreter with a console of its own. Called by
    `TrayApp._open_settings` once the settings dialog has closed asking for it — either
    via its "Open Full Setup Wizard (Terminal)…" button or its "hooks aren't installed"
    prompt (see `SettingsDialog.launch_wizard`).
    """
    try:
        if _stdin_is_interactive():
            from tintaview.ui.wizard import run_wizard

            run_wizard()
            return

        command = _console_command()
        if command is None:
            QtWidgets.QMessageBox.information(
                None, "TintaView", t("tray.wizard.terminal_hint"),
            )
            return

        kwargs: dict[str, object] = {}
        if sys.platform == "win32":
            # Without this the child inherits "no console" from pythonw.exe and dies
            # on its first prompt exactly as the in-process call did.
            kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
        subprocess.Popen([*command, "setup"], **kwargs)  # type: ignore[arg-type]
    except Exception:
        # A failure to open the wizard must never kill the tray.
        log.exception("could not open the setup wizard")
        QtWidgets.QMessageBox.warning(None, "TintaView", t("tray.wizard.open_failed"))


class TrayApp(QtCore.QObject):
    """Owns the tray icon, the flyout and the polling timers.

    Split out from `run_tray()` so it's constructible — and testable — without a
    running Qt event loop: tests build a `TrayApp` directly against a fake server
    and call `_poll_state()` / `_apply_state()` synchronously.
    """

    #: Emitted from an HTTP worker thread when a second `tintaview` launch asks this
    #: instance to surface itself (see `StatusServer.request_show`). A signal, not a
    #: direct call: showing a widget from a non-GUI thread is undefined behaviour, and
    #: a queued signal is Qt's supported way across that boundary.
    show_requested = QtCore.Signal()

    #: Emitted from an HTTP worker thread when `GET /quit` asks this instance to exit
    #: (`StatusServer.on_quit`). Same reason as `show_requested`: quitting a
    #: QApplication from a non-GUI thread is not something Qt supports.
    quit_requested = QtCore.Signal()

    def __init__(self, cfg: Config, server: Any, app: QtWidgets.QApplication) -> None:
        super().__init__()
        self._cfg = cfg
        self._server = server
        self._app = app

        # Applied here rather than only in `cli.py` so every route into the tray — an
        # embedder, a test building a TrayApp directly — renders in the configured
        # language. `set_language` is idempotent, so doing it in both places is free.
        set_language(cfg.ui.language)

        self._prev_effective = "none"
        #: What the tray icon and tooltip currently show. `_poll_state` runs every 1.5 s
        #: and the answer is almost always the same one as last time — `setIcon` makes the
        #: shell rebuild and repaint the tray item, so re-setting an identical icon is
        #: pure cost. None means "unknown, set it".
        self._icon_key: tuple[Any, ...] | None = None
        self._anim_key: tuple[Any, ...] | None = None
        self._tooltip_key: tuple[Any, ...] | None = None
        self._blink_on = True
        self._sound_action: QtGui.QAction | None = None  # set by _build_menu below
        self._usage_results: dict[str, UsageResult] = {}
        self._last_usage_fetch = 0.0
        # Last engine note we ballooned about — so a sticky "G HUB restarted" doesn't
        # re-fire a notification on every state poll.
        self._engine_note_shown: str | None = None

        #: Unanswered-confirm escalation (see `_escalate_confirm`). `None` whenever the
        #: effective status is anything but confirm, so leaving confirm — for any reason,
        #: including the watchdog releasing a dead session — is what resets the nagging.
        self._confirm_since: float | None = None
        self._escalations_sent = 0
        self._escalation_command_ran = False

        #: (agent, row label) pairs already alerted on — see `_check_usage_alerts`.
        self._usage_alerted: set[tuple[str, str]] = set()

        # If `server` doesn't expose `state_payload` (some other object standing in
        # for a real StatusServer), fall back to polling its `/state` HTTP endpoint.
        self._has_direct_state = callable(getattr(server, "state_payload", None))
        self._state_worker = StateWorker(server)
        self._state_worker.state_ready.connect(self._apply_state)

        self._stats_worker = StatsWorker(cfg)
        self._stats_worker.results_ready.connect(self._apply_results)

        self._update_worker = UpdateCheckWorker()
        self._update_worker.update_available.connect(self._on_update_available)

        self._manual_update_worker = ManualUpdateWorker()
        self._manual_update_worker.check_ready.connect(self._on_manual_check)
        self._manual_update_worker.install_done.connect(self._on_update_installed)

        self._doctor_worker = DoctorWorker()
        self._hook_worker = HookCheckWorker(cfg)
        self._hook_worker.missing_ready.connect(self._on_hooks_missing)
        self._doctor_worker.report_ready.connect(self._show_doctor_report)
        self._doctor_dialog: DoctorReportDialog | None = None

        #: One engine rebuild at a time — see `_refresh_lighting`. The thread is kept so
        #: tests can join it; nothing in the app waits on it.
        self._lighting_lock = threading.Lock()
        self._lighting_thread: threading.Thread | None = None

        self.show_requested.connect(self._on_show_requested)
        # A second launch of TintaView pops this instance's panel instead of exiting in
        # silence. Guarded because `server` may be any object with a state payload.
        with contextlib.suppress(AttributeError):
            server.on_show = self.show_requested.emit
        # `GET /quit`, wired exactly like `/show`: the handler runs on an HTTP worker
        # thread, so it may only ever *signal* the GUI thread. Guarded the same way —
        # `server` may be any stand-in, and older StatusServers have no `on_quit` at all.
        self.quit_requested.connect(self._on_quit_requested)
        with contextlib.suppress(AttributeError):
            server.on_quit = self.quit_requested.emit

        self.flyout = Flyout(
            collapsed=cfg.ui.collapsed_agents,
            on_toggle=self._on_flyout_toggle,
            cfg=cfg,
            on_settings=self._open_settings,
        )

        self.tray = QtWidgets.QSystemTrayIcon(icons.brand_icon(ICON_SIZE))
        self.tray.setToolTip(t("tray.tooltip.connecting"))
        self.tray.activated.connect(self._on_activated)
        # Held as an attribute: `QSystemTrayIcon.setContextMenu` doesn't take ownership,
        # and the menu is replaced wholesale on a language change (see `_apply_settings`),
        # so the live one needs a Python reference of its own to stay alive.
        self._menu = self._build_menu()
        self.tray.setContextMenu(self._menu)
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

        self.anim_timer = QtCore.QTimer(self)
        self.anim_timer.setInterval(ANIM_TICK_MS)
        self.anim_timer.timeout.connect(self._update_anim_icon)

        self._poll_state()
        self._stats_worker.fetch()
        # Once, at startup only — see HookCheckWorker. A periodic version with a "Fix
        # hooks" menu item was removed at the maintainer's request.
        self._hook_worker.fetch()
        if cfg.update.check:
            self._update_worker.fetch()

    # --- menu -------------------------------------------------------------

    def _build_menu(self) -> QtWidgets.QMenu:
        """Build the context menu from scratch.

        Called again after a language change (see `_apply_settings`) rather than
        retranslating each action in place: the menu is a handful of items with no state
        beyond the chime check mark, which is re-synced from config either way.
        """
        menu = QtWidgets.QMenu()
        menu.addAction(t("tray.menu.refresh_usage"), self._stats_worker.fetch)
        sound_action = menu.addAction(t("tray.menu.sound_on_confirm"))
        sound_action.setCheckable(True)
        sound_action.setChecked(self._cfg.ui.chime_on_confirm)
        sound_action.toggled.connect(self._set_sound)
        # Kept as an attribute: the Settings dialog edits the same `chime_on_confirm`, so
        # this check mark has to be re-synced after an accept or the menu starts
        # contradicting the dialog.
        self._sound_action = sound_action

        pause_action = menu.addAction(t("tray.menu.pause_lighting"))
        pause_action.setCheckable(True)
        pause_action.setChecked(self._lighting_paused())
        pause_action.toggled.connect(self._set_paused)
        # Same reason as the chime action: the menu is rebuilt on a language change and
        # the check mark has to be re-synced from the controller, not remembered here.
        self._pause_action = pause_action

        menu.addSeparator()
        menu.addAction(t("tray.menu.settings"), self._open_settings)
        menu.addAction(t("tray.menu.check_updates"), self._check_updates)
        menu.addAction(t("tray.menu.open_logs"), self._open_logs)
        menu.addAction(t("tray.menu.diagnostics"), self._run_diagnostics)
        menu.addSeparator()
        menu.addAction(t("tray.menu.about"), self._show_about)
        menu.addSeparator()
        menu.addAction(t("tray.menu.quit"), self._app.quit)
        return menu

    # --- lighting pause -----------------------------------------------------

    def _controller(self):
        """The lighting controller, or None when `server` is a stand-in without one."""
        return getattr(self._server, "controller", None)

    def _lighting_paused(self) -> bool:
        controller = self._controller()
        return bool(getattr(controller, "paused", False))

    def _set_paused(self, paused: bool) -> None:
        """Release the lights (or take them back). Runtime-only, never written to
        config — see `LightController.set_paused`."""
        controller = self._controller()
        if controller is None:
            return
        try:
            controller.set_paused(paused)
        except Exception:
            log.exception("could not %s lighting", "pause" if paused else "resume")

    # --- support actions ----------------------------------------------------

    def _open_logs(self) -> None:
        """Open the log directory in the platform file manager.

        Worth a menu item because the logs live next to the venv under
        `%LOCALAPPDATA%\\TintaView` on Windows — a path nobody finds by guessing, in a
        build with no console to print it in.
        """
        from tintaview.core.log import log_path

        folder = log_path().parent
        try:
            folder.mkdir(parents=True, exist_ok=True)
            opened = QtGui.QDesktopServices.openUrl(
                QtCore.QUrl.fromLocalFile(str(folder))
            )
        except OSError:
            log.exception("could not open the logs folder")
            opened = False
        if not opened:
            QtWidgets.QMessageBox.information(
                None, "TintaView", t("tray.logs.open_failed", path=str(folder))
            )

    def _run_diagnostics(self) -> None:
        """Run `doctor` in the background and show its report — see `DoctorReportDialog`,
        which is what opens immediately on a placeholder while the run takes its seconds.
        """
        if self._doctor_dialog is None:
            self._doctor_dialog = DoctorReportDialog()
        self._doctor_dialog.set_text(t("tray.diagnostics.running"))
        self._doctor_dialog.surface()
        self._doctor_worker.fetch()

    def _show_doctor_report(self, report: str) -> None:
        if self._doctor_dialog is None:
            return
        self._doctor_dialog.set_text(report or t("tray.diagnostics.failed"))

    def _on_show_requested(self) -> None:
        """A second `tintaview` launch asked this instance to surface itself."""
        if self._usage_results:
            self.flyout.set_results(self._usage_results)
        self._show_flyout_near_cursor()

    def _set_sound(self, on: bool) -> None:
        self._cfg.ui.chime_on_confirm = on
        try:
            from tintaview.core.config import save

            save(self._cfg)
        except Exception:
            log.exception("could not persist chime_on_confirm")

    def _on_flyout_toggle(self, agent_key: str, collapsed: bool) -> None:
        """The flyout owns collapse/expand as live UI state; this just mirrors that
        state into config so a section stays collapsed across restarts."""
        agents = self._cfg.ui.collapsed_agents
        if collapsed and agent_key not in agents:
            agents.append(agent_key)
        elif not collapsed and agent_key in agents:
            agents.remove(agent_key)
        else:
            return
        try:
            from tintaview.core.config import save

            save(self._cfg)
        except Exception:
            log.exception("could not persist collapsed_agents")

    def _open_settings(self) -> None:
        """Open the native settings dialog, in-process.

        Unlike the console wizard (see `run_console_setup` below), this needs no
        subprocess: the tray already owns a running Qt event loop, so a `QDialog` is
        just another window. Only the knobs `SettingsDialog` actually covers are
        reachable here — hooks/autostart/engine-specific setup stay behind its "Open
        Full Setup Wizard (Terminal)…" button, which raises `launch_wizard` for the
        hand-off below.

        The wizard is started *after* the dialog has closed and its settings have been
        applied: it re-reads the config from disk, so launching it from inside the
        dialog would have it race the caller that is still applying the accepted copy.
        """
        from tintaview.ui.settings_dialog import SettingsDialog

        try:
            dialog = SettingsDialog(self._cfg)
            accepted = dialog.exec() == QtWidgets.QDialog.Accepted
            if accepted:
                self._apply_settings(dialog.result_cfg)
            if dialog.launch_wizard:
                run_console_setup()
        except Exception:
            log.exception("could not open the settings dialog")
            QtWidgets.QMessageBox.warning(None, "TintaView", t("tray.settings.open_failed"))

    def _apply_settings(self, new_cfg: Config) -> None:
        """Push a saved `SettingsDialog` result into the live config and refresh
        whatever depends on it — `new_cfg` is a separate object (the dialog edits a
        copy so Cancel changes nothing), so each field is copied across rather than
        replacing `self._cfg` outright, which `StatusServer`/`LightController` also
        hold a reference to.

        Every field the dialog can write has to be mirrored here *and* have its live
        consumer refreshed — a value that only lands in the config object is a setting
        that appears to do nothing until the next restart, which is the whole failure
        mode this dialog exists to avoid.
        """
        engine_changed = new_cfg.engine.mode != self._cfg.engine.mode
        language_changed = new_cfg.ui.language != self._cfg.ui.language

        self._cfg.enabled_agents = list(new_cfg.enabled_agents)
        self._cfg.agents = new_cfg.agents  # newly enabled agents' seeded defaults
        self._cfg.ui.chime_on_confirm = new_cfg.ui.chime_on_confirm
        self._cfg.ui.language = new_cfg.ui.language
        self._cfg.stats.poll_seconds = new_cfg.stats.poll_seconds
        # `StatsService` holds this very object, so this line is what makes the setting
        # take effect without a restart — see `tests/test_ui.py`'s mirror guard, which
        # exists because omitting exactly this line shipped a tick box that did nothing.
        self._cfg.stats.show_estimate = new_cfg.stats.show_estimate
        # `StatsService` reads `show_trend` on its next poll; the alert thresholds are
        # read by `_check_usage_alerts` right here. Lowering the threshold re-arms every
        # window that is already over the new one — the latch is keyed by row, not by
        # threshold, so a window sitting above both values would otherwise stay silent
        # about a limit the user just said they wanted to hear about.
        self._cfg.stats.show_trend = new_cfg.stats.show_trend
        if new_cfg.stats.alert_threshold != self._cfg.stats.alert_threshold:
            self._usage_alerted.clear()
        self._cfg.stats.alert_enabled = new_cfg.stats.alert_enabled
        self._cfg.stats.alert_threshold = new_cfg.stats.alert_threshold
        # Escalation is read by `_track_confirm` off the state poll, so these take effect
        # on the next tick — including mid-confirm, which is the point: someone who opens
        # Settings *because* the nagging is too frequent must not have to wait for the
        # current confirm to clear.
        self._cfg.escalation.enabled = new_cfg.escalation.enabled
        self._cfg.escalation.after_seconds = new_cfg.escalation.after_seconds
        self._cfg.escalation.command = new_cfg.escalation.command
        self._cfg.update.check = new_cfg.update.check
        # The flyout holds this very `Config`, so the band picks these up on its next
        # paint — which the `set_results` call further down schedules, along with the
        # re-measure the band's height needs and the clock tick's re-arm.
        self._cfg.ui.clocks.enabled = new_cfg.ui.clocks.enabled
        self._cfg.ui.clocks.format = new_cfg.ui.clocks.format
        # Copies, not the dialog's own `ClockConfig` objects: that config is a deep copy
        # the dialog goes on owning after this returns, and the flyout paints straight
        # off these.
        self._cfg.ui.clocks.clocks = [replace(clock) for clock in new_cfg.ui.clocks.clocks]
        self._cfg.engine.mode = new_cfg.engine.mode
        for status in ("idle", "working", "confirm"):
            setattr(self._cfg.colors, status, getattr(new_cfg.colors, status))
            # The hardware palette is what `LightController` actually sends — copying
            # only the icon colours would repaint the tray and leave the LEDs alone.
            setattr(self._cfg.colors.device, status, getattr(new_cfg.colors.device, status))

        self.usage_timer.setInterval(max(1000, int(self._cfg.stats.poll_seconds * 1000)))

        # A language is not a value anything re-reads on its own: the menu's action
        # texts were baked in when it was built, and the tooltip and the usage rows are
        # only rebuilt on their next refresh. Switch the catalogue first, then rebuild
        # the menu — `_poll_state` (tooltip) and `_stats_worker.fetch` (row labels) at
        # the end of this method cover the rest. Rows that fall back to the on-disk cache
        # keep the language they were fetched in until the next good poll replaces them.
        if language_changed:
            set_language(self._cfg.ui.language)
            self._menu = self._build_menu()
            self.tray.setContextMenu(self._menu)
            # The tooltip is only re-set when its (status, count) changes, and a language
            # switch changes neither — so drop the latch or the old language's tooltip
            # survives until the next session opens or closes.
            self._tooltip_key = None

        # The context menu's own copy of chime_on_confirm. Signals blocked so setting the
        # check mark doesn't re-enter `_set_sound` and save the config a second time.
        if self._sound_action is not None:
            self._sound_action.blockSignals(True)
            self._sound_action.setChecked(self._cfg.ui.chime_on_confirm)
            self._sound_action.blockSignals(False)

        # A disabled agent's usage section would otherwise sit in the flyout until the
        # next restart: `_apply_results` merges rather than replaces (deliberately — a
        # partial fetch must not blank a section), so dropping it has to happen here.
        for key in [k for k in self._usage_results if k not in self._cfg.enabled_agents]:
            del self._usage_results[key]
        self._reorder_results()
        self.flyout.set_results(self._usage_results)

        # Unconditional re-apply, not just on an engine change: `reset_engine` only drops
        # the old engine, and nothing else calls `apply()` until the *next* status
        # transition, so a mid-session engine switch would leave the lights dark and a
        # colour change wouldn't reach the hardware at all.
        self._refresh_lighting(reset=engine_changed)

        self._stats_worker.fetch()
        self._poll_state()

    def _refresh_lighting(self, reset: bool) -> None:
        """Rebuild (when `reset`) and re-paint the lighting engine — on a worker thread.

        This pair is the slowest thing the settings dialog can trigger: `reset_engine()`
        closes the engine and the `apply()` behind it opens a new one and paints, i.e. a
        Chroma REST open + heartbeat, an OpenRGB snapshot of every device, or a G HUB
        sidecar process restart. On the GUI thread that froze the tray for seconds every
        time OK was pressed. The in-flight guard stops a second OK — or an OK arriving
        while the first rebuild is still opening the engine — from opening it twice.
        """
        if self._controller() is None:
            return
        if not self._lighting_lock.acquire(blocking=False):
            return

        def run() -> None:
            try:
                if reset:
                    controller = self._controller()
                    try:
                        if controller is not None:
                            controller.reset_engine()
                    except Exception:
                        log.exception("could not reset lighting engine after a settings change")
                self._reapply_lighting()
            finally:
                self._lighting_lock.release()

        thread = threading.Thread(target=run, daemon=True, name="tv-tray-engine")
        self._lighting_thread = thread
        try:
            thread.start()
        except Exception:
            self._lighting_lock.release()
            raise

    def _reapply_lighting(self) -> None:
        """Re-send the current effective status, so a config change takes effect on the
        hardware now rather than at the next status transition.

        Through `StatusServer.apply_status()` rather than `controller.apply()`: the server
        owns a single-slot applier thread that every hook event already goes through, so
        calling the controller directly would both skip its newest-wins coalescing and put
        the vendor SDK call on whichever thread happened to ask. Falls back to the
        controller for a stand-in server that has no `apply_status` (the tray is documented
        as working against any object with a state payload).
        """
        apply_status = getattr(self._server, "apply_status", None)
        try:
            if callable(apply_status):
                apply_status()
                return
            controller = self._controller()
            state = getattr(self._server, "state", None)
            if controller is None or state is None:
                return
            controller.apply(state.effective())
        except Exception:
            log.exception("could not re-apply lighting after a settings change")

    def _show_about(self) -> None:
        show_about()

    def _on_update_available(self, tag: str, current: str) -> None:
        """Startup check found a newer release — surface it as a tray balloon rather
        than a modal dialog: this fires unattended on every launch, so it must never
        interrupt whatever the user is doing. "Check for updates" in the menu (below)
        is where the actual install prompt lives."""
        # Not translated, and deliberately just the app's name: every update balloon
        # shares this title, so anything more specific ends up wrong on three of the
        # four (it used to read "TintaView update available" above "Checking for
        # updates…"). The dialogs in this file are titled the same way.
        self.tray.showMessage(
            "TintaView",
            t("tray.update.balloon_body", latest=tag, current=current),
            QtWidgets.QSystemTrayIcon.Information,
            8000,
        )

    def _check_updates(self) -> None:
        """Check for a newer release and offer to install it — nothing here blocks.

        Both halves used to run inline on the GUI thread, contradicting this docstring's
        own promise: `latest_release()` is a 10 s-timeout HTTPS call, and on Linux/macOS
        `run_update()` runs `sh install.sh` synchronously, which rebuilds the private venv
        and takes minutes. The tray, the flyout and the broker's Qt callbacks were all
        dead for the duration, with only a frozen icon to show for it. The work moved to
        `ManualUpdateWorker`; everything below reports through dialogs and balloons, since
        a windowed build has no console for `run_update`'s own progress output.
        """
        # No balloon here, by the rule in this module's docstring: the user just asked
        # for this and `_on_manual_check` answers in a dialog, so a "Checking for
        # updates…" toast is a second notification saying less than the first — and on
        # Windows toasts are queued, so it arrived *after* the answer it was meant to
        # precede.
        self._manual_update_worker.check()  # False = a check or install is already running

    def _on_manual_check(self, outcome: str, tag: str, notes: str) -> None:
        """The manual check came back — on the GUI thread, via a queued signal."""
        from tintaview import __version__

        worker = ManualUpdateWorker
        if outcome == worker.OUTCOME_UNSUPPORTED:
            QtWidgets.QMessageBox.information(None, "TintaView", t("tray.update.unsupported"))
            return
        if outcome == worker.OUTCOME_FAILED:
            QtWidgets.QMessageBox.information(None, "TintaView", t("tray.update.check_failed"))
            return
        if outcome == worker.OUTCOME_CURRENT:
            QtWidgets.QMessageBox.information(
                None, "TintaView", t("tray.update.up_to_date", version=__version__)
            )
            return

        notes_block = f"\n\n{notes}" if notes else ""
        answer = QtWidgets.QMessageBox.question(
            None, "TintaView",
            t("tray.update.confirm", latest=tag, current=__version__, notes=notes_block),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return

        # The installer replaces files this process is running from, so hand off and let
        # run_update's own platform logic (verify SHA-256, run silently, detach on
        # Windows) take over — on a worker thread, with a balloon rather than a modal
        # dialog, because on Linux/macOS it does not return for minutes.
        if not self._manual_update_worker.install():
            return
        self.tray.showMessage(
            "TintaView",
            t("tray.update.installing"),
            QtWidgets.QSystemTrayIcon.Information,
            15000,
        )

    def _on_update_installed(self, code: int) -> None:
        """The install finished — on the GUI thread, however long it took.

        Success is worth saying out loud rather than staying silent like the startup
        check does: this process is still running the *old* code out of a venv the
        installer has just replaced, so "it worked" is only half the message.

        Both outcomes balloon. The install takes minutes on Linux/macOS and the user is
        long gone by the time it lands, so the failure is no more "an answer they are
        waiting for" than the success is — a modal warning stealing focus that late is
        the interruption the docstring's rule exists to prevent. The wording carries the
        next step (`tintaview update` in a terminal) either way.
        """
        self.tray.showMessage(
            "TintaView",
            t("tray.update.installed") if code == 0 else t("tray.update.failed"),
            QtWidgets.QSystemTrayIcon.Information if code == 0 else QtWidgets.QSystemTrayIcon.Warning,
            15000,
        )

    def _on_hooks_missing(self, agents: list) -> None:
        """The startup hook check found agents with no working hooks: say so once, and
        point at the fix that already exists — Settings → "Open Full Setup Wizard"."""
        agents = [str(a) for a in agents]
        if not agents:
            return
        self.tray.showMessage(
            t("tray.hooks.balloon_title"),
            t("tray.hooks.balloon_body", agents=", ".join(agents)),
            QtWidgets.QSystemTrayIcon.Warning,
            10000,
        )

    def _on_quit_requested(self) -> None:
        """`GET /quit` asked this instance to exit — marshalled onto the GUI thread."""
        self._app.quit()

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
        agents_payload = payload.get("agents", {})
        self.flyout.set_status(
            {k: v.get("effective", "none") for k, v in agents_payload.items()},
            {k: v.get("tool", "") for k, v in agents_payload.items()},
        )

        effective = payload.get("effective", "none")

        if effective == "confirm":
            self.anim_timer.stop()
            if not self.blink_timer.isActive():
                self._blink_on = True
                self.blink_timer.start()
                self._set_icon(("confirm", self._cfg.colors.rgb("confirm")),
                               lambda: icons.state_icon(self._cfg.colors.rgb("confirm"), ICON_SIZE))
        elif effective == "working":
            self.blink_timer.stop()
            if not self.anim_timer.isActive():
                self.anim_timer.start()
            self._update_anim_icon()
        else:
            self.blink_timer.stop()
            self.anim_timer.stop()
            rgb = None if effective in (STATUS_NONE, "idle") else self._cfg.colors.rgb(effective)
            self._set_icon((effective, rgb), lambda: self._icon_for_status(effective))

        if effective == "confirm" and self._prev_effective != "confirm":
            self._chime()
        self._prev_effective = effective

        self._track_confirm(effective, agents_payload)

        self._surface_engine_note(payload)

        # Tooltip likewise: it is a function of exactly these two values, and this runs
        # every 1.5 s. `_apply_settings` clears the latch on a language change.
        tooltip_key = (effective, payload.get("count", 0))
        if tooltip_key != self._tooltip_key:
            self._tooltip_key = tooltip_key
            self.tray.setToolTip(self._tooltip_for(payload))

    def _set_icon(self, key: tuple[Any, ...], build) -> None:
        """`setIcon` only when the icon would actually differ.

        `key` identifies what would be drawn (status plus, where it matters, the colour it
        is drawn in, so a colour change in Settings still repaints). Qt has no cheap "is
        this the same icon" test and `setIcon` makes the platform shell rebuild the tray
        item either way, so the guard has to live here.
        """
        if key == self._icon_key:
            return
        self._icon_key = key
        self._anim_key = None  # the pulse no longer owns the icon
        self.tray.setIcon(build())

    def _surface_engine_note(self, payload: dict) -> None:
        """Balloon once when the lighting engine reports a new problem note.

        Same channel as the update-available balloon: unattended, never modal. Clearing
        the note (paints succeeding again) resets the latch so a later failure can
        notify again.
        """
        engine = payload.get("engine") or {}
        note = engine.get("note") if isinstance(engine, dict) else None
        if not note:
            self._engine_note_shown = None
            return
        if note == self._engine_note_shown:
            return
        self._engine_note_shown = note
        self.tray.showMessage(
            t("tray.engine.balloon_title"),
            note,
            QtWidgets.QSystemTrayIcon.Warning,
            10000,
        )

    def _icon_for_status(self, status: str) -> QtGui.QIcon:
        # "No session" and "idle" both show the mark in the logo's own colours, static —
        # at rest (whether or not a session is open) the tray is just the TintaView logo.
        # "working" is the only state that visibly does something (it pulses, see
        # _update_anim_icon); "confirm" is the same mark flooded with its status colour,
        # blinking. The earlier objection to a multicolour icon among solid ones — that it
        # reads as a different icon and looks muddy at 16px — was really an objection to
        # *scaling the gradient PNG down*. Drawing it shares the status icons' exact
        # geometry, so it reads as the same mark, and the hues are flat per capsule rather
        # than smoothly interpolated, so nothing turns to mush.
        if status in (STATUS_NONE, "idle"):
            return icons.brand_icon(ICON_SIZE)
        return icons.state_icon(self._cfg.colors.rgb(status), ICON_SIZE)

    def _on_blink(self) -> None:
        self._blink_on = not self._blink_on
        confirm_rgb = self._cfg.colors.rgb("confirm")
        rgb = confirm_rgb if self._blink_on else _dim(confirm_rgb)
        self._set_icon(("blink", rgb), lambda: icons.state_icon(rgb, ICON_SIZE))

    def _update_anim_icon(self) -> None:
        """Redraws the breathing working icon for the current instant.

        Called on every anim_timer tick and isn't reset when working is (re-)entered,
        so it runs off the shared monotonic clock rather than a per-state-entry phase.

        The brightness is quantised (`icons.pulse_step`), which does two things: the
        handful of resulting colours land in `state_icon`'s cache instead of re-rendering
        nine pixmaps per tick, and a tick that lands on the same step as the last one skips
        `setIcon` entirely — near the top and bottom of the cosine most of them do. The
        local clock is `now`, not `t`: `t` is this module's i18n lookup.
        """
        now = time.monotonic()
        rgb = self._cfg.colors.rgb("working")
        key = (rgb, icons.pulse_step(now))
        if key == self._anim_key:
            return
        self._anim_key = key
        self._icon_key = None  # the pulse owns the icon now
        self.tray.setIcon(icons.pulse_icon_for_step(rgb, key[1], ICON_SIZE))

    def _tooltip_for(self, payload: dict) -> str:
        # One line per agent used to run here, but that list can only grow (JetBrains
        # and Copilot already joined the original three) while Windows' tray tooltip
        # cannot: Shell_NotifyIcon's szTip buffer is a fixed WCHAR[128], and enough
        # enabled agents silently hard-truncated it mid-word. A single aggregate count
        # has no such ceiling and needs nothing added when a new agent shows up — the
        # per-agent breakdown (with its per-status dots) lives in the flyout instead.
        count = payload.get("count", 0)
        if not count:
            return t("tray.tooltip.no_sessions")
        return t("tray.tooltip.active_sessions", count=count)

    # --- unanswered confirm ---------------------------------------------------------

    def _track_confirm(self, effective: str, agents_payload: dict) -> None:
        """Keep the "how long has this confirm been waiting?" clock, and escalate on it.

        Driven from `_apply_state`, i.e. off the existing 1.5 s state poll, rather than
        from a timer of its own: the poll already knows the moment confirm starts and the
        moment it ends, and a second clock could only ever disagree with it.
        """
        if effective != "confirm":
            self._confirm_since = None
            self._escalations_sent = 0
            self._escalation_command_ran = False
            return

        now = time.monotonic()
        if self._confirm_since is None:
            self._confirm_since = now
            return

        escalation = self._cfg.escalation
        if not escalation.enabled or escalation.after_seconds <= 0:
            return
        waited = now - self._confirm_since
        # `// after_seconds` rather than a running deadline: a laptop that suspended for
        # an hour comes back owing exactly one escalation, not sixty.
        due = int(waited // escalation.after_seconds)
        if due <= self._escalations_sent:
            return
        self._escalations_sent = due
        self._escalate_confirm(waited, agents_payload)

    def _escalate_confirm(self, waited: float, agents_payload: dict) -> None:
        """Nag: chime again, balloon, and (once per confirm) run the user's command."""
        self._chime()
        waiting = [
            _agent_label(key)
            for key, value in sorted(agents_payload.items())
            if value.get("effective") == "confirm"
        ]
        # The agent's own words for what it is asking, when its hook sends any — see
        # `core/state.py`. Only ever from a session that is waiting *now*, and only the
        # first: two agents asking at once is a balloon, not a transcript.
        question = next(
            (
                str(value.get("question") or "")
                for _key, value in sorted(agents_payload.items())
                if value.get("effective") == "confirm" and value.get("question")
            ),
            "",
        )
        message = self._escalation_message(waited, waiting, question)
        self.tray.showMessage(
            "TintaView", message, QtWidgets.QSystemTrayIcon.Warning, 10000,
        )
        if not self._escalation_command_ran:
            self._escalation_command_ran = True
            self._run_escalation_command(waiting, question, message)

    def _escalation_message(self, waited: float, waiting: list[str], question: str) -> str:
        """The one sentence both the balloon and the command's `TINTAVIEW_MESSAGE` use.

        Worded once, here, so a user command that just echoes `TINTAVIEW_MESSAGE` says
        exactly what the balloon says — including when there is no question to quote,
        which is every Cursor confirm and any agent whose hook sent nothing.
        `duration_text`, not a minutes count of our own: an interval set to 15 s made the
        first three reminders all claim "1 min".
        """
        waited_text = fmt.duration_text(waited)
        agents = ", ".join(waiting)
        if not waiting:
            return t("tray.escalate.balloon_body_unknown", waited=waited_text)
        if question:
            return t("tray.escalate.balloon_body_question",
                     waited=waited_text, agents=agents, question=question)
        return t("tray.escalate.balloon_body", waited=waited_text, agents=agents)

    def _run_escalation_command(self, waiting: list[str], question: str = "",
                                message: str = "") -> None:
        """Fire the configured command and forget about it.

        Through the shell, because the whole point is that the user writes whatever their
        setup needs (`curl`, `ntfy`, a PowerShell one-liner) in one config field, and
        detached, because it is not ours to wait for: `Popen` returns immediately, the
        exit code is never read, and a command that blocks forever blocks only itself.
        TintaView's own state is handed over in the environment rather than interpolated
        into the string — a quoting bug in `TINTAVIEW_AGENTS` must not be able to change
        what the command does.

        Three variables, all always set so a command never has to handle a missing one:
        `TINTAVIEW_AGENTS` (who is waiting), `TINTAVIEW_QUESTION` (what they are asking,
        empty when the agent sent nothing) and `TINTAVIEW_MESSAGE` (the ready-made
        sentence, which is the one most commands want).
        """
        command = self._cfg.escalation.command.strip()
        if not command:
            return
        env = dict(os.environ)
        env["TINTAVIEW_STATUS"] = "confirm"
        env["TINTAVIEW_AGENTS"] = ", ".join(waiting)
        env["TINTAVIEW_QUESTION"] = question
        env["TINTAVIEW_MESSAGE"] = message
        try:
            kwargs: dict[str, Any] = {}
            if sys.platform == "win32":
                # No console flash: the tray runs windowed, and `shell=True` here means
                # cmd.exe, which would otherwise pop a window for a `curl` one-liner.
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            else:
                # Its own process group, so a command that outlives the tray isn't killed
                # by the Ctrl+C that stops a dev run.
                kwargs["start_new_session"] = True
            subprocess.Popen(command, shell=True, env=env, **kwargs)  # noqa: S602 - the user's own command, by design
        except Exception:
            # A broken command is the user's to fix, and telling them about it in a
            # modal at 2am is worse than the log line they can find when they look.
            log.exception("escalation command failed to start: %r", command)

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
        self._reorder_results()
        self._last_usage_fetch = time.monotonic()
        self.flyout.set_results(self._usage_results)
        self._check_usage_alerts(results)

    def _check_usage_alerts(self, results: dict) -> None:
        """Balloon the first time a usage window crosses `stats.alert_threshold`.

        Fed only the results of *this* poll, not the merged set: a cached section that
        nobody refreshed has not crossed anything, and re-alerting on it every poll is
        exactly the noise this is supposed to replace.

        One alert per window per crossing. The latch is keyed by (agent, row label) and
        released only when that row comes back *under* the threshold — a window that
        sits at 94% for three hours has one thing to say, and it already said it.
        """
        stats = self._cfg.stats
        if not stats.alert_enabled:
            return
        threshold = stats.alert_threshold
        for key, result in results.items():
            for row in getattr(result, "rows", []):
                # `kind == "limit"` only: a credits row's percentage is a spend figure
                # against a budget, which is not a window that runs out and resets, and
                # an info row's `pct` is meaningless (`show_pct` is what says so).
                if row.kind != "limit" or not row.show_pct:
                    continue
                latch = (key, row.label)
                if row.pct < threshold:
                    self._usage_alerted.discard(latch)
                    continue
                if latch in self._usage_alerted:
                    continue
                self._usage_alerted.add(latch)
                self._chime()
                self.tray.showMessage(
                    "TintaView",
                    t("tray.usage_alert.balloon_body",
                      agent=_agent_label(key), label=row.label, pct=int(row.pct)),
                    QtWidgets.QSystemTrayIcon.Warning,
                    10000,
                )

    def _reorder_results(self) -> None:
        """Re-key `_usage_results` into `cfg.enabled_agents` order.

        The flyout renders dict order as section order, and `dict.update` keeps an
        existing key where it already was — so after the settings dialog reorders the
        agents, a merge alone would leave the flyout in the *old* order until the next
        restart. Anything not in `enabled_agents` (there only until the next
        `_apply_settings` prune) keeps its relative position at the end.
        """
        keys = [k for k in self._cfg.enabled_agents if k in self._usage_results]
        keys += [k for k in self._usage_results if k not in keys]
        self._usage_results = {k: self._usage_results[k] for k in keys}

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
        # Positioning (and re-clamping on later resize) lives on Flyout itself now —
        # see `Flyout.show_near` — since it has to run again on every collapse/expand,
        # not just here at initial open.
        self.flyout.show_near(QtGui.QCursor.pos())


def _register_windows_identity() -> None:
    """Name the process to the Windows shell, so its balloons are headed "TintaView".

    Without it they are headed "Python", with the Python logo: the tray is a bare
    `pythonw.exe` with no shortcut of its own, so that is all Windows has to go on. Runs
    here rather than beside `set_app_user_model_id()` in `cli.py` because the icon the
    registration points at has to be *rendered* first, and drawing needs a QApplication.
    No-op off Windows, and never fatal — the tray works unnamed.
    """
    if sys.platform != "win32":
        return
    try:
        from tintaview.core.config import config_dir
        from tintaview.install.win_identity import register_app_identity

        register_app_identity(icons.write_brand_png(config_dir() / "notification-icon.png"))
    except Exception:
        log.debug("could not register the Windows app identity", exc_info=True)


def run_tray(cfg: Config, server: Any) -> int:
    """Build the QApplication, the tray icon and the flyout, and run the event
    loop. Signature matches what `cli.py`'s `_cmd_run` calls: `run_tray(cfg, server)`
    with an already-started `StatusServer`.
    """
    app = QtWidgets.QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # closing the flyout must not quit the tray
    _register_windows_identity()
    if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
        print(t("tray.no_system_tray"), file=sys.stderr)
        return 1
    _tray = TrayApp(cfg, server, app)  # noqa: F841 - kept alive by Qt's event loop / parenting
    return app.exec()
