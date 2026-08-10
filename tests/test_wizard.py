"""End-to-end tests for the setup wizard, plus focused tests for the WSL-split helpers.

The wizard is exercised the same way a real terminal session would drive it: `input()`
is replaced with a small queue that hands back scripted answers one at a time, and the
assertions look at what actually landed on disk (config.toml, the agent's hooks config,
the hook script + hook.env) rather than at wizard internals.

Everything is isolated from the real machine: `HOME`/`TINTAVIEW_HOME` point at a tmp
dir, `detect.is_wsl` is forced False so the suite behaves the same on a real WSL box (as
this repo's dev machine happens to be) as it does in CI, and `install.autostart`'s
`subprocess`/`shutil.which` are stubbed so nothing ever shells out to a real systemctl.

`tintaview.install.wsl` is tested separately at the bottom: its own suite stubs
`subprocess` completely, so no test anywhere in this file can invoke a real `wsl.exe` or
touch a real `~/.claude`, `~/.codex` or `~/.cursor`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tintaview.agents.base import HOOK_SENTINEL
from tintaview.core import config as config_mod
from tintaview.install import detect
from tintaview.install.detect import Environment
from tintaview.ui import wizard

# --------------------------------------------------------------------------- isolation


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """No test here may see the real HOME, WSL state, or shell out for real."""
    home = tmp_path / "home"
    home.mkdir()
    tv_home = tmp_path / "tvhome"
    monkeypatch.setenv("HOME", str(home))
    # pathlib.Path.home() on Windows reads USERPROFILE, not HOME — without this the
    # Claude/Codex/Cursor adapters' default_home() escapes this sandbox into the real
    # CI runner's profile whenever this suite actually runs on a Windows host.
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("TINTAVIEW_HOME", str(tv_home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    # This suite runs on a real WSL box as well as in CI — pin detection to plain
    # "linux" so the wizard's behaviour (and these tests) don't depend on that.
    monkeypatch.setattr(detect, "is_wsl", lambda: False)

    from tintaview.install import autostart

    monkeypatch.setattr(autostart.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        autostart.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
    )
    return home, tv_home


def _feed(monkeypatch, answers: list[str]):
    """`input()` returns each of `answers` in turn, then EOFError — like stdin closing."""
    it = iter(answers)

    def fake_input(prompt: str = "") -> str:
        del prompt
        try:
            return next(it)
        except StopIteration as exc:
            raise EOFError from exc

    monkeypatch.setattr("builtins.input", fake_input)


def _forbid_input(monkeypatch):
    def fake_input(prompt: str = "") -> str:
        raise AssertionError(f"input() must not be called under assume_yes (prompt={prompt!r})")

    monkeypatch.setattr("builtins.input", fake_input)


def _claude_settings(home: Path) -> Path:
    return home / ".claude" / "settings.json"


def _hook_bin_name() -> str:
    """These full-wizard runs never override platform detection, so the installed
    hook script's extension follows the real host, same as production code."""
    return "tv-hook.cmd" if sys.platform == "win32" else "tv-hook.sh"


# --------------------------------------------------------------------------- full runs


def test_writes_config_matching_answers(monkeypatch, _isolated):
    home, tv_home = _isolated
    _feed(monkeypatch, [
        "",          # platform: accept detected
        "",          # agents: accept pre-ticked (default config ships enabled=["claude"])
        "openrgb",   # engine: explicit, non-default choice
        "",          # install path: accept default
        "n",         # autostart: no
        "y",         # hooks: apply the diff for claude
        "n",         # verify: skip the live check
    ])

    assert wizard.run_wizard() == 0

    cfg = config_mod.load(tv_home / "config.toml")
    assert cfg.engine.mode == "openrgb"
    assert cfg.enabled_agents == ["claude"]

    settings = _claude_settings(home)
    assert HOOK_SENTINEL in settings.read_text()

    hook_bin = tv_home / "bin" / _hook_bin_name()
    assert hook_bin.exists()
    assert hook_bin.stat().st_mode & 0o111  # executable


def test_rerun_keeps_current_engine_as_default(monkeypatch, _isolated):
    home, tv_home = _isolated
    _feed(monkeypatch, ["", "", "openrgb", "", "n", "y", "n"])
    assert wizard.run_wizard() == 0

    # Reconfigure: blank answer on the engine question should keep "openrgb", not fall
    # back to whatever auto-detection would otherwise suggest.
    _feed(monkeypatch, ["", "", "", "", "n", "y", "n"])
    assert wizard.run_wizard() == 0

    cfg = config_mod.load(tv_home / "config.toml")
    assert cfg.engine.mode == "openrgb"


