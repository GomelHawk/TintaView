"""One autostart entry per platform — enabling/disabling TintaView at login.

Per the locked decisions in AGENTS.md, TintaView is a *single* process (tray + status
broker in-process), so there is exactly one autostart mechanism per platform, not the
alternative of a background-service entry plus a separate tray entry:

- **Windows**: a value under `HKCU\\...\\CurrentVersion\\Run`. No admin, no Scheduled
  Task — explorer.exe runs it for the interactive user at logon, which is exactly where a
  tray icon needs to be. This was a Startup-folder `.lnk` until Windows 11 turned out to
  refuse *any* shortcut written into that folder as a persistence technique; see
  `_enable_windows` for the measurements behind the switch.
- **Linux**: a systemd `--user` unit (restart-on-failure, gated on the graphical
  session) *plus* an XDG `~/.config/autostart/*.desktop` entry. The two are not
  redundant: systemd `--user` may be unavailable or not import the graphical session's
  environment on some setups, while the `.desktop` file is honoured natively by GNOME/
  KDE/XFCE/Cinnamon at login with no systemd involved at all. Writing both means one of
  them works almost everywhere. Running twice is harmless — `cli._cmd_run` already exits
  quietly if the port is taken, so a double-launch never shows two tray icons.
- **macOS**: a LaunchAgent plist with `RunAtLoad`, loaded with `launchctl`.

Every backend is written to degrade: a failed autostart must never fail (or abort) an
install, so every external command and every filesystem write is wrapped and logged
rather than allowed to raise. Nothing here ever requests admin/sudo.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core.config import Config

log = logging.getLogger(__name__)

#: Base name shared by the systemd unit, the .desktop file and the log prefix.
SERVICE_NAME = "tintaview"
#: launchd reverse-DNS label, per Apple convention.
LAUNCHD_LABEL = "com.tintaview.app"

_SUBPROCESS_TIMEOUT = 15  # seconds — these are one-shot config commands, not the daemon

_LINUX_SERVICE_TEMPLATE = """\
[Unit]
Description=TintaView status broker + tray
After=graphical-session.target

[Service]
ExecStart={exec}
Restart=on-failure

[Install]
WantedBy=graphical-session.target
"""

_LINUX_DESKTOP_TEMPLATE = """\
[Desktop Entry]
Type=Application
Name=TintaView
Comment=Agent-status lighting and usage tray
Exec={exec}
Terminal=false
X-GNOME-Autostart-enabled=true
"""

_MACOS_PLIST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
{args}
    </array>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
"""


# --------------------------------------------------------------------------- helpers


def _executable_command() -> list[str]:
    """The command line that should run TintaView at login.

    Windows deliberately does *not* use the `tintaview` console script, even though pip
    puts one in the venv's Scripts directory. Two reasons, both load-bearing:

    - It is a console application, so a login launch would park an empty console window
      on the taskbar for the whole session behind a tray app that has no terminal UI.
      `pythonw.exe` is the windowed interpreter and shows nothing.
    - `pythonw.exe` carries the Python Software Foundation's Authenticode signature, and
      a venv's copy of it keeps that signature; the pip-generated shim is an unsigned
      binary. Windows **Smart App Control** only permits executables that are signed or
      cloud-reputable, so routing the login entry point through the signed interpreter
      keeps autostart working on machines where SAC is enforcing (see `__main__.py`).

    Elsewhere, prefer the `tintaview` console script `install.sh` puts on PATH, resolved
    fresh each time so autostart keeps working even if the venv is later reinstalled at
    the same prefix. `python -m tintaview` is the fallback everywhere, and is what a
    from-checkout dev run that never went through an installer ends up on.
    """
    if sys.platform == "win32":
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if pythonw.exists():
            return [str(pythonw), "-m", "tintaview"]
        return [sys.executable, "-m", "tintaview"]
    exe = shutil.which("tintaview")
    if exe:
        return [exe]
    return [sys.executable, "-m", "tintaview"]


def _run(*args: str) -> bool:
    """Run an external command, never raising. True only on a clean exit."""
    try:
        result = subprocess.run(
            list(args), capture_output=True, timeout=_SUBPROCESS_TIMEOUT, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("autostart: could not run %s: %s", " ".join(args), exc)
        return False
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "ignore").strip() if result.stderr else ""
        log.warning("autostart: %s exited %s: %s", " ".join(args), result.returncode, stderr)
        return False
    return True


def _write_text(path: Path, content: str) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        log.warning("autostart: could not write %s: %s", path, exc)
        return False
    return True


# --------------------------------------------------------------------------- Windows


