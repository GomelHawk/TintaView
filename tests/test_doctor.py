"""Tests for `tintaview doctor`.

Everything runs against a throwaway TINTAVIEW_HOME/HOME under tmp_path — nothing here
may create or touch `~/.tintaview`, `~/.claude`, `~/.codex` or `~/.cursor` for real.
Slow/real integrations (engine hardware probes, network usage APIs) are monkeypatched
out; the one piece of real infrastructure exercised end-to-end is a genuine
`StatusServer` bound to an ephemeral port, since the DAEMON check's whole point is to
tell a real daemon apart from "nothing there" and "something else entirely".
"""

from __future__ import annotations

import pathlib
import socket
import stat
import sys
from pathlib import PurePosixPath

import pytest

from tintaview.agents import base as agents_base
from tintaview.core import config as config_mod
from tintaview.core.config import AgentConfig, Config, ServerConfig
from tintaview.core.server import StatusServer
from tintaview.install import doctor as D
from tintaview.install import hooks as hooks_mod
from tintaview.stats import service as stats_service_mod
from tintaview.stats.model import UsageResult, UsageRow


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """No test may touch the real user's ~/.tintaview, ~/.claude, ~/.codex, ~/.cursor."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    # pathlib.Path.home() on Windows reads USERPROFILE, not HOME — without this, the
    # agent adapters' default_home() (~/.claude etc.) escapes this sandbox into the
    # real CI runner's profile whenever this suite actually runs on a Windows host.
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("TINTAVIEW_HOME", str(tmp_path / "home" / ".tintaview"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    return tmp_path


@pytest.fixture(autouse=True)
def _no_real_engines(monkeypatch):
    """Never let doctor's ENGINE check reach out to a real Chroma/OpenRGB port —
    slow, flaky, and irrelevant to what these tests check."""
    from tintaview.engines import factory as factory_mod

    monkeypatch.setattr(
        factory_mod, "available_engines",
        lambda cfg: [("chroma", False), ("ghub", False), ("openrgb", False)],
    )


@pytest.fixture(autouse=True)
def _no_real_stats(monkeypatch):
    """Default stats stub: every enabled agent reports one clean OK row, no network."""

    class _OkStatsService:
        def __init__(self, cfg):
            self._cfg = cfg

        def fetch_all(self, timeout: float = 15.0):
            return {
                key: UsageResult(agent=key, rows=[UsageRow(label="ok", pct=1.0)], source="official")
                for key in self._cfg.enabled_agents
            }

    monkeypatch.setattr(stats_service_mod, "StatsService", _OkStatsService)


def _write_config(**overrides) -> Config:
    cfg = Config(
        server=ServerConfig(host="127.0.0.1", port=overrides.pop("port", _free_port())),
        enabled_agents=overrides.pop("enabled_agents", []),
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    config_mod.save(cfg)
    return config_mod.load()


def _write_hook_bin() -> None:
    path = config_mod.hook_bin_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _write_hook_env(url: str) -> None:
    path = config_mod.hook_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"TINTAVIEW_URL={url}\nTINTAVIEW_CURL=curl\n", encoding="utf-8")


def _install_claude_hooks(hook_bin=None) -> None:
    adapter = agents_base.get("claude")
    plan = hooks_mod.plan_install(adapter, hook_bin or config_mod.hook_bin_path())
    hooks_mod.apply(plan)


# --------------------------------------------------------------------------- healthy


def test_healthy_install_returns_zero(capsys):
    cfg = _write_config(enabled_agents=["claude"])
    _write_hook_bin()
    _write_hook_env(f"http://{cfg.server.host}:{cfg.server.port}")
    _install_claude_hooks()

    server = StatusServer(cfg)
    assert server.start()
    try:
        rc = D.run_doctor(verbose=False)
    finally:
        server.stop()

    out = capsys.readouterr().out
    assert "[FAIL]" not in out, out
    assert rc == 0


# --------------------------------------------------------------------------- hook script


def test_missing_hook_script_is_fail_naming_the_fix(capsys):
    _write_config(enabled_agents=[])  # keep the report focused: no daemon, no agents

    rc = D.run_doctor(verbose=False)
    out = capsys.readouterr().out

    assert rc == 1
    assert "[FAIL] HOOK SCRIPT" in out
    assert "does not exist" in out
    assert "tintaview hooks install --agent all" in out


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="NTFS has no POSIX exec bit; doctor deliberately skips this check on Windows",
)
def test_hook_script_not_executable_is_fail(capsys):
    _write_config(enabled_agents=[])
    path = config_mod.hook_bin_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o644)  # explicitly not executable

    rc = D.run_doctor(verbose=False)
    out = capsys.readouterr().out

    assert rc == 1
    assert "[FAIL] HOOK SCRIPT" in out
    assert "not executable" in out
    assert "chmod +x" in out


def test_hook_env_url_mismatch_is_fail(capsys):
    cfg = _write_config(enabled_agents=[])
    _write_hook_bin()
    _write_hook_env("http://127.0.0.1:9999")  # doesn't match cfg.server.port

    rc = D.run_doctor(verbose=False)
    out = capsys.readouterr().out

    assert rc == 1
    assert "[FAIL] HOOK SCRIPT" in out
    assert "does not point at the configured daemon URL" in out
    assert f"http://{cfg.server.host}:{cfg.server.port}" in out


# --------------------------------------------------------------------------- agent hooks


def test_stale_path_hook_is_reported(capsys):
    _write_config(enabled_agents=["claude"])
    _write_hook_bin()
    _write_hook_env(f"http://127.0.0.1:{_free_port()}")
    # Install against a hook_bin path that is NOT the one doctor will check against —
    # simulates a moved/reinstalled TintaView.
    _install_claude_hooks(hook_bin=config_mod.config_dir() / "old-location" / "tv-hook.sh")

    rc = D.run_doctor(verbose=False)
    out = capsys.readouterr().out

    assert rc == 1
    assert "[FAIL] AGENT HOOKS" in out
    assert "old tv-hook path" in out
    assert "tintaview hooks install --agent claude" in out


def test_missing_agent_hooks_is_fail_naming_the_fix(capsys):
    _write_config(enabled_agents=["claude"])
    _write_hook_bin()
    _write_hook_env(f"http://127.0.0.1:{_free_port()}")
    # Deliberately never installed for claude.

    rc = D.run_doctor(verbose=False)
    out = capsys.readouterr().out

    assert rc == 1
    assert "[FAIL] AGENT HOOKS" in out
    assert "hooks missing" in out
    assert "tintaview hooks install --agent claude" in out


# --------------------------------------------------------------------------- daemon


def test_daemon_not_running_is_fail(capsys):
    _write_config(enabled_agents=[], port=_free_port())  # nothing is listening there

    rc = D.run_doctor(verbose=False)
    out = capsys.readouterr().out

    assert rc == 1
    assert "[FAIL] DAEMON" in out
    assert "not running" in out
    assert "tintaview run" in out


def test_daemon_port_owned_by_something_else_is_fail(capsys):
    port = _free_port()
    _write_config(enabled_agents=[], port=port)

    # A plain HTTP server that answers, but not with TintaView's /healthz shape.
    import http.server
    import threading

    class _Foreign(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b"not tintaview"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", port), _Foreign)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        rc = D.run_doctor(verbose=False)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2.0)

    out = capsys.readouterr().out
    assert rc == 1
    assert "[FAIL] DAEMON" in out
    assert "another program is probably bound" in out


# --------------------------------------------------------------------------- credential safety


def test_no_credential_value_ever_appears_in_output(monkeypatch, capsys):
    fake_token = "sk-FAKE-SECRET-TOKEN-should-never-be-printed"  # noqa: S105 - test fixture value

    class _LeakyLookingProvider:
        """Holds a real-looking credential internally, but its contract (like every
        real UsageProvider) is to never put it in the returned result."""

        def __init__(self) -> None:
            self._token = fake_token

        def fetch(self, agent_config, timeout: float = 15.0) -> UsageResult:
            _ = self._token  # touched, but never surfaced
            return UsageResult(agent="claude", error="Claude usage unavailable (stubbed for a test).")

    class _StatsServiceWithLeakyProvider:
        def __init__(self, cfg):
            self._cfg = cfg
            self._provider = _LeakyLookingProvider()

        def fetch_all(self, timeout: float = 15.0):
            return {"claude": self._provider.fetch(self._cfg.agent("claude"), timeout)}

    monkeypatch.setattr(stats_service_mod, "StatsService", _StatsServiceWithLeakyProvider)

    _write_config(enabled_agents=["claude"])
    _write_hook_bin()
    _write_hook_env(f"http://127.0.0.1:{_free_port()}")
    _install_claude_hooks()

    D.run_doctor(verbose=False)
    out = capsys.readouterr().out

    assert fake_token not in out


# --------------------------------------------------------------------------- engine reasons


def test_ghub_unavailable_reason_names_the_platform_when_not_windows():
    from tintaview.install.detect import Environment

    env = Environment(platform="linux", mode="native")
    reason = D._engine_unavailable_reason("ghub", env, Config())
    assert "Windows-only" in reason
    assert "platform=linux" in reason


def test_ghub_unavailable_reason_points_at_g_hub_download_when_dll_missing(monkeypatch):
    from tintaview.engines import ghub as ghub_engine
    from tintaview.engines import ghub_env
    from tintaview.install.detect import Environment

    monkeypatch.setattr(ghub_engine, "discover_dll_path", lambda cfg: None)
    monkeypatch.setattr(
        ghub_env, "inspect",
        lambda cfg: ghub_env.GHubEnvironment(
            dll_path=None, running=None, dynamic_lighting=None,
            foreground_only=None, integration="unknown",
        ),
    )
    env = Environment(platform="windows", mode="native")

    reason = D._engine_unavailable_reason("ghub", env, Config())
    assert "wasn't found" in reason
    assert "logitechg.com" in reason


def test_ghub_unavailable_reason_when_ghub_not_running(monkeypatch):
    from pathlib import Path

    from tintaview.engines import ghub as ghub_engine
    from tintaview.engines import ghub_env
    from tintaview.install.detect import Environment

    path = Path("C:/fake/LogitechLed.dll")
    monkeypatch.setattr(ghub_engine, "discover_dll_path", lambda cfg: path)
    monkeypatch.setattr(
        ghub_env, "inspect",
        lambda cfg: ghub_env.GHubEnvironment(
            dll_path=path, running=False, dynamic_lighting=None,
            foreground_only=None, integration="unknown",
        ),
    )
    env = Environment(platform="windows", mode="native")

    reason = D._engine_unavailable_reason("ghub", env, Config())
    assert "not running" in reason


def test_pinned_ghub_prints_measured_blockers(monkeypatch, capsys):
    from pathlib import Path

    from tintaview.engines import factory as factory_mod
    from tintaview.engines import ghub as ghub_engine
    from tintaview.engines import ghub_env
    from tintaview.install.detect import Environment

    path = Path("C:/fake/LogitechLed.dll")
    monkeypatch.setattr(
        factory_mod, "available_engines",
        lambda cfg: [("chroma", False), ("ghub", True), ("openrgb", False)],
    )
    monkeypatch.setattr(ghub_engine, "discover_dll_path", lambda cfg: path)
    monkeypatch.setattr(
        ghub_env, "inspect",
        lambda cfg: ghub_env.GHubEnvironment(
            dll_path=path, running=True, dynamic_lighting=True,
            foreground_only=None, integration="unknown",
        ),
    )
    cfg = Config()
    cfg.engine.mode = "ghub"
    D._check_engine(
        D._Reporter(verbose=False), cfg, Environment(platform="windows", mode="native"),
    )
    out = capsys.readouterr().out
    assert "Dynamic Lighting" in out
    assert "turn these ON" not in out  # measured blockers replace the full checklist


def test_auto_mode_does_not_print_ghub_checklist(monkeypatch, capsys):
    from tintaview.engines import factory as factory_mod
    from tintaview.install.detect import Environment

    monkeypatch.setattr(
        factory_mod, "available_engines",
        lambda cfg: [("chroma", False), ("ghub", False), ("openrgb", False)],
    )
    cfg = Config()
    cfg.engine.mode = "auto"
    D._check_engine(
        D._Reporter(verbose=False), cfg, Environment(platform="windows", mode="native"),
    )
    out = capsys.readouterr().out
    assert "turn these ON" not in out


# --------------------------------------------------------------------------- paint self-test


def test_paint_selftest_ok_when_user_confirms(monkeypatch, capsys):
    """`tintaview doctor --paint` drives the configured engine and asks the user —
    SDK success alone is not enough (G HUB silent no-ops)."""
    from tintaview.engines import factory as factory_mod
    from tintaview.engines.base import LightingEngine

    class _PaintEngine(LightingEngine):
        name = "fake"
        display_name = "Fake Paint"

        def __init__(self) -> None:
            self.colors: list[tuple[int, int, int]] = []
            self.opened = False
            self.closed = False

        def probe(self) -> bool:
            return True

        def open(self) -> bool:
            self.opened = True
            return True

        def set_color(self, r: int, g: int, b: int) -> None:
            self.colors.append((r, g, b))

        def close(self) -> None:
            self.closed = True

        @property
        def active(self) -> bool:
            return self.opened and not self.closed

    engine = _PaintEngine()
    monkeypatch.setattr(factory_mod, "make_engine", lambda cfg: engine)
    monkeypatch.setattr(D.time, "sleep", lambda s: None)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    reporter = D._Reporter(verbose=False)
    D._paint_selftest(reporter, Config())

    out = capsys.readouterr().out
    assert engine.opened and engine.closed
    assert len(engine.colors) == 3
    assert reporter.fails == 0
    assert "PAINT" in out
    assert "user confirmed" in out


def test_paint_selftest_fails_when_engine_cannot_open(monkeypatch, capsys):
    from tintaview.engines import factory as factory_mod
    from tintaview.engines.base import LightingEngine

    class _DeadEngine(LightingEngine):
        name = "fake"
        display_name = "Dead Paint"

        def probe(self) -> bool:
            return False

        def open(self) -> bool:
            return False

        def set_color(self, r: int, g: int, b: int) -> None:
            raise AssertionError("must not paint when open failed")

        def close(self) -> None:
            pass

        @property
        def active(self) -> bool:
            return False

    monkeypatch.setattr(factory_mod, "make_engine", lambda cfg: _DeadEngine())
    monkeypatch.setattr(D.time, "sleep", lambda s: None)

    reporter = D._Reporter(verbose=False)
    D._paint_selftest(reporter, Config())

    out = capsys.readouterr().out
    assert reporter.fails == 1
    assert "PAINT" in out
    assert "could not open" in out
    assert "[FAIL" in out


# --------------------------------------------------------------------------- config


def test_missing_config_file_is_warn_not_fail(capsys):
    # No config.toml written at all — config_mod.load() falls back to defaults.
    D.run_doctor(verbose=False)
    out = capsys.readouterr().out
    assert "[WARN] CONFIG" in out
    assert "tintaview setup" in out


def test_invalid_toml_is_fail(capsys):
    path = config_mod.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is not [ valid toml", encoding="utf-8")

    D.run_doctor(verbose=False)
    out = capsys.readouterr().out

    assert "[FAIL] CONFIG" in out
    assert "not valid TOML" in out


# --------------------------------------------------------------------------- windowed callers


def test_can_prompt_is_false_without_a_console(monkeypatch):
    """`sys.stdin` is None under `pythonw.exe`, so `sys.stdin.isatty()` raises rather
    than returning False — which is what broke the tray's Run diagnostics."""
    monkeypatch.setattr(D.sys, "stdin", None)
    assert D._can_prompt() is False


