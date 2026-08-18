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


@pytest.fixture(autouse=True)
def _no_real_component_installs(monkeypatch):
    """No test may run pip, run winget, or probe a real OpenRGB socket.

    The wizard offers to install missing prerequisites, and its default answer is "yes" —
    without this a test that picks OpenRGB would shell out to the network on a
    developer's machine.
    """
    from tintaview.engines import openrgb as openrgb_engine
    from tintaview.install import components

    monkeypatch.setattr(components, "openrgb_python_installed", lambda: True)
    monkeypatch.setattr(components, "winget_available", lambda: False)
    monkeypatch.setattr(components, "winget_package_installed", lambda pkg: None)
    monkeypatch.setattr(
        components, "install_openrgb_python",
        lambda: pytest.fail("the wizard tried to run pip"),
    )
    monkeypatch.setattr(
        components, "winget_install",
        lambda pkg: pytest.fail("the wizard tried to run winget"),
    )
    monkeypatch.setattr(openrgb_engine.OpenRGBEngine, "probe", lambda self: False)


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


def test_agent_numbers_are_a_selection_not_a_toggle(monkeypatch, capsys, _isolated):
    """Typing "1" means "I want agent 1", not "flip agent 1's checkbox".

    The toggle reading is the confusing one: with everything pre-ticked, "1 3" *removed*
    those two — the opposite of what it looks like it does.
    """
    home, tv_home = _isolated
    _feed(monkeypatch, [
        "",   # platform: accept
        "1",  # agents: claude, and only claude
        "",   # engine: accept default
        "",   # install path: accept default
        "n",  # autostart: no
        "y",  # hooks: apply
        "n",  # verify: skip
    ])

    assert wizard.run_wizard() == 0
    assert config_mod.load(tv_home / "config.toml").enabled_agents == ["claude"]


def test_multiselect_returns_exactly_what_was_typed(monkeypatch):
    items = [("a", "Agent A", True), ("b", "Agent B", True), ("c", "Agent C", True)]
    _feed(monkeypatch, ["1 3"])
    assert wizard._prompt_multiselect("q", items, assume_yes=False) == ["a", "c"]


def test_multiselect_accepts_commas_and_ignores_repeats(monkeypatch):
    items = [("a", "A", False), ("b", "B", False), ("c", "C", False)]
    _feed(monkeypatch, ["3,1,1"])
    assert wizard._prompt_multiselect("q", items, assume_yes=False) == ["c", "a"]


def test_multiselect_enter_keeps_the_suggestion(monkeypatch):
    items = [("a", "A", True), ("b", "B", False), ("c", "C", True)]
    _feed(monkeypatch, [""])
    assert wizard._prompt_multiselect("q", items, assume_yes=False) == ["a", "c"]


def test_multiselect_requires_a_pick_when_nothing_is_suggested(monkeypatch, capsys):
    """Enter can't mean "keep the suggestion" when there is no suggestion to keep."""
    items = [("a", "A", False), ("b", "B", False)]
    _feed(monkeypatch, ["", "2"])
    assert wizard._prompt_multiselect("q", items, assume_yes=False) == ["b"]
    assert "at least one" in capsys.readouterr().out.lower()


def test_multiselect_default_order_reorders_the_suggestion(monkeypatch):
    # Items are preselected in list order (a, b, c), but a previously-configured
    # order of ["c", "a", "b"] should win when the user just presses Enter — otherwise
    # re-running the wizard would silently reset a custom display order every time.
    items = [("a", "A", True), ("b", "B", True), ("c", "C", True)]
    _feed(monkeypatch, [""])
    assert wizard._prompt_multiselect(
        "q", items, assume_yes=False, default_order=["c", "a", "b"]
    ) == ["c", "a", "b"]


def test_multiselect_default_order_ignores_unselected_and_unknown_keys(monkeypatch):
    # "b" isn't preselected, so it's not in the default at all; a stale key in
    # default_order that no longer matches any item must not blow up the sort.
    items = [("a", "A", True), ("b", "B", False), ("c", "C", True)]
    _feed(monkeypatch, [""])
    assert wizard._prompt_multiselect(
        "q", items, assume_yes=False, default_order=["stale", "c", "a"]
    ) == ["c", "a"]


def test_choice_is_answered_by_number(monkeypatch):
    options = [("chroma", "Razer Chroma"), ("openrgb", "OpenRGB"), ("none", "Status only")]
    _feed(monkeypatch, ["2"])
    assert wizard._prompt_choice("q", options, "chroma", assume_yes=False) == "openrgb"


