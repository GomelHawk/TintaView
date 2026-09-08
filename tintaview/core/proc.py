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
`ui.tray.run_console_setup` (`CREATE_NEW_CONSOLE` — the wizard *is* a terminal program)
and `install.restart` (`DETACHED_PROCESS`, so the replacement outlives its parent).

Stdlib only, and in `core/` rather than `install/`, because the engines spawn processes
too and the headless core must be able to import it.
"""

from __future__ import annotations

import subprocess
from typing import Any

#: 0 anywhere that isn't Windows, where the flag doesn't exist.
CREATE_NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def no_window(**kwargs: Any) -> dict[str, Any]:
    """`kwargs` plus `creationflags=CREATE_NO_WINDOW`, OR-ed into any already there.

    Spread into a `subprocess` call: ``subprocess.run(cmd, **no_window(timeout=10))``.
    """
    kwargs["creationflags"] = kwargs.get("creationflags", 0) | CREATE_NO_WINDOW
    return kwargs
