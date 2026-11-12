"""Which kind of tray icon Qt should create: D-Bus (its default) or X11.

Decided before the `QApplication` exists, because Qt picks its platform and tray backend
at startup and offers no switch afterwards. No Qt import here — the one question that
needs Qt is asked in a child process.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

log = logging.getLogger(__name__)

#: A session-bus address that cannot connect. Handed to Qt — and only to Qt, only until the
#: tray icon exists — so that it cannot see GNOME's StatusNotifierWatcher (see below).
NO_SESSION_BUS = "unix:path=/nonexistent/tintaview-legacy-tray"

#: Run in a child process: can Qt start on X11 here, and does a panel accept an X11
#: (XEmbed) tray icon once D-Bus is out of the way? A child, because Qt *aborts the
#: process* when its xcb plugin cannot load (a missing libxcb-* library).
LEGACY_TRAY_PROBE = (
    "import sys\n"
    "from PySide6 import QtWidgets\n"
    "app = QtWidgets.QApplication(['tintaview-probe'])\n"
    "sys.stdout.write('1' if QtWidgets.QSystemTrayIcon.isSystemTrayAvailable() else '0')\n"
)


def gnome_legacy_tray_enabled() -> bool:
    """Does GNOME's AppIndicator extension host X11 tray icons? (Its default: yes.)"""
    try:
        out = subprocess.run(
            ["gsettings", "get", "org.gnome.shell.extensions.appindicator",
             "legacy-tray-enabled"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and out.stdout.strip() == "true"


def use_legacy_tray(environ: dict[str, str] | None = None) -> bool:
    """Should this tray use an X11 (XEmbed) icon instead of a D-Bus one?

    Only on GNOME, where it is the one way a left click reaches the app. GNOME shows tray
    icons through the AppIndicator extension, which opens a D-Bus icon's *menu* on a left
    click and hands the app nothing but a double click (`indicatorStatusIcon.js`,
    `vfunc_button_press_event`, unchanged v58-v66). Its X11 tray icons are given the raw
    click instead (`IndicatorStatusTrayIcon`, `this._icon.click(event)`), so Qt's
    QSystemTrayIcon behaves exactly as on Windows: left click → `Trigger` → the usage
    panel, right click → the context menu. Qt itself picks D-Bus whenever the watcher is
    on the bus, on Wayland always (`QGenericUnixTheme::shouldUseDBusTray`), with no switch
    — hence X11 plus a session bus Qt cannot reach. Every other desktop (KDE, XFCE, …)
    already sends a left click to the app and is left alone. Anything in doubt — another
    desktop, a QT_QPA_PLATFORM someone chose, the extension's legacy tray off, Qt unable
    to start on X11 — answers False, and the tray starts exactly as before.
    """
    env = os.environ if environ is None else environ
    if not sys.platform.startswith("linux"):
        return False
    if env.get("QT_QPA_PLATFORM") or not env.get("DISPLAY"):
        return False
    if "GNOME" not in env.get("XDG_CURRENT_DESKTOP", "").upper():
        return False
    if not gnome_legacy_tray_enabled():
        return False
    probe_env = {**env, "QT_QPA_PLATFORM": "xcb", "DBUS_SESSION_BUS_ADDRESS": NO_SESSION_BUS}
    try:
        out = subprocess.run(
            [sys.executable, "-c", LEGACY_TRAY_PROBE],
            env=probe_env, capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    ok = out.returncode == 0 and out.stdout.strip() == "1"
    if not ok:
        log.info("GNOME: X11 tray icon unavailable, using the D-Bus one (%s)",
                 (out.stderr or "").strip()[-300:])
    return ok
