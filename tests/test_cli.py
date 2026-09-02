"""Tests for `tintaview.cli` — the argument parser and the run command's plumbing.

No subprocess and no real tray: `_cmd_run` is exercised with a stubbed
`StatusServer`, since what is worth pinning here is the ordering (logging before the
config load) and the signal/`/quit` wiring, not Qt.
"""

from __future__ import annotations

import signal
import threading

import pytest

from tintaview import cli

# --------------------------------------------------------------------------- parser


@pytest.mark.parametrize(
    "argv",
    [
        ["--headless"],
        ["run", "--headless"],
        ["--headless", "run"],
    ],
)
def test_every_headless_spelling_is_headless(argv):
    """All three used to be accepted and only two worked: `--headless` was declared on
    both the top-level parser and the `run` subparser, and argparse copies a subparser's
    defaults over what the top-level parser already parsed — so `tintaview --headless
    run` silently started the tray."""
    args = cli.build_parser().parse_args(argv)
    assert args.func is cli._cmd_run
    assert args.headless is True


@pytest.mark.parametrize("argv", [[], ["run"]])
def test_without_the_flag_the_tray_runs(argv):
    args = cli.build_parser().parse_args(argv)
    assert args.func is cli._cmd_run
    assert args.headless is False


def test_other_subcommands_still_parse():
    parser = cli.build_parser()
    assert parser.parse_args(["hooks", "status"]).action == "status"
    assert parser.parse_args(["doctor", "-v"]).verbose is True
    assert parser.parse_args(["update", "--check-only"]).check_only is True


# --------------------------------------------------------------------------- run


class _FakeServer:
    """Just enough StatusServer for `_cmd_run --headless`."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.url = "http://127.0.0.1:0"
        self.on_quit = None
        self.on_show = None
        self.stopped = False

    def start(self) -> bool:
        return True

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def fake_server(monkeypatch):
    from tintaview.core import server as server_mod

    created: list[_FakeServer] = []

    def factory(cfg):
        created.append(_FakeServer(cfg))
        return created[-1]

    monkeypatch.setattr(server_mod, "StatusServer", factory)
    return created


def test_logging_is_configured_before_the_config_is_read(monkeypatch, fake_server):
    """Under pythonw a config failure before `log.setup()` is an invisible exit: no
    console, no window, and nothing written down."""
    order: list[str] = []
    monkeypatch.setattr(cli.log_mod, "setup", lambda name="tintaview": order.append("log"))

    def _load(path=None):
        order.append("config")
        raise RuntimeError("unreadable config")

    monkeypatch.setattr(cli.config_mod, "load", _load)

    with pytest.raises(RuntimeError):
        cli.main(["--headless"])
    assert order == ["log", "config"]


def test_headless_quit_stops_the_server(monkeypatch, fake_server):
    """`/quit` and SIGTERM land on the same park event, so both reach `server.stop()`
    rather than killing the process with the device still held."""
    monkeypatch.setattr(cli.log_mod, "setup", lambda name="tintaview": None)
    monkeypatch.setattr(cli.config_mod, "load", lambda path=None: cli.config_mod.Config())

    installed: dict[int, object] = {}
    monkeypatch.setattr(cli, "_install_signal_handlers",
                        lambda handler: installed.setdefault("handler", handler))

    result: list[int] = []
    done = threading.Event()

    def run() -> None:
        result.append(cli.main(["--headless"]))
        done.set()

    thread = threading.Thread(target=run, daemon=True, name="test-cli-run")
    thread.start()

    server = _wait_for(lambda: fake_server[0] if fake_server else None)
    quit_callback = _wait_for(lambda: server.on_quit)
    quit_callback()  # what StatusServer.request_quit() calls on an HTTP worker thread

    assert done.wait(5.0)
    assert result == [0]
    assert server.stopped is True
    # The same handler is what SIGTERM/SIGINT would have run.
    assert callable(installed["handler"])


def _wait_for(get, timeout: float = 5.0):
    deadline = threading.Event()
    for _ in range(int(timeout * 200)):
        value = get()
        if value is not None:
            return value
        deadline.wait(0.005)
    raise AssertionError("timed out waiting for the run thread")


def test_signal_handlers_are_installed_for_the_signals_that_exist():
    """Guarded per signal: Windows has no SIGTERM worth the name, and a handler that
    cannot be installed must never stop TintaView from starting."""
    calls: list[object] = []
    previous = {name: signal.getsignal(getattr(signal, name))
                for name in ("SIGTERM", "SIGINT") if hasattr(signal, name)}
    try:
        cli._install_signal_handlers(lambda *a: calls.append(a))
        for name in previous:
            assert signal.getsignal(getattr(signal, name)) is not previous[name]
    finally:
        for name, handler in previous.items():
            signal.signal(getattr(signal, name), handler)
