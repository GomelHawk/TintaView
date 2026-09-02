"""``tintaview doctor`` — a checklist for "the lights stopped working".

The audience is explicitly non-technical: a colleague who just knows the tray icon
isn't reacting anymore. So the contract here is stricter than a normal health check —
every line printed is one of OK / WARN / FAIL, and every WARN/FAIL names the exact next
action (a command to run or a file to fix), never just "something is wrong". Vague
output is treated as a bug in this module, not an acceptable diagnostic.

Checks run in a fixed order, roughly cheapest/most-fundamental first, so an early
failure (bad config) doesn't get buried under a wall of downstream noise caused by it:

    1. ENVIRONMENT   — platform/mode/python/version, autostart
    2. CONFIG        — does it exist, does it parse, what's enabled
    3. DAEMON        — is the broker actually answering, and is it really TintaView
    4. ENGINE        — which lighting backends probe OK, and why the others don't
    5. HOOK SCRIPT   — tv-hook + hook.env, the "silent killer" if they drift
    6. AGENT HOOKS   — per-agent install status (+ Codex's feature flag)
    7. STATS         — can each agent's usage provider produce rows
    8. LIVE HOOK TEST (--verbose only) — an interactive, best-effort real-event check
    9. PAINT (--paint only) — open the configured engine, cycle colours, ask "did you see it?"

Nothing here ever prints a credential's *value* — only, where useful, the path to the
file that holds one. Every network/subprocess call is wrapped so one flaky check can
never take the rest of the report down with it.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import sys
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

from .. import __version__
from ..core import config as config_mod
from ..core import log as log_mod

if TYPE_CHECKING:
    from ..agents.base import AgentAdapter
    from ..core.config import Config
    from .detect import Environment

log = logging.getLogger(__name__)

_HTTP_TIMEOUT = 2.0
_LIVE_TEST_SECONDS = 30.0


# --------------------------------------------------------------------------- reporting


def _can_prompt() -> bool:
    """Is there a console this process can actually ask a question on?

    `sys.stdin.isatty()` is not enough on its own: a windowed build has no standard
    handles at all, so `sys.stdin` is **None** under `pythonw.exe` and the attribute
    lookup raises. `doctor` is reachable from the tray (Run diagnostics) as well as from
    a terminal, so every interactive step has to go through this.
    """
    stdin = getattr(sys, "stdin", None)
    if stdin is None:
        return False
    try:
        return bool(stdin.isatty())
    except (AttributeError, ValueError, OSError):
        # ValueError: the handle was closed under us. OSError: a detached console.
        return False


class _Reporter:
    """Prints the checklist and keeps score. Never raises — a bug in one check must
    not stop the rest of the report, or crash the very tool a confused user was
    pointed at."""

    def __init__(self, verbose: bool) -> None:
        self.verbose = verbose
        self.fails = 0
        self.warns = 0

    def _emit(self, tag: str, section: str, message: str, fix: str | None) -> None:
        print(f"[{tag:<4}] {section:<14} {message}")
        if fix:
            for line in fix.splitlines():
                print(f"          -> {line}")

    def ok(self, section: str, message: str) -> None:
        self._emit("OK", section, message, None)

    def warn(self, section: str, message: str, fix: str) -> None:
        self.warns += 1
        self._emit("WARN", section, message, fix)

    def fail(self, section: str, message: str, fix: str) -> None:
        self.fails += 1
        self._emit("FAIL", section, message, fix)


# --------------------------------------------------------------------------- http helpers


def _http_get_json(url: str, timeout: float = _HTTP_TIMEOUT) -> tuple[bool | None, object]:
    """GET ``url`` and try to parse the body as JSON.

    Three-way result, because "nothing there" and "something wrong there" need
    different fixes: ``(True, payload)`` on parseable JSON (any HTTP status —
    ``/healthz`` never errors, but this stays honest if that ever changes),
    ``(False, description)`` when something answered but didn't speak JSON, and
    ``(None, description)`` when the connection itself couldn't be made.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except OSError:
            raw = b""
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        return None, str(getattr(exc, "reason", exc))

    try:
        return True, json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False, "response was not valid JSON"


# --------------------------------------------------------------------------- 1. environment


