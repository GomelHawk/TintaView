"""Windows console-window suppression for every subprocess TintaView spawns.

A GUI build has no console of its own, so on Windows each `subprocess.run` /
`Popen` that starts a console program gets a **new console window** — it flashes up,
steals focus and disappears. One is a nuisance; `install.wsl.missing_hooks` runs one
`wsl.exe` per enabled agent, so pressing OK in the settings dialog flashed a row of
them across the screen.

`CREATE_NO_WINDOW` is Windows-only and absent from `subprocess` on other platforms,
so it is read with `getattr` and resolves to no flags at all elsewhere — the helper is
safe to spread on every call site unconditionally, which is the point: a call site that
has to remember a platform check is a call site that will forget.

Two deliberate exceptions, both of which want a window or want to be detached:
`ui.console_setup.run_console_setup` (`CREATE_NEW_CONSOLE` — the wizard *is* a terminal program)
and `install.restart` (`DETACHED_PROCESS`, so the replacement outlives its parent).

Stdlib only, and in `core/` rather than `install/`, because the engines spawn processes
too and the headless core must be able to import it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

#: 0 anywhere that isn't Windows, where the flag doesn't exist.
CREATE_NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def system_tool(name: str) -> str | None:
    """Full path of a Windows system tool (`wsl.exe`, `powershell.exe`), or None.

    Never the bare name, and never a copy in the current directory: on Windows both
    `shutil.which` and `CreateProcess` look in the working directory *before* System32,
    so `tintaview update` run from a folder holding a planted `powershell.exe` ran that
    one — with `-ExecutionPolicy Bypass`. Looked up under `%SystemRoot%\\System32` first;
    a PATH hit is accepted only when it is not in the working directory. Off Windows it
    is a plain PATH lookup.
    """
    if sys.platform != "win32":
        return shutil.which(name)
    root = Path(os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR") or r"C:\Windows")
    for candidate in (root / "System32" / name,
                      root / "System32" / "WindowsPowerShell" / "v1.0" / name):
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(name)
    if not found:
        return None
    try:
        if Path(found).resolve().parent == Path.cwd().resolve():
            return None
    except OSError:
        return None
    return found


def no_window(**kwargs: Any) -> dict[str, Any]:
    """`kwargs` plus `creationflags=CREATE_NO_WINDOW`, OR-ed into any already there.

    Spread into a `subprocess` call: ``subprocess.run(cmd, **no_window(timeout=10))``.
    """
    kwargs["creationflags"] = kwargs.get("creationflags", 0) | CREATE_NO_WINDOW
    return kwargs
