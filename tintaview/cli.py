"""`tintaview` — one entry point for every mode the app runs in.

    tintaview                 tray UI with the status broker in-process (the normal case)
    tintaview --headless      broker only, no GUI (servers, WSL-only boxes)
    tintaview setup           the install/reconfigure wizard
    tintaview doctor          diagnostics
    tintaview hooks …         install / status / uninstall the agents' hooks
    tintaview update          check for and install a new version

One process, not two — rather than splitting the broker and the tray into separate
executables, which meant two autostart entries, two logs and two things to update. The
broker is cheap enough to live inside the tray process, so it does.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .core import config as config_mod
from .core import log as log_mod

log = logging.getLogger(__name__)


def _cmd_run(args: argparse.Namespace) -> int:
    # Logging FIRST, before anything that can fail. Under `pythonw.exe` there is no
    # console and no stderr worth the name, so a config that raises on the way in used
    # to be an invisible exit: no window, no message, nothing in the log. `log_mod.setup`
    # deliberately needs nothing from the config (only `config_dir()`, which is paths and
    # env), so this ordering costs nothing.
    log_mod.setup("tintaview")
    cfg = config_mod.load()

    # Before anything can render a label: the stats providers build their row text on
    # worker threads started by the tray, and `TrayApp` itself applies this too (so an
    # embedder or a test constructing it directly gets the configured language), but the
    # first usage poll can be in flight before that constructor is even reached.
    from .i18n import set_language

    set_language(cfg.ui.language)

    if not args.headless and sys.platform == "win32":
        # Optional: if someone launches the tray via python.exe, hide the console.
        # Autostart uses pythonw; G HUB painting goes through a python.exe sidecar.
        from .install.win_console import hide_console_if_python_exe

        hide_console_if_python_exe()

    from .core.server import StatusServer

    server = StatusServer(cfg)
    if not server.start():
        return _defer_to_running_instance(cfg, headless=args.headless)

    if args.headless:
        print(f"TintaView status broker listening on {server.url}")
        import threading

        park = threading.Event()
        # systemd/launchd stop the daemon with SIGTERM, and Ctrl+C sends SIGINT. Both
        # have to reach the `finally` below, or the process dies holding the device and
        # the lights stay on TintaView's last colour until the vendor app is restarted.
        _install_signal_handlers(lambda *_a: park.set())
        # `/quit` is the same shutdown, asked for over HTTP by `install/restart.py` or a
        # second launch, so it lands on the same event rather than a second path.
        server.on_quit = park.set
        try:
            park.wait()
        except KeyboardInterrupt:
            pass
        finally:
            server.stop()
        return 0

    try:
        from .ui.tray import run_tray
    except ImportError as exc:
        server.stop()
        print(f"The tray needs PySide6 ({exc}). Install it, or run with --headless.",
              file=sys.stderr)
        return 2

    # Qt installs no signal handling of its own, and a Python handler only runs when the
    # interpreter next gets control — which the tray's periodic QTimer guarantees, so
    # quitting the QApplication from the handler is enough. PySide6 is optional, so the
    # import stays inside the handler and never at module scope.
    def _quit_qt(*_args) -> None:
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            return
        app = QApplication.instance()
        if app is not None:
            app.quit()

    _install_signal_handlers(_quit_qt)

    try:
        return run_tray(cfg, server)
    finally:
        server.stop()


def _install_signal_handlers(handler) -> None:
    """Install `handler` for SIGTERM and SIGINT, where the platform has them.

    Windows has no SIGTERM worth the name and a service manager may hand us a process
    whose signals can't be reset, so every step is guarded: failing to install one must
    never stop TintaView from starting.
    """
    import signal

    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError, RuntimeError) as exc:  # not the main thread, etc.
            log.debug("could not install a %s handler: %r", name, exc)


def _defer_to_running_instance(cfg, headless: bool) -> int:
    """Another process owns the port, so this one steps aside.

    Two tray icons would be confusing, but exiting in silence is worse: launched from a
    Start-menu shortcut there is no console to read the message in, so a second launch
    looks like nothing happened at all. Asking the running instance to open its usage
    panel is what the user meant by launching the app again, so do that — the running
    instance is TintaView's own `/show`, on loopback, and the worst it can do is pop a
    window a tray click already opens. A headless daemon has no panel and no `on_show`,
    so it answers `shown: false` and this falls back to the plain message.
    """
    from .core.server import request_show

    host, port = cfg.server.host, cfg.server.port
    if not headless and request_show(host, port):
        print(f"TintaView is already running on {host}:{port} — showed its usage panel.")
        return 0
    print(f"TintaView is already running on {host}:{port}.", file=sys.stderr)
    return 0


def _cmd_setup(args: argparse.Namespace) -> int:
    from .ui.wizard import run_wizard

    return run_wizard(platform_override=args.platform, assume_yes=args.yes)


def _cmd_doctor(args: argparse.Namespace) -> int:
    from .install.doctor import run_doctor

    return run_doctor(verbose=args.verbose, paint=args.paint)


def _cmd_hooks(args: argparse.Namespace) -> int:
    from .agents import base as agents_base
    from .install import hooks as hooks_mod
    from .install import wsl

    cfg = config_mod.load()
    hook_bin = Path(args.hook_bin) if args.hook_bin else config_mod.hook_bin_path()

    if args.agent == "all":
        adapters = [a for a in agents_base.all_agents() if cfg.is_enabled(a.key) or args.all_agents]
        if not adapters:
            adapters = agents_base.all_agents()
    else:
        adapter = agents_base.get(args.agent)
        if adapter is None:
            print(f"unknown agent {args.agent!r}", file=sys.stderr)
            return 2
        adapters = [adapter]

    project_dir = Path.cwd() if args.scope == "project" else None
    failures = 0

    # Deploy the hook script itself before wiring any agent to it — otherwise the
    # configs would point at a path that doesn't exist yet, which fails silently at
    # runtime. `doctor` tells users to run this command to fix a missing hook script,
    # so it has to actually write one.
    if args.action == "install" and not args.hook_bin:
        from .install.detect import detect
        from .install.hookscript import install_hook_script

        try:
            written = install_hook_script(cfg, detect())
            print(f"Hook script     {written}")
        except OSError as exc:
            print(f"could not install the hook script: {exc}", file=sys.stderr)
            failures += 1

    if args.action == "status":
        # Measured against the install that actually wrote the hooks: in a WSL split that
        # is the distro's tv-hook.sh and the agent's config behind its UNC path, not the
        # Windows-side tv-hook.cmd and C:\Users\you (AGENTS.md, "WSL split install").
        check = wsl.HookCheck(hook_bin) if args.hook_bin else wsl.hook_check(cfg)
        if check is None:
            print("WSL distro not reachable — cannot check the hooks inside it", file=sys.stderr)
            return 1
        for adapter in adapters:
            state = wsl.hook_status(cfg, adapter, check, args.scope, project_dir)
            print(f"{adapter.display_name:<14} {state}")
        return 0

    for adapter in adapters:

        try:
            plan = (hooks_mod.plan_install(adapter, hook_bin, args.scope, project_dir)
                    if args.action == "install"
                    else hooks_mod.plan_uninstall(adapter, args.scope, project_dir))
        except ValueError as exc:
            print(f"{adapter.display_name}: {exc}", file=sys.stderr)
            failures += 1
            continue

        if not plan.changes:
            print(f"{adapter.display_name:<14} already up to date ({plan.path})")
            continue

        print(f"\n=== {adapter.display_name} — {plan.path} ===")
        for note in plan.notes:
            print(f"  note: {note}")
        print(plan.diff)

        if not (args.yes or _confirm(f"Apply this change to {plan.path}?")):
            print("  skipped.")
            continue

        backup = hooks_mod.apply(plan)
        print(f"  written. backup: {backup}" if backup else "  written.")

        # Codex additionally gates hooks behind a feature flag in its own config.toml.
        # Wiring hooks.json without it means nothing ever fires, so the CLI has to
        # offer the same edit the wizard does rather than leaving a half-install.
        if adapter.key == "codex":
            failures += _apply_codex_flag(adapter, args.yes)

    return 1 if failures else 0


def _apply_codex_flag(adapter, assume_yes: bool) -> int:
    """Offer the Codex hooks feature-flag edit. Returns 1 on failure, else 0."""
    from .install import codex_flag

    path = adapter.default_home() / "config.toml"
    try:
        plan = codex_flag.plan(path, adapter.version())
    except Exception as exc:  # noqa: BLE001 - a malformed TOML must not abort the run
        print(f"  could not read {path}: {exc}", file=sys.stderr)
        return 1

    if plan.action == "unsupported":
        print(f"  note: {plan.reason}")
        return 0
    if not plan.changes:
        print(f"  feature flag: {plan.reason}")
        return 0

    print(f"\n--- {plan.path} (Codex hooks feature flag) ---")
    print(f"  note: {plan.reason}")
    print(codex_flag.diff(plan))
    if not (assume_yes or _confirm(f"Apply this change to {plan.path}?")):
        print("  skipped — Codex hooks will not fire until this flag is set.")
        return 0
    codex_flag.apply(plan)
    print("  written.")
    return 0


def _cmd_update(args: argparse.Namespace) -> int:
    from .install.update import run_update

    return run_update(check_only=args.check_only)


def _confirm(question: str) -> bool:
    try:
        answer = input(f"{question} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def _add_headless(parser: argparse.ArgumentParser, *, default) -> None:
    """Declare `--headless` on `parser`, in one place for all three spellings.

    `tintaview --headless`, `tintaview run --headless` and `tintaview --headless run`
    all have to mean the same thing. The last one used to start the tray: argparse
    parses a subcommand into its own namespace and copies *every* key of it over the
    one the top-level parser produced, so the `run` parser's `headless=False` default
    overwrote the True the top-level flag had just set. `SUPPRESS` on the subparser is
    the fix — the key only exists there when the flag was actually passed there.
    """
    parser.add_argument("--headless", action="store_true", default=default,
                        help="run the status broker without the tray UI")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tintaview", description=__doc__.splitlines()[0])
    p.add_argument("--version", action="version", version=f"TintaView {__version__}")
    _add_headless(p, default=False)
    p.set_defaults(func=_cmd_run)

    sub = p.add_subparsers(dest="command")

    run = sub.add_parser("run", help="run the tray (default)")
    _add_headless(run, default=argparse.SUPPRESS)
    run.set_defaults(func=_cmd_run)

    setup = sub.add_parser("setup", help="install or reconfigure TintaView")
    setup.add_argument("--platform", help="force the platform when detection is wrong "
                                          "(windows|wsl|linux|macos)")
    setup.add_argument("-y", "--yes", action="store_true", help="accept every default")
    setup.set_defaults(func=_cmd_setup)

    doctor = sub.add_parser("doctor", help="diagnose an install")
    doctor.add_argument("-v", "--verbose", action="store_true")
    doctor.add_argument(
        "--paint", action="store_true",
        help="cycle the lighting engine through red/yellow/green and ask if you saw it",
    )
    doctor.set_defaults(func=_cmd_doctor)

    hooks = sub.add_parser("hooks", help="manage the agents' hook configuration")
    hooks.add_argument("action", choices=["install", "status", "uninstall"])
    hooks.add_argument("--agent", default="all", help="claude | codex | cursor | all")
    hooks.add_argument("--scope", default="user", choices=["user", "project"])
    hooks.add_argument("--hook-bin", help="override the tv-hook path written into configs")
    hooks.add_argument("--all-agents", action="store_true",
                       help="include agents not enabled in the config")
    hooks.add_argument("-y", "--yes", action="store_true",
                       help="apply without showing the diff for confirmation")
    hooks.set_defaults(func=_cmd_hooks)

    update = sub.add_parser("update", help="check for a new version")
    update.add_argument("--check-only", action="store_true")
    update.set_defaults(func=_cmd_update)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