#: The per-user autorun key explorer.exe reads at logon. Per-user, so no admin is needed,
#: and it is a plain registry value rather than a Scheduled Task (AGENTS.md, "Locked decisions").
_WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_WINDOWS_RUN_VALUE = "TintaView"


def _windows_startup_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _windows_legacy_shortcut_path() -> Path:
    """Where autostart used to live, kept only so `disable()` can still clean it up."""
    return _windows_startup_dir() / "TintaView.lnk"


def _enable_windows(command: list[str]) -> bool:
    """Register the login command in HKCU's Run key.

    **Not a Startup-folder shortcut**, which is what this used to be. Windows 11 blocks
    any `.lnk` from landing in the Startup folder: creating one there through
    `WScript.Shell` fails with an access-denied error, and so does building it elsewhere
    and copying it in — measured on a stock machine with Controlled Folder Access off and
    no Attack Surface Reduction rules configured, so this is the built-in persistence
    protection, not something the user opted into or can be asked to turn off. Writing a
    plain file into that same folder succeeds, so it is specifically shortcuts-as-autorun
    that are refused.

    The Run key is the other per-user autorun mechanism, needs no admin, and is written
    with `winreg` — no PowerShell subprocess and no COM. It keeps every property the
    locked decision actually cared about: one entry, per-user, no Scheduled Task.
    """
    import winreg  # Windows-only stdlib module; importing at call time keeps this file importable everywhere

    # The Run key holds a single command *string*, not a program plus separate arguments
    # the way a .lnk does, so the whole command line is quoted here. list2cmdline applies
    # the rules Windows itself parses with, which matters the moment the venv sits under
    # a path containing a space -- as %LOCALAPPDATA% does for plenty of user names.
    value = subprocess.list2cmdline(command)
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _WINDOWS_RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, _WINDOWS_RUN_VALUE, 0, winreg.REG_SZ, value)
    except OSError as exc:
        log.warning("autostart: could not write the Run key value: %s", exc)
        return False
    return True


def _disable_windows() -> bool:
    import winreg

    ok = True
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _WINDOWS_RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, _WINDOWS_RUN_VALUE)
    except FileNotFoundError:
        pass  # already absent, which is the state disable() is asked to produce
    except OSError as exc:
        log.warning("autostart: could not remove the Run key value: %s", exc)
        ok = False

    # Also clear the Startup shortcut this used to create, so an install that predates
    # the switch does not end up launching TintaView twice at login.
    legacy = _windows_legacy_shortcut_path()
    try:
        if legacy.exists():
            legacy.unlink()
    except OSError as exc:
        log.warning("autostart: could not remove %s: %s", legacy, exc)
        ok = False
    return ok


def _status_windows() -> bool:
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _WINDOWS_RUN_KEY, 0, winreg.KEY_QUERY_VALUE
        ) as key:
            winreg.QueryValueEx(key, _WINDOWS_RUN_VALUE)
    except FileNotFoundError:
        return _windows_legacy_shortcut_path().exists()
    except OSError as exc:
        log.warning("autostart: could not read the Run key value: %s", exc)
        return False
    return True


# --------------------------------------------------------------------------- Linux


def _xdg_config_home() -> Path:
    override = os.environ.get("XDG_CONFIG_HOME")
    return Path(override) if override else Path.home() / ".config"


def _linux_service_path() -> Path:
    return _xdg_config_home() / "systemd" / "user" / f"{SERVICE_NAME}.service"


def _linux_desktop_path() -> Path:
    return _xdg_config_home() / "autostart" / f"{SERVICE_NAME}.desktop"


def _shell_join(command: list[str]) -> str:
    import shlex

    return " ".join(shlex.quote(part) for part in command)


def _enable_linux(command: list[str]) -> bool:
    exec_line = _shell_join(command)
    service_path = _linux_service_path()
    desktop_path = _linux_desktop_path()

    wrote_service = _write_text(service_path, _LINUX_SERVICE_TEMPLATE.format(exec=exec_line))
    wrote_desktop = _write_text(desktop_path, _LINUX_DESKTOP_TEMPLATE.format(exec=exec_line))
    if not (wrote_service or wrote_desktop):
        # Neither fallback could even be written — nothing left to try.
        return False

    systemctl = shutil.which("systemctl")
    if systemctl and wrote_service:
        if not _run(systemctl, "--user", "daemon-reload"):
            log.warning("autostart: systemd unit written at %s but daemon-reload failed; "
                        "the .desktop autostart entry is still in place as a fallback.",
                        service_path)
        elif not _run(systemctl, "--user", "enable", "--now", f"{SERVICE_NAME}.service"):
            log.warning("autostart: systemd unit written at %s but `enable --now` failed; "
                        "the .desktop autostart entry is still in place as a fallback.",
                        service_path)
    else:
        log.info("autostart: no user systemd found — relying on the .desktop entry at "
                  "%s, which GNOME/KDE/XFCE/Cinnamon read natively at login.",
                  desktop_path)
    return True