def _check_environment(reporter: _Reporter) -> Environment:
    from . import detect

    env = detect.detect()
    bits = [f"platform={env.platform}", f"mode={env.mode}"]
    if env.distro:
        bits.append(f"distro={env.distro}")
    bits.append(f"python={platform.python_version()}")
    bits.append(f"tintaview={__version__}")
    bits.append(f"frozen={'yes' if getattr(sys, 'frozen', False) else 'no'}")
    reporter.ok("ENVIRONMENT", " ".join(bits))

    for note in env.notes:
        reporter.ok("ENVIRONMENT", note)

    # autostart.py is being written concurrently elsewhere in the repo — degrade
    # quietly rather than let an incomplete module take the whole report down.
    try:
        from . import autostart

        enabled = autostart.status()
    except (ImportError, AttributeError) as exc:
        log.info("doctor: autostart status unavailable (%s)", exc)
    else:
        if enabled:
            reporter.ok("ENVIRONMENT", "autostart at login is enabled")
        else:
            reporter.warn(
                "ENVIRONMENT",
                "autostart at login is not enabled — the tray won't come back after a reboot",
                "run `tintaview setup` and keep the autostart step checked",
            )
    return env


# --------------------------------------------------------------------------- 2. config


def _check_config(reporter: _Reporter) -> Config:
    path = config_mod.config_path()

    if not path.exists():
        reporter.warn(
            "CONFIG",
            f"no config file at {path} — TintaView is running on built-in defaults",
            "run `tintaview setup` to create one",
        )
    else:
        try:
            with open(path, "rb") as fh:
                tomllib.load(fh)
        except OSError as exc:
            reporter.fail(
                "CONFIG", f"{path} could not be read ({exc})",
                f"check the permissions on {path}",
            )
        except tomllib.TOMLDecodeError as exc:
            reporter.fail(
                "CONFIG", f"{path} is not valid TOML ({exc})",
                f"fix the syntax error in {path}, or delete it and run `tintaview setup` "
                "to regenerate it",
            )
        else:
            reporter.ok("CONFIG", f"parses OK: {path}")

    cfg = config_mod.load()
    if not cfg.enabled_agents:
        reporter.warn(
            "CONFIG", "no agents are listed as enabled",
            f"add at least one under [agents] enabled in {path}, or run `tintaview setup`",
        )
    else:
        reporter.ok("CONFIG", f"enabled agents: {', '.join(cfg.enabled_agents)} ({path})")
    return cfg


# --------------------------------------------------------------------------- 3. daemon


def _check_daemon(reporter: _Reporter, cfg: Config) -> bool:
    """Returns True only if a real TintaView daemon answered — gates the live hook test."""
    host, port = cfg.server.host, cfg.server.port
    base = f"http://{host}:{port}"

    reachable, health = _http_get_json(f"{base}/healthz")
    if reachable is None:
        reporter.fail(
            "DAEMON", f"nothing is answering at {base} ({health}) — TintaView is not running",
            "start it with `tintaview run --headless` (or `tintaview run` for the tray)",
        )
        return False

    if reachable is False or not isinstance(health, dict) or "ok" not in health:
        reporter.fail(
            "DAEMON",
            f"{base}/healthz answered, but not the way TintaView does ({health!r})",
            f"another program is probably bound to port {port} — change `server.port` "
            f"in {cfg.path or config_mod.config_path()} to a free port and restart TintaView",
        )
        return False

    remote_version = health.get("version")
    if remote_version and remote_version != __version__:
        reporter.warn(
            "DAEMON",
            f"running, but version {remote_version} — this `doctor` is from {__version__}",
            "restart TintaView (or run `tintaview update`) so both match",
        )
    else:
        reporter.ok("DAEMON", f"reachable at {base} (version {remote_version or 'unknown'})")

    _, state = _http_get_json(f"{base}/state")
    if isinstance(state, dict):
        engine = state.get("engine") or {}
        reporter.ok(
            "DAEMON",
            f"/state: effective={state.get('effective', '?')} "
            f"engine={engine.get('name', '?')} (active={engine.get('active', False)}) "
            f"blinking={state.get('blinking', False)}",
        )
    return True


# --------------------------------------------------------------------------- 4. engine