def test_enforces_at_least_one_agent(monkeypatch, capsys, _isolated):
    home, tv_home = _isolated
    _feed(monkeypatch, [
        "",   # platform: accept
        "1",  # agents: toggle claude OFF -> empty selection -> must re-prompt
        "1",  # agents: toggle claude back ON
        "",   # engine: accept default
        "",   # install path: accept default
        "n",  # autostart: no
        "y",  # hooks: apply
        "n",  # verify: skip
    ])

    assert wizard.run_wizard() == 0

    out = capsys.readouterr().out
    assert "at least one" in out.lower()

    cfg = config_mod.load(tv_home / "config.toml")
    assert cfg.enabled_agents == ["claude"]


def test_hook_diff_shown_but_not_applied_when_declined(monkeypatch, capsys, _isolated):
    home, tv_home = _isolated
    _feed(monkeypatch, ["", "", "", "", "n", "n", "n"])  # "n" declines the hook diff

    assert wizard.run_wizard() == 0

    out = capsys.readouterr().out
    assert HOOK_SENTINEL in out  # the diff was shown...
    assert "Skipped" in out
    assert not _claude_settings(home).exists()  # ...but never written


def test_hook_diff_shown_and_applied_when_accepted(monkeypatch, capsys, _isolated):
    home, tv_home = _isolated
    _feed(monkeypatch, ["", "", "", "", "n", "y", "n"])

    assert wizard.run_wizard() == 0

    out = capsys.readouterr().out
    assert HOOK_SENTINEL in out
    settings = _claude_settings(home)
    assert settings.exists()
    assert HOOK_SENTINEL in settings.read_text()


def test_assume_yes_needs_no_input(monkeypatch, _isolated):
    home, tv_home = _isolated
    _forbid_input(monkeypatch)

    assert wizard.run_wizard(assume_yes=True) == 0

    cfg = config_mod.load(tv_home / "config.toml")
    assert cfg.enabled_agents  # at least one agent, per the same rule as interactive mode
    assert _claude_settings(home).exists()

    hook_bin = tv_home / "bin" / _hook_bin_name()
    assert hook_bin.exists()
    assert hook_bin.stat().st_mode & 0o111
    assert (tv_home / "hook.env").exists()


# --------------------------------------------------------------------------- install_hook_script


def test_install_hook_script_uses_curl_exe_inside_wsl(_isolated):
    home, tv_home = _isolated
    cfg = config_mod.Config()
    env = Environment(platform="wsl", mode="wsl-split", distro="Ubuntu")

    hook_bin = wizard.install_hook_script(cfg, env)

    assert hook_bin == tv_home / "bin" / "tv-hook.sh"
    assert hook_bin.exists()
    if sys.platform != "win32":
        # chmod is a no-op on NTFS regardless of the simulated target — install_hook_script
        # rightly skips it when the real host (not env.platform) is Windows.
        assert hook_bin.stat().st_mode & 0o111
    env_text = (tv_home / "hook.env").read_text()
    assert "TINTAVIEW_CURL=curl.exe" in env_text
    assert "TINTAVIEW_URL=http://127.0.0.1:8777" in env_text


def test_install_hook_script_uses_plain_curl_elsewhere(_isolated):
    home, tv_home = _isolated
    cfg = config_mod.Config()
    env = Environment(platform="linux", mode="native")

    wizard.install_hook_script(cfg, env)

    env_text = (tv_home / "hook.env").read_text()
    assert "TINTAVIEW_CURL=curl\n" in env_text
    assert "curl.exe" not in env_text


# --------------------------------------------------------------------------- wsl.py


class _FakeCompleted:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def wsl_mod(monkeypatch):
    from tintaview.install import wsl

    monkeypatch.setattr(wsl.shutil, "which", lambda name: "/usr/bin/wsl.exe")
    return wsl


def test_run_in_builds_expected_wsl_command(monkeypatch, wsl_mod):
    calls = []

    def fake_run(cmd, input=None, capture_output=True, timeout=None, check=False):
        calls.append((cmd, input))
        return _FakeCompleted(stdout=b"hello\n")

    monkeypatch.setattr(wsl_mod.subprocess, "run", fake_run)

    out = wsl_mod.run_in("Ubuntu", ["echo", "hi"], input="payload")

    assert out == "hello\n"
    assert len(calls) == 1
    cmd, sent_input = calls[0]
    assert cmd == ["/usr/bin/wsl.exe", "-d", "Ubuntu", "--", "echo", "hi"]
    assert sent_input == b"payload"


def test_run_in_missing_wsl_exe_degrades_cleanly(monkeypatch, wsl_mod):
    monkeypatch.setattr(wsl_mod.shutil, "which", lambda name: None)
    with pytest.raises(wsl_mod.WslError, match="wsl.exe"):
        wsl_mod.run_in("Ubuntu", ["echo", "hi"])


def test_run_in_nonzero_exit_raises_wsl_error_with_stderr(monkeypatch, wsl_mod):
    monkeypatch.setattr(
        wsl_mod.subprocess, "run",
        lambda *a, **k: _FakeCompleted(returncode=1, stderr=b"boom"),
    )
    with pytest.raises(wsl_mod.WslError, match="boom"):
        wsl_mod.run_in("Ubuntu", ["false"])


