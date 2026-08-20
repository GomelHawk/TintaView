"""Interactive verify of the *checkout* G HUB path (new code, not installed 0.3.3).

Run from Windows PowerShell with the install venv's interpreter, but the script path
must be this repo — `sys.path` puts the checkout first, so you exercise local
`engines/ghub.py` + `ghub_env.py`, not the wheel in the venv.

Quit the TintaView tray first (one LogiLedInit per process). G HUB must be running.

    C:\\Users\\igork\\AppData\\Local\\TintaView\\venv\\Scripts\\python.exe `
      \\\\wsl.localhost\\Ubuntu\\home\\igor\\tintaview\\TintaView\\scripts\\ghub_checkout_verify.py

After each paint step the script asks y/n. Answer honestly — that is the whole point.
"""

from __future__ import annotations

import contextlib
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

_HOLD = 4.0


def _ask(prompt: str) -> bool:
    """True on empty/y/yes. Anything else is no."""
    try:
        raw = input(f"{prompt} [Y/n] ").strip().lower()
    except EOFError:
        print("(no TTY — treating as n)", flush=True)
        return False
    return raw in ("", "y", "yes")


def _banner(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def main() -> int:
    if sys.platform != "win32":
        print("Windows Python only — not WSL python.", file=sys.stderr)
        return 2

    # Import only after path insert so we never silently use the venv's 0.3.3 package.
    import tintaview.engines.ghub as ghub_mod
    import tintaview.engines.ghub_env as ghub_env
    from tintaview.core.config import GHubConfig
    from tintaview.engines.ghub import GHubEngine

    ghub_file = Path(ghub_mod.__file__).resolve()
    env_file = Path(ghub_env.__file__).resolve()
    print("checkout root:", REPO)
    print("loaded ghub.py:    ", ghub_file)
    print("loaded ghub_env.py:", env_file)
    if REPO.resolve() not in ghub_file.parents:
        print(
            "FATAL: ghub.py is not under this checkout — you are not testing the new code.",
            file=sys.stderr,
        )
        return 1
    if not hasattr(ghub_env, "inspect"):
        print("FATAL: ghub_env.inspect missing — old tree?", file=sys.stderr)
        return 1
    if not hasattr(ghub_mod, "_COMMIT_KEY"):
        print(
            "FATAL: checkout ghub.py has no _COMMIT_KEY — this is not the new pump code.",
            file=sys.stderr,
        )
        return 1

    print("\nQuit the TintaView tray if it is running, then press Enter.", flush=True)
    with contextlib.suppress(EOFError):
        input()

    cfg = GHubConfig()
    _banner("1. Environment (new: ghub_env.inspect — no SDK init)")
    info = ghub_env.inspect(cfg)
    print(f"  dll_path:          {info.dll_path}")
    print(f"  ghub running:      {info.running!r}")
    print(f"  dynamic_lighting:  {info.dynamic_lighting!r}")
    print(f"  foreground_only:   {info.foreground_only!r}")
    print(f"  integration:       {info.integration!r}")
    problems = ghub_env.blockers(info)
    if problems:
        print("  blockers:")
        for line in problems:
            print(f"    - {line}")
    else:
        print("  blockers: (none measured)")

    _banner("2. probe() must not touch the SDK (new behaviour)")
    engine = GHubEngine(cfg)
    probe_ok = engine.probe()
    print(f"  probe() -> {probe_ok}")
    print("  (no LogiLedInit yet — Integrations should not gain a new entry from probe alone)")
    if info.dll_path is None:
        print("No DLL — cannot paint. Install/start G HUB, fix blockers above, re-run.", flush=True)
        return 1
    if info.running is False:
        print("G HUB is not running — start it, then re-run.", flush=True)
        return 1

    _banner("3. open() — real LogiLedInitWithName (same as the tray)")
    if not engine.open():
        print("open() failed. Tray still holding the SDK? G HUB up?", file=sys.stderr)
        return 1
    print(f"  open() ok  active={engine.active}  status_note={engine.status_note!r}")

    results: list[tuple[str, bool]] = []

    def paint(label: str, rgb: tuple[int, int, int]) -> None:
        print(f"\n>>> painting {label} {rgb} for {int(_HOLD)}s …", flush=True)
        engine.set_color(*rgb)
        time.sleep(_HOLD)
        note = engine.status_note
        if note:
            print(f"    status_note: {note}", flush=True)
        ok = _ask(f"  Did you SEE {label} on the device?")
        results.append((label, ok))
        print(f"    recorded: {'YES' if ok else 'NO'}", flush=True)

    _banner("4. Paint (same GHubEngine.set_color as the tray — new pump + commit)")
    paint("RED", (255, 0, 0))
    paint("YELLOW", (255, 200, 0))
    paint("GREEN", (0, 255, 0))

    print("\n>>> confirm-style blink RED/off for 6s …", flush=True)
    deadline = time.monotonic() + 6.0
    on = False
    while time.monotonic() < deadline:
        on = not on
        engine.set_color(*( (255, 0, 0) if on else (0, 0, 0) ))
        time.sleep(0.4)
    blink_ok = _ask("  Did you SEE the red blink?")
    results.append(("BLINK", blink_ok))

    print("\n>>> close() — RestoreLighting then LogiLedShutdown (measured handback) …", flush=True)
    engine.close()
    time.sleep(2.0)
    print("close() returned (SDK shut down). Look at the device NOW.", flush=True)
    restore_ok = _ask(
        "  Is lighting back to your G HUB profile already (not still our red)?"
    )
    results.append(("RESTORE", restore_ok))

    print("\n>>> reopen after Shutdown — second session must Init again …", flush=True)
    if not engine.open():
        print("  re-open() FAILED after Shutdown — tell the agent.", flush=True)
        results.append(("REOPEN", False))
    else:
        engine.set_color(0, 255, 0)
        time.sleep(_HOLD)
        reopen_ok = _ask("  Did you SEE GREEN again after re-open?")
        results.append(("REOPEN", reopen_ok))
        engine.close()
        time.sleep(1.5)
        restore2 = _ask("  After second close, back to G HUB profile again?")
        results.append(("RESTORE2", restore2))

    _banner("Summary — tell the agent this block")
    print(f"  ghub.py: {ghub_file}")
    print(f"  running={info.running!r}  dynamic_lighting={info.dynamic_lighting!r}  "
          f"integration={info.integration!r}")
    for label, ok in results:
        print(f"  {label:8}  {'WORKS' if ok else 'FAIL'}")
    all_ok = all(ok for _, ok in results)
    print(f"\n  overall: {'ALL WORKS' if all_ok else 'SOME FAILED'}")
    print(f"  final status_note: {engine.status_note!r}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