def _engine_unavailable_reason(name: str, env: Environment, cfg: Config) -> str:
    if name == "chroma":
        if not env.supports_chroma:
            return f"the Chroma REST SDK is Windows-only; this machine reports platform={env.platform}"
        return "Razer Synapse doesn't seem to be running (its Chroma Connect SDK is what's probed) — start Synapse"
    if name == "ghub":
        # Prefer measured blockers (process list, Dynamic Lighting, Integrations) over
        # the old guess that collapsed "DLL missing" and "init refused" into one label.
        if not env.supports_ghub:
            return (
                "the Logitech LED Illumination SDK is Windows-only; this machine "
                f"reports platform={env.platform}"
            )
        from ..engines.ghub_env import blockers, inspect

        info = inspect(cfg.engine.ghub)
        problems = blockers(info)
        if problems:
            return problems[0]
        # DLL present, G HUB not known-stopped, nothing else measured — still not
        # probing as available, so fall back to the start-order advice.
        path = info.dll_path
        return (
            f"found the SDK at {path}, but it isn't usable right now — make sure G HUB "
            'is running with "Game lighting control" enabled in its settings, and that '
            "G HUB was started before TintaView (restart TintaView if you started G HUB "
            "afterwards)"
        )
    if name == "openrgb":
        # Two different failures wear the same "not available" label, and only one of
        # them is fixed by touching OpenRGB. Saying "start the SDK server" to someone
        # whose install simply lacks openrgb-python sends them to restart software that
        # was never the problem.
        import importlib.util

        if importlib.util.find_spec("openrgb") is None:
            return (
                "openrgb-python isn't installed in this TintaView environment, so the "
                "OpenRGB engine can't be used at all — reinstall TintaView, or run "
                "`pip install openrgb-python` inside its virtual environment"
            )
        o = cfg.engine.openrgb
        return (
            f"the OpenRGB SDK server isn't answering on {o.host}:{o.port} — open OpenRGB, "
            "enable Settings > SDK Server > Server, and leave OpenRGB running"
        )
    return "not available on this platform"


def _check_engine(reporter: _Reporter, cfg: Config, env: Environment) -> None:
    from ..engines.factory import available_engines

    probes = dict(available_engines(cfg))
    mode = cfg.engine.mode

    for name in ("chroma", "ghub", "openrgb"):
        if name not in probes:
            continue
        if probes[name]:
            if name == "ghub":
                from ..engines.ghub import discover_dll_path

                path = discover_dll_path(cfg.engine.ghub)
                reporter.ok("ENGINE", f"ghub: available ({path})" if path else "ghub: available")
            else:
                reporter.ok("ENGINE", f"{name}: available")
            continue
        reason = _engine_unavailable_reason(name, env, cfg)
        if mode == name:
            reporter.fail(
                "ENGINE", f"{name}: not available ({reason})",
                f"fix the issue above, or set `engine.mode` in "
                f"{cfg.path or config_mod.config_path()} to \"auto\" or \"none\"",
            )
        else:
            reporter.warn(
                "ENGINE", f"{name}: not available ({reason})",
                "informational only — it isn't the configured engine, so no action is needed "
                "unless you intended to use it",
            )

    if mode in ("chroma", "ghub", "openrgb", "none"):
        selected = mode
    else:
        selected = next((n for n in cfg.engine.order if probes.get(n)), "none")

    if mode == "auto" and selected == "none":
        reporter.warn(
            "ENGINE", "auto mode found no usable lighting engine — running status-only",
            "start Razer Synapse, Logitech G HUB, or OpenRGB (with its SDK server "
            "enabled) if you expected lighting on this machine",
        )
    else:
        reporter.ok("ENGINE", f"engine in use: {selected}")

    if mode == "ghub":
        from ..engines.ghub import format_setup_notes
        from ..engines.ghub_env import blockers, inspect

        info = inspect(cfg.engine.ghub)
        problems = blockers(info)
        if problems:
            for line in problems:
                reporter.warn("ENGINE", line, "see docs/TROUBLESHOOTING.md#g-hub-lights-dont-change")
        else:
            # Nothing measured as wrong — print the full checklist only when every
            # signal came back unknown, so the user still has something actionable.
            if (
                info.running is None
                and info.dynamic_lighting is None
                and info.integration == "unknown"
            ):
                for line in format_setup_notes().splitlines():
                    reporter.ok("ENGINE", line)
            else:
                reporter.ok("ENGINE", "no G HUB environmental blockers measured")