def test_can_prompt_is_false_on_a_closed_handle(monkeypatch):
    class _Closed:
        def isatty(self):
            raise ValueError("I/O operation on closed file")

    monkeypatch.setattr(D.sys, "stdin", _Closed())
    assert D._can_prompt() is False


def test_can_prompt_is_true_on_a_tty(monkeypatch):
    class _Tty:
        def isatty(self):
            return True

    monkeypatch.setattr(D.sys, "stdin", _Tty())
    assert D._can_prompt() is True


def test_live_hook_test_never_prompts_when_not_interactive(monkeypatch):
    """The regression: with a reachable daemon (always true when run *from* the tray,
    which is itself the daemon) the verbose run reached a prompt it could not answer."""
    def boom(*args, **kwargs):
        raise AssertionError("must not prompt")

    monkeypatch.setattr("builtins.input", boom)
    reporter = D._Reporter(verbose=True)

    D._live_hook_test(reporter, Config(), daemon_ok=True, interactive=False)  # must not raise


def test_run_doctor_auto_detects_a_missing_console(monkeypatch):
    """A windowed caller that forgets to pass `interactive` must still not prompt."""
    monkeypatch.setattr(D.sys, "stdin", None)
    seen: list[bool] = []
    monkeypatch.setattr(D, "_live_hook_test",
                        lambda r, c, d, interactive=True: seen.append(interactive))
    monkeypatch.setattr(D, "_check_daemon", lambda r, c: True)

    D.run_doctor(verbose=True, paint=False)

    assert seen == [False]


