"""Opening the console setup wizard from a windowed tray — a terminal window of its own.

The wizard is a text-mode program and the tray has no terminal, so "open the wizard" is
"find a console interpreter, find a terminal emulator, start one in the other". Kept out
of `ui/tray.py`: none of it touches the tray icon, and all of it differs per platform.
"""

from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from PySide6 import QtWidgets

from tintaview.i18n import t

log = logging.getLogger(__name__)


def console_command() -> list[str] | None:
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
    # -P: the wizard must not import from whatever directory the tray was started in.
    return [str(exe), "-P", "-m", "tintaview"]


#: Terminal emulators tried on Linux, in order, each with the argv prefix that makes it run
#: a command: the desktop defaults first (Ubuntu 25.10+ ships Ptyxis, earlier GNOME
#: Terminal; KDE Konsole; XFCE; GNOME Console), then the Debian alternative, then xterm.
LINUX_TERMINALS: tuple[tuple[str, ...], ...] = (
    ("ptyxis", "--"),
    ("gnome-terminal", "--"),
    ("konsole", "-e"),
    ("xfce4-terminal", "-x"),
    ("kgx", "--"),
    ("x-terminal-emulator", "-e"),
    ("xterm", "-e"),
)

#: Keeps the wizard's window open after it exits, so its last lines can be read.
HOLD_SCRIPT = '"$@"; printf "\\nPress Enter to close this window. "; read _'


def terminal_argv(command: list[str], platform: str | None = None) -> list[str] | None:
    """Argv that runs `command` in a *new terminal window*, or None if none can be found.

    Every platform gets one: Windows a new console (via `CREATE_NEW_CONSOLE` at the call
    site, so `command` as is), macOS Terminal.app, Linux the first terminal emulator on
    PATH. The tray is started from a menu or at login with no terminal of its own, so a
    plain `Popen` of a text-mode program there is invisible and dies on its first prompt.
    """
    platform = platform or sys.platform
    if platform == "win32":
        return command
    if platform == "darwin":
        line = " ".join(shlex.quote(part) for part in command)
        return [
            "osascript",
            "-e", f'tell application "Terminal" to do script {applescript_str(line)}',
            "-e", 'tell application "Terminal" to activate',
        ]
    held = ["sh", "-c", HOLD_SCRIPT, "sh", *command]
    for name, *flag in LINUX_TERMINALS:
        exe = shutil.which(name)
        if exe:
            return [exe, *flag, *held]
    return None


def applescript_str(text: str) -> str:
    """`text` as an AppleScript string literal."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def run_console_setup() -> None:
    """Open the full setup wizard in a terminal window of its own — on every platform.

    The wizard is a deliberately text-mode `print`/`input` flow (see
    `tintaview.ui.wizard`), and the tray runs windowed: started from the Start Menu, the
    applications menu, Launchpad or at login, it has no terminal at all. It is never run
    in-process, even from a tray that does have one: it would block the GUI thread for as
    long as the user took to answer, and its last step restarts the running tray — this
    very process. Called by `TrayApp._open_settings` once the settings dialog has closed
    asking for it (see `SettingsDialog.launch_wizard`).
    """
    try:
        command = console_command()
        argv = terminal_argv([*command, "setup"]) if command is not None else None
        if argv is None:
            QtWidgets.QMessageBox.information(
                None, "TintaView",
                t("tray.wizard.terminal_hint", command=setup_hint(command)),
            )
            return

        kwargs: dict[str, object] = {}
        if sys.platform == "win32":
            # Without this the child inherits "no console" from pythonw.exe and dies
            # on its first prompt.
            kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
        else:
            kwargs["start_new_session"] = True
            kwargs["stdin"] = subprocess.DEVNULL
        subprocess.Popen(argv, **kwargs)  # type: ignore[arg-type]
    except Exception:
        # A failure to open the wizard must never kill the tray.
        log.exception("could not open the setup wizard")
        QtWidgets.QMessageBox.warning(None, "TintaView", t("tray.wizard.open_failed"))


def setup_hint(command: list[str] | None) -> str:
    """The exact command to type when no terminal could be opened for the user."""
    if not command:
        return "tintaview setup"
    return " ".join(shlex.quote(part) for part in [*command, "setup"])