# --------------------------------------------------------------------------- 5. hook script


#: Sentinel for "nobody has resolved the split home yet, do it now". `None` is a real
#: answer (not a split install, or the distro is unreachable), so it cannot double as
#: "unknown" — and `run_doctor` resolving it once is the whole point: `wsl.exe` has a
#: 20-second timeout and two checks used to pay it independently.
_UNRESOLVED = object()


def _wsl_split_home(env: Environment) -> str | None:
    """The distro's POSIX `$HOME` in a WSL split, else None — see `wsl.split_home`, which
    the tray, the settings dialog and `hooks status` share with this report."""
    from . import wsl

    return wsl.split_home(env)


def _remote_path(distro: str, posix_path: object) -> Path:
    """A distro-side POSIX path as the Windows side can open it."""
    from . import detect

    return Path(detect.wsl_path_to_unc(distro, str(posix_path)))


def _configured_adapter(cfg: Config, adapter: AgentAdapter) -> AgentAdapter:
    """Kept as a thin alias: the resolution now lives in `wsl.configured_adapter` so the
    CLI, the settings dialog and the tray share it with this report."""
    from . import wsl

    return wsl.configured_adapter(cfg, adapter)


def _check_hook_script(
    reporter: _Reporter, cfg: Config, env: Environment, split_home: object = _UNRESOLVED
) -> None:
    from . import wsl

    if split_home is _UNRESOLVED:
        split_home = _wsl_split_home(env)
    if split_home is not None:
        # The agents run inside the distro, so that is where the script they invoke
        # lives. The Windows-side `bin\tv-hook.cmd` is *correctly* absent in a split
        # install — checking for it reported five failures on a working machine.
        hook_bin = _remote_path(env.distro, wsl.remote_hook_bin(split_home))
        hook_env = _remote_path(env.distro, wsl.remote_hook_env(split_home))
        where = f" inside {env.distro}"
    else:
        hook_bin = config_mod.hook_bin_path()
        hook_env = config_mod.hook_env_path()
        where = ""

    if not hook_bin.exists():
        reporter.fail(
            "HOOK SCRIPT", f"{hook_bin} does not exist",
            "run `tintaview hooks install --agent all` to write it (or reinstall TintaView)",
        )
    elif split_home is None and sys.platform != "win32" and not os.access(hook_bin, os.X_OK):
        # Skipped for a distro-side script: the executable bit is not readable through a
        # UNC path, and `install_hook` chmod +x'd it inside the distro where it counts.
        reporter.fail(
            "HOOK SCRIPT", f"{hook_bin} exists but is not executable",
            f"run `chmod +x {hook_bin}`",
        )
    else:
        reporter.ok("HOOK SCRIPT", f"{hook_bin} exists{where}")

    expected_url = f"http://{cfg.server.host}:{cfg.server.port}"

    if not hook_env.exists():
        reporter.fail(
            "HOOK SCRIPT", f"{hook_env} is missing",
            "run `tintaview hooks install --agent all` to regenerate it, or reinstall TintaView",
        )
        return

    try:
        text = hook_env.read_text(encoding="utf-8")
    except OSError as exc:
        reporter.fail(
            "HOOK SCRIPT", f"{hook_env} could not be read ({exc})",
            f"check the permissions on {hook_env}",
        )
        return

    # Only ever report the path and the expected value — never the file's raw contents,
    # which is otherwise a harmless-looking way to leak TINTAVIEW_CURL overrides etc.
    if expected_url not in text:
        reporter.fail(
            "HOOK SCRIPT",
            f"{hook_env} does not point at the configured daemon URL {expected_url}",
            f"edit {hook_env} and set TINTAVIEW_URL={expected_url}, then run `tintaview doctor` again",
        )
    else:
        reporter.ok("HOOK SCRIPT", f"{hook_env} points at {expected_url}{where}")


# --------------------------------------------------------------------------- 6. agent hooks