def test_choice_still_accepts_the_key_and_defaults_on_enter(monkeypatch):
    options = [("chroma", "Razer Chroma"), ("openrgb", "OpenRGB")]
    _feed(monkeypatch, ["openrgb"])
    assert wizard._prompt_choice("q", options, "chroma", assume_yes=False) == "openrgb"
    _feed(monkeypatch, [""])
    assert wizard._prompt_choice("q", options, "chroma", assume_yes=False) == "chroma"


def test_choice_reprompts_on_an_out_of_range_number(monkeypatch, capsys):
    options = [("a", "A"), ("b", "B")]
    _feed(monkeypatch, ["7", "1"])
    assert wizard._prompt_choice("q", options, "b", assume_yes=False) == "a"
    assert "1 to 2" in capsys.readouterr().out


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


# --------------------------------------------------------------------------- restart


def test_restart_targets_the_pid_the_daemon_reports(monkeypatch):
    """Identified by reported PID, never by scanning for python processes.

    A developer's machine can easily have a checkout, a test run and an install all
    looking like "a python process running tintaview"; killing the wrong one is worse
    than not restarting at all.
    """
    from tintaview.core.config import Config
    from tintaview.install import restart as restart_mod

    stopped: list[int] = []
    launched: list[bool] = []
    monkeypatch.setattr(restart_mod, "running_pid", lambda cfg: 4242)
    monkeypatch.setattr(restart_mod, "_stop", lambda pid: stopped.append(pid) or True)
    monkeypatch.setattr(restart_mod, "_launch", lambda: launched.append(True) or True)

    assert restart_mod.restart_if_running(Config()) is True
    assert stopped == [4242]
    assert launched == [True]


def test_restart_is_a_no_op_when_nothing_is_running(monkeypatch):
    """The fresh-install path: the installer starts the tray itself after the wizard."""
    from tintaview.core.config import Config
    from tintaview.install import restart as restart_mod

    def boom():
        raise AssertionError("must not launch a second copy when none was running")

    monkeypatch.setattr(restart_mod, "running_pid", lambda cfg: None)
    monkeypatch.setattr(restart_mod, "_launch", boom)
    assert restart_mod.restart_if_running(Config()) is False


def test_restart_never_kills_the_wizards_own_process(monkeypatch):
    import os

    from tintaview.core.config import Config
    from tintaview.install import restart as restart_mod

    def boom(pid):
        raise AssertionError("asked to kill the process running the wizard")

    monkeypatch.setattr(restart_mod, "running_pid", lambda cfg: os.getpid())
    monkeypatch.setattr(restart_mod, "_stop", boom)
    assert restart_mod.restart_if_running(Config()) is False


def test_restart_failure_never_breaks_the_wizard(monkeypatch):
    from tintaview.core.config import Config
    from tintaview.install import restart as restart_mod

    def boom(cfg):
        raise RuntimeError("network stack on fire")

    monkeypatch.setattr(restart_mod, "running_pid", boom)
    assert restart_mod.restart_if_running(Config()) is False


# --------------------------------------------------------------------------- prerequisites


def test_openrgb_offers_to_install_the_missing_client_library(monkeypatch, capsys):
    """Choosing OpenRGB with nothing to talk to it used to just write config and go quiet.

    The user then got no lighting plus a `doctor` line blaming the SDK server — advice
    that sends them to fix software that was never the problem.
    """
    from tintaview.core.config import Config
    from tintaview.install import components

    installed: list[bool] = []
    monkeypatch.setattr(components, "openrgb_python_installed", lambda: False)
    monkeypatch.setattr(
        components, "install_openrgb_python",
        lambda: (installed.append(True), (True, "openrgb-python installed"))[1],
    )
    _feed(monkeypatch, ["y"])  # yes, install it

    wizard._ensure_openrgb_ready(Config(), assume_yes=False)

    assert installed == [True]
    out = capsys.readouterr().out
    assert "openrgb-python" in out
    # And the part nothing can automate is still spelled out.
    assert "SDK Server" in out


def test_openrgb_declining_the_install_is_respected(monkeypatch, capsys):
    from tintaview.core.config import Config
    from tintaview.install import components

    monkeypatch.setattr(components, "openrgb_python_installed", lambda: False)
    monkeypatch.setattr(
        components, "install_openrgb_python",
        lambda: pytest.fail("installed despite the user declining"),
    )
    _feed(monkeypatch, ["n"])

    wizard._ensure_openrgb_ready(Config(), assume_yes=False)
    assert "skipped" in capsys.readouterr().out.lower()


