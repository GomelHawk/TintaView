"""Stopping a running TintaView so the wizard's new settings take effect.

The thing under test is the *order* of the shutdown, not the relaunch: only `GET /quit`
closes the lighting engine, so only `/quit` gives the user their own lighting back. A
signal skips the engine teardown entirely and leaves the rig stuck on whatever colour
TintaView last painted — which is what happened every time before, since `_stop` opened
with SIGTERM.

`_is_alive` is stubbed in most of these: the real one either signals (POSIX) or calls
`OpenProcess` (Windows), and neither is something to point at a made-up PID.
"""

from __future__ import annotations

import http.server
import os
import signal
import sys
import threading
import types

import pytest

from tintaview.core.config import Config, ServerConfig
from tintaview.install import restart as R


class _QuitHandler(http.server.BaseHTTPRequestHandler):
    """Answers /quit with a configurable status and records what was asked for."""

    received: list[str] = []
    quit_status: int = 200

    def do_GET(self):  # noqa: N802 (stdlib method name)
        type(self).received.append(self.path)
        self.send_response(type(self).quit_status)
        self.end_headers()

    def log_message(self, *_args):
        pass


@pytest.fixture
def broker():
    """A fake broker on a free port, yielded as `(cfg, received, set_status)`."""
    received: list[str] = []
    handler = type("Handler", (_QuitHandler,), {"received": received, "quit_status": 200})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cfg = Config(server=ServerConfig(host="127.0.0.1", port=server.server_address[1]))
    try:
        yield cfg, received, lambda code: setattr(handler, "quit_status", code)
    finally:
        server.shutdown()
        thread.join(timeout=2)


# --------------------------------------------------------------------------- /quit first


def test_stop_asks_the_broker_to_quit_before_signalling(broker, monkeypatch):
    cfg, received, _ = broker
    monkeypatch.setattr(R, "_is_alive", lambda pid: not received)  # exits once asked

    def no_signals(*_args):
        raise AssertionError("SIGTERM was sent even though /quit worked")

    monkeypatch.setattr(R.os, "kill", no_signals)

    assert R._stop(4242, cfg) is True
    assert received == ["/quit"]


def test_stop_falls_back_to_a_signal_when_quit_is_not_implemented(broker, monkeypatch):
    """An install predating `/quit` answers 404; the restart must still work."""
    cfg, received, set_status = broker
    set_status(404)
    alive = [True]
    monkeypatch.setattr(R, "_is_alive", lambda pid: alive[0])

    signalled: list[int] = []

    def fake_kill(pid, sig):
        signalled.append(sig)
        alive[0] = False

    monkeypatch.setattr(R.os, "kill", fake_kill)

    assert R._stop(4242, cfg) is True
    assert received == ["/quit"]
    assert signalled == [signal.SIGTERM]


def test_stop_falls_back_to_a_signal_when_quit_is_accepted_but_ignored(broker, monkeypatch):
    """200 is not proof of an exit — a wedged process still has to be signalled."""
    cfg, _received, _ = broker
    monkeypatch.setattr(R, "_SHUTDOWN_GRACE", 0.05)
    monkeypatch.setattr(R, "_SIGNAL_GRACE", 0.05)
    monkeypatch.setattr(R, "_POLL", 0.01)
    monkeypatch.setattr(R, "_is_alive", lambda pid: True)

    signalled: list[int] = []
    monkeypatch.setattr(R.os, "kill", lambda pid, sig: signalled.append(sig))

    assert R._stop(4242, cfg) is True
    assert signalled[0] == signal.SIGTERM
    assert signalled[-1] == getattr(signal, "SIGKILL", signal.SIGTERM)


