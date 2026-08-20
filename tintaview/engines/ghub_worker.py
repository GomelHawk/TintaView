"""``python -m tintaview.engines.ghub_worker`` — LED SDK process for the G HUB sidecar.

Spawned by the tray when it runs under ``pythonw.exe``. See ``ghub_sidecar.py``.
"""

from __future__ import annotations

import sys

from .ghub_sidecar import worker_main

if __name__ == "__main__":
    sys.exit(worker_main())
