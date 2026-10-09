"""`install.hookscript.refresh_if_outdated` — the startup refresh of an installed
`tv-hook` that an upgrade left behind (it is never rewritten by the installer)."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from tintaview.core.config import Config
from tintaview.install import hookscript, wsl


@pytest.fixture
def native(monkeypatch, tmp_path):
    """A native install whose hook script lives at tmp_path/bin/tv-hook.sh."""
    monkeypatch.setenv("TINTAVIEW_HOME", str(tmp_path))
    hook = tmp_path / "bin" / "tv-hook.sh"
    monkeypatch.setattr(wsl, "hook_check", lambda cfg, env=None: wsl.HookCheck(hook))
    return hook


def test_states(native):
    assert hookscript.script_state(native) == hookscript.SCRIPT_MISSING
    native.parent.mkdir(parents=True)
    native.write_text("#!/bin/sh\n# an old shim\n", encoding="utf-8")
    assert hookscript.script_state(native) == hookscript.SCRIPT_OUTDATED
    native.write_text(hookscript.packaged_script("tv-hook.sh"), encoding="utf-8")
    assert hookscript.script_state(native) == hookscript.SCRIPT_CURRENT


def test_line_endings_do_not_count(native):
    native.parent.mkdir(parents=True)
    crlf = hookscript.packaged_script("tv-hook.sh").replace("\n", "\r\n")
    native.write_bytes(crlf.encode("utf-8"))
    assert hookscript.script_state(native) == hookscript.SCRIPT_CURRENT


def test_an_outdated_script_is_rewritten(native):
    native.parent.mkdir(parents=True)
    native.write_text("#!/bin/sh\n# an old shim\n", encoding="utf-8")

    assert hookscript.refresh_if_outdated(Config()) is True

    assert native.read_text(encoding="utf-8") == hookscript.packaged_script("tv-hook.sh")
    assert hookscript.refresh_if_outdated(Config()) is False  # idempotent


def test_a_current_script_is_left_alone(native):
    native.parent.mkdir(parents=True)
    native.write_text(hookscript.packaged_script("tv-hook.sh"), encoding="utf-8")
    before = native.stat().st_mtime_ns
    assert hookscript.refresh_if_outdated(Config()) is False
    assert native.stat().st_mtime_ns == before


def test_a_missing_script_is_not_installed(native):
    """Installing is the wizard's job (with hook.env); a refresh only replaces."""
    assert hookscript.refresh_if_outdated(Config()) is False
    assert not native.exists()


def test_hook_env_is_never_touched(native, tmp_path):
    native.parent.mkdir(parents=True)
    native.write_text("old", encoding="utf-8")
    env = tmp_path / "hook.env"
    env.write_text("TINTAVIEW_URL=http://127.0.0.1:9999\n", encoding="utf-8")
    hookscript.refresh_if_outdated(Config())
    assert env.read_text(encoding="utf-8") == "TINTAVIEW_URL=http://127.0.0.1:9999\n"


def test_an_unreachable_distro_does_nothing(monkeypatch):
    monkeypatch.setattr(wsl, "hook_check", lambda cfg, env=None: None)
    assert hookscript.refresh_if_outdated(Config()) is False


def test_split_install_reads_and_writes_inside_the_distro(monkeypatch):
    remote = PurePosixPath("/home/me/.tintaview/bin/tv-hook.sh")
    monkeypatch.setattr(
        wsl, "hook_check", lambda cfg, env=None: wsl.HookCheck(remote, "Ubuntu", "/home/me")
    )
    files = {str(remote): "#!/bin/sh\n# old\n"}

    def fake_run_in(distro, argv, **kwargs):
        assert distro == "Ubuntu"
        return files.get(str(remote), hookscript._MISSING_MARK)

    writes = []

    def fake_write(distro, path, content, *, executable=False):
        writes.append((distro, path, executable))
        files[path] = content

    monkeypatch.setattr(wsl, "run_in", fake_run_in)
    monkeypatch.setattr(wsl, "_write_remote_file", fake_write)

    assert hookscript.refresh_if_outdated(Config()) is True
    assert writes == [("Ubuntu", str(remote), True)]
    assert files[str(remote)] == hookscript.packaged_script("tv-hook.sh")


def test_split_install_unreadable_is_unknown_not_outdated(monkeypatch):
    def boom(*a, **k):
        raise wsl.WslError("distro stopped")

    monkeypatch.setattr(wsl, "run_in", boom)
    state = hookscript.script_state(PurePosixPath("/home/me/x/tv-hook.sh"), "Ubuntu")
    assert state == hookscript.SCRIPT_UNKNOWN


def test_both_packaged_scripts_exist():
    for name in ("tv-hook.sh", "tv-hook.cmd"):
        assert hookscript.packaged_script(name).strip()
        assert Path(hookscript.__file__).parent.parent.joinpath("hooks", name).is_file()
