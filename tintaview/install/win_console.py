"""Windows console helpers for the tray process.

Autostart uses ``pythonw.exe`` (no console). If the tray is started via ``python.exe``
instead, hide its console window so login does not leave a terminal on the taskbar.
G HUB lighting under ``pythonw`` is handled by a ``python.exe`` sidecar
(``engines/ghub_sidecar.py``), not by switching the whole tray off pythonw.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def hide_console_if_python_exe() -> None:
    """Hide the console window when this process is ``python.exe`` on Windows.

    No-op under ``pythonw.exe``, non-Windows, or if there is no console attached.
    Never raises — a failed hide must not block the tray.
    """
    if sys.platform != "win32":
        return
    try:
        if Path(sys.executable).name.lower() != "python.exe":
            return
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        hwnd = kernel32.GetConsoleWindow()
        if not hwnd:
            return
        # SW_HIDE = 0
        user32.ShowWindow(hwnd, 0)
    except Exception as e:
        log.debug("hide_console_if_python_exe failed: %r", e)
