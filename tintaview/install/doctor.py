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

    for name in ("chroma", "openrgb"):
        if name not in probes:
            continue
        if probes[name]:
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

    if mode in ("chroma", "openrgb", "none"):
        selected = mode
    else:
        selected = next((n for n in cfg.engine.order if probes.get(n)), "none")

    if mode == "auto" and selected == "none":
        reporter.warn(
            "ENGINE", "auto mode found no usable lighting engine — running status-only",
            "start Razer Synapse or OpenRGB (with its SDK server enabled) if you expected "
            "lighting on this machine",
        )
    else:
        reporter.ok("ENGINE", f"engine in use: {selected}")


# --------------------------------------------------------------------------- 5. hook script


def _check_hook_script(reporter: _Reporter, cfg: Config) -> None:
    hook_bin = config_mod.hook_bin_path()

    if not hook_bin.exists():
        reporter.fail(
            "HOOK SCRIPT", f"{hook_bin} does not exist",
            "run `tintaview hooks install --agent all` to write it (or reinstall TintaView)",
        )
    elif sys.platform != "win32" and not os.access(hook_bin, os.X_OK):
        reporter.fail(
            "HOOK SCRIPT", f"{hook_bin} exists but is not executable",
            f"run `chmod +x {hook_bin}`",
        )
    else:
        reporter.ok("HOOK SCRIPT", f"{hook_bin} exists and is executable")

    hook_env = config_mod.hook_env_path()
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
        reporter.ok("HOOK SCRIPT", f"{hook_env} points at {expected_url}")


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


def _check_agent_hooks(reporter: _Reporter, cfg: Config) -> None:
    from ..agents import base as agents_base
    from . import hooks as hooks_mod

    if not cfg.enabled_agents:
        return  # already reported by _check_config

    hook_bin = config_mod.hook_bin_path()

    for key in cfg.enabled_agents:
        adapter = agents_base.get(key)
        if adapter is None:
            reporter.warn(
                "AGENT HOOKS", f"{key}: not a known agent (claude/codex/cursor)",
                f"fix the [agents] enabled list in {cfg.path or config_mod.config_path()}",
            )
            continue

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


def _live_hook_test(reporter: _Reporter, cfg: Config, daemon_ok: bool) -> None:
    if not daemon_ok:
        return

    host, port = cfg.server.host, cfg.server.port
    base = f"http://{host}:{port}"
    curl_cmd = f'curl -s "{base}/v1/event/working?agent=claude&sid=doctor-test"'
    reporter.ok("LIVE HOOK TEST", f"to fire a synthetic event by hand, run: {curl_cmd}")

    if not sys.stdin.isatty():
        return  # never block a non-interactive run (CI, tests, a piped invocation)

    try:
        answer = input(
            "Wait ~30s for a real hook event from a live agent session? [y/N] "
        ).strip().lower()
    except EOFError:
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


# --------------------------------------------------------------------------- entry point


def run_doctor(verbose: bool = False) -> int:
    """Run every check and print a report. 0 if everything essential is healthy, else 1."""
    reporter = _Reporter(verbose)

    env = _check_environment(reporter)
    cfg = _check_config(reporter)
    daemon_ok = _check_daemon(reporter, cfg)
    _check_engine(reporter, cfg, env)
    _check_hook_script(reporter, cfg)
    _check_agent_hooks(reporter, cfg)
    _check_stats(reporter, cfg)
    if verbose:
        _live_hook_test(reporter, cfg, daemon_ok)

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
