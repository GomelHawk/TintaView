"""`python -m tintaview` — the entry point autostart actually launches on Windows.

The Startup shortcut runs the venv's **`pythonw.exe -m tintaview`** rather than the
`tintaview.exe` console shim pip generates, for two reasons that both matter:

- `pythonw.exe` is the windowed interpreter, so a tray app started at login does not
  also open a console window and leave it on the taskbar for the whole session.
- `pythonw.exe` is Authenticode-signed by the Python Software Foundation, and a venv's
  copy keeps that signature. Windows **Smart App Control** blocks executables that are
  neither signed nor cloud-reputable, and it will never trust a freshly built,
  one-of-a-kind binary — which is exactly why TintaView is not shipped as a compiled
  bundle (docs/PLAN.md §8.3). Launching through the signed interpreter keeps the whole
  app on the allowed side of that policy.

`console_scripts` cannot provide this: the shim is a small unsigned `.exe` of its own.
It happens to be cloud-reputable today because pip generates the identical binary for
every package on PyPI, but that is not something to depend on for the login entry point.
"""

from __future__ import annotations

import sys

from tintaview.cli import main

if __name__ == "__main__":
    sys.exit(main())