def test_openrgb_offers_winget_only_when_the_app_is_actually_absent(monkeypatch, capsys):
    from tintaview.core.config import Config
    from tintaview.install import components

    calls: list[str] = []
    monkeypatch.setattr(components, "winget_available", lambda: True)
    monkeypatch.setattr(components, "winget_package_installed", lambda pkg: False)
    monkeypatch.setattr(
        components, "winget_install",
        lambda pkg: (calls.append(pkg), (True, "installed"))[1],
    )
    _feed(monkeypatch, ["y"])

    wizard._ensure_openrgb_ready(Config(), assume_yes=False)
    assert calls == [components.OPENRGB_WINGET_ID]


def test_openrgb_does_not_offer_winget_when_the_app_is_already_installed(monkeypatch, capsys):
    """Installed but unreachable means the SDK server is off — reinstalling won't help."""
    from tintaview.core.config import Config
    from tintaview.install import components

    monkeypatch.setattr(components, "winget_available", lambda: True)
    monkeypatch.setattr(components, "winget_package_installed", lambda pkg: True)
    monkeypatch.setattr(
        components, "winget_install", lambda pkg: pytest.fail("offered a pointless reinstall")
    )

    wizard._ensure_openrgb_ready(Config(), assume_yes=False)
    assert "SDK Server" in capsys.readouterr().out


def test_openrgb_ready_says_so_and_asks_nothing(monkeypatch, capsys):
    from tintaview.core.config import Config
    from tintaview.engines import openrgb as openrgb_engine

    monkeypatch.setattr(openrgb_engine.OpenRGBEngine, "probe", lambda self: True)
    wizard._ensure_openrgb_ready(Config(), assume_yes=False)

    out = capsys.readouterr().out
    assert "reachable" in out.lower()
    assert "SDK Server" not in out  # nothing left to explain


def test_ghub_missing_dll_offers_winget_install(monkeypatch, capsys):
    """A missing SDK DLL is a definite "G HUB isn't installed" signal on its own — unlike
    OpenRGB's library-vs-app-vs-server ambiguity — so this should always say so and, when
    winget can act on it, offer to install G HUB itself."""
    from tintaview.core.config import Config
    from tintaview.engines import ghub as ghub_engine
    from tintaview.install import components

    monkeypatch.setattr(ghub_engine, "discover_dll_path", lambda cfg: None)
    calls: list[str] = []
    monkeypatch.setattr(components, "winget_available", lambda: True)
    monkeypatch.setattr(components, "winget_package_installed", lambda pkg: False)
    monkeypatch.setattr(
        components, "winget_install",
        lambda pkg: (calls.append(pkg), (True, "installed"))[1],
    )
    _feed(monkeypatch, ["y"])

    wizard._ensure_ghub_ready(Config(), assume_yes=False)

    assert calls == [components.GHUB_WINGET_ID]
    out = capsys.readouterr().out
    assert "wasn't found" in out
    assert "G HUB" in out


def test_ghub_declining_the_winget_install_is_respected(monkeypatch, capsys):
    from tintaview.core.config import Config
    from tintaview.engines import ghub as ghub_engine
    from tintaview.install import components

    monkeypatch.setattr(ghub_engine, "discover_dll_path", lambda cfg: None)
    monkeypatch.setattr(components, "winget_available", lambda: True)
    monkeypatch.setattr(components, "winget_package_installed", lambda pkg: False)
    monkeypatch.setattr(
        components, "winget_install",
        lambda pkg: pytest.fail("installed despite the user declining"),
    )
    _feed(monkeypatch, ["n"])

    wizard._ensure_ghub_ready(Config(), assume_yes=False)
    assert "skipped" in capsys.readouterr().out.lower()


def test_ghub_missing_dll_without_winget_points_at_manual_download(monkeypatch, capsys):
    """No winget (e.g. Linux, or macOS): no prompt, just a direct link."""
    from tintaview.core.config import Config
    from tintaview.engines import ghub as ghub_engine
    from tintaview.install import components

    monkeypatch.setattr(ghub_engine, "discover_dll_path", lambda cfg: None)
    monkeypatch.setattr(components, "winget_available", lambda: False)

    wizard._ensure_ghub_ready(Config(), assume_yes=False)

    out = capsys.readouterr().out
    assert "logitechg.com" in out
    assert "winget" not in out.lower()