def test_list_distros_delegates_to_detect(monkeypatch, wsl_mod):
    monkeypatch.setattr(wsl_mod.detect, "wsl_distros", lambda: ["Ubuntu", "Debian"])
    assert wsl_mod.list_distros() == ["Ubuntu", "Debian"]


def test_distro_home(monkeypatch, wsl_mod):
    monkeypatch.setattr(
        wsl_mod.subprocess, "run",
        lambda *a, **k: _FakeCompleted(stdout=b"/home/dmitry"),
    )
    assert wsl_mod.distro_home("Ubuntu") == "/home/dmitry"


def test_install_hook_writes_script_and_env_over_stdin(monkeypatch, wsl_mod):
    monkeypatch.setattr(wsl_mod, "distro_home", lambda distro: "/home/dmitry")
    writes = []

    def fake_write(distro, remote_path, content, *, executable=False):
        writes.append((distro, remote_path, content, executable))

    monkeypatch.setattr(wsl_mod, "_write_remote_file", fake_write)

    path = wsl_mod.install_hook("Ubuntu", "http://127.0.0.1:8777")

    assert str(path) == "/home/dmitry/.tintaview/bin/tv-hook.sh"
    assert len(writes) == 2
    (d1, p1, content1, exe1), (d2, p2, content2, exe2) = writes
    assert d1 == d2 == "Ubuntu"
    assert p1 == "/home/dmitry/.tintaview/bin/tv-hook.sh"
    assert exe1 is True
    assert "curl" in content1  # the real tv-hook.sh contents were read from disk
    assert p2 == "/home/dmitry/.tintaview/hook.env"
    assert "TINTAVIEW_URL=http://127.0.0.1:8777" in content2
    assert "TINTAVIEW_CURL=curl.exe" in content2
    assert exe2 is False


def test_install_agent_hooks_prefers_unc_route_when_nothing_installed(monkeypatch, wsl_mod):
    monkeypatch.setattr(wsl_mod, "distro_home", lambda distro: "/home/dmitry")
    monkeypatch.setattr(wsl_mod, "_tintaview_available", lambda distro: False)

    result = wsl_mod.install_agent_hooks("Ubuntu", ["claude"], assume_yes=False)

    assert result["route"] == "unc"
    plan = result["plans"]["claude"]
    assert not isinstance(plan, str)  # a real HookPlan, ready for the wizard to confirm
    assert str(plan.path) == r"\\wsl.localhost\Ubuntu\home\dmitry\.claude\settings.json"
    assert plan.action == "create"


def test_install_agent_hooks_uses_remote_tintaview_when_available(monkeypatch, wsl_mod):
    monkeypatch.setattr(wsl_mod, "distro_home", lambda distro: "/home/dmitry")
    monkeypatch.setattr(wsl_mod, "_tintaview_available", lambda distro: True)
    calls = []

    def fake_run_in(distro, argv, input=None, timeout=wsl_mod._TIMEOUT):
        calls.append(argv)
        return "already up to date"

    monkeypatch.setattr(wsl_mod, "run_in", fake_run_in)

    result = wsl_mod.install_agent_hooks("Ubuntu", ["claude", "codex"], assume_yes=True)

    assert result["route"] == "tintaview"
    assert calls == [
        ["tintaview", "hooks", "install", "--agent", "claude", "-y"],
        ["tintaview", "hooks", "install", "--agent", "codex", "-y"],
    ]
    assert result["plans"]["claude"] == "already up to date"


def test_install_agent_hooks_degrades_when_distro_unreachable(monkeypatch, wsl_mod):
    def boom(distro):
        raise wsl_mod.WslError(f"{distro} is not running")

    monkeypatch.setattr(wsl_mod, "distro_home", boom)

    result = wsl_mod.install_agent_hooks("Ubuntu", ["claude"], assume_yes=False)

    assert result["route"] == "failed"
    assert "not running" in result["notes"][0]
    assert result["plans"] == {}


def test_agent_homes_unc(monkeypatch, wsl_mod):
    monkeypatch.setattr(wsl_mod, "distro_home", lambda distro: "/home/dmitry")
    homes = wsl_mod.agent_homes_unc("Ubuntu")
    assert homes["claude"] == r"\\wsl.localhost\Ubuntu\home\dmitry\.claude"
    assert homes["codex"] == r"\\wsl.localhost\Ubuntu\home\dmitry\.codex"


def test_agent_homes_unc_degrades_to_empty_dict_when_unreachable(monkeypatch, wsl_mod):
    def boom(distro):
        raise wsl_mod.WslError("nope")

    monkeypatch.setattr(wsl_mod, "distro_home", boom)
    assert wsl_mod.agent_homes_unc("Ubuntu") == {}