def _check_codex_flag(reporter: _Reporter, cfg: Config, adapter: AgentAdapter) -> None:
    from . import codex_flag

    acfg = cfg.agent("codex")
    home = config_mod.expand(acfg.home) if acfg.home else adapter.default_home()
    codex_config_path = home / "config.toml"
    version = adapter.version()
    plan = codex_flag.plan(codex_config_path, version)

    if plan.action == "unsupported":
        reporter.warn(
            "AGENT HOOKS", f"Codex feature flag: {plan.reason}",
            "upgrade Codex, or accept the idle-only `notify` fallback on this version",
        )
    elif plan.action == "noop":
        reporter.ok("AGENT HOOKS", f"Codex feature flag: {plan.reason} ({codex_config_path})")
    else:
        diff_text = codex_flag.diff(plan)
        fix = f"apply this change to {codex_config_path}:\n{diff_text}".rstrip()
        reporter.fail(
            "AGENT HOOKS", f"Codex feature flag not set yet: {plan.reason}", fix,
        )


def _check_agent_hooks(
    reporter: _Reporter, cfg: Config, env: Environment, split_home: object = _UNRESOLVED
) -> None:
    from ..agents import base as agents_base
    from . import hooks as hooks_mod
    from . import wsl

    if not cfg.enabled_agents:
        return  # already reported by _check_config

    if split_home is _UNRESOLVED:
        split_home = _wsl_split_home(env)
    if split_home is not None:
        # The hooks were written pointing at the distro's own tv-hook.sh, so that is
        # what "is this path still current?" has to be measured against. Comparing them
        # to the Windows path would report every agent as `stale-path`.
        hook_bin = wsl.remote_hook_bin(split_home)
    else:
        hook_bin = config_mod.hook_bin_path()

    for key in cfg.enabled_agents:
        adapter = agents_base.get(key)
        if adapter is None:
            if key in agents_base.STATS_ONLY_NAMES:
                # Not a misconfiguration: these have no scriptable event API, so they
                # are usage-only *by design* and belong in `agents.enabled` so their
                # cards appear. Telling the user to remove them would delete the very
                # usage the STATS section reports as working.
                reporter.ok(
                    "AGENT HOOKS",
                    f"{agents_base.display_name(key)}: usage only — no hooks to install",
                )
                continue
            known = "/".join(a.key for a in agents_base.all_agents())
            reporter.warn(
                "AGENT HOOKS", f"{key}: not a known agent ({known})",
                f"fix the [agents] enabled list in {cfg.path or config_mod.config_path()}",
            )
            continue

        adapter = wsl.check_adapter(cfg, adapter, wsl.HookCheck(hook_bin, env.distro, split_home))
        path = adapter.hooks_config_path()
        try:
            state = hooks_mod.status(adapter, hook_bin)
        except Exception as exc:  # noqa: BLE001 - one bad adapter must not sink the report
            reporter.fail(
                "AGENT HOOKS", f"{adapter.display_name}: could not check hooks ({exc})",
                f"run `tintaview hooks status --agent {key}` for details",
            )
            continue

        if state == hooks_mod.STATUS_INSTALLED:
            reporter.ok("AGENT HOOKS", f"{adapter.display_name}: installed ({path})")
        elif state == hooks_mod.STATUS_MISSING:
            reporter.fail(
                "AGENT HOOKS", f"{adapter.display_name}: hooks missing ({path})",
                f"run `tintaview hooks install --agent {key}`",
            )
        elif state == hooks_mod.STATUS_PARTIAL:
            reporter.fail(
                "AGENT HOOKS", f"{adapter.display_name}: hooks partially installed ({path})",
                f"run `tintaview hooks install --agent {key}` to fill in the missing events",
            )
        elif state == hooks_mod.STATUS_UNREADABLE:
            # Deliberately not "run hooks install": the file is there and probably fine,
            # we just could not open or parse it (permissions, a lock, a sleeping distro
            # behind a UNC path, hand-edited JSON). Installing from here would plan a
            # CREATE and replace the user's own hooks with only ours.
            reporter.warn(
                "AGENT HOOKS",
                f"{adapter.display_name}: {path} exists but could not be read or parsed",
                "check the file's permissions and that it is valid JSON — do NOT run "
                f"`tintaview hooks install --agent {key}` until it can be read, or the "
                "merge would have nothing of yours to merge into",
            )
        elif state == hooks_mod.STATUS_STALE_PATH:
            reporter.fail(
                "AGENT HOOKS",
                f"{adapter.display_name}: hooks point at an old tv-hook path ({path})",
                f"run `tintaview hooks install --agent {key}` to repoint them at {hook_bin}",
            )
        else:
            reporter.warn(
                "AGENT HOOKS", f"{adapter.display_name}: unrecognised hook state {state!r} ({path})",
                f"run `tintaview hooks status --agent {key}` and, if in doubt, "
                f"`tintaview hooks install --agent {key}`",
            )

        if key == "codex":
            _check_codex_flag(reporter, cfg, adapter)


