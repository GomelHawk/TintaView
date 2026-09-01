"""Tests for the HTTP status broker: server.py, controller.py, stalldetect.py.

Runs a real `StatusServer` on an ephemeral port (`server.port = 0`) with a fake
`LightingEngine`, and drives it with plain `urllib` requests — no mocking of the HTTP
layer itself, since the ack-before-I/O ordering and the watchdog/`  /state` interaction
are exactly the things worth exercising end-to-end.

Kept fast on purpose: `blink_ms` is set tiny in the test config, and the only real
sleeps in this file are short (<=50ms), used either to let the async blink thread tick
or to cross a tiny stall/watchdog deadline. The stall detector's own timing tests use
an injected fake clock and manual `tick()` calls instead of sleeping at all.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from urllib.error import HTTPError

import pytest

from tintaview import __version__
from tintaview.core.config import AgentConfig, Config
from tintaview.core.controller import LightController
from tintaview.core.server import StatusServer
from tintaview.core.stalldetect import StallDetector
from tintaview.engines.base import LightingEngine

# --------------------------------------------------------------------------- fixtures


class FakeEngine(LightingEngine):
    """Records every call instead of touching real hardware."""

    name = "fake"
    display_name = "Fake Engine"

    def __init__(self) -> None:
        self._active = False
        self.opens = 0
        self.closes = 0
        self.colors: list[tuple[int, int, int]] = []
        self.heartbeats = 0
        self.open_delay = 0.0  # simulate a slow vendor SDK call

    def probe(self) -> bool:
        return True

    def open(self) -> bool:
        if self.open_delay:
            time.sleep(self.open_delay)
        self._active = True
        self.opens += 1
        return True

    def set_color(self, r: int, g: int, b: int) -> None:
        self.colors.append((r, g, b))

    def close(self) -> None:
        self._active = False
        self.closes += 1

    def heartbeat(self) -> None:
        self.heartbeats += 1

    @property
    def active(self) -> bool:
        return self._active


def make_cfg() -> Config:
    cfg = Config()
    cfg.server.port = 0  # ephemeral: lets many tests/servers run without clashing
    cfg.server.host = "127.0.0.1"
    cfg.colors.blink_ms = 20  # fast blink so tests don't need long sleeps
    # `Config()` enables only "claude" by default, and the ingress now drops events for
    # agents the user hasn't enabled. These tests are about the state machine, not that
    # filter (which `test_disabled_agent_events_are_ignored` covers), so enable all three.
    cfg.enabled_agents = ["claude", "codex", "cursor"]
    return cfg


def make_server(cfg: Config | None = None, engine: FakeEngine | None = None):
    cfg = cfg or make_cfg()
    engine = engine if engine is not None else FakeEngine()
    controller = LightController(cfg, engine=engine)
    server = StatusServer(cfg, controller=controller)
    assert server.start() is True
    return server, engine


@pytest.fixture
def server_engine():
    server, engine = make_server()
    try:
        yield server, engine
    finally:
        server.stop()


# --------------------------------------------------------------------------- HTTP helpers


def _event(server: StatusServer, event: str, agent: str, sid: str, tool: str | None = None) -> None:
    qs = f"agent={agent}&sid={sid}"
    if tool:
        qs += f"&tool={tool}"
    url = f"{server.url}/v1/event/{event}?{qs}"
    with urllib.request.urlopen(url, timeout=2) as resp:
        assert resp.status == 200
        # The hook ack is deliberately empty (Content-Length: 0) — see server.py's
        # _write_ack docstring: it must go out before any lighting I/O runs.
        assert resp.read() == b""


def _legacy_event(server: StatusServer, event: str, sid: str) -> None:
    url = f"{server.url}/{event}?sid={sid}"
    with urllib.request.urlopen(url, timeout=2) as resp:
        assert resp.status == 200
        assert resp.read() == b""


def _get_state(server: StatusServer) -> dict:
    with urllib.request.urlopen(f"{server.url}/state", timeout=2) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _wait_until(predicate, timeout: float = 1.0, interval: float = 0.01) -> bool:
    """Poll `predicate` with short sleeps — a safety net against the inherent race
    between "ack sent" and "state mutated" (the server acks before doing any work by
    design), not a substitute for the server actually being fast."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# --------------------------------------------------------------------------- lifecycle


