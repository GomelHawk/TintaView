"""`python -m tintaview` — the entry point autostart actually launches on Windows.

The Startup / Run entry runs the venv's **`pythonw.exe -m tintaview`** rather than the
`tintaview.exe` console shim pip generates, for two reasons that both matter:

- `pythonw.exe` is the windowed interpreter, so a tray app started at login does not
  also open a console window and leave it on the taskbar for the whole session.
- `pythonw.exe` is Authenticode-signed by the Python Software Foundation, and a venv's
  copy keeps that signature. Windows **Smart App Control** blocks executables that are
  neither signed nor cloud-reputable — which is exactly why TintaView is not shipped as
  a compiled bundle (AGENTS.md, "Packaging: no compiled bundle, ever").

G HUB's legacy LED SDK silently no-ops under `pythonw` (measured). The tray still
launches as `pythonw`; `GHubEngine` spawns a `python.exe` sidecar only for that engine
(`engines/ghub_sidecar.py`). Chroma/OpenRGB stay in-process.

`console_scripts` cannot provide this: the shim is a small unsigned `.exe` of its own.
It happens to be cloud-reputable today because pip generates the identical binary for
every package on PyPI, but that is not something to depend on for the login entry point.
"""

from __future__ import annotations

import sys

from tintaview.cli import main

if __name__ == "__main__":
    sys.exit(main())