def test_run_doctor_forbids_prompting_when_asked_to(monkeypatch):
    """An explicit False has to beat a perfectly usable tty: a tray started from a
    terminal has a stdin the user cannot see, and would hang on a prompt."""
    class _Tty:
        def isatty(self):
            return True

    monkeypatch.setattr(D.sys, "stdin", _Tty())
    seen: list[bool] = []
    monkeypatch.setattr(D, "_live_hook_test",
                        lambda r, c, d, interactive=True: seen.append(interactive))
    monkeypatch.setattr(D, "_check_daemon", lambda r, c: True)

    D.run_doctor(verbose=True, paint=False, interactive=False)

    assert seen == [False]


def test_paint_selftest_warns_instead_of_claiming_an_unconfirmed_pass(monkeypatch, capsys):
    """Without a prompt the one thing this check proves — a human saw light — is
    unestablished, so it must not report OK."""
    from tintaview.engines import factory as factory_mod
    from tintaview.engines.base import LightingEngine

    class _PaintEngine(LightingEngine):
        name = "fake"
        display_name = "Fake Paint"

        def probe(self) -> bool:
            return True

        def open(self) -> bool:
            return True

        def set_color(self, r: int, g: int, b: int) -> None:
            pass

        def close(self) -> None:
            pass

        @property
        def active(self) -> bool:
            return True

    monkeypatch.setattr(factory_mod, "make_engine", lambda cfg: _PaintEngine())
    monkeypatch.setattr(D.time, "sleep", lambda s: None)

    def boom(*args, **kwargs):
        raise AssertionError("must not prompt")

    monkeypatch.setattr("builtins.input", boom)
    reporter = D._Reporter(verbose=True)

    D._paint_selftest(reporter, Config(), interactive=False)

    out = capsys.readouterr().out
    assert reporter.warns == 1
    assert reporter.fails == 0
    assert "could not ask whether you saw it" in out