def test_full_session_lifecycle(server_engine):
    server, engine = server_engine
    cfg = server._cfg

    _event(server, "session-start", agent="claude", sid="s1")
    assert _wait_until(lambda: _get_state(server)["effective"] == "idle")
    state = _get_state(server)
    assert state["agents"]["claude"]["sessions"] == {"s1": "idle"}
    assert state["engine"] == {"name": "fake", "active": True, "note": None, "paused": False}
    assert engine.opens == 1  # opened lazily on the first non-"none" status

    _event(server, "working", agent="claude", sid="s1")
    assert _wait_until(lambda: _get_state(server)["effective"] == "working")
    # device_rgb, not rgb: the hardware gets saturated primaries, the tray icon
    # gets the brand palette (see DeviceColorsConfig).
    assert engine.colors[-1] == cfg.colors.device_rgb("working")

    _event(server, "confirm", agent="claude", sid="s1")
    assert _wait_until(lambda: _get_state(server)["effective"] == "confirm")
    assert _wait_until(lambda: _get_state(server)["blinking"] is True)
    # Poll for both blink halves rather than sleeping a fixed window and snapshotting:
    # the (fast, 20ms-period) blink thread's actual wake-up latency is at the mercy of
    # the OS scheduler, which can be surprisingly slow on loaded CI hosts.
    assert _wait_until(lambda: cfg.colors.device_rgb("confirm") in engine.colors[-5:])
    assert _wait_until(lambda: (0, 0, 0) in engine.colors[-5:])

    _event(server, "idle", agent="claude", sid="s1")
    assert _wait_until(lambda: _get_state(server)["effective"] == "idle")
    assert _wait_until(lambda: _get_state(server)["blinking"] is False)

    _event(server, "session-end", agent="claude", sid="s1")
    assert _wait_until(lambda: _get_state(server)["effective"] == "none")
    state = _get_state(server)
    assert state["engine"] == {"name": "fake", "active": False, "note": None, "paused": False}
    assert engine.closes == 1


def test_legacy_alias_defaults_to_claude(server_engine):
    server, _engine = server_engine
    _legacy_event(server, "working", sid="legacy1")
    assert _wait_until(lambda: "legacy1" in _get_state(server)["agents"].get("claude", {}).get("sessions", {}))
    state = _get_state(server)
    assert state["agents"]["claude"]["sessions"]["legacy1"] == "working"
    assert state["effective"] == "working"


def test_legacy_session_start_and_end_roundtrip(server_engine):
    server, engine = server_engine
    _legacy_event(server, "session-start", sid="legacy2")
    assert _wait_until(lambda: _get_state(server)["effective"] == "idle")
    _legacy_event(server, "session-end", sid="legacy2")
    assert _wait_until(lambda: _get_state(server)["effective"] == "none")
    assert engine.closes == 1


def test_multi_agent_priority_confirm_beats_working(server_engine):
    server, _engine = server_engine
    _event(server, "session-start", agent="claude", sid="c1")
    _event(server, "confirm", agent="claude", sid="c1")
    _event(server, "session-start", agent="codex", sid="x1")
    _event(server, "working", agent="codex", sid="x1")

    assert _wait_until(lambda: _get_state(server)["agents"].get("codex", {}).get("effective") == "working")
    state = _get_state(server)
    assert state["effective"] == "confirm"
    assert state["agents"]["claude"]["effective"] == "confirm"
    assert state["agents"]["codex"]["effective"] == "working"


def test_healthz(server_engine):
    server, _engine = server_engine
    with urllib.request.urlopen(f"{server.url}/healthz", timeout=2) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    assert body == {"ok": True, "version": __version__}


def test_unknown_event_is_404(server_engine):
    server, _engine = server_engine
    try:
        urllib.request.urlopen(f"{server.url}/v1/event/not-a-real-event?agent=claude&sid=s1", timeout=2)
        raise AssertionError("expected HTTPError")
    except HTTPError as exc:
        assert exc.code == 404


# --------------------------------------------------------------------------- watchdog


def test_state_does_not_reset_watchdog_timer(server_engine):
    server, _engine = server_engine
    _event(server, "session-start", agent="claude", sid="s1")
    assert _wait_until(lambda: not server.state.empty())

    time.sleep(0.1)
    before = server.state.idle_seconds()
    # Loose lower bound, not ~0.1: Windows CI runners' sleep()/clock granularity can
    # undershoot a tight one by a few ms even though nothing is actually wrong.
    assert before >= 0.08

    # Polling /state repeatedly must NOT touch the watchdog's last-event timestamp —
    # otherwise the tray polling it would keep a dead agent's lights on forever.
    for _ in range(3):
        _get_state(server)
    after = server.state.idle_seconds()
    assert after >= before  # kept climbing; /state reads did not reset it

    # A real event still resets it.
    _event(server, "working", agent="claude", sid="s1")
    assert _wait_until(lambda: server.state.idle_seconds() < before)


