"""Tests for tintaview.engines.

Chroma is exercised against a real (fake) HTTP server on a random localhost port —
no Razer hardware or Synapse needed. OpenRGB is exercised against a fake ``openrgb``
module injected into ``sys.modules`` — no openrgb-python install needed. Together these
cover the full lifecycle (probe/open/set_color/close) without any vendor SDK present,
which is also exactly the environment CI runs in.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tintaview.core.config import ChromaConfig, Config, OpenRGBConfig
from tintaview.engines.chroma import ChromaEngine
from tintaview.engines.factory import available_engines, make_engine
from tintaview.engines.null import NullEngine
from tintaview.engines.openrgb import OpenRGBEngine

# --------------------------------------------------------------------------- Chroma


class _ChromaHandler(BaseHTTPRequestHandler):
    """Records every request so tests can assert on method/path/body, and answers the
    session-open POST the way real Chroma Connect does: with a URI to use from then on."""

    def _reply(self, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self) -> dict | None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw) if raw else None
        self.server.requests.append((self.command, self.path, body))  # type: ignore[attr-defined]
        return body

    def do_POST(self) -> None:
        self._record()
        session_uri = f"http://127.0.0.1:{self.server.server_port}/session123"
        self._reply({"uri": session_uri})

    def do_PUT(self) -> None:
        self._record()
        self._reply({})

    def do_DELETE(self) -> None:
        self._record()
        self._reply({})

    def log_message(self, *args: object) -> None:
        pass  # keep test output quiet


@pytest.fixture
def chroma_server():
    server = HTTPServer(("127.0.0.1", 0), _ChromaHandler)
    server.requests = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)


def _closed_port() -> int:
    """A localhost port nothing is listening on, for connection-refused tests."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_chroma_open_set_color_heartbeat_close(chroma_server):
    url = f"http://127.0.0.1:{chroma_server.server_port}/razer/chromasdk"
    engine = ChromaEngine(ChromaConfig(devices=["mouse"]), url=url)

    assert engine.open() is True
    assert engine.active is True
    method, path, _ = chroma_server.requests[-1]
    assert (method, path) == ("POST", "/razer/chromasdk")

    engine.set_color(0, 255, 136)  # r, g, b
    method, path, body = chroma_server.requests[-1]
    assert (method, path) == ("PUT", "/session123/mouse")
    assert body["effect"] == "CHROMA_STATIC"
    assert body["param"]["color"] == (136 << 16) | (255 << 8) | 0  # BGR packing

    engine.heartbeat()
    method, path, _ = chroma_server.requests[-1]
    assert (method, path) == ("PUT", "/session123/heartbeat")

    engine.close()
    method, path, _ = chroma_server.requests[-1]
    assert (method, path) == ("DELETE", "/session123")
    assert engine.active is False


def test_chroma_set_color_writes_every_configured_device(chroma_server):
    url = f"http://127.0.0.1:{chroma_server.server_port}/razer/chromasdk"
    engine = ChromaEngine(ChromaConfig(devices=["mouse", "headset"]), url=url)
    assert engine.open() is True

    engine.set_color(10, 20, 30)
    paths = {path for method, path, _ in chroma_server.requests if method == "PUT"}
    assert paths == {"/session123/mouse", "/session123/headset"}


def test_chroma_probe_leaves_no_orphaned_session(chroma_server):
    url = f"http://127.0.0.1:{chroma_server.server_port}/razer/chromasdk"
    engine = ChromaEngine(ChromaConfig(), url=url)

    assert engine.probe() is True
    assert engine.active is False  # probe() must never take control
    methods = [m for m, _, _ in chroma_server.requests]
    assert methods[-2:] == ["POST", "DELETE"]  # opened, then immediately released


def test_chroma_open_failure_sets_cooldown():
    url = f"http://127.0.0.1:{_closed_port()}/razer/chromasdk"
    engine = ChromaEngine(ChromaConfig(), url=url)

    assert engine.open() is False
    assert engine.active is False
    assert engine.in_cooldown() is True


def test_chroma_probe_failure_does_not_set_cooldown():
    url = f"http://127.0.0.1:{_closed_port()}/razer/chromasdk"
    engine = ChromaEngine(ChromaConfig(), url=url)

    assert engine.probe() is False
    assert engine.in_cooldown() is False  # only open() failures back off, per old code


# --------------------------------------------------------------------------- OpenRGB


class _FakeMode:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeColor:
    """Stand-in for openrgb.utils.RGBColor — same fields, comparable for assertions."""

    def __init__(self, red: int, green: int, blue: int) -> None:
        self.red = red
        self.green = green
        self.blue = blue

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _FakeColor):
            return NotImplemented
        return (self.red, self.green, self.blue) == (other.red, other.green, other.blue)

    def __repr__(self) -> str:
        return f"_FakeColor({self.red}, {self.green}, {self.blue})"


