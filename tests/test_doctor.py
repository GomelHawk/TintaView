"""Tests for `tintaview doctor`.

Everything runs against a throwaway TINTAVIEW_HOME/HOME under tmp_path — nothing here
may create or touch `~/.tintaview`, `~/.claude`, `~/.codex` or `~/.cursor` for real.
Slow/real integrations (engine hardware probes, network usage APIs) are monkeypatched
out; the one piece of real infrastructure exercised end-to-end is a genuine
`StatusServer` bound to an ephemeral port, since the DAEMON check's whole point is to
tell a real daemon apart from "nothing there" and "something else entirely".
"""

from __future__ import annotations

import socket
import stat
import sys

import pytest

from tintaview.agents import base as agents_base
from tintaview.core import config as config_mod
from tintaview.core.config import Config, ServerConfig
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
    from tintaview.install.detect import Environment

    monkeypatch.setattr(ghub_engine, "discover_dll_path", lambda cfg: None)
    env = Environment(platform="windows", mode="native")

    reason = D._engine_unavailable_reason("ghub", env, Config())
    assert "wasn't found" in reason
    assert "logitechg.com" in reason


def test_ghub_unavailable_reason_explains_start_order_when_dll_found_but_refuses(monkeypatch):
    from pathlib import Path

    from tintaview.engines import ghub as ghub_engine
    from tintaview.install.detect import Environment

    monkeypatch.setattr(ghub_engine, "discover_dll_path", lambda cfg: Path("C:/fake/LogitechLed.dll"))
    env = Environment(platform="windows", mode="native")

    reason = D._engine_unavailable_reason("ghub", env, Config())
    assert "Game lighting control" in reason
    assert "before TintaView" in reason


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