# --------------------------------------------------------------------------- WSL split


def _fake_distro(tmp_path, monkeypatch, distro="Ubuntu", home="/home/dev"):
    r"""A stand-in for `\\wsl.localhost\<distro>\...`, backed by a real temp tree.

    Returns `(unc_root, posix_home, env)`. `wsl_path_to_unc` is redirected onto the tree
    so the checks open real files, and `distro_home` answers without spawning wsl.exe.
    """
    from tintaview.install import detect as detect_mod
    from tintaview.install import wsl as wsl_mod

    unc_root = tmp_path / "wsl" / distro
    monkeypatch.setattr(detect_mod, "wsl_path_to_unc", lambda d, p: str(unc_root) + str(p))
    monkeypatch.setattr(wsl_mod, "distro_home", lambda d: home)
    env = detect_mod.Environment(
        platform=detect_mod.PLATFORM_WINDOWS, mode=detect_mod.MODE_WSL_SPLIT, distro=distro
    )
    return unc_root, home, env


def _write_distro_hook_files(unc_root, home, url="http://127.0.0.1:8777"):
    from tintaview.install import wsl as wsl_mod

    bin_path = pathlib.Path(str(unc_root) + str(wsl_mod.remote_hook_bin(home)))
    env_path = pathlib.Path(str(unc_root) + str(wsl_mod.remote_hook_env(home)))
    bin_path.parent.mkdir(parents=True, exist_ok=True)
    bin_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(f"TINTAVIEW_URL={url}\nTINTAVIEW_CURL=curl.exe\n", encoding="utf-8")
    return bin_path, env_path