# --------------------------------------------------------------------------- 7. stats


def _check_stats(reporter: _Reporter, cfg: Config) -> None:
    from ..stats.service import StatsService

    if not cfg.enabled_agents:
        return

    service = StatsService(cfg)
    try:
        results = service.fetch_all(timeout=5.0)
    except Exception as exc:  # noqa: BLE001 - StatsService already guards providers; belt and braces
        reporter.warn(
            "STATS", f"usage stats could not be fetched ({exc})",
            "this does not affect lighting — try `tintaview doctor` again later",
        )
        return

    for key in cfg.enabled_agents:
        result = results.get(key)
        if result is None:
            reporter.warn(
                "STATS", f"{key}: no usage provider registered for this agent",
                "lighting is unaffected — this only means the usage panel has nothing to show",
            )
            continue
        if result.ok:
            labels = ", ".join(
                f"{row.label}={row.pct:.0f}%" if row.show_pct else row.label for row in result.rows
            )
            reporter.ok("STATS", f"{key}: source={result.source} — {labels}")
        else:
            reporter.warn(
                "STATS", f"{key}: {result.error or 'no usage data available'}",
                "usage stats are informational only — lighting still works without them",
            )


# --------------------------------------------------------------------------- 8. live hook test


def _sessions_snapshot(state: object) -> frozenset[tuple[str, str, str]]:
    if not isinstance(state, dict):
        return frozenset()
    agents = state.get("agents") or {}
    out: set[tuple[str, str, str]] = set()
    for agent, info in agents.items():
        if not isinstance(info, dict):
            continue
        for sid, status in (info.get("sessions") or {}).items():
            out.add((agent, sid, status))
    return frozenset(out)


def _live_hook_test(reporter: _Reporter, cfg: Config, daemon_ok: bool,
                    interactive: bool = True) -> None:
    if not daemon_ok:
        return

    host, port = cfg.server.host, cfg.server.port
    base = f"http://{host}:{port}"
    curl_cmd = f'curl -s "{base}/v1/event/working?agent=claude&sid=doctor-test"'
    reporter.ok("LIVE HOOK TEST", f"to fire a synthetic event by hand, run: {curl_cmd}")

    if not interactive:
        # Never block a run that cannot answer: CI, a piped invocation, or the tray's
        # Run diagnostics — which is a *windowed* caller, so it has no stdin to prompt
        # on and nowhere to show the prompt even if it did. `run_doctor` decides this;
        # see `_can_prompt`.
        return

    try:
        answer = input(
            "Wait ~30s for a real hook event from a live agent session? [y/N] "
        ).strip().lower()
    except (EOFError, RuntimeError, OSError):
        # RuntimeError is "input(): lost sys.stdin" — reachable only if a caller forces
        # `interactive=True` without a console. `doctor` must degrade, never crash: it
        # is the tool a confused user was pointed at.
        return
    if answer not in ("y", "yes"):
        return

    _, before_state = _http_get_json(f"{base}/state")
    before = _sessions_snapshot(before_state)
    deadline = time.monotonic() + _LIVE_TEST_SECONDS
    new_events: frozenset[tuple[str, str, str]] = frozenset()

    while time.monotonic() < deadline:
        _, state = _http_get_json(f"{base}/state")
        after = _sessions_snapshot(state)
        new_events = after - before
        if new_events:
            break
        time.sleep(1.0)

    if new_events:
        first = sorted(new_events)[0]
        reporter.ok("LIVE HOOK TEST", f"saw a hook event: agent={first[0]} sid={first[1]} status={first[2]}")
    else:
        reporter.warn(
            "LIVE HOOK TEST", "no hook event observed within 30s",
            "start a session in an enabled agent now and re-run `tintaview doctor -v`, "
            "or re-check the AGENT HOOKS section above",
        )


