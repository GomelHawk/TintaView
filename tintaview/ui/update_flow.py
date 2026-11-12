"""The tray's update conversation: the unattended startup check, "Check for updates…",
the install prompt and the balloon when an install ends.

Owns both update workers and reports through the tray icon it is given. Nothing here
blocks the GUI thread — the network and the installer run in the workers.
"""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from tintaview.i18n import t
from tintaview.ui.workers import ManualUpdateWorker, UpdateCheckWorker


class UpdateFlow(QtCore.QObject):
    def __init__(self, tray: QtWidgets.QSystemTrayIcon,
                 parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self.tray = tray
        self.startup_worker = UpdateCheckWorker()
        self.startup_worker.update_available.connect(self.on_update_available)
        self.manual_worker = ManualUpdateWorker()
        self.manual_worker.check_ready.connect(self.on_manual_check)
        self.manual_worker.install_done.connect(self.on_update_installed)

    def check_at_startup(self) -> None:
        """The unattended check: a balloon if there is something newer, silence if not."""
        self.startup_worker.fetch()

    def on_update_available(self, tag: str, current: str) -> None:
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

    def check(self) -> None:
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
        # for this and `on_manual_check` answers in a dialog, so a "Checking for
        # updates…" toast is a second notification saying less than the first — and on
        # Windows toasts are queued, so it arrived *after* the answer it was meant to
        # precede.
        self.manual_worker.check()  # False = a check or install is already running

    def on_manual_check(self, outcome: str, tag: str, notes: str) -> None:
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
        if not self.manual_worker.install():
            return
        self.tray.showMessage(
            "TintaView",
            t("tray.update.installing"),
            QtWidgets.QSystemTrayIcon.Information,
            15000,
        )

    def on_update_installed(self, code: int) -> None:
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
