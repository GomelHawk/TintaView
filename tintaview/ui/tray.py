"""The system-tray front end: the icon, its blink loop, the context menu and the
usage flyout. `run_tray(cfg, server)` is `cli.py`'s entry point for the GUI path
(see `_cmd_run`) — `server` is an already-started `StatusServer`.

The broker runs in this same process (one process, per AGENTS.md) rather than behind an HTTP
port of its own, so `TrayApp` reads `server.state_payload()` directly — a plain
in-process dict build under a lock, not I/O — and only falls back to HTTP if that
method isn't there at all (e.g. some other object standing in for a real server).
"""

from __future__ import annotations

import datetime
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6 import QtCore, QtGui, QtWidgets

from tintaview.core.config import Config
from tintaview.core.events import STATUS_NONE
from tintaview.i18n import set_language, t
from tintaview.ui import icons
from tintaview.ui.flyout import Flyout

if TYPE_CHECKING:  # pragma: no cover - types only
    from tintaview.stats.model import UsageResult

log = logging.getLogger(__name__)

STATE_POLL_MS = 1500
ANIM_TICK_MS = 100  # working-pulse redraw rate
USAGE_MIN_REFRESH_S = 30.0  # ignore flyout-open refreshes more frequent than this
CLICK_REOPEN_GUARD_S = 0.25  # guards against "the click that just closed it" reopening it
ICON_SIZE = 128
FIRST_COPYRIGHT_YEAR = 2026


def _dim(rgb: tuple[int, int, int], factor: float = 0.3) -> tuple[int, int, int]:
    """A darkened variant of `rgb`, used for the "off" half of the confirm blink.

    Derived from `cfg.colors.confirm` rather than a hardcoded dim colour — icon
    colours must come from config, not be baked into this module.
    """
    return tuple(max(0, min(255, int(c * factor))) for c in rgb)  # type: ignore[return-value]



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