class _FakeDevice:
    def __init__(self, name, dtype, mode_names, active_mode, colors) -> None:
        self.name = name
        self.type = dtype
        self.modes = [_FakeMode(n) for n in mode_names]
        self.active_mode = active_mode
        self.colors = colors
        self.mode_calls: list = []
        self.color_calls: list = []
        self.colors_calls: list = []

    def set_mode(self, mode) -> None:
        self.mode_calls.append(mode)
        if isinstance(mode, str):
            self.active_mode = next(
                i for i, m in enumerate(self.modes) if m.name.lower() == mode.lower()
            )
        elif isinstance(mode, int):
            self.active_mode = mode

    def set_color(self, color) -> None:
        self.color_calls.append(color)

    def set_colors(self, colors) -> None:
        self.colors_calls.append(list(colors))


def _install_fake_openrgb(monkeypatch, devices, raise_on_connect=False):
    """Injects a minimal fake `openrgb` package into sys.modules for the test's
    duration (monkeypatch restores the previous entries automatically afterwards)."""
    utils_mod = types.ModuleType("openrgb.utils")

    class DeviceType:
        MOUSE = 6
        KEYBOARD = 5
        HEADSET = 8

    utils_mod.DeviceType = DeviceType
    utils_mod.RGBColor = _FakeColor

    openrgb_mod = types.ModuleType("openrgb")
    openrgb_mod.utils = utils_mod

    class FakeOpenRGBClient:
        def __init__(self, address="127.0.0.1", port=6742, name="openrgb-python") -> None:
            if raise_on_connect:
                raise OSError("connection refused")
            self.address = address
            self.port = port
            self.name = name
            self.devices = devices
            self.disconnect_called = False

        def disconnect(self) -> None:
            self.disconnect_called = True

    openrgb_mod.OpenRGBClient = FakeOpenRGBClient

    monkeypatch.setitem(sys.modules, "openrgb", openrgb_mod)
    monkeypatch.setitem(sys.modules, "openrgb.utils", utils_mod)
    return openrgb_mod, DeviceType


def test_openrgb_direct_mode_filter_and_restore(monkeypatch):
    mouse = _FakeDevice(
        "Mouse", 6, ["Static", "Direct"], active_mode=0,
        colors=[_FakeColor(1, 2, 3), _FakeColor(4, 5, 6)],
    )
    keyboard = _FakeDevice(  # no Direct mode -> must be skipped entirely
        "Keyboard", 5, ["Static", "Breathing"], active_mode=0, colors=[_FakeColor(9, 9, 9)],
    )
    _install_fake_openrgb(monkeypatch, [mouse, keyboard])

    engine = OpenRGBEngine(OpenRGBConfig(device_types=[], direct_mode_only=True,
                                          restore_on_release=True))
    assert engine.probe() is True

    assert engine.open() is True
    assert engine.active is True
    assert mouse.mode_calls == ["Direct"]
    assert mouse.active_mode == 1
    assert keyboard.mode_calls == []  # never touched — no Direct mode to switch into

    engine.set_color(10, 20, 30)
    assert mouse.color_calls[-1] == _FakeColor(10, 20, 30)
    assert keyboard.color_calls == []  # non-Direct device is never driven

    engine.close()
    assert engine.active is False
    assert mouse.mode_calls[-1] == 0  # restored to its original mode index
    assert mouse.colors_calls[-1] == [_FakeColor(1, 2, 3), _FakeColor(4, 5, 6)]


def test_openrgb_restore_disabled_skips_restore(monkeypatch):
    mouse = _FakeDevice("Mouse", 6, ["Static", "Direct"], active_mode=0,
                         colors=[_FakeColor(1, 2, 3)])
    _install_fake_openrgb(monkeypatch, [mouse])

    engine = OpenRGBEngine(OpenRGBConfig(direct_mode_only=True, restore_on_release=False))
    assert engine.open() is True
    engine.close()

    assert mouse.mode_calls == ["Direct"]  # only the takeover call, no restore call
    assert mouse.colors_calls == []


def test_openrgb_device_types_filter(monkeypatch):
    mouse = _FakeDevice("Mouse", 6, ["Direct"], active_mode=0, colors=[_FakeColor(0, 0, 0)])
    keyboard = _FakeDevice("Keyboard", 5, ["Direct"], active_mode=0, colors=[_FakeColor(0, 0, 0)])
    _install_fake_openrgb(monkeypatch, [mouse, keyboard])

    engine = OpenRGBEngine(OpenRGBConfig(device_types=["mouse"], direct_mode_only=True))
    assert engine.open() is True
    assert mouse.mode_calls == ["Direct"]
    assert keyboard.mode_calls == []  # filtered out by device_types, not by mode