def _install_claude_hooks_in_distro(unc_root, home, hook_bin=None):
    """Wire the distro's `.claude/settings.json` exactly as the installer does."""
    from tintaview.install import wsl as wsl_mod

    adapter = agents_base.get("claude")
    settings = unc_root / "home" / "dev" / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    remote = wsl_mod.RemotePathAdapter(adapter, settings)
    plan = hooks_mod.plan_install(remote, hook_bin or wsl_mod.remote_hook_bin(home))
    hooks_mod.apply(plan)
    return settings


def test_wsl_split_hook_script_is_checked_inside_the_distro(tmp_path, monkeypatch, capsys):
    """The regression: the agents run inside the distro, so that is where the script
    they invoke lives. The Windows-side `bin\\tv-hook.cmd` is correctly absent, and
    checking for it failed a working install."""
    unc_root, home, env = _fake_distro(tmp_path, monkeypatch)
    _write_distro_hook_files(unc_root, home)
    cfg = _write_config(enabled_agents=[], port=8777)
    reporter = D._Reporter(verbose=True)

    D._check_hook_script(reporter, cfg, env)

    out = capsys.readouterr().out
    assert reporter.fails == 0, out
    assert "inside Ubuntu" in out
    assert "tv-hook.sh" in out
    assert "tv-hook.cmd" not in out  # never looked on the Windows side


