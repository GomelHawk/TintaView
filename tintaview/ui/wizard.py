"""The setup wizard — a text-mode flow, deliberately, because it's the same code run by
`install.ps1`'s post-install step, `install.sh` over SSH, and `tintaview setup` in a
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

IMPORTANT: `ui/settings_dialog.py` (the tray's "Settings…" popup) exposes a subset of
this wizard's knobs — see AGENTS.md's "Two config UIs — touch both". Any `Config`
field this wizard covers that the dialog *also* covers, adding one, renaming one, or
changing its choices, needs the same change made there too, or the two surfaces
silently drift apart.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

from ..agents import base as agents_base
from ..core import config as config_mod
from ..engines.factory import (
    ENGINE_DISPLAY,
    ENGINE_MODES,
    available_engines,
    engine_supported,
)
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
    """`options` is `[(key, label), ...]`; returns one key.

    Answered by **number**, not by typing the key's name. The keys are internal config
    values ("openrgb", "wsl-split") and asking someone to retype one is both more work
    and more ways to get it wrong than picking "2". The key is still accepted, so an
    existing habit or a copied instruction keeps working.
    """
    if assume_yes:
        return default
    keys = [k for k, _ in options]
    print(question)
    for i, (key, label) in enumerate(options, start=1):
        marker = "  (current)" if key == default else ""
        print(f"  {i}. {label}{marker}")
    default_num = keys.index(default) + 1 if default in keys else 1
    while True:
        raw = _input(f"Enter a number [{default_num}]: ").strip()
        if not raw:
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return keys[int(raw) - 1]
        if raw in keys:  # still accept the key itself
            return raw
        print(f"Please enter a number from 1 to {len(options)}.")


def _prompt_multiselect(
    question: str, items: list[tuple[str, str, bool]], assume_yes: bool,
    default_order: list[str] | None = None,
) -> list[str]:
    """`items` is `[(key, label, preselected), ...]`; returns the keys the user picked.

    What you type **is** the selection — "1 3" means items 1 and 3 and nothing else, in
    that order. This used to toggle each number against a pre-ticked list, which reads
    fine once you know it and is quietly confusing until then: typing "1 3" when
    everything was already ticked *deselected* those two, the exact opposite of what it
    looks like it does. A plain selection needs no explanation and cannot be misread.

    Enter keeps the suggestion (what was detected, or what is already configured), which
    is shown explicitly rather than implied by checkboxes. `default_order`, if given,
    reorders that suggestion to match it (e.g. an existing config's already-chosen
    order) — otherwise it falls back to `items`' own order. Without this, simply
    pressing Enter to re-confirm settings would silently reset a previously-configured
    order back to the fixed list order every time.
    """
    default_keys = [k for k, _, selected in items if selected]
    if default_order:
        order_index = {k: i for i, k in enumerate(default_order)}
        default_keys.sort(key=lambda k: order_index.get(k, len(default_order)))
    if assume_yes:
        return default_keys or [items[0][0]]

    suggested = [str(i) for i, (_k, _l, sel) in enumerate(items, start=1) if sel]
    while True:
        print(question)
        for i, (_key, label, _selected) in enumerate(items, start=1):
            print(f"  {i}. {label}")
        if suggested:
            print(f'Enter the numbers you want, e.g. "1 3". '
                  f'Press Enter for {" ".join(suggested)}.')
        else:
            print('Enter the numbers you want, e.g. "1 3".')

        raw = _input("> ").strip()
        if not raw:
            if default_keys:
                return default_keys
            print("Pick at least one — TintaView needs at least one agent to do anything.")
            continue

        chosen: list[str] = []
        bad = False
        for token in raw.replace(",", " ").split():
            if not token.isdigit() or not (1 <= int(token) <= len(items)):
                print(f"'{token}' isn't one of the numbers listed above.")
                bad = True
                break
            key = items[int(token) - 1][0]
            if key not in chosen:  # "1 1 3" is a typo, not a request for duplicates
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


#: Stats-only integrations: no hook layer at all, so they have no `AgentAdapter` and
#: never go through `install.hooks`. Kept out of `AgentAdapter` rather than forced in
#: with stub hook methods, which would make the wizard's hook-diff/confirmation step
#: run for something that installs nothing.
#:   - JetBrains AI Assistant is an IDE plugin with no scriptable event API at all.
#:   - GitHub Copilot CLI has a real hook system, but it is dispatched over an
#:     internal "SDK callback transport" for `@github/copilot-sdk` embedders, not a
#:     documented external shell-command hook — see providers/copilot.py.
#:
#: Only the *detect callables* live here — they're needed nowhere but this interactive
#: flow. The keys and display labels come from `agents_base.STATS_ONLY_AGENTS`, which is
#: their single source: this list used to repeat both, and the settings dialog's copy
#: then drifted out of step with it.
def _jetbrains_detect() -> bool:
    from ..stats.providers import jetbrains as jetbrains_mod

    try:
        return bool(jetbrains_mod.detect())
    except Exception:
        return False


def _copilot_detect() -> bool:
    from ..stats.providers import copilot as copilot_mod

    try:
        return bool(copilot_mod.detect())
    except Exception:
        return False


_STATS_ONLY_DETECT: dict[str, Callable[[], bool]] = {
    "jetbrains": _jetbrains_detect,
    "copilot": _copilot_detect,
}

#: `(key, display label, detect callable)`, assembled from the shared key/label list.
#: A key added to `agents_base.STATS_ONLY_AGENTS` without a detect callable here shows
#: up as "not found" rather than crashing the wizard.
_STATS_ONLY_AGENTS: tuple[tuple[str, str, Callable[[], bool]], ...] = tuple(
    (key, label, _STATS_ONLY_DETECT.get(key, lambda: False))
    for key, label in agents_base.STATS_ONLY_AGENTS
)


def _step_agents(cfg: config_mod.Config, env: Environment, assume_yes: bool) -> list[str]:
    print("\n=== Agents ===")
    adapters = agents_base.all_agents()
    items = []
    for adapter in adapters:
        detected = _detect_agent(adapter, env)
        preselected = detected or adapter.key in cfg.enabled_agents
        mark = _MARK_READY if detected else _MARK_NOT_RUNNING
        status = "installed" if detected else "not found — you can still pick it"
        items.append((adapter.key, f"{mark} {adapter.display_name} — {status}", preselected))

    for key, label, detect_fn in _STATS_ONLY_AGENTS:
        detected = detect_fn()
        preselected = detected or key in cfg.enabled_agents
        mark = _MARK_READY if detected else _MARK_NOT_RUNNING
        status = "usage found" if detected else "usage stats only, no hook install"
        items.append((key, f"{mark} {label} — {status}", preselected))

    chosen = _prompt_multiselect(
        "Which coding agents do you use? Type the numbers in the order you'd like them "
        'shown in the tray flyout and tooltip — e.g. "2 1" shows the second agent above '
        "the first.",
        items, assume_yes, default_order=cfg.enabled_agents,
    )

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


#: Engine names, labels and per-platform gating come from `engines.factory` — the tray's
#: settings dialog reads the same tables, so neither config UI can rename or mis-gate an
#: engine on its own. Only the wizard-specific prose lives here.
_ENGINE_DISPLAY = ENGINE_DISPLAY

#: What to tell someone whose pick isn't answering yet — one lookup rather than an
#: `if`/`elif` ladder that grows another branch every time an engine is added.
_ENGINE_NOT_RUNNING_HINT = {
    "chroma": "start Razer Synapse",
    "ghub": "start Logitech G HUB",
    "openrgb": "start OpenRGB and turn on its SDK server",
}


#: Availability markers. Every option stays selectable — someone configuring a machine
#: before installing Synapse or OpenRGB, or setting up over SSH, has a perfectly good
#: reason to choose something that isn't answering yet. But an option that cannot work
#: right now has to *say so on its own line*, or the wizard silently accepts a choice
#: that produces no lighting at all and gives no hint why.
_MARK_READY = "[ready]"
_MARK_NOT_RUNNING = "[not running]"
_MARK_UNSUPPORTED = "[unavailable here]"


def _engine_label(name: str, env: Environment, probe_ok: bool) -> str:
    display = _ENGINE_DISPLAY.get(name, name)
    if name == "auto":
        return f"{_MARK_READY} {display} — use whichever is running"
    if name == "none":
        return f"{_MARK_READY} {display}, just tracks activity"
    if not engine_supported(name, env):
        return f"{_MARK_UNSUPPORTED} {display} — not supported on {env.platform}"
    if probe_ok:
        return f"{_MARK_READY} {display} — running now"
    hint = _ENGINE_NOT_RUNNING_HINT.get(name, "check that it's running")
    return f"{_MARK_NOT_RUNNING} {display} — you can still pick it; {hint}"


def _step_engine(cfg: config_mod.Config, env: Environment, assume_yes: bool) -> str:
    print("\n=== Lighting ===")
    probes = dict(available_engines(cfg))

    detected = [n for n in ("chroma", "ghub", "openrgb") if probes.get(n)]
    if detected:
        print("  Detected: " + ", ".join(_ENGINE_DISPLAY.get(n, n) for n in detected))
    else:
        print("  No lighting software is answering right now (Razer Synapse for Chroma, "
              "Logitech G HUB for G HUB, the OpenRGB app with its SDK server on for "
              "OpenRGB).")

    # "auto" first and default (see `ENGINE_MODES`). Pinning a single engine is what
    # turns "the app I picked isn't running" into "no lighting at all, silently" — auto
    # re-probes on every start and falls back on its own, so it survives Synapse being
    # closed or OpenRGB being installed later. The explicit choices stay for anyone
    # running both and wanting one.
    order = list(ENGINE_MODES)
    options = [(name, _engine_label(name, env, probes.get(name, False))) for name in order]

    current = cfg.engine.mode if cfg.engine.mode in order else None
    default = current or "auto"

    choice = _prompt_choice("Which engine should drive your lights?", options, default, assume_yes)

    if choice == "auto":
        print(
            "  TintaView will probe "
            + " then ".join(_ENGINE_DISPLAY.get(n, n) for n in cfg.engine.order)
            + " each time it starts, and fall back to status-only if neither answers."
        )
    elif choice != "none":
        if not engine_supported(choice, env):
            print(
                f"  Note: {choice} isn't supported on {env.platform} — TintaView will "
                "fall back to status-only until you change this."
            )
        elif not probes.get(choice):
            print(
                f"  Note: {choice} didn't respond just now. That's fine if it just isn't "
                "running yet — TintaView will keep trying once it's installed."
            )

    if choice == "ghub":
        from ..engines.ghub import format_setup_notes

        print(
            "  Good news: unlike OpenRGB, G HUB can keep running — TintaView drives the "
            "lights through the same SDK G HUB itself uses, so there's nothing to close.\n"
            "  Note: that SDK has no way to target a single device, so this drives every "
            "detected Logitech G device at once."
        )
        print(format_setup_notes(indent="  "))
        _ensure_ghub_ready(cfg, assume_yes)

    if choice == "openrgb":
        print(
            "  Warning: OpenRGB and Razer Synapse / Logitech G HUB both try to drive the "
            "same lighting hardware. Running both at once makes them fight over your "
            "devices — only run one at a time. If your devices are Logitech, the "
            "built-in G HUB engine above can run alongside G HUB instead of fighting it."
        )
        if env.platform in (PLATFORM_LINUX, PLATFORM_WSL):
            print(
                "  On Linux, OpenRGB also usually needs udev rules and the i2c-dev "
                "kernel module before it can see your hardware — see openrgb.org for "
                "the install steps for your distro."
            )
        _ensure_openrgb_ready(cfg, assume_yes)

    cfg.engine.mode = choice
    return choice


def _ensure_openrgb_ready(cfg: config_mod.Config, assume_yes: bool) -> None:
    """Check what OpenRGB needs, and offer to install what TintaView can install.

    Picking an engine in a list used to be all it took to end up with no lighting and a
    `doctor` line blaming the SDK server — the three prerequisites (client library,
    application, SDK server) are indistinguishable from the outside once it just doesn't
    work. So each is named, and the two that can be automated are offered rather than
    described.
    """
    from ..install import components

    if not components.openrgb_python_installed():
        print(
            "\n  OpenRGB needs the 'openrgb-python' package, which isn't in this "
            "TintaView environment.\n"
            "  Without it the OpenRGB engine can't be used at all."
        )
        if assume_yes or _prompt_yes_no("  Install it now?", True, assume_yes):
            ok, message = components.install_openrgb_python()
            print(f"  {'OK: ' if ok else 'Failed: '}{message}")
            if not ok:
                print("  Install it by hand with: pip install openrgb-python")
        else:
            print("  Skipped — the OpenRGB engine will stay unavailable until it's installed.")

    # The library alone proves nothing: the app has to be running and its SDK server
    # switched on. Re-probe rather than trusting the earlier `available_engines` scan,
    # which ran before any of the above.
    from ..engines.openrgb import OpenRGBEngine

    try:
        reachable = OpenRGBEngine(cfg.engine.openrgb).probe()
    except Exception:
        reachable = False
    if reachable:
        print("  OpenRGB is reachable — you're set.")
        return

    o = cfg.engine.openrgb
    print(f"\n  Nothing is answering on {o.host}:{o.port}, so OpenRGB isn't reachable yet.")

    installed = components.winget_package_installed(components.OPENRGB_WINGET_ID)
    if installed is False and components.winget_available() and not assume_yes:
        print("  The OpenRGB application doesn't appear to be installed on this machine.")
        if _prompt_yes_no("  Install OpenRGB now with winget?", True, assume_yes):
            ok, message = components.winget_install(components.OPENRGB_WINGET_ID)
            print(f"  {'OK: ' if ok else 'Failed: '}{message}")
            if not ok:
                print("  Download it yourself from https://openrgb.org instead.")
    elif installed is False:
        print("  The OpenRGB application doesn't appear to be installed — get it from "
              "https://openrgb.org.")

    # Not automatable at any point: the SDK server is off by default and lives behind a
    # checkbox in OpenRGB's own UI.
    print(
        "  Then open OpenRGB and turn on Settings > SDK Server > 'Start Server', leave it "
        "running, and re-run `tintaview setup` (or `tintaview doctor`) to confirm."
    )


def _ensure_ghub_ready(cfg: config_mod.Config, assume_yes: bool) -> None:
    """Check what G HUB needs, and offer to install it if the SDK DLL can't be found.

    G HUB has only two prerequisites, not OpenRGB's three: the application (which ships
    the DLL — no separate client library to install) and it actually running with
    lighting control enabled. A missing DLL is a definite "G HUB isn't installed" signal
    on its own, unlike OpenRGB's library-vs-app-vs-server ambiguity, so this always says
    so before deciding whether winget can offer to fix it.
    """
    from ..engines.ghub import GHubEngine, discover_dll_path
    from ..install import components

    path = discover_dll_path(cfg.engine.ghub)
    if path is None:
        print(
            "\n  The Logitech LED Illumination SDK DLL wasn't found — it ships inside "
            "Logitech G HUB itself, and G HUB doesn't appear to be installed here."
        )
        installed = components.winget_package_installed(components.GHUB_WINGET_ID)
        if installed is False and components.winget_available() and not assume_yes:
            if _prompt_yes_no("  Install Logitech G HUB now with winget?", True, assume_yes):
                ok, message = components.winget_install(components.GHUB_WINGET_ID)
                print(f"  {'OK: ' if ok else 'Failed: '}{message}")
                if not ok:
                    print("  Download it yourself from "
                          "https://www.logitechg.com/en-us/innovation/g-hub.html instead.")
            else:
                print("  Skipped — the G HUB engine will stay unavailable until it's installed.")
        else:
            print("  Get it from https://www.logitechg.com/en-us/innovation/g-hub.html.")
        print("  Then re-run `tintaview setup` (or `tintaview doctor`) to confirm.")
        return

    try:
        reachable = GHubEngine(cfg.engine.ghub).probe()
    except Exception:
        reachable = False
    if reachable:
        print(f"  Found the SDK at {path} and it responded — you're set.")
        return

    print(
        f"\n  Found the SDK at {path}, but it didn't respond just now. Make sure G HUB "
        'is running with "Game lighting control" enabled in its settings, and that G '
        "HUB was started before TintaView — start G HUB first, then restart TintaView, "
        "and re-run `tintaview doctor` to confirm."
    )


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

    # A tray that is already running read its config at startup, so until it is restarted
    # none of the answers above have any visible effect — which reads as "the wizard did
    # nothing". Do it here, right after the save, so the live check below tests the
    # instance that is actually running the new settings.
    from ..install import restart as restart_mod

    if restart_mod.restart_if_running(cfg):
        print("Restarted TintaView so the new settings take effect.")

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

    Called identically by `install.ps1`'s post-install step, `install.sh`, and `tintaview
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
