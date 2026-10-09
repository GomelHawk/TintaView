"""`core/keepawake.py` — the tray's "Keep awake" toggle.

The OS side (a power request, logind, caffeinate) cannot be observed from a test, so
these pin the lifecycle: which inhibitor is chosen, and that releasing — or the tray
dying — always ends it.
"""

from __future__ import annotations

import sys

import pytest

from tintaview.core import keepawake
from tintaview.core.keepawake import KeepAwake

# A stand-in inhibitor with the same shape as `systemd-inhibit … cat`: it lives until
# its stdin closes.
_CAT = [sys.executable, "-c", "import sys; sys.stdin.read()"]


@pytest.fixture
def posix_child(monkeypatch):
    """Route acquire() through the Linux branch with `_CAT` as the only command."""
    monkeypatch.setattr(keepawake.sys, "platform", "linux")
    monkeypatch.setattr(keepawake, "_linux_commands", lambda: [_CAT])


def test_acquire_and_release_a_child_inhibitor(posix_child):
    awake = KeepAwake()
    assert awake.acquire() is True
    child = awake._child
    assert awake.active

    awake.release()

    assert not awake.active
    assert child is not None and child.poll() is not None


def test_acquire_is_idempotent(posix_child):
    awake = KeepAwake()
    awake.acquire()
    first = awake._child
    awake.acquire()
    try:
        assert awake._child is first
    finally:
        awake.release()


def test_inhibitor_ends_when_its_stdin_closes(posix_child):
    """The crash-safety property: a dead tray closes the pipe, and the inhibitor goes
    with it instead of holding the machine awake forever."""
    awake = KeepAwake()
    awake.acquire()
    child = awake._child
    assert child is not None and child.stdin is not None
    child.stdin.close()
    child.wait(timeout=5)
    assert not awake.active  # noticed, not reported as still held


def test_falls_through_to_the_next_command_and_reports_failure(monkeypatch):
    monkeypatch.setattr(keepawake.sys, "platform", "linux")
    failing = [sys.executable, "-c", "raise SystemExit(1)"]
    monkeypatch.setattr(keepawake, "_linux_commands", lambda: [failing, ["no-such-binary-x"]])
    awake = KeepAwake()
    assert awake.acquire() is False
    assert not awake.active

    monkeypatch.setattr(keepawake, "_linux_commands", lambda: [failing, _CAT])
    try:
        assert awake.acquire() is True
    finally:
        awake.release()


def test_linux_prefers_gnome_session_inhibit_on_gnome(monkeypatch):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "ubuntu:GNOME")
    monkeypatch.setattr(keepawake.shutil, "which", lambda name: f"/usr/bin/{name}")
    commands = keepawake._linux_commands()
    assert [c[0] for c in commands] == ["gnome-session-inhibit", "systemd-inhibit"]
    assert all(c[-1] == "cat" for c in commands)
    # Sleep only: the screen must stay free to blank.
    assert commands[0][1:3] == ["--inhibit", "suspend"]
    assert "--what=sleep" in commands[1]


def test_linux_skips_gnome_inhibitor_elsewhere(monkeypatch):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    monkeypatch.setattr(keepawake.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert [c[0] for c in keepawake._linux_commands()] == ["systemd-inhibit"]


def test_macos_caffeinate_watches_our_pid(monkeypatch):
    monkeypatch.setattr(keepawake.sys, "platform", "darwin")
    spawned = []
    monkeypatch.setattr(keepawake, "_spawn", lambda cmd: spawned.append(cmd))
    KeepAwake().acquire()
    assert spawned[0][0] == "caffeinate"
    assert {"-i", "-s"} <= set(spawned[0])
    assert "-d" not in spawned[0]  # the display may sleep
    assert spawned[0][-2:] == ["-w", str(keepawake.os.getpid())]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows power request")
def test_windows_power_request_round_trip():
    awake = KeepAwake()
    assert awake.acquire() is True
    assert awake._handle is not None
    awake.release()
    assert not awake.active


def test_release_without_acquire_is_harmless():
    KeepAwake().release()