# --------------------------------------------------------------------------- 9. paint self-test


def _paint_selftest(reporter: _Reporter, cfg: Config, interactive: bool = True) -> None:
    """Drive the configured engine through a short colour cycle and ask the user.

    SDK success and "the user saw light" are not the same thing — G HUB in particular
    can return true from SetLighting while the mouse stays dark (integration off,
    Dynamic Lighting, onboard memory). This is the only check that closes that gap.
    """
    from ..engines.factory import make_engine

    engine = make_engine(cfg)
    print(
        f"\nPaint self-test via {engine.display_name}. Quit any other TintaView tray "
        "first — two clients fighting over the same SDK usually means init fails."
    )
    try:
        if not engine.open():
            reporter.fail(
                "PAINT", f"could not open {engine.display_name}",
                "fix the ENGINE lines above, then re-run `tintaview doctor --paint`",
            )
            return
        hold = 2.0
        for name, rgb in (
            ("red", (255, 0, 0)),
            ("yellow", (255, 200, 0)),
            ("green", (0, 255, 0)),
        ):
            print(f"  → {name}…", flush=True)
            engine.set_color(*rgb)
            time.sleep(hold)
    except Exception as e:
        reporter.fail("PAINT", f"paint cycle raised: {e!r}", "see the log for details")
        return
    finally:
        try:
            engine.close()
        except Exception as e:
            log.debug("doctor paint: close failed: %r", e)

    if not interactive:
        # The cycle ran, but the only thing this check actually proves — that a human
        # saw light — can't be established without asking. Say that rather than
        # claiming a pass nobody confirmed.
        reporter.warn(
            "PAINT", "ran the colour cycle but could not ask whether you saw it",
            "run `tintaview doctor --paint` from a terminal to confirm it by eye",
        )
        return

    try:
        answer = input("  Did your lights change colour? [Y/n] ").strip().lower()
    except (EOFError, RuntimeError, OSError):
        answer = ""  # same reasoning as _live_hook_test's prompt
    if answer in ("", "y", "yes"):
        reporter.ok("PAINT", "user confirmed the lights moved")
    else:
        reporter.fail(
            "PAINT", "user did not see the lights change",
            "for G HUB: check Integrations, Game lighting control, onboard memory, "
            "and Windows Dynamic Lighting — then restart TintaView and retry",
        )


# --------------------------------------------------------------------------- entry point


def run_doctor(verbose: bool = False, paint: bool = False,
               interactive: bool | None = None) -> int:
    """Run every check and print a report. 0 if everything essential is healthy, else 1.

    `interactive` gates the two steps that ask the user a question. None (the default)
    auto-detects a console; False forbids prompting even when one exists — which is what
    a GUI caller must pass, since a tray started from a terminal *does* have a usable
    stdin and would otherwise block on a prompt nobody can see.
    """
    if interactive is None:
        interactive = _can_prompt()
    reporter = _Reporter(verbose)

    env = _check_environment(reporter)
    cfg = _check_config(reporter)
    daemon_ok = _check_daemon(reporter, cfg)
    _check_engine(reporter, cfg, env)
    # Resolved once and handed to both checks: `_wsl_split_home` shells out to `wsl.exe`
    # with a 20-second timeout, and each check used to pay for its own call — so a
    # stopped distro made `doctor` sit there for the better part of a minute.
    split_home = _wsl_split_home(env)
    _check_hook_script(reporter, cfg, env, split_home)
    _check_agent_hooks(reporter, cfg, env, split_home)
    _check_stats(reporter, cfg)
    if verbose:
        _live_hook_test(reporter, cfg, daemon_ok, interactive)
    if paint:
        _paint_selftest(reporter, cfg, interactive)

    print()
    if reporter.fails:
        extra = f", {reporter.warns} warning(s)" if reporter.warns else ""
        print(f"{reporter.fails} check(s) failed{extra} — see above.")
    elif reporter.warns:
        print(f"All essential checks passed ({reporter.warns} warning(s) — see above).")
    else:
        print("All checks passed.")
    print(f"Log file: {log_mod.log_path('tintaview')}")

    return 1 if reporter.fails else 0
