"""Full TintaView-tray lighting path against G HUB, with every SDK call printed.

This is **not** `doctor --paint` (raw engine + sleep). It drives colours the same way
the live tray does:

  Config.load() → LightController.apply(status) → make_engine → GHubEngine
  open / set_color / blink thread / close / heartbeat

Quit the TintaView tray first (one LogiLedInit per process).

Windows PowerShell (checkout code on the path):

    & "$env:LOCALAPPDATA\\TintaView\\venv\\Scripts\\python.exe" `
      \\\\wsl.localhost\\Ubuntu\\home\\igor\\tintaview\\TintaView\\scripts\\ghub_tray_simulate.py

Installed wheel only (no checkout on sys.path):

    & "$env:LOCALAPPDATA\\TintaView\\venv\\Scripts\\python.exe" `
      \\\\wsl.localhost\\Ubuntu\\home\\igor\\tintaview\\TintaView\\scripts\\ghub_tray_simulate.py `
      --installed

After each step answer y/n honestly. Paste the whole Summary block back to the agent.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_HOLD = 4.0
_CONFIRM_SECONDS = 6.0

#: Set by ``--yes``: no interactive prompts (needed under ``pythonw.exe``, which has
#: no console). Eyes still matter — watch the mouse; the Summary will say AUTO-YES.
_AUTO_YES = False


def _ask(prompt: str) -> bool:
    if _AUTO_YES:
        print(f"{prompt} [Y/n] Y  (auto)", flush=True)
        return True
    try:
        raw = input(f"{prompt} [Y/n] ").strip().lower()
    except EOFError:
        print("(no TTY — treating as n)", flush=True)
        return False
    return raw in ("", "y", "yes")


def _banner(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def _ensure_qt_app():
    """Match the live tray: a QApplication so the process has a Win32 message queue.

    Bare ``pythonw.exe`` has no console and no window. G HUB's LED SDK init then
    hangs inside ``LogiLedInitWithName`` (log stops at TRACE _init_on_pump with no
    return). ``python.exe`` works without Qt because the console supplies a queue;
    the tray always has Qt — so a pythonw simulation without Qt is not the tray path.

    Never pass a ``\\\\wsl.localhost\\...`` script path as argv[0]: Qt's platform
    plugin aborts hard under pythonw (log stops right after the 3b banner).
    """
    try:
        from PySide6 import QtWidgets
    except ImportError:
        print(
            "WARNING: PySide6 not importable — pythonw run may hang on LogiLedInit "
            "(install [ui] extra / use the TintaView venv).",
            flush=True,
        )
        return None
    app = QtWidgets.QApplication.instance()
    if app is None:
        print("  creating QApplication(['tv-ghub-simulate']) …", flush=True)
        try:
            app = QtWidgets.QApplication(["tv-ghub-simulate"])
            app.setQuitOnLastWindowClosed(False)
        except Exception:
            import traceback

            print("  QApplication FAILED:", flush=True)
            traceback.print_exc()
            return None
    print(f"  Qt QApplication: {type(app).__name__} (tray-like message queue)", flush=True)
    return app


def _hold_sleep(app, seconds: float) -> None:
    """Sleep while pumping Qt events — same shape as a live tray event loop."""
    if seconds <= 0:
        return
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if app is not None:
            app.processEvents()
        time.sleep(0.05)


def _install_sdk_trace(ghub_mod) -> None:
    """Wrap every Python-level SDK helper so returns are visible without guessing."""

    def wrap(name: str) -> None:
        orig = getattr(ghub_mod, name)

        def traced(*args, **kwargs):
            # args[0] is often the dll; keep the line readable
            rest = args[1:] if args else ()
            print(f"  TRACE {name}{rest!r} …", flush=True)
            try:
                result = orig(*args, **kwargs)
            except Exception as e:
                print(f"  TRACE {name} RAISED {e!r}", flush=True)
                raise
            print(f"  TRACE {name} -> {result!r}", flush=True)
            return result

        setattr(ghub_mod, name, traced)

    for name in (
        "_paint",
        "_commit_paint",
        "_save_lighting",
        "_shutdown_sdk",
        "_nudge_pct",
        "_drain_windows_messages",
    ):
        if hasattr(ghub_mod, name):
            wrap(name)

    # Init runs as a bound method; wrap the pump-callable factory instead.
    orig_init = ghub_mod.GHubEngine._init_on_pump

    def init_traced(self):
        print(
            f"  TRACE _init_on_pump exe={sys.executable!r} pid={os.getpid()} "
            f"app_name={ghub_mod._APP_NAME!r}",
            flush=True,
        )
        if hasattr(ghub_mod, "_ensure_thread_message_queue"):
            print("  TRACE calling _ensure_thread_message_queue …", flush=True)
        ok = orig_init(self)
        print(f"  TRACE _init_on_pump -> {ok!r}  initialized={self._initialized}", flush=True)
        return ok

    ghub_mod.GHubEngine._init_on_pump = init_traced  # type: ignore[method-assign]

    orig_set = ghub_mod.GHubEngine.set_color

    def set_traced(self, r: int, g: int, b: int) -> None:
        print(
            f"  TRACE set_color rgb=({r},{g},{b}) active={self.active} "
            f"note={self.status_note!r} failures={self._set_failures}",
            flush=True,
        )
        orig_set(self, r, g, b)
        print(
            f"  TRACE set_color done note={self.status_note!r} failures={self._set_failures}",
            flush=True,
        )

    ghub_mod.GHubEngine.set_color = set_traced  # type: ignore[method-assign]


def _wrap_dll_exports(dll) -> None:
    """Log raw ctypes returns — G HUB often returns True while the mouse ignores us."""
    if dll is None or getattr(dll, "_tv_traced", False):
        return
    for name in (
        "LogiLedInit",
        "LogiLedInitWithName",
        "LogiLedSetTargetDevice",
        "LogiLedSaveCurrentLighting",
        "LogiLedRestoreLighting",
        "LogiLedSetLighting",
        "LogiLedSetLightingForTargetZone",
        "LogiLedShutdown",
    ):
        orig = getattr(dll, name, None)
        if orig is None:
            print(f"  DLL missing export: {name}", flush=True)
            continue

        def make(n: str, o):
            def wrapped(*args):
                result = o(*args)
                print(f"  DLL {n}{args!r} -> {result!r}", flush=True)
                return result

            return wrapped

        setattr(dll, name, make(name, orig))
    dll._tv_traced = True  # type: ignore[attr-defined]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--installed",
        action="store_true",
        help="use the venv's installed tintaview package (do not prepend the checkout)",
    )
    parser.add_argument(
        "--hold",
        type=float,
        default=_HOLD,
        help=f"seconds to hold each solid colour (default {_HOLD})",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="answer Y to every prompt (required under pythonw.exe — watch the mouse)",
    )
    parser.add_argument(
        "--skip-wait",
        action="store_true",
        help="do not wait for Enter after 'quit the tray' (for non-interactive runs)",
    )
    parser.add_argument(
        "--log",
        metavar="PATH",
        help="also write all output to this file (use under pythonw — console redirect is unreliable)",
    )
    args = parser.parse_args(argv)

    global _AUTO_YES
    _AUTO_YES = bool(args.yes)

    log_fh = None
    if args.log:
        log_path = Path(args.log)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # Intentionally not a context manager: under pythonw this handle becomes
        # sys.stdout/stderr for the process lifetime and must stay open (see finally).
        log_fh = open(log_path, "w", encoding="utf-8", buffering=1)  # noqa: SIM115
        # pythonw.exe has no console: sys.stdout / stderr are None and every print
        # would crash (or PowerShell `*>` captures nothing). Always bind real files.
        if sys.stdout is None and sys.stderr is None:
            sys.stdout = log_fh
            sys.stderr = log_fh
        else:

            class _Tee:
                def __init__(self, *streams):
                    self._streams = streams

                def write(self, data: str) -> int:
                    for s in self._streams:
                        if s is None:
                            continue
                        s.write(data)
                        s.flush()
                    return len(data)

                def flush(self) -> None:
                    for s in self._streams:
                        if s is not None:
                            s.flush()

            sys.stdout = _Tee(sys.stdout, log_fh)  # type: ignore[assignment]
            sys.stderr = _Tee(sys.stderr, log_fh)  # type: ignore[assignment]
        print(f"(logging to {log_path})", flush=True)
    elif sys.stdout is None or sys.stderr is None:
        print(
            "FATAL: running under pythonw with no console and no --log. "
            "Re-run with --log PATH.",
            file=sys.__stderr__ or sys.stderr,
        )
        return 2

    try:
        return _main_body(args)
    finally:
        if log_fh is not None:
            with contextlib.suppress(Exception):
                log_fh.flush()
            # Do not close if still bound as sys.stdout (pythonw path).
            if sys.stdout is not log_fh and sys.stderr is not log_fh:
                log_fh.close()


def _main_body(args: argparse.Namespace) -> int:
    if sys.platform != "win32":
        print("Windows Python only — not WSL.", file=sys.stderr)
        return 2

    if not args.installed:
        # TEMP copies of this script have the wrong REPO parent; allow an explicit root.
        repo = os.environ.get("TINTAVIEW_REPO", str(REPO))
        sys.path.insert(0, repo)
        print(f"  sys.path[0]: {repo}", flush=True)

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
        force=True,
    )

    import tintaview
    import tintaview.engines.ghub as ghub_mod
    import tintaview.engines.ghub_env as ghub_env
    from tintaview.core import config as config_mod
    from tintaview.core.controller import LightController
    from tintaview.core.events import (
        STATUS_CONFIRM,
        STATUS_IDLE,
        STATUS_NONE,
        STATUS_WORKING,
    )
    from tintaview.engines.factory import make_engine

    ghub_file = Path(ghub_mod.__file__).resolve()
    tv_file = Path(tintaview.__file__).resolve()

    _banner("0. Process / package under test")
    print(f"  executable:  {sys.executable}")
    print(f"  pid:         {os.getpid()}")
    print(f"  tintaview:   {getattr(tintaview, '__version__', '?')} @ {tv_file}")
    print(f"  ghub.py:     {ghub_file}")
    print(f"  checkout:    {REPO}")
    if args.installed:
        print("  mode:        --installed (venv package)")
    elif REPO.resolve() not in ghub_file.parents:
        print(
            "  WARNING: ghub.py is NOT under the checkout — you are testing the "
            "installed wheel. Pass --installed deliberately, or fix the path.",
            flush=True,
        )
    else:
        print("  mode:        checkout first on sys.path")

    if args.skip_wait:
        print("\n(skip-wait: assuming the TintaView tray is already quit)", flush=True)
    else:
        print("\nQuit the TintaView tray completely, then press Enter.", flush=True)
        with contextlib.suppress(EOFError):
            input()

    _install_sdk_trace(ghub_mod)

    cfg = config_mod.load()
    _banner("1. Config (same file the tray reads)")
    print(f"  path:         {config_mod.config_path()}")
    print(f"  engine.mode:  {cfg.engine.mode!r}")
    print(f"  engine.order: {cfg.engine.order!r}")
    print(f"  ghub.device_types: {cfg.engine.ghub.device_types!r}")
    print(f"  ghub.dll_path:     {cfg.engine.ghub.dll_path!r}")
    for status in (STATUS_IDLE, STATUS_WORKING, STATUS_CONFIRM):
        icon = getattr(cfg.colors, status)
        device = cfg.colors.device_rgb(status)
        print(f"  colours.{status}: icon={icon}  device_rgb={device}")

    _banner("2. ghub_env.inspect (no SDK init)")
    info = ghub_env.inspect(cfg.engine.ghub)
    print(f"  dll_path:         {info.dll_path}")
    print(f"  running:          {info.running!r}")
    print(f"  dynamic_lighting: {info.dynamic_lighting!r}")
    print(f"  foreground_only:  {info.foreground_only!r}")
    print(f"  integration:      {info.integration!r}")
    for line in ghub_env.blockers(info):
        print(f"  blocker: {line}")

    _banner("3. factory.make_engine (same as LightController first use)")
    engine = make_engine(cfg)
    print(f"  make_engine -> {engine.name!r} ({engine.display_name})")
    if engine.name != "ghub":
        print(
            f"FATAL: expected ghub, got {engine.name!r}. Fix engine.mode in config.",
            file=sys.stderr,
        )
        return 1

    # Probe must not init; then open and wrap the live DLL.
    print(f"  probe() -> {engine.probe()}", flush=True)

    _banner("3b. QApplication (required under pythonw — same as the tray)")
    app = _ensure_qt_app()

    _banner("4. LightController — exact tray apply() path")
    # Inject the already-built engine so we keep the same traced instance.
    ctrl = LightController(cfg, engine=engine)
    ctrl.start_heartbeat()

    results: list[tuple[str, bool]] = []

    def step(label: str, status: str, *, hold: float | None = None, ask: str) -> None:
        hold = args.hold if hold is None else hold
        print(f"\n>>> apply({status!r})  [{label}]", flush=True)
        ctrl.apply(status)
        # First open loads the DLL — wrap exports so later paints show raw returns.
        _wrap_dll_exports(getattr(engine, "_dll", None))
        st = ctrl.engine_status()
        print(
            f"  /state-like engine={st}  controller.blinking={ctrl.blinking}",
            flush=True,
        )
        if hold > 0:
            print(f"  holding {hold:.1f}s …", flush=True)
            _hold_sleep(app, hold)
        ok = _ask(ask)
        results.append((label, ok))
        print(f"  recorded: {'YES' if ok else 'NO'}", flush=True)

    try:
        step(
            "IDLE",
            STATUS_IDLE,
            ask="Did the mouse become solid IDLE green (device palette)?",
        )
        step(
            "WORKING",
            STATUS_WORKING,
            ask="Did the mouse become solid WORKING yellow/amber?",
        )
        print(f"\n>>> apply({STATUS_CONFIRM!r}) — blink thread for {_CONFIRM_SECONDS:.0f}s",
              flush=True)
        ctrl.apply(STATUS_CONFIRM)
        _wrap_dll_exports(getattr(engine, "_dll", None))
        print(f"  engine={ctrl.engine_status()} blinking={ctrl.blinking}", flush=True)
        _hold_sleep(app, _CONFIRM_SECONDS)
        blink_ok = _ask("Did you SEE confirm red blinking on/off?")
        results.append(("CONFIRM_BLINK", blink_ok))
        print(f"  recorded: {'YES' if blink_ok else 'NO'}", flush=True)

        step(
            "WORKING2",
            STATUS_WORKING,
            ask="Back to solid WORKING yellow after confirm?",
        )
        step(
            "IDLE2",
            STATUS_IDLE,
            ask="Back to solid IDLE green?",
        )

        print(f"\n>>> apply({STATUS_NONE!r}) — close / hand back to G HUB", flush=True)
        ctrl.apply(STATUS_NONE)
        _hold_sleep(app, 2.0)
        print(f"  engine={ctrl.engine_status()}", flush=True)
        restore_ok = _ask("Is the mouse back on your G HUB profile (not our solid colour)?")
        results.append(("RESTORE", restore_ok))
    finally:
        ctrl.shutdown()

    _banner("Summary — paste this whole block to the agent")
    print(f"  exe:        {sys.executable}")
    print(f"  pid:        {os.getpid()}")
    print(f"  ghub.py:    {ghub_file}")
    print(f"  mode:       {'installed' if args.installed else 'checkout'}")
    print(f"  auto_yes:   {_AUTO_YES}")
    print(f"  engine.mode:{cfg.engine.mode!r}")
    print(
        f"  env:        running={info.running!r} integration={info.integration!r} "
        f"dynamic_lighting={info.dynamic_lighting!r}"
    )
    print(f"  status_note:{getattr(engine, 'status_note', None)!r}")
    for label, ok in results:
        seen = "AUTO-YES (watch mouse!)" if _AUTO_YES else ("WORKS" if ok else "FAIL")
        print(f"  {label:14}  {seen}")
    all_ok = all(ok for _, ok in results)
    print(f"\n  overall: {'ALL WORKS' if all_ok else 'SOME FAILED'}")
    if _AUTO_YES:
        print(
            "  --yes was set: Summary answers are not eye-confirmed. "
            "Did the mouse actually cycle green/yellow/red blink?"
        )
    elif all_ok:
        print(
            "  If this ALL WORKS under python.exe but the live pythonw tray does not: "
            "the tray must paint via the python.exe G HUB sidecar "
            "(log: 'session opened via python.exe sidecar'), not by launching the "
            "whole tray as python.exe."
        )
    else:
        print(
            "  If this FAILS the same way as the tray: the bug is in the shared "
            "GHubEngine path (SDK silent no-op / zones / init) — not 'Integrations UI'."
        )
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
