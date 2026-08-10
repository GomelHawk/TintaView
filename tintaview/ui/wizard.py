"""The setup wizard — a text-mode flow, deliberately, because it's the same code run by
`Setup.exe`'s post-install step, `install.sh` over SSH, and `tintaview setup` in a
terminal. Qt would rule out the first two, so this is plain `print`/`input` throughout.

This is also the one place in TintaView a non-technical user actually looks at, so the
rules that matter more here than anywhere else in the codebase:

- every prompt shows its default and accepts an empty answer;
- bad input re-prompts, it never raises;
- nothing destructive happens without the user seeing exactly what will change first
  (the hook diffs in particular — see `install.hooks`);
- `assume_yes` answers every question with its default and asks nothing, for silent/CI
  installs;
- re-running the wizard against an existing config shows *current* values as defaults,
  not fresh ones, so "reconfigure" doesn't mean "start over".
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from ..agents import base as agents_base
from ..core import config as config_mod
from ..engines.factory import available_engines
from ..install import codex_flag, detect
from ..install import hooks as hooks_mod
from ..install.detect import (
    MODE_WSL_SPLIT,
    PLATFORM_LINUX,
    PLATFORM_MACOS,
    PLATFORM_WINDOWS,
    PLATFORM_WSL,
    Environment,
)

_LIVE_CHECK_TIMEOUT = 60.0
_LIVE_CHECK_POLL = 1.0


# --------------------------------------------------------------------------- I/O helpers


def _input(prompt: str) -> str:
    """`input()` that treats a closed stdin (EOF) as an empty answer instead of raising —
    the same "never crash on bad input" contract applies to input running out entirely.
    """
    try:
        return input(prompt)
    except EOFError:
        return ""


def _prompt_text(question: str, default: str, assume_yes: bool) -> str:
    if assume_yes:
        return default
    suffix = f" [{default}]" if default else ""
    raw = _input(f"{question}{suffix}: ").strip()
    return raw or default


def _prompt_yes_no(question: str, default: bool, assume_yes: bool) -> bool:
    if assume_yes:
        return default
    hint = "Y/n" if default else "y/N"
    while True:
        raw = _input(f"{question} [{hint}]: ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("Please answer y or n.")


def _prompt_choice(question: str, options: list[tuple[str, str]], default: str, assume_yes: bool) -> str:
    """`options` is `[(key, label), ...]`; the answer must be one of the keys."""
    if assume_yes:
        return default
    keys = [k for k, _ in options]
    print(question)
    for key, label in options:
        marker = "*" if key == default else " "
        print(f"  [{marker}] {key} - {label}")
    while True:
        raw = _input(f"Choose [{default}]: ").strip()
        if not raw:
            return default
        if raw in keys:
            return raw
        print(f"Please type one of: {', '.join(keys)}")


def _prompt_multiselect(
    question: str, items: list[tuple[str, str, bool]], assume_yes: bool
) -> list[str]:
    """`items` is `[(key, label, preselected), ...]`. At least one key must come back —
    re-prompts (or, under `assume_yes`, falls back to the first item) until it does.
    """
    default_keys = [k for k, _, selected in items if selected]
    if assume_yes:
        return default_keys or [items[0][0]]

    while True:
        print(question)
        for i, (_key, label, selected) in enumerate(items, start=1):
            mark = "x" if selected else " "
            print(f"  {i}. [{mark}] {label}")
        print(
            "Type the numbers to toggle (e.g. '1 3'), or press Enter to accept the "
            "list shown above."
        )
        raw = _input("> ").strip()
        if not raw:
            chosen = list(default_keys)
        else:
            chosen = list(default_keys)
            bad = False
            for token in raw.replace(",", " ").split():
                if not token.isdigit() or not (1 <= int(token) <= len(items)):
                    print(f"'{token}' isn't one of the numbers listed above.")
                    bad = True
                    break
                key = items[int(token) - 1][0]
                if key in chosen:
                    chosen.remove(key)
                else:
                    chosen.append(key)
            if bad:
                continue
        if chosen:
            return chosen
        print("Pick at least one — TintaView needs at least one agent to do anything.")


# --------------------------------------------------------------------------- step 1: welcome


def _step_welcome() -> None:
    print("=" * 64)
    print("TintaView setup")
    print("=" * 64)
    print(
        "TintaView watches Claude Code, Codex CLI and Cursor while you work and turns "
        "that into one simple status light: green when idle, yellow while the agent is "
        "busy, red when it's waiting on you.\n"
        "\n"
        "This wizard asks a handful of questions, then writes its settings to:\n"
        f"    {config_mod.config_dir()}\n"
        "\n"
        "Nothing outside of that is changed without showing you exactly what and asking "
        "first."
    )


# --------------------------------------------------------------------------- step 2: platform


def _mode_explainer(env: Environment) -> str:
    if env.platform == PLATFORM_WINDOWS and env.mode == MODE_WSL_SPLIT:
        return (
            "This is a 'WSL split' install: the status light and tray run on Windows, "
            "while the hooks that tell TintaView what your agent is doing get installed "
            "inside your Linux distro, where the agent itself runs."
        )
    if env.platform == PLATFORM_WSL:
        return (
            "You're running this inside a WSL Linux distro. The hooks install here, but "
            "the lights need TintaView's Windows installer run once on the Windows side "
            "too — this side alone gives you hooks with no lights yet."
        )
    if env.platform == PLATFORM_MACOS:
        return "On macOS, TintaView can track status but lighting support is limited."
    return "Everything — hooks, lights and the tray — installs on this one machine."


def _step_platform(env: Environment, assume_yes: bool) -> Environment:
    print("\n=== Platform ===")
    print(f"Detected: {env.platform} (mode: {env.mode})")
    for note in env.notes:
        print(f"  note: {note}")
    print(_mode_explainer(env))

    options = [
        (PLATFORM_WINDOWS, "Windows"),
        (PLATFORM_WSL, "WSL (inside a Linux distro on Windows)"),
        (PLATFORM_LINUX, "Linux"),
        (PLATFORM_MACOS, "macOS"),
    ]
    choice = _prompt_choice(
        "Is that right, or should I set this up for a different platform?",
        options, env.platform, assume_yes,
    )
    if choice != env.platform:
        env = detect.detect(override=choice)
        print(f"Using {env.platform} (mode: {env.mode}) instead.")

    if env.platform == PLATFORM_WINDOWS and env.mode == MODE_WSL_SPLIT:
        distros = env.wsl_distros or detect.wsl_distros()
        if len(distros) > 1:
            distro_options = [(d, d) for d in distros]
            env.distro = _prompt_choice(
                "Which WSL distro runs your coding agents?",
                distro_options, env.distro or distros[0], assume_yes,
            )
        elif distros:
            env.distro = distros[0]
    return env


def _detect_agent(adapter, env: Environment) -> bool:
    """Is `adapter` installed? On a Windows-side wizard driving a WSL split, "installed"
    means installed *inside the distro* — checked over `wsl.exe`, not `Path.home()` on
    this (Windows) machine, which would only ever say "not found".
    """
    if env.platform == PLATFORM_WINDOWS and env.mode == MODE_WSL_SPLIT and env.distro:
        try:
            from ..install import wsl as wsl_mod

            home = wsl_mod.distro_home(env.distro)
            wsl_mod.run_in(env.distro, ["test", "-d", f"{home}/{adapter.default_home().name}"])
            return True
        except Exception:
            return False
    try:
        return bool(adapter.detect())
    except Exception:
        return False


# --------------------------------------------------------------------------- step 3: agents


def _step_agents(cfg: config_mod.Config, env: Environment, assume_yes: bool) -> list[str]:
    print("\n=== Agents ===")
    adapters = agents_base.all_agents()
    items = []
    for adapter in adapters:
        detected = _detect_agent(adapter, env)
        preselected = detected or adapter.key in cfg.enabled_agents
        status = "detected" if detected else "not found"
        items.append((adapter.key, f"{adapter.display_name} — {status}", preselected))

    chosen = _prompt_multiselect("Which coding agents do you use?", items, assume_yes)

    for key in chosen:
        adapter = agents_base.get(key)
        if adapter is None:
            continue
        for note in adapter.setup_notes():
            print(f"  {adapter.display_name}: {note}")

    cfg.enabled_agents = chosen
    for key in chosen:
        adapter = agents_base.get(key)
        is_new = key not in cfg.agents
        acfg = cfg.agent(key)
        if is_new and adapter is not None:
            acfg.confirm_detection = adapter.default_confirm_detection

    if env.platform == PLATFORM_WINDOWS and env.mode == MODE_WSL_SPLIT and env.distro:
        try:
            from ..install import wsl as wsl_mod

            homes = wsl_mod.agent_homes_unc(env.distro)
        except Exception:
            homes = {}
        for key in chosen:
            if key in homes:
                cfg.agent(key).home = homes[key]

    return chosen


# --------------------------------------------------------------------------- step 4: engine


def _engine_label(name: str, env: Environment, probe_ok: bool) -> str:
    if name == "none":
        return "Status-only (no lights, just tracks activity) — always available"
    supported = env.supports_chroma if name == "chroma" else env.supports_openrgb
    if not supported:
        return f"{name} — not supported on this platform"
    return f"{name} — {'detected' if probe_ok else 'not running right now'}"


def _step_engine(cfg: config_mod.Config, env: Environment, assume_yes: bool) -> str:
    print("\n=== Lighting ===")
    probes = dict(available_engines(cfg))

    order = ["chroma", "openrgb", "none"]
    options = [(name, _engine_label(name, env, probes.get(name, False))) for name in order]

    current = cfg.engine.mode if cfg.engine.mode in ("chroma", "openrgb", "none") else None
    default = current or ("chroma" if probes.get("chroma") else "openrgb" if probes.get("openrgb") else "none")

    choice = _prompt_choice("Which engine should drive your lights?", options, default, assume_yes)

    if choice != "none":
        supported = env.supports_chroma if choice == "chroma" else env.supports_openrgb
        if not supported:
            print(
                f"  Note: {choice} isn't supported on {env.platform} — TintaView will "
                "fall back to status-only until you change this."
            )
        elif not probes.get(choice):
            print(
                f"  Note: {choice} didn't respond just now. That's fine if it just isn't "
                "running yet — TintaView will keep trying once it's installed."
            )

    if choice == "openrgb":
        print(
            "  Warning: OpenRGB and Razer Synapse / Logitech G HUB both try to drive the "
            "same lighting hardware. Running both at once makes them fight over your "
            "devices — only run one at a time."
        )
        if env.platform in (PLATFORM_LINUX, PLATFORM_WSL):
            print(
                "  On Linux, OpenRGB also usually needs udev rules and the i2c-dev "
                "kernel module before it can see your hardware — see openrgb.org for "
                "the install steps for your distro."
            )

    cfg.engine.mode = choice
    return choice


# --------------------------------------------------------------------------- step 5: install path


def _default_install_path(env: Environment) -> str:
    if env.platform == PLATFORM_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return str(Path(base) / "TintaView")
    if env.platform == PLATFORM_MACOS:
        return "/Applications/TintaView.app"
    return str(Path.home() / ".local" / "share" / "tintaview")


def _step_install_path(env: Environment, assume_yes: bool) -> Path:
    print("\n=== Install location ===")
    print(
        "This is where TintaView's own program files live — separate from its settings, "
        f"which always live in {config_mod.config_dir()}."
    )
    default = _default_install_path(env)
    raw = _prompt_text("Install path", default, assume_yes)
    return config_mod.expand(raw)


# --------------------------------------------------------------------------- step 6: autostart


def _autostart_module():
    try:
        from ..install import autostart

        return autostart
    except ImportError:
        return None


def _step_autostart(env: Environment, assume_yes: bool) -> bool | None:
    print("\n=== Start automatically ===")
    if env.platform == PLATFORM_WSL:
        print(
            "Autostart isn't set up from inside WSL — the tray runs on the Windows "
            "side. Run TintaView's Windows installer there and enable autostart when it "
            "asks."
        )
        return None

    mod = _autostart_module()
    if mod is None:
        print("Autostart support isn't available in this build — start TintaView manually for now.")
        return None

    try:
        current = bool(mod.status())
    except (AttributeError, Exception):  # autostart must never abort setup
        current = False

    want = _prompt_yes_no("Start TintaView automatically when you log in?", current or True, assume_yes)
    try:
        ok = bool(mod.enable() if want else mod.disable())
    except (AttributeError, Exception):
        ok = False

    if want:
        print("  Done." if ok else "  Couldn't set that up automatically — you can start "
                                    "TintaView manually any time.")
    else:
        print("  OK — you'll need to start TintaView yourself each time.")
    return want


# --------------------------------------------------------------------------- step 7: hooks


def _show_hook_plan(plan: hooks_mod.HookPlan) -> None:
    print(f"\n--- {plan.path} ---")
    for note in plan.notes:
        print(f"  note: {note}")
    if plan.changes:
        print(plan.diff.rstrip("\n"))


def _confirm_and_apply_hooks(plan: hooks_mod.HookPlan, assume_yes: bool) -> bool:
    _show_hook_plan(plan)
    if not plan.changes:
        print("  Already up to date — nothing to change.")
        return True
    if not _prompt_yes_no(f"Apply this change to {plan.path}?", True, assume_yes):
        print("  Skipped — hooks were not installed for this agent.")
        return False
    hooks_mod.apply(plan)
    print("  Done.")
    return True


def _confirm_and_apply_codex_flag(plan: codex_flag.FlagPlan, assume_yes: bool) -> None:
    if plan.action == "unsupported":
        print(f"  Codex feature flag: {plan.reason}")
        return
    if not plan.changes:
        print(f"  Codex feature flag: {plan.reason}")
        return
    print(f"\n--- {plan.path} (Codex hooks feature flag) ---")
    print(f"  note: {plan.reason}")
    print(codex_flag.diff(plan).rstrip("\n"))
    if _prompt_yes_no(f"Apply this change to {plan.path}?", True, assume_yes):
        codex_flag.apply(plan)
        print("  Done.")
    else:
        print("  Skipped.")


def _step_hooks_native(cfg: config_mod.Config, assume_yes: bool) -> dict[str, bool]:
    hook_bin = config_mod.hook_bin_path()
    applied: dict[str, bool] = {}
    for key in cfg.enabled_agents:
        adapter = agents_base.get(key)
        if adapter is None:
            continue
        try:
            plan = hooks_mod.plan_install(adapter, hook_bin)
        except ValueError as exc:
            print(f"\n--- {adapter.display_name} ---\n  Could not read its config: {exc}")
            applied[key] = False
            continue
        applied[key] = _confirm_and_apply_hooks(plan, assume_yes)
        if key == "codex":
            fplan = codex_flag.plan(adapter.default_home() / "config.toml", adapter.version())
            _confirm_and_apply_codex_flag(fplan, assume_yes)
    return applied


def _step_hooks_wsl_split(cfg: config_mod.Config, env: Environment, assume_yes: bool) -> dict:
    from ..install import wsl as wsl_mod

    if not env.distro:
        print("  No WSL distro selected — hooks were not installed.")
        return {"route": "failed", "plans": {}}

    result = wsl_mod.install_agent_hooks(env.distro, cfg.enabled_agents, assume_yes)
    for note in result["notes"]:
        print(f"  note: {note}")

    if result["route"] == "failed":
        print(f"  Couldn't reach {env.distro} — hooks were not installed.")
        return result

    if result["route"] == "tintaview":
        for key, output in result["plans"].items():
            adapter = agents_base.get(key)
            name = adapter.display_name if adapter else key
            print(f"\n--- {name} (inside {env.distro}) ---\n  {output}")
        return result

    # route == "unc": same show-diff-then-confirm flow as a native install.
    for key, plan in result["plans"].items():
        if isinstance(plan, str):
            adapter = agents_base.get(key)
            name = adapter.display_name if adapter else key
            print(f"\n--- {name} ---\n  {plan}")
            continue
        _confirm_and_apply_hooks(plan, assume_yes)
        if key == "codex":
            try:
                home = wsl_mod.distro_home(env.distro)
                fplan = wsl_mod.codex_flag_plan_unc(env.distro, home)
                _confirm_and_apply_codex_flag(fplan, assume_yes)
            except wsl_mod.WslError as exc:
                print(f"  Codex feature flag: couldn't check it inside {env.distro} ({exc}).")
    return result


def _step_hooks(cfg: config_mod.Config, env: Environment, assume_yes: bool) -> dict:
    print("\n=== Hooks ===")
    if env.platform == PLATFORM_WINDOWS and env.mode == MODE_WSL_SPLIT:
        return _step_hooks_wsl_split(cfg, env, assume_yes)
    return {"route": "native", "applied": _step_hooks_native(cfg, assume_yes)}


# --------------------------------------------------------------------------- hook script + hook.env

# Lives in tintaview.install.hookscript so `tintaview hooks install` can deploy the
# hook without importing the UI layer; re-exported here for the wizard's own use.
from ..install.hookscript import install_hook_script  # noqa: E402

# --------------------------------------------------------------------------- step 8: verify


def _wait_for_agent_event(server, agent_keys: list[str], timeout: float, poll: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = server.state.snapshot()
        if any(key in snapshot.get("agents", {}) for key in agent_keys):
            return True
        time.sleep(poll)
    return False


def _step_verify(cfg: config_mod.Config, env: Environment, assume_yes: bool) -> int:
    print("\n=== Save & verify ===")
    path = config_mod.save(cfg)
    print(f"Settings saved to {path}.")

    if env.platform == PLATFORM_WINDOWS and env.mode == MODE_WSL_SPLIT and env.distro:
        from ..install import wsl as wsl_mod

        url = f"http://{cfg.server.host}:{cfg.server.port}"
        try:
            hook_path = wsl_mod.install_hook(env.distro, url)
            print(f"Hook script installed inside {env.distro} at {hook_path}.")
        except wsl_mod.WslError as exc:
            print(f"Couldn't install the hook script inside {env.distro}: {exc}")
            print("Fix that and re-run setup, or install it by hand — see docs/TROUBLESHOOTING.md.")
            return 1
        print(
            "Live verification across the Windows/WSL boundary isn't automated — start a "
            "session in your agent inside the distro and watch the tray icon on Windows."
        )
        return 0

    hook_path = install_hook_script(cfg, env)
    print(f"Hook script installed at {hook_path}.")

    if assume_yes:
        print(
            "Skipping the live check for a silent install. Run `tintaview doctor` "
            "afterwards to confirm the hooks fired."
        )
        return 0

    # The live check waits up to a minute for a human to go and start an agent session.
    # That only makes sense at a terminal: piped through `curl … | sh`, run from the
    # Windows installer's post-install step, or in CI, there is nobody to act on the
    # prompt and the wait would just look like a hang.
    if not sys.stdin.isatty():
        print(
            "Not running interactively — skipping the live check. "
            "Run `tintaview doctor` once your agent is up to confirm the hooks fired."
        )
        return 0

    if not _prompt_yes_no(
        "Run a quick live check now (start the status server and wait for a real event "
        "from your agent)?", True, assume_yes,
    ):
        print("Skipped. You can check any time with `tintaview doctor`.")
        return 0

    from ..core.server import StatusServer

    server = StatusServer(cfg)
    started = server.start()
    if not started:
        print("TintaView already seems to be running — checking its current status instead.")

    names = ", ".join(
        a.display_name for a in (agents_base.get(k) for k in cfg.enabled_agents) if a is not None
    )
    print(f"Now start a session in {names} — waiting for the first hook event…")
    try:
        seen = _wait_for_agent_event(
            server, cfg.enabled_agents, _LIVE_CHECK_TIMEOUT, _LIVE_CHECK_POLL
        )
    finally:
        if started:
            server.stop()

    if seen:
        print("Got it — TintaView saw your agent. Everything is wired up correctly.")
    else:
        print(
            "No event arrived within a minute. That doesn't always mean something's "
            "wrong — try starting a fresh session in your agent, or run `tintaview "
            "doctor` for a more detailed check."
        )
    return 0


# --------------------------------------------------------------------------- entry point


def _run(platform_override: str | None, assume_yes: bool) -> int:
    cfg = config_mod.load()
    env = detect.detect(override=platform_override)

    _step_welcome()
    env = _step_platform(env, assume_yes)
    _step_agents(cfg, env, assume_yes)
    _step_engine(cfg, env, assume_yes)
    _step_install_path(env, assume_yes)
    _step_autostart(env, assume_yes)
    _step_hooks(cfg, env, assume_yes)
    return _step_verify(cfg, env, assume_yes)


def run_wizard(platform_override: str | None = None, assume_yes: bool = False) -> int:
    """Run the setup wizard. Returns 0 on success, matching a Unix exit code.

    Called identically by `Setup.exe`'s post-install step, `install.sh`, and `tintaview
    setup` — see the module docstring for why this has to stay plain text/stdin.
    """
    try:
        return _run(platform_override, assume_yes)
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled.")
        return 1
    except Exception as exc:  # a non-technical user must never see a traceback
        print(f"\nSetup hit an unexpected problem: {exc}")
        print("Nothing further was changed. Please try again, or report this.")
        return 1
