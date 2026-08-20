"""Hardware check for the G HUB engine in this checkout.

Must run under **Windows** Python, with G HUB already started. The tray must not be
holding the LED SDK — quit TintaView first, or LogiLedInit will fail.

Uses `tintaview/engines/ghub.py` from this tree, not the installed venv. Config, hooks
and the venv are not touched. Lighting is restored on exit.

    <prefix>\\venv\\Scripts\\python.exe scripts\\ghub_smoke.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

_HOLD = 5.0
_WARMUP = 3.0
#: Same as LightController's confirm blink — confirm colour vs black, `colors.blink_ms`.
_BLINK_SECONDS = 8.0


def _blink_confirm(engine, rgb: tuple[int, int, int], interval: float, seconds: float) -> None:
    """Confirm-style blink: red / off on the same cadence the tray uses."""
    deadline = time.monotonic() + seconds
    on = False
    while time.monotonic() < deadline:
        on = not on
        engine.set_color(*(rgb if on else (0, 0, 0)))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))


def main() -> int:
    if sys.platform != "win32":
        print("this has to run under Windows Python — the G HUB SDK is a Windows DLL",
              file=sys.stderr)
        print("use the TintaView venv's python.exe, not WSL python", file=sys.stderr)
        return 2

    from tintaview.core.config import Config, GHubConfig
    from tintaview.core.events import STATUS_CONFIRM
    from tintaview.engines.ghub import GHubEngine, discover_dll_path, format_setup_notes

    path = discover_dll_path(GHubConfig())
    print(f"DLL:    {path or '(not found)'}")
    if path is None:
        return 1

    print(format_setup_notes())
    print()

    engine = GHubEngine(GHubConfig())
    if not engine.open():
        print("open() failed — quit the TintaView tray, make sure G HUB is running,",
              file=sys.stderr)
        print("then retry. LogiLedInit is one-per-process and hates a second client.",
              file=sys.stderr)
        return 1

    colors = Config().colors
    confirm_rgb = colors.device_rgb(STATUS_CONFIRM)
    blink_s = max(colors.blink_ms, 10) / 1000.0

    print(f"engine: {engine.display_name} active={engine.active}")
    print("build: split-paint + 1% nudge + PeekMessage")
    print(f"warmup {int(_WARMUP)}s (ignore whatever flashes — G HUB is taking over)\n",
          flush=True)
    engine.set_color(*confirm_rgb)
    time.sleep(_WARMUP)

    try:
        print(
            f">>> BLINK RED — confirm-style on/off for {int(_BLINK_SECONDS)}s "
            f"(#{colors.confirm[1:]} / off every {colors.blink_ms} ms)",
            flush=True,
        )
        _blink_confirm(engine, confirm_rgb, blink_s, _BLINK_SECONDS)

        for name, rgb in (
            ("RED", (255, 0, 0)),
            ("YELLOW", (255, 200, 0)),
            ("GREEN", (0, 255, 0)),
        ):
            print(f">>> {name} — mouse should become {name.lower()}", flush=True)
            engine.set_color(*rgb)
            time.sleep(_HOLD / 2)
            print(f"    …halfway; if it is not {name.lower()} yet, wait", flush=True)
            time.sleep(_HOLD / 2)
    finally:
        engine.close()
        print("\nrestored to G HUB's own profile.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