def _disable_linux() -> bool:
    service_path = _linux_service_path()
    desktop_path = _linux_desktop_path()
    systemctl = shutil.which("systemctl")

    if systemctl and service_path.exists():
        _run(systemctl, "--user", "disable", "--now", f"{SERVICE_NAME}.service")

    ok = True
    for path in (service_path, desktop_path):
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            log.warning("autostart: could not remove %s: %s", path, exc)
            ok = False

    if systemctl:
        _run(systemctl, "--user", "daemon-reload")
    return ok


def _status_linux() -> bool:
    # The .desktop entry has no "enabled" state beyond existing — most desktops read it
    # unconditionally — so its presence alone is enough to call autostart active.
    if _linux_desktop_path().exists():
        return True

    service_path = _linux_service_path()
    if not service_path.exists():
        return False

    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False
    try:
        result = subprocess.run(
            [systemctl, "--user", "is-enabled", f"{SERVICE_NAME}.service"],
            capture_output=True, timeout=_SUBPROCESS_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


# --------------------------------------------------------------------------- macOS


def _macos_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _xml_escape(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _enable_macos(command: list[str]) -> bool:
    plist_path = _macos_plist_path()
    args_xml = "\n".join(f"        <string>{_xml_escape(part)}</string>" for part in command)
    content = _MACOS_PLIST_TEMPLATE.format(label=LAUNCHD_LABEL, args=args_xml)
    if not _write_text(plist_path, content):
        return False

    launchctl = shutil.which("launchctl")
    if not launchctl:
        log.warning("autostart: launchctl not found; the LaunchAgent plist is in place "
                    "at %s and will be picked up next login regardless.", plist_path)
        return True

    # Unload first in case a stale copy from a previous version is already loaded —
    # `load` on an already-loaded label is a silent no-op on some macOS versions, so this
    # is what makes re-running enable() actually pick up a changed command line.
    _run(launchctl, "unload", str(plist_path))
    if not _run(launchctl, "load", "-w", str(plist_path)):
        log.warning("autostart: plist written at %s but `launchctl load` failed; it will "
                    "still be picked up at next login.", plist_path)
    return True


def _disable_macos() -> bool:
    plist_path = _macos_plist_path()
    launchctl = shutil.which("launchctl")
    if launchctl and plist_path.exists():
        _run(launchctl, "unload", "-w", str(plist_path))

    try:
        if plist_path.exists():
            plist_path.unlink()
    except OSError as exc:
        log.warning("autostart: could not remove %s: %s", plist_path, exc)
        return False
    return True


def _status_macos() -> bool:
    plist_path = _macos_plist_path()
    if not plist_path.exists():
        return False

    launchctl = shutil.which("launchctl")
    if not launchctl:
        return True  # the plist is in place; best effort without launchctl available

    try:
        result = subprocess.run(
            [launchctl, "list", LAUNCHD_LABEL],
            capture_output=True, timeout=_SUBPROCESS_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    return result.returncode == 0


# --------------------------------------------------------------------------- dispatch


def enable(cfg: Config | None = None) -> bool:
    """Turn on autostart for this platform. Never raises; False means it degraded.

    `cfg` is accepted (and currently unused) so the wizard and `install.sh`'s Python
    entry point can call this uniformly with the loaded config, for any future
    platform-specific choice (e.g. headless-only autostart) without changing the call
    sites again.
    """
    del cfg
    command = _executable_command()
    try:
        if sys.platform == "win32":
            return _enable_windows(command)
        if sys.platform == "darwin":
            return _enable_macos(command)
        return _enable_linux(command)
    except Exception as exc:  # belt and suspenders: autostart must never break an install
        log.warning("autostart: enabling failed unexpectedly: %s", exc)
        return False


def disable() -> bool:
    """Turn off autostart for this platform. Never raises."""
    try:
        if sys.platform == "win32":
            return _disable_windows()
        if sys.platform == "darwin":
            return _disable_macos()
        return _disable_linux()
    except Exception as exc:
        log.warning("autostart: disabling failed unexpectedly: %s", exc)
        return False


def status() -> bool:
    """Whether autostart looks active right now. Never raises."""
    try:
        if sys.platform == "win32":
            return _status_windows()
        if sys.platform == "darwin":
            return _status_macos()
        return _status_linux()
    except Exception as exc:
        log.warning("autostart: status check failed unexpectedly: %s", exc)
        return False
