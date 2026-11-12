"""The windows the tray opens: "About", and the `doctor` report viewer.

Both are plain Qt dialogs with no knowledge of the tray, the config or the status broker
— they take what they show and hand back a widget. They were methods on `TrayApp`, which
made that class the union of three unrelated jobs (a tray icon, six background workers'
slots, and window construction); the workers moved to `workers.py` for the same reason.

The doctor viewer is a class rather than a function because it is *reused*: the report
arrives seconds after the window opens (see `WORKERS`' `DoctorWorker`), so the window has
to exist first, show a placeholder, and then be filled in.
"""

from __future__ import annotations

import datetime

from PySide6 import QtCore, QtGui, QtWidgets

from tintaview.i18n import t
from tintaview.ui import icons

#: First year in the copyright line. A range is rendered only once a second year exists,
#: so the notice reads "2026" in its first year rather than "2026-2026".
FIRST_COPYRIGHT_YEAR = 2026


def copyright_years(today: datetime.date | None = None) -> str:
    year = (today or datetime.date.today()).year
    if year <= FIRST_COPYRIGHT_YEAR:
        return str(FIRST_COPYRIGHT_YEAR)
    return f"{FIRST_COPYRIGHT_YEAR}-{year}"


#: The open About window, kept so it is not garbage-collected while shown and so a
#: second click brings it forward instead of opening another.
_about_dialog: QtWidgets.QDialog | None = None


def show_about() -> None:
    """The About box: the mark, the version, the copyright line. A plain window, not a
    modal dialog: a modal one blocked the usage panel and every other window of the app
    for as long as it stayed open."""
    from tintaview import __version__

    global _about_dialog
    if _about_dialog is not None:
        _about_dialog.show()
        _about_dialog.raise_()
        _about_dialog.activateWindow()
        return

    dialog = QtWidgets.QDialog(None)
    dialog.setWindowTitle(t("tray.about.title"))

    logo = QtWidgets.QLabel()
    pixmap = icons.logo_pixmap(480)
    if not pixmap.isNull():
        logo.setPixmap(pixmap)
    logo.setAlignment(QtCore.Qt.AlignCenter)

    version_label = QtWidgets.QLabel(t("tray.about.version", version=__version__))
    version_label.setAlignment(QtCore.Qt.AlignCenter)

    # Not translated on purpose: a copyright notice is the same line in every language,
    # and the two names in it are names.
    copyright_label = QtWidgets.QLabel(
        f"Copyright (C) {copyright_years()} Dmitry Koshelenko, Igor Koshelenko"
    )
    copyright_label.setAlignment(QtCore.Qt.AlignCenter)

    buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok)
    buttons.accepted.connect(dialog.accept)

    layout = QtWidgets.QVBoxLayout(dialog)
    layout.addWidget(logo)
    layout.addWidget(version_label)
    layout.addWidget(copyright_label)
    layout.addWidget(buttons)
    layout.setSizeConstraint(QtWidgets.QLayout.SetFixedSize)

    def _closed(_result: int) -> None:
        global _about_dialog
        _about_dialog = None
        dialog.deleteLater()

    dialog.finished.connect(_closed)
    _about_dialog = dialog
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()


class DoctorReportDialog(QtWidgets.QDialog):
    """`doctor`'s report, in a window that can be selected and copied from.

    Opens on a placeholder and is filled in when the run finishes, rather than appearing
    only once there is something to show: `doctor` probes the daemon, the lighting engine
    and every agent's hooks over the network, which takes seconds — long enough that a
    menu item that appeared to do nothing would get clicked again.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(t("tray.diagnostics.title"))
        self.resize(760, 520)
        layout = QtWidgets.QVBoxLayout(self)
        self._view = QtWidgets.QPlainTextEdit()
        self._view.setReadOnly(True)
        # Monospace: `doctor` aligns its report in columns, which a proportional font
        # shreds. Selectable (a read-only QPlainTextEdit still is) so the report can be
        # copied into a bug report.
        self._view.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        layout.addWidget(self._view)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def set_text(self, text: str) -> None:
        self._view.setPlainText(text)

    def text(self) -> str:
        return self._view.toPlainText()

    def surface(self) -> None:
        """Show it, and bring it to the front if it is already open behind something."""
        self.show()
        self.raise_()
        self.activateWindow()
