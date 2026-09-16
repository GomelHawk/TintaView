"""Who Windows thinks this process is, in the notification area and on the taskbar.

TintaView runs as a plain `pythonw.exe` out of its own venv — there is no `.exe` of its
own and, by design, no Start Menu shortcut (autostart is a `HKCU\\...\\Run` value; see
`autostart.py`). Windows therefore has nothing to identify the process by and falls back
to the interpreter: tray balloons are headed **"Python"**, with the Python logo, and the
app shows up under that name in Settings → Notifications.

The fix is the AppUserModelID (AUMID), the shell's identity token for a process:

1. `set_app_user_model_id()` pins one on the process, instead of letting Windows derive
   a per-executable one from `pythonw.exe`.
2. `register_app_identity()` tells the shell what that AUMID *is* — a display name and
   an icon — by writing `HKCU\\Software\\Classes\\AppUserModelId\\<AUMID>`. This is the
   registration path for a desktop app with no shortcut to carry the ID (Windows 10
   1709+); without it the AUMID is just an unresolvable string and the header stays
   whatever Windows guessed.

Both are best-effort and silent: an identity that cannot be set is cosmetic, and must
never keep the tray from starting.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)

#: `CompanyName.ProductName`, per the AUMID convention. Never user-visible once
#: `register_app_identity` has run — that is what `DisplayName` is for.
APP_USER_MODEL_ID = "Piesoft.TintaView"

#: What the notification header, the Action Center entry and Settings → Notifications
#: show. Not translated: it is the product's name in every language.
DISPLAY_NAME = "TintaView"


def set_app_user_model_id(aumid: str = APP_USER_MODEL_ID) -> bool:
    """Pin this process's AUMID. True if it was set.

    Called before Qt builds anything: the shell reads the AUMID when a window or a
    notification icon is created, so a later call would leave the first ones tagged with
    the interpreter's identity.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(aumid)
        return True
    except Exception as e:
        log.debug("SetCurrentProcessExplicitAppUserModelID failed: %r", e)
        return False


def register_app_identity(
    icon_path: Path | None = None, aumid: str = APP_USER_MODEL_ID
) -> bool:
    """Give the AUMID a name and an icon under HKCU. True if the key was written.

    Rewritten on every start rather than once at install time: it is two small values in
    the user's own hive, and it is the only thing standing between the user and a
    notification headed "Python" — so a hive restored from a backup, or an install that
    predates this code, fixes itself on the next launch.
    """
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER, rf"Software\Classes\AppUserModelId\{aumid}"
        ) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, DISPLAY_NAME)
            if icon_path is not None:
                winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(icon_path))
        return True
    except Exception as e:
        log.debug("AppUserModelId registration failed: %r", e)
        return False
