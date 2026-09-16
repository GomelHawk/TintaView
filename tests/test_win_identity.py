"""Tests for `tintaview.install.win_identity` — the AppUserModelID that stops Windows
heading TintaView's notifications "Python".

Both calls really do something on Windows: one sets process state, the other writes to
`HKCU`. So every test here **forces** `sys.platform` and injects its own `ctypes`/
`winreg`, rather than letting the host decide what runs — a test suite may not leave a
key in the developer's (or a CI runner's) registry, and the interesting behaviour is the
same on every OS once the platform is pinned.
"""

from __future__ import annotations

import sys

import pytest

from tintaview.install import win_identity


class _FakeKey:
    def __init__(self, path: str, values: dict) -> None:
        self.path = path
        self._values = values

    def __enter__(self) -> _FakeKey:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _FakeWinreg:
    """Just the three names `register_app_identity` touches."""

    HKEY_CURRENT_USER = "HKCU"
    REG_SZ = 1

    def __init__(self) -> None:
        self.created: list[str] = []
        self.values: dict[str, str] = {}

    def CreateKey(self, root: str, path: str) -> _FakeKey:  # noqa: N802 - winreg's own name
        self.created.append(f"{root}\\{path}")
        return _FakeKey(path, self.values)

    def SetValueEx(self, key: _FakeKey, name: str, _reserved: int,  # noqa: N802 - winreg's own name
                   _kind: int, value: str) -> None:
        self.values[name] = value


class _BrokenCtypes:
    """Stands in for a Windows build where the shell entry point isn't there."""

    @property
    def windll(self):
        raise OSError("shell32 is not available")


@pytest.fixture
def on_windows(monkeypatch):
    monkeypatch.setattr(win_identity.sys, "platform", "win32")


def test_both_calls_are_silent_no_ops_off_windows(monkeypatch):
    monkeypatch.setattr(win_identity.sys, "platform", "linux")
    # Injected so a *Windows* test run can't reach the real shell32/registry through
    # this test either: the platform check is what has to stop it, not the host.
    monkeypatch.setitem(sys.modules, "ctypes", _BrokenCtypes())
    monkeypatch.setitem(sys.modules, "winreg", _FakeWinreg())

    assert win_identity.set_app_user_model_id() is False
    assert win_identity.register_app_identity(None) is False


def test_the_aumid_is_pinned_on_the_process(on_windows, monkeypatch):
    class _Shell:
        def __init__(self) -> None:
            self.aumids: list[str] = []

        def SetCurrentProcessExplicitAppUserModelID(self, aumid):  # noqa: N802 - Win32's own name
            self.aumids.append(aumid)

    class _Ctypes:
        def __init__(self, shell: _Shell) -> None:
            self.windll = type("_Windll", (), {"shell32": shell})()

    shell = _Shell()
    monkeypatch.setitem(sys.modules, "ctypes", _Ctypes(shell))

    assert win_identity.set_app_user_model_id() is True
    assert shell.aumids == [win_identity.APP_USER_MODEL_ID]


def test_the_registration_gives_the_aumid_a_name_and_an_icon(on_windows, monkeypatch, tmp_path):
    """Without these two values the AUMID is an unresolvable string and the notification
    header stays whatever Windows guessed from the interpreter."""
    fake = _FakeWinreg()
    monkeypatch.setitem(sys.modules, "winreg", fake)
    icon = tmp_path / "notification-icon.png"

    assert win_identity.register_app_identity(icon) is True

    assert fake.created == [rf"HKCU\Software\Classes\AppUserModelId\{win_identity.APP_USER_MODEL_ID}"]
    assert fake.values == {"DisplayName": "TintaView", "IconUri": str(icon)}


def test_a_registration_without_an_icon_still_names_the_app(on_windows, monkeypatch):
    fake = _FakeWinreg()
    monkeypatch.setitem(sys.modules, "winreg", fake)

    assert win_identity.register_app_identity(None) is True

    assert fake.values == {"DisplayName": "TintaView"}


def test_a_broken_shell_call_is_swallowed(on_windows, monkeypatch):
    """Whatever an odd Windows build raises, the tray still starts: this runs in the
    startup path and a cosmetic identity is never worth failing over."""
    monkeypatch.setitem(sys.modules, "ctypes", _BrokenCtypes())

    assert win_identity.set_app_user_model_id() is False


def test_a_refused_registry_write_is_swallowed(on_windows, monkeypatch):
    class _RefusingWinreg(_FakeWinreg):
        def CreateKey(self, root: str, path: str):  # noqa: N802 - winreg's own name
            raise PermissionError("locked-down hive")

    monkeypatch.setitem(sys.modules, "winreg", _RefusingWinreg())

    assert win_identity.register_app_identity(None) is False


def test_the_display_name_is_the_product_name():
    """The AUMID is an internal token; this is the string users actually see."""
    assert win_identity.DISPLAY_NAME == "TintaView"
