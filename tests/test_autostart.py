"""Autostart backends, exercised per-platform via `sys.platform` monkeypatching plus a
stubbed subprocess layer — real `systemctl`/`launchctl`/PowerShell calls must never run
here. Each backend is checked for: the file(s) it writes, `enable()` idempotency,
`disable()` removing them, and `status()` reflecting reality — including when the
platform's own command reports "not enabled" even though the file is still on disk.
"""

from __future__ import annotations

import contextlib
import sys

import pytest

from tintaview.install import autostart


class FakeCompletedProcess:
    def __init__(self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRun:
    """Stands in for `subprocess.run`: records every call, never touches the OS."""

    def __init__(self, returncode: int = 0):
        self.calls: list[list[str]] = []
        self.returncode = returncode

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        return FakeCompletedProcess(returncode=self.returncode)


@pytest.fixture
def fake_run(monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(autostart.subprocess, "run", fake)
    return fake


@pytest.fixture
def fake_which(monkeypatch):
    """Every tool name resolves, as if present on PATH — deterministic, no real lookup."""
    monkeypatch.setattr(autostart.shutil, "which", lambda name: f"/usr/bin/{name}")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """No test may touch the real user's HOME/APPDATA/XDG dirs."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # pathlib.Path.home() on Windows reads USERPROFILE, not HOME — without this, every
    # sys.platform == "darwin"/"linux" monkeypatch below still resolves Path.home() to
    # the real CI runner's profile when the tests actually execute on a Windows host.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    return tmp_path


# --------------------------------------------------------------------------- Linux


def test_linux_enable_writes_service_and_desktop_files(tmp_path, monkeypatch, fake_run, fake_which):
    monkeypatch.setattr(sys, "platform", "linux")
    config_home = tmp_path / ".config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    assert autostart.enable() is True

    service = config_home / "systemd" / "user" / "tintaview.service"
    desktop = config_home / "autostart" / "tintaview.desktop"
    assert service.exists()
    assert desktop.exists()

    service_text = service.read_text()
    assert "ExecStart=" in service_text
    assert "Restart=on-failure" in service_text
    assert "After=graphical-session.target" in service_text
    assert "WantedBy=graphical-session.target" in service_text
    assert "Exec=" in desktop.read_text()

    joined = [" ".join(c) for c in fake_run.calls]
    assert any("daemon-reload" in c for c in joined)
    assert any("enable" in c and "--now" in c and "tintaview.service" in c for c in joined)


def test_linux_enable_is_idempotent(tmp_path, monkeypatch, fake_run, fake_which):
    monkeypatch.setattr(sys, "platform", "linux")
    config_home = tmp_path / ".config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    assert autostart.enable() is True
    first = (config_home / "systemd" / "user" / "tintaview.service").read_text()
    assert autostart.enable() is True
    second = (config_home / "systemd" / "user" / "tintaview.service").read_text()

    assert first == second
    assert list((config_home / "autostart").glob("tintaview*.desktop")) == [
        config_home / "autostart" / "tintaview.desktop"
    ]


def test_linux_disable_removes_files(tmp_path, monkeypatch, fake_run, fake_which):
    monkeypatch.setattr(sys, "platform", "linux")
    config_home = tmp_path / ".config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    autostart.enable()
    assert autostart.disable() is True

    assert not (config_home / "systemd" / "user" / "tintaview.service").exists()
    assert not (config_home / "autostart" / "tintaview.desktop").exists()


def test_linux_status_false_when_nothing_installed(tmp_path, monkeypatch, fake_which):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))

    assert autostart.status() is False


def test_linux_status_follows_systemctl_is_enabled(tmp_path, monkeypatch, fake_which):
    monkeypatch.setattr(sys, "platform", "linux")
    config_home = tmp_path / ".config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    # Only the systemd unit exists (no .desktop fallback) so status must ask systemctl.
    service_dir = config_home / "systemd" / "user"
    service_dir.mkdir(parents=True)
    (service_dir / "tintaview.service").write_text("[Unit]\n")

    monkeypatch.setattr(autostart.subprocess, "run",
                        lambda args, **kw: FakeCompletedProcess(returncode=0))
    assert autostart.status() is True

    monkeypatch.setattr(autostart.subprocess, "run",
                        lambda args, **kw: FakeCompletedProcess(returncode=1))
    assert autostart.status() is False


def test_linux_status_true_from_desktop_file_alone(tmp_path, monkeypatch, fake_which):
    """No systemd at all (e.g. a minimal distro) — the .desktop fallback still counts."""
    monkeypatch.setattr(sys, "platform", "linux")
    config_home = tmp_path / ".config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    autostart_dir = config_home / "autostart"
    autostart_dir.mkdir(parents=True)
    (autostart_dir / "tintaview.desktop").write_text("[Desktop Entry]\n")

    def boom(*args, **kwargs):
        raise AssertionError("status() must not shell out when the .desktop file alone answers it")

    monkeypatch.setattr(autostart.subprocess, "run", boom)
    assert autostart.status() is True


# --------------------------------------------------------------------------- Windows


class FakeWinreg:
    """A stand-in for the `winreg` stdlib module, which only exists on Windows.

    The Windows backend has to be exercised on the Linux/macOS CI runners too, and a real
    registry write would be an unacceptable side effect even when they *are* Windows.
    Modelled closely enough to catch the mistakes that matter: the key/value names, the
    exact command string written, and `FileNotFoundError` for a value that isn't there
    (which is what `winreg` actually raises, and what `disable()`/`status()` branch on).
    """

    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 0x0002
    KEY_QUERY_VALUE = 0x0001
    REG_SZ = 1

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.opened: list[tuple[str, str, int]] = []

    def OpenKey(self, root, sub_key, reserved, access):  # noqa: N802 - mirrors winreg's API
        self.opened.append((root, sub_key, access))
        return contextlib.nullcontext(self)

    def SetValueEx(self, key, name, reserved, type_, value):  # noqa: N802
        self.values[name] = value

    def QueryValueEx(self, key, name):  # noqa: N802
        if name not in self.values:
            raise FileNotFoundError(name)
        return (self.values[name], self.REG_SZ)

    def DeleteValue(self, key, name):  # noqa: N802
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


@pytest.fixture
def fake_winreg(monkeypatch):
    fake = FakeWinreg()
    monkeypatch.setitem(sys.modules, "winreg", fake)
    return fake


def test_windows_enable_writes_the_run_key_not_a_startup_shortcut(
    tmp_path, monkeypatch, fake_run, fake_which, fake_winreg
):
    """Windows 11 blocks any .lnk written into the Startup folder — see `_enable_windows`."""
    monkeypatch.setattr(sys, "platform", "win32")
    appdata = tmp_path / "AppData" / "Roaming"
    monkeypatch.setenv("APPDATA", str(appdata))
    venv_scripts = tmp_path / "Program Files" / "TintaView" / "venv" / "Scripts"
    venv_scripts.mkdir(parents=True)
    (venv_scripts / "pythonw.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(venv_scripts / "python.exe"))

    assert autostart.enable() is True

    # Nothing was shelled out to: no PowerShell, no COM, no subprocess at all.
    assert fake_run.calls == []
    startup = appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    assert not startup.exists(), "the Startup folder must not be touched any more"

    assert fake_winreg.opened[0][1] == r"Software\Microsoft\Windows\CurrentVersion\Run"
    value = fake_winreg.values["TintaView"]
    # The windowed, PSF-signed interpreter — not the unsigned pip console shim.
    # G HUB paints via a python.exe sidecar spawned from the tray.
    assert "pythonw.exe" in value
    assert value.endswith("-m tintaview")
    # The install prefix here contains a space, which is the normal case under
    # %LOCALAPPDATA% for most user names: the path must come back out quoted.
    assert value.startswith('"') and '" -m' in value


def test_windows_status_and_disable(tmp_path, monkeypatch, fake_run, fake_which, fake_winreg):
    monkeypatch.setattr(sys, "platform", "win32")
    appdata = tmp_path / "AppData" / "Roaming"
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(sys, "executable", str(tmp_path / "venv" / "Scripts" / "python.exe"))

    assert autostart.status() is False
    assert autostart.enable() is True
    assert autostart.status() is True

    assert autostart.disable() is True
    assert autostart.status() is False
    assert "TintaView" not in fake_winreg.values

    # disable() on an already-disabled install is a no-op, not an error: winreg raises
    # FileNotFoundError for a missing value and that must not surface as a failure.
    assert autostart.disable() is True


def test_windows_disable_also_clears_a_legacy_startup_shortcut(
    tmp_path, monkeypatch, fake_run, fake_which, fake_winreg
):
    """An install predating the Run-key switch must not end up launching twice."""
    monkeypatch.setattr(sys, "platform", "win32")
    appdata = tmp_path / "AppData" / "Roaming"
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(sys, "executable", str(tmp_path / "venv" / "Scripts" / "python.exe"))

    legacy = (appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
              / "TintaView.lnk")
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"")

    # With no Run-key value, status() still reports the old shortcut as "enabled".
    assert autostart.status() is True

    assert autostart.disable() is True
    assert not legacy.exists()
    assert autostart.status() is False


# --------------------------------------------------------------------------- macOS


def test_macos_enable_writes_plist_and_loads_it(tmp_path, monkeypatch, fake_run, fake_which):
    monkeypatch.setattr(sys, "platform", "darwin")

    assert autostart.enable() is True

    plist = tmp_path / "Library" / "LaunchAgents" / "com.tintaview.app.plist"
    assert plist.exists()
    content = plist.read_text()
    assert "com.tintaview.app" in content
    assert "RunAtLoad" in content
    assert "<true/>" in content

    joined = [" ".join(c) for c in fake_run.calls]
    assert any(c.endswith(f"load -w {plist}") for c in joined)


def test_macos_enable_is_idempotent(tmp_path, monkeypatch, fake_run, fake_which):
    monkeypatch.setattr(sys, "platform", "darwin")

    assert autostart.enable() is True
    plist = tmp_path / "Library" / "LaunchAgents" / "com.tintaview.app.plist"
    first = plist.read_text()
    assert autostart.enable() is True
    assert plist.read_text() == first


def test_macos_disable_unloads_and_removes(tmp_path, monkeypatch, fake_run, fake_which):
    monkeypatch.setattr(sys, "platform", "darwin")

    autostart.enable()
    assert autostart.disable() is True

    plist = tmp_path / "Library" / "LaunchAgents" / "com.tintaview.app.plist"
    assert not plist.exists()


def test_macos_status_follows_launchctl_list(tmp_path, monkeypatch, fake_which):
    monkeypatch.setattr(sys, "platform", "darwin")

    assert autostart.status() is False

    plist_dir = tmp_path / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True)
    (plist_dir / "com.tintaview.app.plist").write_text("<plist/>")

    monkeypatch.setattr(autostart.subprocess, "run",
                        lambda args, **kw: FakeCompletedProcess(returncode=0))
    assert autostart.status() is True

    monkeypatch.setattr(autostart.subprocess, "run",
                        lambda args, **kw: FakeCompletedProcess(returncode=1))
    assert autostart.status() is False


# --------------------------------------------------------------------------- degradation


def test_enable_never_raises_when_every_command_fails(tmp_path, monkeypatch):
    """No PowerShell/systemctl/launchctl on PATH at all — enable() must still return
    a bool, never propagate an exception, per the "must not fail an install" contract.
    """
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setattr(autostart.shutil, "which", lambda name: None)

    assert autostart.enable() in (True, False)
    assert autostart.disable() in (True, False)
    assert autostart.status() in (True, False)