def test_hook_ack_is_not_blocked_by_slow_engine_io():
    cfg = make_cfg()
    engine = FakeEngine()
    engine.open_delay = 0.05  # simulate a slow Chroma/OpenRGB connect
    server, _engine = make_server(cfg, engine)
    try:
        start = time.monotonic()
        _event(server, "session-start", agent="claude", sid="s1")
        elapsed = time.monotonic() - start
        # The 200 ack must come back long before the (slow) engine.open() finishes —
        # this is the single most important constraint carried over from the old
        # server: a slow lighting SDK call must never make a hook time out.
        assert elapsed < engine.open_delay
        assert _wait_until(lambda: engine.opens == 1)
    finally:
        server.stop()


# --------------------------------------------------------------------------- stall detector (unit)


def test_stall_detector_fires_after_deadline():
    fired: list[tuple[str, str]] = []
    clock = {"t": 0.0}
    detector = StallDetector(lambda agent, sid: fired.append((agent, sid)), clock=lambda: clock["t"])

    detector.tool_start("cursor", "s1", stall_seconds=8.0)
    clock["t"] = 5.0
    detector.tick()
    assert fired == []  # not yet due

    clock["t"] = 8.5
    detector.tick()
    assert fired == [("cursor", "s1")]

    # Firing once removes the arm — a second tick at the same time must not refire.
    detector.tick()
    assert fired == [("cursor", "s1")]


def test_stall_detector_any_event_cancels_the_arm():
    fired: list[tuple[str, str]] = []
    clock = {"t": 0.0}
    detector = StallDetector(lambda agent, sid: fired.append((agent, sid)), clock=lambda: clock["t"])

    detector.tool_start("cursor", "s1", stall_seconds=8.0)
    clock["t"] = 2.0
    detector.tool_end("cursor", "s1")  # tool finished normally before the deadline

    clock["t"] = 100.0
    detector.tick()
    assert fired == []


def test_stall_detector_only_armed_sessions_are_candidates():
    fired: list[tuple[str, str]] = []
    clock = {"t": 0.0}
    detector = StallDetector(lambda agent, sid: fired.append((agent, sid)), clock=lambda: clock["t"])

    # Never armed via tool_start -> tick() must never invent a stall for it.
    clock["t"] = 10_000.0
    detector.tick()
    assert fired == []


def test_stall_detector_manual_start_stop_thread():
    """The daemon thread itself: start()/stop() must not require real waiting in the
    test — tick() is called on a background thread here only to prove start/stop
    plumbing works, not to test timing (that's covered by the tick() tests above)."""
    detector = StallDetector(lambda agent, sid: None)
    detector.start()
    thread = detector._thread
    assert thread is not None and thread.is_alive()
    detector.stop()
    assert not thread.is_alive()
    assert detector._thread is None  # stop() clears it, so a later start() rebuilds it


# --------------------------------------------------------------------------- stall detector (server integration)


def test_stall_promotes_cursor_session_to_confirm():
    cfg = make_cfg()
    cfg.agents["cursor"] = AgentConfig(confirm_detection="stall", stall_seconds=0.02)
    server, _engine = make_server(cfg)
    try:
        _event(server, "session-start", agent="cursor", sid="c1")
        _event(server, "tool-start", agent="cursor", sid="c1", tool="run_terminal")
        time.sleep(0.05)  # cross the 20ms stall deadline
        server._stall.tick()  # normally the daemon thread does this; drive it directly
        assert _wait_until(lambda: _get_state(server)["agents"]["cursor"]["effective"] == "confirm")
    finally:
        server.stop()


def test_stall_does_not_fire_for_event_confirm_detection():
    """Claude/Codex use confirm_detection="event" (the default) — a long tool call
    must never be armed for them, or a slow build would eventually paint them red."""
    cfg = make_cfg()
    server, _engine = make_server(cfg)
    try:
        _event(server, "session-start", agent="claude", sid="c1")
        _event(server, "tool-start", agent="claude", sid="c1", tool="pytest")
        time.sleep(0.05)
        server._stall.tick()
        assert _get_state(server)["agents"]["claude"]["effective"] == "working"
    finally:
        server.stop()