def test_stop_without_a_config_signals_straight_away(monkeypatch):
    """`cfg=None` means "we don't know where the broker is" — no HTTP call at all."""
    def no_http(*_args, **_kwargs):
        raise AssertionError("nothing to ask: there is no broker URL")

    monkeypatch.setattr(R.urllib.request, "urlopen", no_http)
    alive = [True]
    monkeypatch.setattr(R, "_is_alive", lambda pid: alive[0])
    signalled: list[int] = []
    monkeypatch.setattr(R.os, "kill", lambda pid, sig: (signalled.append(sig), alive.__setitem__(0, False)))

    assert R._stop(4242) is True
    assert signalled == [signal.SIGTERM]


def test_stop_is_a_no_op_when_the_process_is_already_gone(monkeypatch):
    monkeypatch.setattr(R, "_is_alive", lambda pid: False)

    def no_http(*_args, **_kwargs):
        raise AssertionError("nothing to quit")

    monkeypatch.setattr(R.urllib.request, "urlopen", no_http)
    monkeypatch.setattr(R.os, "kill", lambda *a: (_ for _ in ()).throw(AssertionError("no signals")))

    assert R._stop(4242, Config()) is True


def test_request_quit_is_false_when_nothing_answers():
    # Port 1 is reserved and nothing listens there, so this is a connection refusal.
    assert R._request_quit(Config(server=ServerConfig(host="127.0.0.1", port=1))) is False


# --------------------------------------------------------------------------- liveness


def test_is_alive_uses_no_signal_on_windows(monkeypatch):
    """`os.kill(pid, 0)` is emulated as TerminateProcess on Windows — the "harmless"
    existence probe would kill the very process we are waiting on."""
    monkeypatch.setattr(R.sys, "platform", "win32")
    monkeypatch.setattr(R, "_windows_is_alive", lambda pid: True)

    def no_signals(*_args):
        raise AssertionError("os.kill must never be used to probe on Windows")

    monkeypatch.setattr(R.os, "kill", no_signals)
    assert R._is_alive(4242) is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal semantics")
def test_is_alive_on_posix_reports_this_process_and_not_a_bogus_pid():
    assert R._is_alive(os.getpid()) is True
    # 2**22 is above every default pid_max, so nothing can be running under it.
    assert R._is_alive(2**22) is False


def test_wait_for_exit_returns_as_soon_as_the_process_goes(monkeypatch):
    calls = [True, True, False]
    monkeypatch.setattr(R, "_POLL", 0.001)
    monkeypatch.setattr(R, "_is_alive", lambda pid: calls.pop(0))
    assert R._wait_for_exit(4242, grace=5.0) is True
    assert calls == []


def test_wait_for_exit_gives_up_after_the_grace_period(monkeypatch):
    monkeypatch.setattr(R, "_POLL", 0.001)
    monkeypatch.setattr(R, "_is_alive", lambda pid: True)
    assert R._wait_for_exit(4242, grace=0.02) is False


# --------------------------------------------------------------------------- restart


def test_restart_passes_the_config_through_so_quit_can_be_used(monkeypatch):
    """`_stop` cannot ask for a graceful shutdown without knowing the broker's port."""
    seen: list[tuple] = []
    monkeypatch.setattr(R, "running_pid", lambda cfg: 4242)
    monkeypatch.setattr(R, "_stop", lambda pid, cfg=None: seen.append((pid, cfg)) or True)
    monkeypatch.setattr(R, "_launch", lambda: True)

    cfg = Config()
    assert R.restart_if_running(cfg) is True
    assert seen == [(4242, cfg)]


def test_the_relaunched_tray_does_not_keep_the_wizards_terminal(monkeypatch):
    """The wizard runs in a terminal window the user then closes; a relaunched tray still
    holding that pty would write to a dead terminal from then on."""
    seen: dict = {}
    monkeypatch.setattr(R, "_launch_in_desktop_session", lambda: False)
    monkeypatch.setattr(R.subprocess, "Popen", lambda argv, **kw: seen.update(kw))
    assert R._launch() is True
    if sys.platform == "win32":
        assert seen["creationflags"] & 0x00000008
    else:
        assert seen["start_new_session"] is True
        for stream in ("stdin", "stdout", "stderr"):
            assert seen[stream] is R.subprocess.DEVNULL