class UpdateCheckWorker(QtCore.QObject):
    """One-shot background check against the GitHub Releases API, off the GUI thread —
    same reasoning as `StatsWorker`: real network I/O must never block painting.

    Only emits when a strictly newer release actually exists; "up to date" and "the
    check failed" (no network, rate-limited, no releases yet) are silent, since this
    runs unattended on every start and neither is something the user needs to see.
    """

    update_available = QtCore.Signal(str, str)  # (latest_tag, current_version)

    def fetch(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="tv-tray-update-check").start()

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

        # Applied here rather than only in `cli.py` so every route into the tray — an
        # embedder, a test building a TrayApp directly — renders in the configured
        # language. `set_language` is idempotent, so doing it in both places is free.
        set_language(cfg.ui.language)

        self._prev_effective = "none"
        self._blink_on = True
        self._sound_action: QtGui.QAction | None = None  # set by _build_menu below
        self._usage_results: dict[str, UsageResult] = {}
        self._last_usage_fetch = 0.0
        # Last engine note we ballooned about — so a sticky "G HUB restarted" doesn't
        # re-fire a notification on every state poll.
        self._engine_note_shown: str | None = None

        # If `server` doesn't expose `state_payload` (some other object standing in
        # for a real StatusServer), fall back to polling its `/state` HTTP endpoint.
        self._has_direct_state = callable(getattr(server, "state_payload", None))
        self._state_worker = StateWorker(server)
        self._state_worker.state_ready.connect(self._apply_state)

        self._stats_worker = StatsWorker(cfg)
        self._stats_worker.results_ready.connect(self._apply_results)

        self._update_worker = UpdateCheckWorker()
        self._update_worker.update_available.connect(self._on_update_available)

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
        menu.addSeparator()
        menu.addAction(t("tray.menu.settings"), self._open_settings)
        menu.addAction(t("tray.menu.check_updates"), self._check_updates)
        menu.addSeparator()
        menu.addAction(t("tray.menu.about"), self._show_about)
        menu.addSeparator()
        menu.addAction(t("tray.menu.quit"), self._app.quit)
        return menu

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
        self._cfg.update.check = new_cfg.update.check
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
        self.flyout.set_results(self._usage_results)

        if engine_changed:
            controller = getattr(self._server, "controller", None)
            if controller is not None:
                try:
                    controller.reset_engine()
                except Exception:
                    log.exception("could not reset lighting engine after a settings change")
        # Unconditional, not just on an engine change: `reset_engine` only drops the old
        # engine, and nothing else calls `apply()` until the *next* status transition, so
        # a mid-session engine switch would leave the lights dark and a colour change
        # wouldn't reach the hardware at all.
        self._reapply_lighting()

        self._stats_worker.fetch()
        self._poll_state()

    def _reapply_lighting(self) -> None:
        """Re-send the current effective status to the controller, so a config change
        takes effect on the hardware now rather than at the next status transition."""
        controller = getattr(self._server, "controller", None)
        state = getattr(self._server, "state", None)
        if controller is None or state is None:
            return
        try:
            controller.apply(state.effective())
        except Exception:
            log.exception("could not re-apply lighting after a settings change")

    def _show_about(self) -> None:
        from tintaview import __version__

        year = datetime.date.today().year
        copyright_years = str(FIRST_COPYRIGHT_YEAR) if year <= FIRST_COPYRIGHT_YEAR else f"{FIRST_COPYRIGHT_YEAR}-{year}"

        dialog = QtWidgets.QDialog(None)
        dialog.setWindowTitle(t("tray.about.title"))

        logo = QtWidgets.QLabel()
        pixmap = icons.logo_pixmap(480)
        if not pixmap.isNull():
            logo.setPixmap(pixmap)
        logo.setAlignment(QtCore.Qt.AlignCenter)

        version_label = QtWidgets.QLabel(t("tray.about.version", version=__version__))
        version_label.setAlignment(QtCore.Qt.AlignCenter)

        # Not translated on purpose: a copyright notice is the same line in every
        # language, and the two names in it are names.
        copyright_label = QtWidgets.QLabel(f"Copyright (C) {copyright_years} Dmitry Koshelenko, Igor Koshelenko")
        copyright_label.setAlignment(QtCore.Qt.AlignCenter)

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok)
        buttons.accepted.connect(dialog.accept)

        layout = QtWidgets.QVBoxLayout(dialog)
        layout.addWidget(logo)
        layout.addWidget(version_label)
        layout.addWidget(copyright_label)
        layout.addWidget(buttons)
        layout.setSizeConstraint(QtWidgets.QLayout.SetFixedSize)

        dialog.exec()

    def _on_update_available(self, tag: str, current: str) -> None:
        """Startup check found a newer release — surface it as a tray balloon rather
        than a modal dialog: this fires unattended on every launch, so it must never
        interrupt whatever the user is doing. "Check for updates" in the menu (below)
        is where the actual install prompt lives."""
        self.tray.showMessage(
            t("tray.update.balloon_title"),
            t("tray.update.balloon_body", latest=tag, current=current),
            QtWidgets.QSystemTrayIcon.Information,
            8000,
        )

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
            QtWidgets.QMessageBox.information(None, "TintaView", t("tray.update.unsupported"))
            return

        release = update_mod.latest_release()
        if release is None:
            QtWidgets.QMessageBox.information(None, "TintaView", t("tray.update.check_failed"))
            return

        tag = str(release.get("tag_name") or "").lstrip("vV").strip()
        if not tag or update_mod.compare_versions(__version__, tag) >= 0:
            QtWidgets.QMessageBox.information(
                None, "TintaView", t("tray.update.up_to_date", version=__version__)
            )
            return

        # The release notes are the project's own published text, quoted as written —
        # the same rule the usage providers follow for anything an API hands back.
        notes = str(release.get("body") or "").strip()
        if len(notes) > 500:
            notes = notes[:500].rstrip() + "…"
        notes_block = f"\n\n{notes}" if notes else ""

        answer = QtWidgets.QMessageBox.question(
            None, "TintaView",
            t("tray.update.confirm", latest=tag, current=__version__, notes=notes_block),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return

        # The installer replaces files this process is running from, so hand off and let
        # run_update's own platform logic (verify SHA-256, run silently) take over.
        code = update_mod.run_update(check_only=False)
        if code != 0:
            QtWidgets.QMessageBox.warning(None, "TintaView", t("tray.update.failed"))

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
        self.flyout.set_status({k: v.get("effective", "none") for k, v in agents_payload.items()})

        effective = payload.get("effective", "none")

        if effective == "confirm":
            self.anim_timer.stop()
            if not self.blink_timer.isActive():
                self._blink_on = True
                self.blink_timer.start()
                self.tray.setIcon(icons.state_icon(self._cfg.colors.rgb("confirm"), ICON_SIZE))
        elif effective == "working":
            self.blink_timer.stop()
            if not self.anim_timer.isActive():
                self.anim_timer.start()
            self._update_anim_icon()
        else:
            self.blink_timer.stop()
            self.anim_timer.stop()
            self.tray.setIcon(self._icon_for_status(effective))

        if effective == "confirm" and self._prev_effective != "confirm":
            self._chime()
        self._prev_effective = effective

        self._surface_engine_note(payload)
        self.tray.setToolTip(self._tooltip_for(payload))

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
        self.tray.setIcon(icons.state_icon(rgb, ICON_SIZE))

    def _update_anim_icon(self) -> None:
        """Redraws the breathing working icon for the current instant.

        Called on every anim_timer tick and isn't reset when working is (re-)entered,
        so it runs off the shared monotonic clock rather than a per-state-entry phase.
        """
        t = time.monotonic()
        self.tray.setIcon(icons.pulse_icon(self._cfg.colors.rgb("working"), t, ICON_SIZE))

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
        # Positioning (and re-clamping on later resize) lives on Flyout itself now —
        # see `Flyout.show_near` — since it has to run again on every collapse/expand,
        # not just here at initial open.
        self.flyout.show_near(QtGui.QCursor.pos())


def run_tray(cfg: Config, server: Any) -> int:
    """Build the QApplication, the tray icon and the flyout, and run the event
    loop. Signature matches what `cli.py`'s `_cmd_run` calls: `run_tray(cfg, server)`
    with an already-started `StatusServer`.
    """
    app = QtWidgets.QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # closing the flyout must not quit the tray
    if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
        print(t("tray.no_system_tray"), file=sys.stderr)
        return 1
    _tray = TrayApp(cfg, server, app)  # noqa: F841 - kept alive by Qt's event loop / parenting
    return app.exec()