# --------------------------------------------------------------------------- port-in-use


def test_start_returns_false_when_port_taken():
    cfg = make_cfg()
    server1, _engine1 = make_server(cfg)
    try:
        # Bind the *same* concrete port the first server ended up on.
        cfg2 = make_cfg()
        cfg2.server.port = int(server1.url.rsplit(":", 1)[1])
        server2 = StatusServer(cfg2, controller=LightController(cfg2, engine=FakeEngine()))
        assert server2.start() is False
    finally:
        server1.stop()


def test_controller_thread_safety_smoke():
    """apply() called concurrently from many threads must never raise nor deadlock."""
    cfg = make_cfg()
    engine = FakeEngine()
    controller = LightController(cfg, engine=engine)

    statuses = ["idle", "working", "confirm", "none"] * 25
    errors: list[BaseException] = []

    def worker(status: str) -> None:
        try:
            controller.apply(status)
        except BaseException as exc:  # pragma: no cover - would fail the test anyway
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(s,)) for s in statuses]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=2.0)

    assert not errors
    controller.shutdown()


def test_engine_status_surfaces_status_note_on_state():
    """G HUB paint failures land on the engine as `status_note`; /state must carry
    them so the tray can balloon + append the tooltip without a second channel."""
    cfg = make_cfg()
    injected = FakeEngine()
    injected.status_note = "G HUB is ignoring lighting commands"
    controller = LightController(cfg, engine=injected)
    controller.apply("idle")

    assert controller.engine_status() == {
        "name": "fake",
        "active": True,
        "note": "G HUB is ignoring lighting commands",
        "paused": False,
    }

    server = StatusServer(cfg, controller=controller)
    assert server.start() is True
    try:
        state = _get_state(server)
        assert state["engine"]["note"] == "G HUB is ignoring lighting commands"
    finally:
        server.stop()


def test_reset_engine_rebuilds_from_current_config():
    """`reset_engine()` (added for the settings dialog's live engine-mode switch)
    must close whatever is open and drop the cached engine, so the *next* status
    change rebuilds from `cfg.engine.mode` via the real factory rather than reusing
    the stale instance.
    """
    cfg = make_cfg()
    injected = FakeEngine()
    controller = LightController(cfg, engine=injected)

    controller.apply("working")
    assert injected.opens == 1
    assert injected.active is True

    cfg.engine.mode = "none"  # what SettingsDialog._apply_settings would have set
    controller.reset_engine()

    assert injected.closes == 1
    assert controller.engine_status() == {"name": "none", "active": False, "note": None,
                                          "paused": False}

    controller.apply("working")
    # The old FakeEngine is never touched again — it was dropped, not reused. A real
    # NullEngine got built in its place via the factory (not just left at None).
    assert injected.opens == 1
    assert controller._engine is not None
    assert controller._engine is not injected
    assert controller.engine_status()["name"] == "none"


def test_reset_engine_cannot_run_during_a_blink_tick():
    """`reset_engine()` is called from the GUI thread while a confirm blink may be
    mid-tick, and the blink loop reaches the engine through `_get_engine()`. The two
    have to be mutually exclusive: a tick running alongside a reset can rebuild the
    engine the instant after it was dropped, leaving one that is never opened and never
    closed.

    Driven deterministically rather than by sleeps — the engine's `set_color` is gated
    on an event, so the blink thread is provably inside a tick when the reset starts.
    """
    cfg = make_cfg()
    cfg.engine.mode = "none"
    injected = FakeEngine()
    in_tick = threading.Event()
    release_tick = threading.Event()
    record_color = injected.set_color

    def gated_set_color(r: int, g: int, b: int) -> None:
        in_tick.set()
        release_tick.wait(5.0)
        record_color(r, g, b)

    injected.set_color = gated_set_color
    controller = LightController(cfg, engine=injected)

    controller.apply("confirm")
    assert in_tick.wait(5.0), "the blink thread never reached a tick"

    resetter = threading.Thread(target=controller.reset_engine, name="test-resetter")
    resetter.start()
    time.sleep(0.1)
    assert resetter.is_alive(), "reset_engine ran while a blink tick was in flight"

    release_tick.set()
    resetter.join(5.0)
    assert not resetter.is_alive()

    assert controller.blinking is False
    assert injected.closes == 1
    time.sleep(cfg.colors.blink_ms / 1000.0 * 3)  # a late tick would land in here
    assert controller._engine is None, "a late blink tick rebuilt the dropped engine"
    controller.shutdown()