class _Systemctl:
    """Records `systemctl` calls; `session` is whether graphical-session.target is up."""

    def __init__(self, session: bool = True) -> None:
        self.session, self.calls = session, []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv[1:])
        ok = self.session if "is-active" in argv else True
        return types.SimpleNamespace(returncode=0 if ok else 3)


@pytest.fixture
def linux_unit(monkeypatch, tmp_path):
    from tintaview.install import autostart

    unit = tmp_path / "tintaview.service"
    unit.write_text("[Unit]\n")
    monkeypatch.setattr(R.sys, "platform", "linux")
    monkeypatch.setattr(autostart, "_linux_service_path", lambda: unit)
    monkeypatch.setattr(R.shutil, "which", lambda name: "/usr/bin/systemctl")
    fake = _Systemctl()
    monkeypatch.setattr(R.subprocess, "run", fake)
    monkeypatch.setattr(R.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("Popen from a shell with no desktop"))
    return fake, unit


def test_the_wizard_over_ssh_restarts_the_tray_in_the_desktop_session(linux_unit):
    """A tray Popen'ed from SSH/a tty has no DISPLAY and dies: the wizard's restart turned
    into closing the tray. The login's own systemd --user unit runs in the session."""
    fake, _ = linux_unit
    assert R._launch() is True
    assert ["--user", "restart", "tintaview.service"] in fake.calls


def test_no_graphical_session_falls_back_to_a_plain_launch(linux_unit, monkeypatch):
    fake, _ = linux_unit
    fake.session = False
    launched: list = []
    monkeypatch.setattr(R.subprocess, "Popen", lambda argv, **kw: launched.append(argv))
    assert R._launch() is True
    assert launched and ["--user", "restart", "tintaview.service"] not in fake.calls


def test_no_autostart_unit_falls_back_to_a_plain_launch(linux_unit, monkeypatch):
    fake, unit = linux_unit
    unit.unlink()
    launched: list = []
    monkeypatch.setattr(R.subprocess, "Popen", lambda argv, **kw: launched.append(argv))
    assert R._launch() is True
    assert launched and fake.calls == []


@pytest.fixture
def mac_agent(monkeypatch, tmp_path):
    from tintaview.install import autostart

    plist = tmp_path / "com.tintaview.app.plist"
    plist.write_text("<plist/>")
    monkeypatch.setattr(R.sys, "platform", "darwin")
    monkeypatch.setattr(autostart, "_macos_plist_path", lambda: plist)
    monkeypatch.setattr(R.shutil, "which", lambda name: "/bin/launchctl")
    monkeypatch.setattr(R.os, "getuid", lambda: 501, raising=False)
    calls: list = []
    monkeypatch.setattr(R.subprocess, "run",
                        lambda argv, **kw: calls.append(argv[1:])
                        or types.SimpleNamespace(returncode=0))
    monkeypatch.setattr(R.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("Popen from a shell with no desktop"))
    return calls, plist


def test_the_wizard_over_ssh_restarts_the_mac_tray_through_launchd(mac_agent):
    """The macOS twin of the systemd route: the autostart LaunchAgent, kickstarted in the
    user's GUI domain, runs in the desktop session wherever the wizard was run from."""
    calls, _ = mac_agent
    assert R._launch() is True
    assert calls == [["kickstart", "-k", "gui/501/com.tintaview.app"]]


def test_mac_without_the_launch_agent_falls_back_to_a_plain_launch(mac_agent, monkeypatch):
    calls, plist = mac_agent
    plist.unlink()
    launched: list = []
    monkeypatch.setattr(R.subprocess, "Popen", lambda argv, **kw: launched.append(argv))
    assert R._launch() is True
    assert launched and calls == []
