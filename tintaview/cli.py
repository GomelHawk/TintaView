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
import sys
from pathlib import Path

from . import __version__
from .core import config as config_mod
from .core import log as log_mod


def _cmd_run(args: argparse.Namespace) -> int:
    cfg = config_mod.load()
    log_mod.setup("tintaview")

    from .core.server import StatusServer

    server = StatusServer(cfg)
    if not server.start():
        # Another instance owns the port. For the tray that's a reason to stop (two
        # tray icons would be confusing); headless can just exit quietly.
        print(f"TintaView is already running on {cfg.server.host}:{cfg.server.port}.",
              file=sys.stderr)
        return 0

    if args.headless:
        print(f"TintaView status broker listening on {server.url}")
        try:
            import threading

            threading.Event().wait()
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

    try:
        return run_tray(cfg, server)
    finally:
        server.stop()


def _cmd_setup(args: argparse.Namespace) -> int:
    from .ui.wizard import run_wizard

    return run_wizard(platform_override=args.platform, assume_yes=args.yes)


def _cmd_doctor(args: argparse.Namespace) -> int:
    from .install.doctor import run_doctor

    return run_doctor(verbose=args.verbose)


def _cmd_hooks(args: argparse.Namespace) -> int:
    from .agents import base as agents_base
    from .install import hooks as hooks_mod

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

    for adapter in adapters:
        if args.action == "status":
            state = hooks_mod.status(adapter, hook_bin, args.scope, project_dir)
            print(f"{adapter.display_name:<14} {state}")
            continue

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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tintaview", description=__doc__.splitlines()[0])
    p.add_argument("--version", action="version", version=f"TintaView {__version__}")
    p.add_argument("--headless", action="store_true",
                   help="run the status broker without the tray UI")
    p.set_defaults(func=_cmd_run, headless=False)

    sub = p.add_subparsers(dest="command")

    run = sub.add_parser("run", help="run the tray (default)")
    run.add_argument("--headless", action="store_true")
    run.set_defaults(func=_cmd_run)

    setup = sub.add_parser("setup", help="install or reconfigure TintaView")
    setup.add_argument("--platform", help="force the platform when detection is wrong "
                                          "(windows|wsl|linux|macos)")
    setup.add_argument("-y", "--yes", action="store_true", help="accept every default")
    setup.set_defaults(func=_cmd_setup)

    doctor = sub.add_parser("doctor", help="diagnose an install")
    doctor.add_argument("-v", "--verbose", action="store_true")
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