def test_blink_picks_up_a_confirm_colour_changed_mid_blink():
    """A colour changed in Settings has to reach the hardware on the next tick:
    `apply("confirm")` won't restart an already-running blink, so the loop must re-read
    the colour rather than caching it before its first iteration.
    """
    cfg = make_cfg()
    cfg.colors.device.confirm = "#FF0000"
    injected = FakeEngine()
    controller = LightController(cfg, engine=injected)

    controller.apply("confirm")
    try:
        assert _wait_until(lambda: (255, 0, 0) in injected.colors, timeout=2.0)

        cfg.colors.device.confirm = "#00FF00"  # what _apply_settings would have written

        # Polled rather than slept for a fixed number of half-periods. The property under
        # test is that the loop *re-reads* the colour — not how many times a daemon
        # thread gets scheduled inside a wall-clock window. A macOS CI runner stretched
        # each 20 ms `Event.wait()` to roughly 50 ms, so the old fixed sleeps returned
        # after only four ticks, all of them from before the change.
        assert _wait_until(lambda: (0, 255, 0) in injected.colors, timeout=2.0), \
            "the blink kept sending the old colour"
    finally:
        controller.shutdown()  # in a finally: a failure must not leak the blink thread


def test_disabled_agent_events_are_ignored():
    """Unticking an agent must stop it driving the lighting, not just stop hook management.

    A hook entry left behind in that agent's config (installed by an earlier run, by hand,
    or in a project-scoped file) keeps firing regardless of what TintaView thinks, so the
    filter has to live at the ingress.
    """
    cfg = make_cfg()
    cfg.enabled_agents = ["claude"]
    server, engine = make_server(cfg)
    try:
        _event(server, "session-start", agent="cursor", sid="c1")
        _event(server, "working", agent="cursor", sid="c1")

        state = _get_state(server)
        assert state["agents"] == {}, "a disabled agent must not appear in /state"
        assert state["effective"] == "none"
        assert engine.colors == [], "a disabled agent must not touch the lighting"

        # ...while an enabled one on the same server is unaffected.
        _event(server, "session-start", agent="claude", sid="a1")
        _event(server, "working", agent="claude", sid="a1")
        assert _get_state(server)["agents"]["claude"]["effective"] == "working"
    finally:
        server.stop()


def test_hardware_gets_saturated_colours_not_the_brand_palette():
    """The tray icon and the LEDs are different media and need different colours.

    The brand red is RGB(244, 45, 60) — more blue than green — which an RGB LED behind a
    diffuser renders visibly pink/purple instead of "stop and look" red.
    """
    cfg = make_cfg()
    server, engine = make_server(cfg)
    try:
        _event(server, "session-start", agent="claude", sid="s1")
        _event(server, "confirm", agent="claude", sid="s1")
        assert _wait_until(lambda: (255, 0, 0) in engine.colors[-5:]), "hardware must get pure red"
        # The two palettes are resolved independently: changing the icon's colour must
        # not follow through to the LEDs.
        cfg.colors.confirm = "#123456"
        assert cfg.colors.device_rgb("confirm") == (255, 0, 0)
    finally:
        server.stop()


def test_blank_device_colour_falls_back_to_the_brand_colour():
    cfg = make_cfg()
    cfg.colors.device.confirm = ""
    assert cfg.colors.device_rgb("confirm") == cfg.colors.rgb("confirm")


def _make_watchdog_server(timeout: float):
    cfg = make_cfg()
    cfg.server.watchdog_timeout = timeout
    return make_server(cfg)


