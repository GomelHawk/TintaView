"""Tests for `tintaview.install.win_identity` — the AppUserModelID that stops Windows
heading TintaView's notifications "Python".

The Windows half (`SetCurrentProcessExplicitAppUserModelID`, the `HKCU` write) can only
run on Windows, so what is pinned here is the contract every *other* platform relies on:
both calls are silent no-ops that return False and never raise, because they sit in the
tray's startup path and a cosmetic identity must never keep the app from starting.
"""

from __future__ import annotations

from tintaview.install import win_identity


def test_both_calls_are_silent_no_ops_off_windows(monkeypatch):
    monkeypatch.setattr(win_identity.sys, "platform", "linux")

    assert win_identity.set_app_user_model_id() is False
    assert win_identity.register_app_identity(None) is False


def test_a_broken_shell_call_is_swallowed(monkeypatch):
    """Whatever ctypes raises on an odd Windows build, the tray still starts."""
    monkeypatch.setattr(win_identity.sys, "platform", "win32")

    # No `ctypes.windll` off Windows, so the import inside the function raises exactly
    # the way a missing/failing shell32 entry point would.
    assert win_identity.set_app_user_model_id() is False
    assert win_identity.register_app_identity(None) is False  # no `winreg` either


def test_the_display_name_is_the_product_name():
    """The AUMID is an internal token; this is the string users actually see."""
    assert win_identity.DISPLAY_NAME == "TintaView"