def test_ghub_dll_found_but_unresponsive_explains_start_order(monkeypatch, capsys):
    from tintaview.core.config import Config
    from tintaview.engines import ghub as ghub_engine

    monkeypatch.setattr(ghub_engine, "discover_dll_path", lambda cfg: Path("C:/fake/LogitechLed.dll"))
    monkeypatch.setattr(ghub_engine.GHubEngine, "probe", lambda self: False)

    wizard._ensure_ghub_ready(Config(), assume_yes=False)

    out = capsys.readouterr().out
    assert "Game lighting control" in out
    assert "before TintaView" in out


def test_ghub_ready_says_so_and_asks_nothing(monkeypatch, capsys):
    from tintaview.core.config import Config
    from tintaview.engines import ghub as ghub_engine

    monkeypatch.setattr(ghub_engine, "discover_dll_path", lambda cfg: Path("C:/fake/LogitechLed.dll"))
    monkeypatch.setattr(ghub_engine.GHubEngine, "probe", lambda self: True)

    wizard._ensure_ghub_ready(Config(), assume_yes=False)

    out = capsys.readouterr().out
    assert "you're set" in out.lower()
    assert "Game lighting control" not in out  # nothing left to explain


def test_engine_step_can_pick_ghub(monkeypatch, capsys, _isolated):
    home, tv_home = _isolated
    _feed(monkeypatch, ["", "", "ghub", "", "n", "y", "n"])  # typed by key, not position

    assert wizard.run_wizard() == 0

    assert config_mod.load(tv_home / "config.toml").engine.mode == "ghub"
    out = capsys.readouterr().out
    assert "G HUB can keep running" in out
    assert "turn these ON" in out
    assert "Turn these OFF" in out
    assert "Game lighting control" in out
    assert "Dynamic Lighting" in out


def test_engine_step_offers_auto_and_defaults_to_it(monkeypatch, capsys, _isolated):
    """Auto-detect must be offered, not just be an undocumented config value.

    Pinning one engine is what turns "the app I picked isn't running" into "no lighting
    at all, silently" — the wizard previously forced that choice, since `auto` existed in
    config.toml but was never on the menu.
    """
    home, tv_home = _isolated
    _feed(monkeypatch, [
        "",   # platform: accept
        "",   # agents: accept
        "",   # engine: accept the default
        "",   # install path
        "n",  # autostart
        "y",  # hooks
        "n",  # verify
    ])

    assert wizard.run_wizard() == 0
    assert config_mod.load(tv_home / "config.toml").engine.mode == "auto"
    assert "Detect automatically" in capsys.readouterr().out


def test_engine_step_still_allows_pinning_one(monkeypatch, _isolated):
    home, tv_home = _isolated
    _feed(monkeypatch, ["", "", "2", "", "n", "y", "n"])  # 2 = Razer Chroma
    assert wizard.run_wizard() == 0
    assert config_mod.load(tv_home / "config.toml").engine.mode == "chroma"


def test_unavailable_options_are_marked_but_still_selectable(monkeypatch, capsys, _isolated):
    """Marked, not hidden or blocked.

    Someone setting a machine up before installing Synapse/OpenRGB, or configuring over
    SSH, has good reason to pick something that isn't answering yet. But an option that
    cannot work right now must say so on its own line — otherwise the wizard accepts a
    choice that silently produces no lighting and gives no hint why.
    """
    home, tv_home = _isolated
    from tintaview.engines import factory

    # Nothing is running: both real engines probe False.
    monkeypatch.setattr(factory, "available_engines", lambda cfg: [("chroma", False), ("openrgb", False), ("none", True)])
    monkeypatch.setattr(wizard, "available_engines", lambda cfg: [("chroma", False), ("openrgb", False), ("none", True)])

    # Typed by key, not position — inserting new engines must not silently renumber
    # what an existing test (or a copied instruction) types.
    _feed(monkeypatch, ["", "", "openrgb", "", "n", "y", "n"])
    assert wizard.run_wizard() == 0

    out = capsys.readouterr().out
    assert "[not running]" in out
    assert "you can still pick it" in out
    # ...and the choice was honoured, not silently overridden.
    assert config_mod.load(tv_home / "config.toml").engine.mode == "openrgb"