def test_watchdog_retires_only_the_silent_session():
    """The multi-agent bug this exists for, from both directions at once.

    A single store-wide "last event" clock got this wrong twice over: a chatty session
    vouched for every other session (so a crashed agent's colour stayed on the lights
    forever), and when the timeout finally did fire it cleared *everything* (so one
    quiet session took every live one down with it). Here `claude/alive` keeps talking
    while `cursor/dead` goes silent — exactly one of them may be retired.
    """
    # A 1s timeout pinged every 0.1s is a 10x margin. A tighter one races the CI
    # scheduler: a single stalled HTTP round trip would silently retire the session
    # this test needs kept alive, and the failure would read as a real regression.
    server, _engine = _make_watchdog_server(1.0)
    stop_pinging = threading.Event()

    def keep_alive() -> None:
        while not stop_pinging.wait(0.1):
            try:
                _event(server, "working", agent="claude", sid="alive")
            except Exception:
                return  # the server is shutting down; nothing left to keep alive

    try:
        _event(server, "working", agent="claude", sid="alive")
        _event(server, "working", agent="cursor", sid="dead")
        assert _wait_until(lambda: _get_state(server)["count"] == 2)

        pinger = threading.Thread(target=keep_alive, daemon=True, name="test-pinger")
        pinger.start()

        # The silent session must be retired...
        assert _wait_until(
            lambda: "cursor" not in _get_state(server)["agents"], timeout=10.0
        ), "the silent session should have been retired"
        # ...and the chatty one must not have gone with it.
        state = _get_state(server)
        assert state["agents"]["claude"]["sessions"] == {"alive": "working"}
        assert state["effective"] == "working"  # the survivor still drives the lights
    finally:
        stop_pinging.set()
        server.stop()


def test_watchdog_releases_the_lights_once_every_session_expires():
    server, engine = _make_watchdog_server(0.2)
    try:
        _event(server, "working", agent="claude", sid="s1")
        assert _wait_until(lambda: engine.opens == 1)

        assert _wait_until(lambda: _get_state(server)["effective"] == "none", timeout=2.0)
        assert _wait_until(lambda: engine.closes == 1)
    finally:
        server.stop()


def test_end_many_reports_one_effective_change_for_the_whole_batch():
    """Retiring several dead sessions must produce a single lighting update, and none
    at all when the survivors still fold to the same colour."""
    from tintaview.core.state import StateStore

    state = StateStore()
    state.set("claude", "a", "working")
    state.set("cursor", "b", "working")
    state.set("cursor", "c", "confirm")

    # Dropping the only `confirm` demotes the fold from confirm to working: one change.
    assert state.end_many([("cursor", "c")]) is True
    assert state.effective() == "working"
    # Dropping one of two equally-working sessions changes nothing anyone can see.
    assert state.end_many([("cursor", "b")]) is False
    assert state.effective() == "working"
    # Dropping the last one releases the lights.
    assert state.end_many([("claude", "a")]) is True
    assert state.effective() == "none"


def test_expired_is_per_session_not_per_store(monkeypatch):
    """Driven by a fake clock, not sleeps: the semantics under test are exact, and a
    loaded runner stalling between two real `sleep`s would make this lie either way."""
    from tintaview.core import state as state_mod

    now = [1_000.0]
    monkeypatch.setattr(state_mod.time, "monotonic", lambda: now[0])

    state = state_mod.StateStore()
    state.set("cursor", "old", "working")
    now[0] += 100.0
    state.set("claude", "new", "working")  # a later event on a *different* session

    # The fresh session must not vouch for the stale one, which is exactly what a
    # single store-wide timestamp did.
    assert state.expired(10.0) == [("cursor", "old")]
    assert state.expired(1_000.0) == []


def test_watchdog_leaves_a_recently_seen_session_alone():
    server, _engine = _make_watchdog_server(5.0)
    try:
        _event(server, "idle", agent="claude", sid="s1")
        assert _wait_until(lambda: _get_state(server)["count"] == 1)
        time.sleep(0.3)  # several watchdog polls, none of them past the timeout
        assert _get_state(server)["count"] == 1
    finally:
        server.stop()


# --------------------------------------------------------------------------- running tool


def test_tool_start_records_the_tool_and_tool_end_clears_it(server_engine):
    server, _engine = server_engine

    _event(server, "tool-start", agent="claude", sid="s1", tool="Bash")
    assert _wait_until(lambda: _get_state(server)["agents"]["claude"]["tool"] == "Bash")

    _event(server, "tool-end", agent="claude", sid="s1", tool="Bash")
    assert _wait_until(lambda: _get_state(server)["agents"]["claude"]["tool"] == "")


def test_a_plain_working_ping_does_not_erase_the_running_tool(server_engine):
    """`working` carries no tool name, so it must leave the last one alone — otherwise
    a chatty PostToolUse stream would blank the label the flyout just showed."""
    server, _engine = server_engine

    _event(server, "tool-start", agent="claude", sid="s1", tool="Edit")
    assert _wait_until(lambda: _get_state(server)["agents"]["claude"]["tool"] == "Edit")

    _event(server, "working", agent="claude", sid="s1")
    assert _get_state(server)["agents"]["claude"]["tool"] == "Edit"