def test_wsl_split_hook_script_genuinely_missing_still_fails(tmp_path, monkeypatch, capsys):
    """The fix must not make the check vacuous."""
    _unc_root, _home, env = _fake_distro(tmp_path, monkeypatch)
    cfg = _write_config(enabled_agents=[])
    reporter = D._Reporter(verbose=True)

    D._check_hook_script(reporter, cfg, env)

    assert reporter.fails == 2  # the script and hook.env
    assert "tv-hook.sh" in capsys.readouterr().out


def test_wsl_split_agent_hooks_are_read_from_the_distro(tmp_path, monkeypatch, capsys):
    unc_root, home, env = _fake_distro(tmp_path, monkeypatch)
    settings = _install_claude_hooks_in_distro(unc_root, home)
    cfg = _write_config(enabled_agents=["claude"],
                        agents={"claude": AgentConfig(home=str(settings.parent))})
    reporter = D._Reporter(verbose=True)

    D._check_agent_hooks(reporter, cfg, env)

    out = capsys.readouterr().out
    assert reporter.fails == 0, out
    assert "Claude Code: installed" in out
    assert str(settings) in out


def test_wsl_split_agent_hooks_compare_against_the_distro_hook_path(tmp_path, monkeypatch, capsys):
    """Hooks pointing somewhere else must still read as stale — proof the comparison
    is against the distro's tv-hook.sh and not merely always-true."""
    unc_root, home, env = _fake_distro(tmp_path, monkeypatch)
    settings = _install_claude_hooks_in_distro(
        unc_root, home, hook_bin=PurePosixPath("/opt/somewhere/else/tv-hook.sh")
    )
    cfg = _write_config(enabled_agents=["claude"],
                        agents={"claude": AgentConfig(home=str(settings.parent))})
    reporter = D._Reporter(verbose=True)

    D._check_agent_hooks(reporter, cfg, env)

    assert reporter.fails == 1
    assert "old tv-hook path" in capsys.readouterr().out