def test_openrgb_no_matching_devices_fails_open(monkeypatch):
    keyboard = _FakeDevice("Keyboard", 5, ["Static"], active_mode=0, colors=[_FakeColor(0, 0, 0)])
    _install_fake_openrgb(monkeypatch, [keyboard])

    engine = OpenRGBEngine(OpenRGBConfig(direct_mode_only=True))
    assert engine.probe() is False  # nothing left after the Direct-mode filter
    assert engine.open() is False
    assert engine.active is False
    assert engine.in_cooldown() is True


def test_openrgb_connect_failure_is_quiet(monkeypatch):
    _install_fake_openrgb(monkeypatch, [], raise_on_connect=True)

    engine = OpenRGBEngine(OpenRGBConfig())
    assert engine.probe() is False
    assert engine.open() is False
    assert engine.active is False


def test_openrgb_missing_dependency_is_quiet(monkeypatch):
    # Simulates openrgb-python not being installed at all: `import openrgb` raises
    # ImportError. Setting sys.modules["openrgb"] = None makes Python do exactly that.
    monkeypatch.setitem(sys.modules, "openrgb", None)

    engine = OpenRGBEngine(OpenRGBConfig())
    assert engine.probe() is False
    assert engine.open() is False
    assert engine.active is False

    engine.set_color(1, 2, 3)  # must never raise even though nothing is connected
    engine.close()


# --------------------------------------------------------------------------- Null


def test_null_engine_is_always_available_and_inert():
    engine = NullEngine()

    assert engine.probe() is True
    assert engine.open() is True
    assert engine.active is False  # never "in control" — status-only

    engine.set_color(1, 2, 3)
    engine.heartbeat()
    engine.close()  # none of the above may raise


# --------------------------------------------------------------------------- factory


def test_factory_mode_forces_engine():
    cfg = Config()

    cfg.engine.mode = "chroma"
    assert isinstance(make_engine(cfg), ChromaEngine)

    cfg.engine.mode = "openrgb"
    assert isinstance(make_engine(cfg), OpenRGBEngine)

    cfg.engine.mode = "none"
    assert isinstance(make_engine(cfg), NullEngine)


def test_factory_auto_picks_first_engine_that_probes_true(monkeypatch):
    monkeypatch.setattr(ChromaEngine, "probe", lambda self: True)
    monkeypatch.setattr(OpenRGBEngine, "probe", lambda self: False)
    cfg = Config()
    cfg.engine.mode = "auto"
    cfg.engine.order = ["chroma", "openrgb"]

    assert isinstance(make_engine(cfg), ChromaEngine)


def test_factory_auto_respects_configured_order(monkeypatch):
    monkeypatch.setattr(ChromaEngine, "probe", lambda self: True)
    monkeypatch.setattr(OpenRGBEngine, "probe", lambda self: True)
    cfg = Config()
    cfg.engine.mode = "auto"
    cfg.engine.order = ["openrgb", "chroma"]  # OpenRGB first -> should win even though
    # both probe true, since auto mode is first-match-wins in configured order.

    assert isinstance(make_engine(cfg), OpenRGBEngine)


def test_factory_auto_falls_back_to_null_when_nothing_probes(monkeypatch):
    monkeypatch.setattr(ChromaEngine, "probe", lambda self: False)
    monkeypatch.setattr(OpenRGBEngine, "probe", lambda self: False)
    cfg = Config()  # default mode is "auto"

    assert isinstance(make_engine(cfg), NullEngine)


def test_factory_auto_survives_a_probe_that_raises(monkeypatch):
    def boom(self):
        raise RuntimeError("vendor SDK exploded")

    monkeypatch.setattr(ChromaEngine, "probe", boom)
    monkeypatch.setattr(OpenRGBEngine, "probe", lambda self: True)
    cfg = Config()
    cfg.engine.mode = "auto"

    assert isinstance(make_engine(cfg), OpenRGBEngine)


def test_available_engines_reports_every_known_engine(monkeypatch):
    monkeypatch.setattr(ChromaEngine, "probe", lambda self: True)
    monkeypatch.setattr(OpenRGBEngine, "probe", lambda self: False)
    cfg = Config()

    assert available_engines(cfg) == [("chroma", True), ("openrgb", False), ("none", True)]


def test_available_engines_never_raises(monkeypatch):
    def boom(self):
        raise RuntimeError("vendor SDK exploded")

    monkeypatch.setattr(ChromaEngine, "probe", boom)
    cfg = Config()

    result = dict(available_engines(cfg))
    assert result["chroma"] is False
    assert result["none"] is True