def test_a_finished_session_advertises_no_tool(server_engine):
    """An idle session's leftover tool name is stale trivia, not status."""
    server, _engine = server_engine

    _event(server, "tool-start", agent="claude", sid="s1", tool="Bash")
    assert _wait_until(lambda: _get_state(server)["agents"]["claude"]["tool"] == "Bash")

    _event(server, "idle", agent="claude", sid="s1")
    assert _wait_until(lambda: _get_state(server)["agents"]["claude"]["effective"] == "idle")
    assert _get_state(server)["agents"]["claude"]["tool"] == ""


def test_the_headline_tool_comes_from_a_working_session(server_engine):
    """With two sessions open under one agent, the section shows the busy one's tool."""
    server, _engine = server_engine

    _event(server, "tool-start", agent="claude", sid="a", tool="Read")
    _event(server, "idle", agent="claude", sid="a")  # 'a' finished, keeps a stale name
    _event(server, "tool-start", agent="claude", sid="b", tool="Bash")

    assert _wait_until(lambda: _get_state(server)["agents"]["claude"]["tool"] == "Bash")


# --------------------------------------------------------------------------- /show


def _get_show(server: StatusServer) -> dict:
    with urllib.request.urlopen(f"{server.url}/show", timeout=2) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_show_reports_false_when_nothing_is_registered(server_engine):
    """A headless daemon owning the port has no panel to show, and must say so rather
    than let a second launch report a window that never appeared."""
    server, _engine = server_engine
    assert _get_show(server) == {"ok": True, "shown": False}


def test_show_invokes_the_registered_callback(server_engine):
    server, _engine = server_engine
    calls: list[int] = []
    server.on_show = lambda: calls.append(1)

    assert _get_show(server)["shown"] is True
    assert calls == [1]


def test_show_survives_a_raising_callback(server_engine):
    """It runs on an HTTP worker thread, where an exception would be logged and lost."""
    server, _engine = server_engine

    def boom() -> None:
        raise RuntimeError("no window today")

    server.on_show = boom
    assert _get_show(server)["shown"] is False


def test_request_show_helper_round_trips(server_engine):
    from tintaview.core.server import request_show

    server, _engine = server_engine
    calls: list[int] = []
    server.on_show = lambda: calls.append(1)

    host, port = server._httpd.server_address[0], server._httpd.server_address[1]
    assert request_show(host, port) is True
    assert calls == [1]


# --------------------------------------------------------------------------- pause


def test_pausing_releases_the_device_and_ignores_later_events():
    server, engine = make_server()
    try:
        _event(server, "working", agent="claude", sid="s1")
        assert _wait_until(lambda: engine.opens == 1)

        server.controller.set_paused(True)
        assert engine.closes == 1
        painted = len(engine.colors)

        # Events keep flowing — the state model still tracks them, the device does not.
        _event(server, "confirm", agent="claude", sid="s1")
        assert _wait_until(lambda: _get_state(server)["effective"] == "confirm")
        assert len(engine.colors) == painted
        assert _get_state(server)["engine"]["paused"] is True
    finally:
        server.stop()


def test_resuming_restores_what_is_actually_happening_now():
    """Not the colour the device last showed: the status may have moved on while paused."""
    cfg = make_cfg()
    server, engine = make_server(cfg)
    try:
        _event(server, "working", agent="claude", sid="s1")
        assert _wait_until(lambda: engine.opens == 1)
        server.controller.set_paused(True)

        _event(server, "idle", agent="claude", sid="s1")
        assert _wait_until(lambda: _get_state(server)["effective"] == "idle")

        server.controller.set_paused(False)
        assert engine.colors[-1] == cfg.colors.device_rgb("idle")
        assert _get_state(server)["engine"]["paused"] is False
    finally:
        server.stop()


def test_pause_is_idempotent_and_never_raises():
    server, engine = make_server()
    try:
        _event(server, "working", agent="claude", sid="s1")
        assert _wait_until(lambda: engine.opens == 1)

        server.controller.set_paused(True)
        server.controller.set_paused(True)
        assert engine.closes == 1  # the second call is a no-op, not a second close
        server.controller.set_paused(False)
        server.controller.set_paused(False)
        assert server.controller.paused is False
    finally:
        server.stop()