def test_wsl_split_agent_hooks_genuinely_missing_still_fails(tmp_path, monkeypatch, capsys):
    unc_root, _home, env = _fake_distro(tmp_path, monkeypatch)
    home_dir = unc_root / "home" / "dev" / ".claude"
    home_dir.mkdir(parents=True, exist_ok=True)
    cfg = _write_config(enabled_agents=["claude"],
                        agents={"claude": AgentConfig(home=str(home_dir))})
    reporter = D._Reporter(verbose=True)

    D._check_agent_hooks(reporter, cfg, env)

    assert reporter.fails == 1
    assert "hooks missing" in capsys.readouterr().out


def test_an_unreachable_distro_falls_back_to_local_checks(tmp_path, monkeypatch, capsys):
    """A stopped distro is a normal condition — it must not turn into a false failure
    report about the wrong filesystem, and it must not raise."""
    from tintaview.install import wsl as wsl_mod

    _unc_root, _home, env = _fake_distro(tmp_path, monkeypatch)

    def boom(distro):
        raise wsl_mod.WslError("Ubuntu isn't running")

    monkeypatch.setattr(wsl_mod, "distro_home", boom)
    _write_hook_bin()
    cfg = _write_config(enabled_agents=[])
    _write_hook_env(f"http://{cfg.server.host}:{cfg.server.port}")
    reporter = D._Reporter(verbose=True)

    D._check_hook_script(reporter, cfg, env)  # must not raise

    out = capsys.readouterr().out
    assert reporter.fails == 0, out
    assert str(config_mod.hook_bin_path()) in out  # the local path, as a fallback


def test_inside_the_distro_the_local_paths_are_used(tmp_path, monkeypatch, capsys):
    """`mode` is wsl-split on *both* sides; only the Windows half looks across the
    boundary. Inside the distro these are ordinary local files."""
    from tintaview.install import detect as detect_mod

    _fake_distro(tmp_path, monkeypatch)
    env = detect_mod.Environment(
        platform=detect_mod.PLATFORM_WSL, mode=detect_mod.MODE_WSL_SPLIT, distro="Ubuntu"
    )
    _write_hook_bin()
    cfg = _write_config(enabled_agents=[])
    _write_hook_env(f"http://{cfg.server.host}:{cfg.server.port}")
    reporter = D._Reporter(verbose=True)

    D._check_hook_script(reporter, cfg, env)

    out = capsys.readouterr().out
    assert reporter.fails == 0, out
    assert str(config_mod.hook_bin_path()) in out


# --------------------------------------------------------------------------- configured home


def test_configured_adapter_resolves_against_the_agent_home(tmp_path):
    adapter = agents_base.get("claude")
    cfg = Config(agents={"claude": AgentConfig(home=str(tmp_path / "elsewhere" / ".claude"))})

    resolved = D._configured_adapter(cfg, adapter)

    assert resolved.hooks_config_path() == tmp_path / "elsewhere" / ".claude" / "settings.json"
    assert resolved.key == "claude"  # everything else passes through


def test_configured_adapter_is_the_adapter_itself_without_an_override(tmp_path):
    adapter = agents_base.get("claude")
    assert D._configured_adapter(Config(), adapter) is adapter


# --------------------------------------------------------------------------- stats-only agents


def test_stats_only_agents_are_not_reported_as_config_errors(capsys):
    """copilot and jetbrains have no scriptable event API, so they are usage-only *by
    design* and belong in `agents.enabled`. Doctor used to call them unknown keys and
    tell the user to delete them — which would remove the usage cards the STATS section
    reports as working."""
    from tintaview.install import detect as detect_mod

    cfg = _write_config(enabled_agents=["copilot", "jetbrains"])
    env = detect_mod.Environment(platform=detect_mod.PLATFORM_LINUX,
                                 mode=detect_mod.MODE_NATIVE)
    reporter = D._Reporter(verbose=True)

    D._check_agent_hooks(reporter, cfg, env)

    out = capsys.readouterr().out
    assert reporter.fails == 0 and reporter.warns == 0, out
    assert "GitHub Copilot CLI: usage only" in out
    assert "JetBrains AI Assistant: usage only" in out
    assert "not a known agent" not in out


def test_a_genuinely_unknown_agent_key_still_warns(capsys):
    from tintaview.install import detect as detect_mod

    cfg = _write_config(enabled_agents=["nonesuch"])
    env = detect_mod.Environment(platform=detect_mod.PLATFORM_LINUX,
                                 mode=detect_mod.MODE_NATIVE)
    reporter = D._Reporter(verbose=True)

    D._check_agent_hooks(reporter, cfg, env)

    out = capsys.readouterr().out
    assert reporter.warns == 1
    assert "nonesuch: not a known agent" in out
    assert "claude/codex/cursor" in out  # listed from the registry, not hardcoded


# --------------------------------------------------------------------------- unreadable hooks


def test_unreadable_agent_config_is_a_warning_that_does_not_say_run_install(capsys):
    """A settings.json that exists but cannot be parsed must not read as "missing".

    "Missing" tells the user to run `hooks install`, which plans a CREATE and would
    replace a file full of their own hooks with only TintaView's.
    """
    from tintaview.install import detect as detect_mod

    cfg = _write_config(enabled_agents=["claude"])
    settings = agents_base.get("claude").hooks_config_path()
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("{ not json at all", encoding="utf-8")
    env = detect_mod.Environment(platform=detect_mod.PLATFORM_LINUX, mode=detect_mod.MODE_NATIVE)
    reporter = D._Reporter(verbose=True)

    D._check_agent_hooks(reporter, cfg, env)

    out = capsys.readouterr().out
    assert reporter.warns == 1 and reporter.fails == 0, out
    assert "could not be read or parsed" in out
    assert "hooks missing" not in out
    assert "do NOT run" in out


# --------------------------------------------------------------------------- wsl.exe cost


def test_run_doctor_resolves_the_wsl_split_home_only_once(tmp_path, monkeypatch):
    """`_wsl_split_home` shells out to `wsl.exe` with a 20-second timeout.

    The hook-script check and the agent-hooks check each used to call it, so a stopped
    distro cost `doctor` two full timeouts back to back.
    """
    from tintaview.install import detect as detect_mod

    _write_config(enabled_agents=["claude"])
    env = detect_mod.Environment(
        platform=detect_mod.PLATFORM_WINDOWS, mode=detect_mod.MODE_WSL_SPLIT, distro="Ubuntu"
    )
    monkeypatch.setattr(D, "_check_environment", lambda reporter: env)

    calls = []

    def counting(passed_env):
        calls.append(passed_env)
        return None  # distro unreachable — degrade to the local checks

    monkeypatch.setattr(D, "_wsl_split_home", counting)
    D.run_doctor(verbose=False)

    assert len(calls) == 1, f"wsl.exe was consulted {len(calls)} times, not once"
