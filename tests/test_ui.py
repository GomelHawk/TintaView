"""Headless Qt tests for the tray UI: icon tinting/caching/fallback, the flyout's
multi-agent painting, and TrayApp's state -> icon/tooltip mapping.

`QT_QPA_PLATFORM=offscreen` must be set before PySide6 is ever imported (by this
module or anything it imports), so it happens as the very first thing here, ahead
of even `pytest.importorskip`.
"""

from __future__ import annotations

import os
import sys
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

import tintaview.ui.tray as tray_mod  # noqa: E402
from tintaview.core.config import Config  # noqa: E402
from tintaview.stats.model import UsageResult, UsageRow  # noqa: E402
from tintaview.ui import icons  # noqa: E402
from tintaview.ui.flyout import Flyout  # noqa: E402
from tintaview.ui.tray import TrayApp  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])
    return app


# --------------------------------------------------------------------------- icons


def test_state_icon_tints_the_mark(qapp):
    icon = icons.state_icon((10, 20, 200), size=64)
    assert not icon.isNull()

    image = icon.pixmap(64, 64).toImage()
    found = False
    for y in range(image.height()):
        for x in range(image.width()):
            color = image.pixelColor(x, y)
            if color.alpha() == 255:
                assert (color.red(), color.green(), color.blue()) == (10, 20, 200)
                found = True
                break
        if found:
            break
    assert found, "expected at least one fully-opaque tinted pixel"


def test_state_icon_is_cached(qapp):
    a = icons.state_icon((1, 2, 3), size=48)
    b = icons.state_icon((1, 2, 3), size=48)
    assert a is b


def test_state_icon_cache_ignores_the_size_argument(qapp):
    """`size` used to be part of the cache key while contributing nothing to the drawing:
    every icon carries all of TRAY_ICON_SIZES, so two calls that differed only in `size`
    rendered the same nine pixmaps twice and stored them twice."""
    assert icons.state_icon((4, 5, 6), size=16) is icons.state_icon((4, 5, 6), size=256)
    assert icons.brand_icon(16) is icons.brand_icon(256)


def test_state_icon_needs_no_asset_on_disk(qapp, monkeypatch, tmp_path):
    """The mark is drawn, not loaded — there is no PNG behind the status icons at all.

    (This replaced a test that monkeypatched `asset_path` to "force the procedural
    fallback": both the silhouette PNGs and the fallback burst it selected are gone, so
    the test was asserting the drawn path either way while claiming otherwise.)
    """
    monkeypatch.setattr(icons, "asset_path", lambda name: tmp_path / "missing" / name)

    icon = icons.state_icon((250, 60, 60), size=77)
    assert not icon.isNull()

    image = icon.pixmap(64, 64).toImage()
    # Spoke 0 points straight up from the centre — sample a point along it, safely
    # between MARK_INNER and MARK_OUTER, and expect it filled with the tint.
    x = 32
    y = int(32 - 64 * (icons.MARK_INNER + icons.MARK_OUTER) / 2)
    color = image.pixelColor(x, y)
    assert color.alpha() > 0
    assert (color.red(), color.green(), color.blue()) == (250, 60, 60)


# --------------------------------------------------------------------------- flyout


def _sample_results() -> dict[str, UsageResult]:
    ok = UsageResult(
        agent="claude",
        header="Your usage limits · Max",
        rows=[
            UsageRow(label="5-hour limit", pct=42.0, right="Resets in 3h",
                     severity="normal", kind="limit"),
            UsageRow(label="Weekly limit", pct=91.0, right="Resets in 2d",
                     severity="critical", kind="limit"),
        ],
    )
    broken = UsageResult(agent="cursor", error="Cursor not signed in.")
    return {"claude": ok, "cursor": broken}


def test_flyout_set_results_resizes_and_paints(qapp):
    flyout = Flyout()
    empty_height = flyout.height()

    flyout.set_results(_sample_results())
    assert flyout.height() > empty_height  # two sections' worth of content added

    pixmap = QtGui.QPixmap(flyout.size())
    flyout.render(pixmap)  # must not raise
    assert not pixmap.isNull()


def test_flyout_row_label_yields_to_the_right_hand_text(qapp):
    """A long label must be elided into the space the right-hand text leaves, not drawn
    over the top of it.

    Both used to be drawn into the same full-width rect, left- and right-aligned, which
    only works while the two are short enough not to meet — translated labels and reset
    times overprinted each other mid-row (Russian's "Сброс через 3 ч 11 мин" is a third
    wider than the English wording).
    """
    flyout = Flyout()
    flyout.set_results({
        "claude": UsageResult(
            agent="claude",
            rows=[UsageRow(label="A label far too long to fit beside its own reset time",
                            pct=50.0, right="Resets in 3 hr 11 min", severity="normal",
                            kind="limit")],
        )
    })
    pixmap = QtGui.QPixmap(flyout.size())
    flyout.render(pixmap)  # must not raise
    assert not pixmap.isNull()


def test_flyout_paints_in_every_language(qapp):
    """Smoke test: no language may break the paint path (a missing catalogue key renders
    as the key, which is ugly but must never be an exception mid-`paintEvent`)."""
    from tintaview import i18n

    flyout = Flyout()
    try:
        for code in i18n.LANGUAGE_CODES:
            i18n.set_language(code)
            flyout.set_results(_sample_results())
            pixmap = QtGui.QPixmap(flyout.size())
            flyout.render(pixmap)
            assert not pixmap.isNull()
            flyout.set_results({})  # the "No agents enabled." path, also translated
            flyout.render(QtGui.QPixmap(flyout.size()))
    finally:
        i18n.set_language("en")


def test_flyout_handles_empty_results(qapp):
    flyout = Flyout()
    flyout.set_results({})
    pixmap = QtGui.QPixmap(flyout.size())
    flyout.render(pixmap)
    assert not pixmap.isNull()


def test_flyout_show_near_positions_above_the_anchor(qapp):
    flyout = Flyout()
    flyout.set_results(_sample_results())
    anchor = QtCore.QPoint(500, 500)

    flyout.show_near(anchor)

    assert flyout.pos().y() == anchor.y() - flyout.height() - 12
    flyout.hide()


def test_flyout_repositions_immediately_after_collapsing_a_section(qapp):
    """Collapsing/expanding a section changes the card's height; the card must
    re-anchor to the same point immediately rather than only being correctly placed
    the next time it happens to be reopened (see Flyout.show_near)."""
    flyout = Flyout()
    flyout.set_results(_sample_results())
    anchor = QtCore.QPoint(500, 500)
    flyout.show_near(anchor)
    height_before = flyout.height()

    flyout._toggle("claude")  # claude has rows and is collapsible; cursor errored, is not

    assert flyout.height() < height_before
    assert flyout.pos().y() == anchor.y() - flyout.height() - 12
    flyout.hide()


# --------------------------------------------------------------------------- tray


class _FakeServer:
    """Stands in for a StatusServer: exposes only what TrayApp actually reads."""

    def __init__(self) -> None:
        self.url = "http://127.0.0.1:0"
        self._payload: dict = {"effective": "none", "agents": {}, "count": 0}

    def set(self, payload: dict) -> None:
        self._payload = payload

    def state_payload(self) -> dict:
        return self._payload


@pytest.fixture
def tray(qapp, monkeypatch, tmp_path):
    # Isolate config.save()'s target file from the developer's real ~/.tintaview.
    monkeypatch.setenv("TINTAVIEW_HOME", str(tmp_path))
    # No real stats fetching here — TrayApp's state handling is what's under test,
    # and a real StatsService would hit disk/network from a background thread with
    # no way to await or clean it up in a synchronous test.
    monkeypatch.setattr(tray_mod.StatsWorker, "fetch", lambda self: None)
    # Same reasoning: the startup update check is real network I/O on a background
    # thread, which every other tray test would otherwise trigger unattended.
    monkeypatch.setattr(tray_mod.UpdateCheckWorker, "fetch", lambda self: None)
    # And the hook-drift check, which reads the developer's own ~/.claude/settings.json
    # (and, on a WSL-split box, a UNC path into a distro). `HookDriftWorker._run` is
    # exercised directly, against a fake registry, further down.
    monkeypatch.setattr(tray_mod.HookDriftWorker, "fetch", lambda self: None)

    cfg = Config()
    cfg.enabled_agents = ["claude", "codex"]
    server = _FakeServer()
    app_instance = TrayApp(cfg, server, qapp)
    yield app_instance, server
    app_instance.state_timer.stop()
    app_instance.usage_timer.stop()
    app_instance.blink_timer.stop()
    app_instance.anim_timer.stop()
    app_instance.tray.hide()


def test_tray_reflects_idle_working_confirm_none(tray):
    app_instance, server = tray
    confirm_rgb = Config().colors.rgb("confirm")

    server.set({
        "effective": "idle",
        "agents": {"claude": {"effective": "idle", "count": 1},
                   "codex": {"effective": "none", "count": 0}},
        "count": 1,
    })
    app_instance._poll_state()
    idle_icon = app_instance.tray.icon().cacheKey()
    # The tooltip is a single aggregate count, not one line per agent: a per-agent
    # breakdown is unbounded as more agents are enabled (that used to overflow
    # Windows' tray tooltip buffer outright) and duplicates the flyout's per-agent
    # status dots, so it isn't repeated here.
    assert app_instance.tray.toolTip() == "1 active session"
    assert not app_instance.blink_timer.isActive()

    server.set({
        "effective": "working",
        "agents": {"claude": {"effective": "working", "count": 2},
                   "codex": {"effective": "none", "count": 0}},
        "count": 2,
    })
    app_instance._poll_state()
    working_icon = app_instance.tray.icon().cacheKey()
    assert app_instance.tray.toolTip() == "2 active sessions"
    assert working_icon != idle_icon

    server.set({
        "effective": "confirm",
        "agents": {"claude": {"effective": "confirm", "count": 1},
                   "codex": {"effective": "none", "count": 0}},
        "count": 1,
    })
    app_instance._poll_state()
    assert app_instance.tray.toolTip() == "1 active session"
    assert app_instance.blink_timer.isActive()
    assert app_instance.tray.icon().cacheKey() == icons.state_icon(confirm_rgb, 128).cacheKey()
    assert app_instance.tray.icon().cacheKey() != working_icon

    server.set({"effective": "none", "agents": {}, "count": 0})
    app_instance._poll_state()
    assert not app_instance.blink_timer.isActive()
    assert app_instance.tray.toolTip() == "No active sessions"
    # "no session" is the mark in the LOGO's colours, not a fourth status tint: at rest
    # the tray is just the TintaView logo, and any single-colour icon means something is
    # happening. It shares the status icons' geometry exactly, so it still reads as the
    # same mark rather than a different icon.
    assert app_instance.tray.icon().cacheKey() == icons.brand_icon(128).cacheKey()
    none_rgb = app_instance._cfg.colors.rgb("none")
    assert app_instance.tray.icon().cacheKey() != icons.state_icon(none_rgb, 128).cacheKey()


def test_tray_tooltip_stays_a_short_aggregate_regardless_of_enabled_agent_count(tray):
    # A one-line-per-agent tooltip grows with every agent TintaView adds support for
    # (JetBrains and Copilot already joined the original three) and eventually overflows
    # Windows' tray tooltip buffer outright. The aggregate count has no such ceiling:
    # it's the same short string whether one agent is enabled or five.
    app_instance, server = tray
    app_instance._cfg.enabled_agents = ["claude", "codex", "cursor", "jetbrains", "copilot"]
    server.set({
        "effective": "idle",
        "agents": {"claude": {"effective": "idle", "count": 1}},
        "count": 1,
    })
    app_instance._poll_state()
    assert app_instance.tray.toolTip() == "1 active session"


def test_tray_sound_toggle_persists(tray, tmp_path):
    app_instance, _ = tray
    actions = app_instance.tray.contextMenu().actions()
    sound_action = next(a for a in actions if a.text() == "Sound on confirm")
    assert sound_action.isChecked() is False

    sound_action.trigger()  # toggles the checkbox and fires `toggled`

    assert app_instance._cfg.ui.chime_on_confirm is True
    assert (tmp_path / "config.toml").exists()


# --------------------------------------------------------------------------- update check


def test_update_check_worker_emits_only_when_a_newer_release_exists(qapp, monkeypatch):
    from tintaview.install import update as update_mod

    monkeypatch.setattr(update_mod, "latest_release", lambda **kw: {"tag_name": "v9.9.9"})
    worker = tray_mod.UpdateCheckWorker()
    seen = []
    worker.update_available.connect(lambda tag, current: seen.append((tag, current)))

    worker._run()

    assert len(seen) == 1
    tag, current = seen[0]
    assert tag == "9.9.9"


def test_update_check_worker_silent_when_already_current_or_unreachable(qapp, monkeypatch):
    from tintaview import __version__
    from tintaview.install import update as update_mod

    worker = tray_mod.UpdateCheckWorker()
    seen = []
    worker.update_available.connect(lambda tag, current: seen.append((tag, current)))

    monkeypatch.setattr(update_mod, "latest_release", lambda **kw: {"tag_name": f"v{__version__}"})
    worker._run()
    assert seen == []

    monkeypatch.setattr(update_mod, "latest_release", lambda **kw: None)  # network/rate-limit/no-release
    worker._run()
    assert seen == []


def test_tray_runs_update_check_on_start_only_when_enabled(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("TINTAVIEW_HOME", str(tmp_path))
    monkeypatch.setattr(tray_mod.StatsWorker, "fetch", lambda self: None)
    monkeypatch.setattr(tray_mod.HookDriftWorker, "fetch", lambda self: None)
    calls = []
    monkeypatch.setattr(tray_mod.UpdateCheckWorker, "fetch", lambda self: calls.append(1))

    cfg = Config()
    cfg.update.check = False
    app_instance = TrayApp(cfg, _FakeServer(), qapp)
    try:
        assert calls == []
    finally:
        app_instance.tray.hide()

    cfg2 = Config()
    cfg2.update.check = True
    app_instance2 = TrayApp(cfg2, _FakeServer(), qapp)
    try:
        assert calls == [1]
    finally:
        app_instance2.tray.hide()


def test_tray_shows_a_balloon_when_an_update_is_found(tray, monkeypatch):
    app_instance, _ = tray
    calls = []
    monkeypatch.setattr(
        QtWidgets.QSystemTrayIcon, "showMessage",
        lambda self, title, message, *a, **k: calls.append((title, message)),
    )

    app_instance._on_update_available("9.9.9", "1.0.0")

    assert len(calls) == 1
    title, message = calls[0]
    assert "9.9.9" in message
    assert "1.0.0" in message


def test_tray_balloons_once_when_engine_note_appears(tray, monkeypatch):
    """Silent G HUB refusals surface via /state's engine.note — balloon once per note.

    The balloon is the *only* place the note is shown: the tray tooltip stays the plain
    aggregate session count, with nothing engine- or agent-specific appended to it.
    """
    app_instance, server = tray
    balloons = []
    monkeypatch.setattr(
        QtWidgets.QSystemTrayIcon, "showMessage",
        lambda self, title, message, *a, **k: balloons.append((title, message)),
    )

    note = "G HUB is ignoring lighting commands — check Integrations"
    payload = {
        "effective": "idle",
        "agents": {"claude": {"effective": "idle", "count": 1},
                   "codex": {"effective": "none", "count": 0}},
        "count": 1,
        "engine": {"name": "ghub", "active": True, "note": note},
    }
    server.set(payload)
    app_instance._poll_state()
    assert len(balloons) == 1
    assert note in balloons[0][1]
    # A pending note must not leak into the tooltip — it is the aggregate count, only.
    assert app_instance.tray.toolTip() == "1 active session"

    # Same note again must not re-balloon; clearing it resets the latch.
    app_instance._poll_state()
    assert len(balloons) == 1

    cleared = dict(payload)
    cleared["engine"] = {"name": "ghub", "active": True, "note": None}
    server.set(cleared)
    app_instance._poll_state()
    assert app_instance.tray.toolTip() == "1 active session"

    server.set(payload)
    app_instance._poll_state()
    assert len(balloons) == 2


def test_mark_is_bold_but_keeps_its_shape(qapp):
    """The icon must read at tray sizes without losing the logo's proportions.

    Both failure modes have happened. The artwork's own capsules are thin enough that at
    16px the status colour reads as a smudge; and emboldening it by fattening the
    capsules turned the burst into a flower — the mark's character is eight *long, slim*
    capsules (4.4x longer than wide in the artwork) around an open centre.
    """
    import math

    from tintaview.ui import icons

    for size in (16, 20, 24, 32):
        img = icons._draw_mark((255, 0, 19), size).toImage()
        painted = sum(
            1 for y in range(img.height()) for x in range(img.width())
            if img.pixelColor(x, y).alpha() > 32
        )
        coverage = painted / (size * size)
        assert 0.20 < coverage < 0.70, f"{size}px coverage {coverage:.0%} — too faint or a blob"

        # The open centre is the mark, not incidental whitespace.
        assert img.pixelColor(size // 2, size // 2).alpha() < 128, f"{size}px: centre closed up"

    # Long and slim, never stubby: this ratio is what separates the burst from a flower.
    length = icons.MARK_OUTER - icons.MARK_INNER
    assert length / icons.MARK_CAPSULE_WIDTH >= 3.0, "capsules got too fat"

    # All eight spokes are capsules; the dot is a NINTH element beyond the ring, not a
    # spoke replaced by a dot.
    assert icons.MARK_SPOKES == 8
    assert icons.MARK_DOT_DISTANCE > icons.MARK_OUTER

    big = icons._draw_mark((255, 255, 255), 256).toImage()
    angle = math.radians(icons.MARK_DOT_ANGLE)

    def alpha_at(fraction: float) -> int:
        x = 128 + math.sin(angle) * 256 * fraction
        y = 128 - math.cos(angle) * 256 * fraction
        return big.pixelColor(int(x), int(y)).alpha()

    assert alpha_at((icons.MARK_INNER + icons.MARK_OUTER) / 2) > 128, "no capsule on the dot's spoke"
    assert alpha_at(icons.MARK_DOT_DISTANCE) > 128, "the accent dot is missing"
    # ...and a clear gap between the capsule's tip and the dot.
    gap = (icons.MARK_OUTER + icons.MARK_DOT_DISTANCE - icons.MARK_DOT_RADIUS) / 2
    assert alpha_at(gap) < 128, "the dot merged into the ring"


def test_brand_mark_shares_the_status_geometry_and_is_multicolour(qapp):
    """The logo mark and the status marks must be the same shape, differing only in colour.

    Loading the gradient PNG for this instead of drawing it is what made the earlier
    attempt look like "a different icon": the artwork's capsules are narrower than
    MARK_CAPSULE_WIDTH, so the idle icon came out visibly thinner than the others.
    """
    from tintaview.ui import icons

    for size in (16, 24, 32):
        brand = icons._draw_mark(None, size, colors=icons.MARK_BRAND_COLORS,
                                 dot_color=icons.MARK_BRAND_DOT).toImage()
        flat = icons._draw_mark((255, 0, 19), size).toImage()

        def opaque(img):
            return {(x, y) for y in range(img.height()) for x in range(img.width())
                    if img.pixelColor(x, y).alpha() > 128}

        # Identical footprint: same capsules, same open centre, same dot.
        assert opaque(brand) == opaque(flat), f"{size}px: brand mark isn't the same shape"

    big = icons._draw_mark(None, 128, colors=icons.MARK_BRAND_COLORS,
                           dot_color=icons.MARK_BRAND_DOT).toImage()
    hues = {
        (c.red(), c.green(), c.blue())
        for y in range(big.height()) for x in range(big.width())
        if (c := big.pixelColor(x, y)).alpha() == 255
    }
    # Three flat zones plus the dot — and each zone must own enough spokes to still be
    # visible at 16px, which is what per-capsule sampled hues failed to do.
    assert icons._BRAND_BLUE in hues
    assert icons._BRAND_GREEN in hues
    assert icons._BRAND_YELLOW in hues
    assert icons.MARK_BRAND_DOT in hues, "the orange accent dot is missing"
    counts = {c: icons.MARK_BRAND_COLORS.count(c) for c in set(icons.MARK_BRAND_COLORS)}
    assert min(counts.values()) >= 2, f"a zone thinner than 2 spokes vanishes at 16px: {counts}"


# --------------------------------------------------------------------------- settings apply


class _FakeController:
    """Only what `TrayApp._apply_settings` and the pause action call on a
    `LightController`."""

    def __init__(self) -> None:
        self.resets = 0
        self.applied: list[str] = []
        self.blinking = False
        self.paused = False

    def reset_engine(self) -> None:
        self.resets += 1

    def apply(self, status: str) -> None:
        self.applied.append(status)

    def set_paused(self, paused: bool) -> None:
        self.paused = paused

    def engine_status(self) -> dict:
        return {"name": "none", "active": False, "note": None, "paused": self.paused}


class _FakeState:
    def __init__(self, effective: str = "idle") -> None:
        self._effective = effective

    def effective(self) -> str:
        return self._effective


@pytest.fixture
def tray_with_controller(tray):
    """The tray fixture's server, plus the controller/state `_apply_settings` refreshes."""
    app_instance, server = tray
    server.controller = _FakeController()
    server.state = _FakeState("working")
    return app_instance, server


def _join_lighting(app_instance, timeout: float = 5.0) -> None:
    """Wait for the engine reset/re-apply `_apply_settings` kicked off.

    That pair is a full engine close + open + paint (a Chroma REST round-trip, an OpenRGB
    snapshot, a G HUB sidecar restart) and no longer runs on the GUI thread, so a test
    asserting on the controller has to wait for it rather than assume it already happened.
    """
    thread = app_instance._lighting_thread
    if thread is not None:
        thread.join(timeout)
        assert not thread.is_alive(), "the lighting refresh thread never finished"


def _accepted_copy(app_instance, **changes) -> Config:
    """A stand-in for what `SettingsDialog.result_cfg` hands back: a deep copy of the
    live config with some fields changed."""
    import copy

    new_cfg = copy.deepcopy(app_instance._cfg)
    for dotted, value in changes.items():
        target = new_cfg
        *path, leaf = dotted.split(".")
        for part in path:
            target = getattr(target, part)
        setattr(target, leaf, value)
    return new_cfg


def test_apply_settings_mirrors_every_field_it_can_write(tray_with_controller):
    """A field that only lands in the dialog's copy is a setting that appears to do
    nothing until the next restart — the exact failure this dialog exists to avoid."""
    app_instance, _server = tray_with_controller
    new_cfg = _accepted_copy(
        app_instance,
        **{
            "enabled_agents": ["codex"],
            "ui.chime_on_confirm": True,
            "stats.poll_seconds": 90,
            "update.check": False,
            "engine.mode": "openrgb",
            "colors.idle": "#010203",
            "colors.device.idle": "#040506",
        },
    )

    app_instance._apply_settings(new_cfg)

    cfg = app_instance._cfg
    assert cfg.enabled_agents == ["codex"]
    assert cfg.ui.chime_on_confirm is True
    assert cfg.stats.poll_seconds == 90
    assert cfg.update.check is False
    assert cfg.engine.mode == "openrgb"
    assert cfg.colors.idle == "#010203"
    # The device palette is what the controller actually sends to the hardware.
    assert cfg.colors.device.idle == "#040506"
    assert app_instance.usage_timer.interval() == 90_000


def test_apply_settings_switches_the_interface_language(tray_with_controller):
    """A language change has to reach the menu and the tooltip.

    Neither retranslates itself: the menu's action texts were baked in when it was
    built, and the tooltip is only rebuilt on the next state poll — so `_apply_settings`
    rebuilds the menu and re-polls. Without that, picking a language did nothing visible
    until the tray was restarted.
    """
    from tintaview import i18n

    app_instance, _server = tray_with_controller
    try:
        app_instance._apply_settings(_accepted_copy(app_instance, **{"ui.language": "ru"}))

        assert app_instance._cfg.ui.language == "ru"
        assert i18n.current_language() == "ru"
        texts = [a.text() for a in app_instance.tray.contextMenu().actions()]
        assert "Выход" in texts
        assert "Quit" not in texts
        assert app_instance.tray.toolTip() == "Нет активных сессий"
    finally:
        # Global state: the rest of this module asserts English text.
        i18n.set_language("en")


def test_apply_settings_resyncs_the_sound_menu_item(tray_with_controller):
    """The context menu keeps its own check mark for `chime_on_confirm`; left alone it
    starts contradicting the dialog that just changed it."""
    app_instance, _server = tray_with_controller
    assert app_instance._sound_action.isChecked() is False

    app_instance._apply_settings(_accepted_copy(app_instance, **{"ui.chime_on_confirm": True}))

    assert app_instance._sound_action.isChecked() is True


def test_apply_settings_resync_does_not_resave_config(tray_with_controller, monkeypatch):
    """Setting the check mark must not re-enter `_set_sound` and write the file again."""
    app_instance, _server = tray_with_controller
    saves = []
    monkeypatch.setattr("tintaview.core.config.save", lambda cfg, path=None: saves.append(cfg))

    app_instance._apply_settings(_accepted_copy(app_instance, **{"ui.chime_on_confirm": True}))

    assert saves == []  # the dialog already saved; the tray must not save again


def test_apply_settings_drops_a_disabled_agents_usage_section(tray_with_controller):
    """`_apply_results` merges rather than replaces (a partial fetch must not blank a
    section), so an unticked agent's flyout card has to be dropped here or it lingers
    until the next restart."""
    app_instance, _server = tray_with_controller
    app_instance._apply_results({
        "claude": UsageResult(agent="claude", rows=[UsageRow(label="5-hour", pct=10.0)]),
        "codex": UsageResult(agent="codex", rows=[UsageRow(label="weekly", pct=20.0)]),
    })
    assert set(app_instance._usage_results) == {"claude", "codex"}

    app_instance._apply_settings(_accepted_copy(app_instance, enabled_agents=["claude"]))

    assert set(app_instance._usage_results) == {"claude"}


def test_apply_settings_reorders_the_flyout_sections(tray_with_controller):
    """Dragging an agent up the settings list has to move its flyout card too.

    The flyout renders dict order as section order, and `dict.update` leaves an
    existing key where it already was — so a merge alone kept the *old* order until
    the next restart, which is exactly what the settings list stopped agreeing with.
    """
    app_instance, _server = tray_with_controller
    app_instance._cfg.enabled_agents = ["claude", "codex", "cursor"]
    app_instance._apply_results({
        "claude": UsageResult(agent="claude", rows=[UsageRow(label="5-hour", pct=10.0)]),
        "codex": UsageResult(agent="codex", rows=[UsageRow(label="weekly", pct=20.0)]),
        "cursor": UsageResult(agent="cursor", rows=[UsageRow(label="monthly", pct=30.0)]),
    })
    assert list(app_instance._usage_results) == ["claude", "codex", "cursor"]

    app_instance._apply_settings(
        _accepted_copy(app_instance, enabled_agents=["claude", "cursor", "codex"])
    )

    assert list(app_instance._usage_results) == ["claude", "cursor", "codex"]


def test_apply_results_keeps_the_configured_agent_order(tray_with_controller):
    """A later partial fetch must not append a re-fetched agent to the end."""
    app_instance, _server = tray_with_controller
    app_instance._cfg.enabled_agents = ["claude", "codex"]
    app_instance._apply_results({
        "codex": UsageResult(agent="codex", rows=[UsageRow(label="weekly", pct=20.0)]),
    })
    app_instance._apply_results({
        "claude": UsageResult(agent="claude", rows=[UsageRow(label="5-hour", pct=10.0)]),
    })

    assert list(app_instance._usage_results) == ["claude", "codex"]


def test_apply_settings_reapplies_lighting_after_an_engine_change(tray_with_controller):
    """`reset_engine()` only drops the old engine. Without a re-apply, nothing calls
    `apply()` until the *next* status transition, so a mid-session engine switch leaves
    the lights dark."""
    app_instance, server = tray_with_controller

    app_instance._apply_settings(_accepted_copy(app_instance, **{"engine.mode": "openrgb"}))
    _join_lighting(app_instance)

    assert server.controller.resets == 1
    assert server.controller.applied == ["working"]  # the live effective status


def test_apply_settings_reapplies_lighting_after_a_colour_change(tray_with_controller):
    """Same reason, without an engine change: a new colour has to reach the hardware
    now, not at the next status transition."""
    app_instance, server = tray_with_controller

    app_instance._apply_settings(_accepted_copy(app_instance, **{"colors.device.working": "#ABCDEF"}))
    _join_lighting(app_instance)

    assert server.controller.resets == 0  # engine unchanged — nothing to rebuild
    assert server.controller.applied == ["working"]


def test_apply_settings_survives_a_server_without_a_controller(tray):
    """`TrayApp` is documented as working against any object standing in for a real
    StatusServer, so neither refresh may assume one is there."""
    app_instance, _server = tray

    app_instance._apply_settings(_accepted_copy(app_instance, **{"engine.mode": "none"}))

    assert app_instance._cfg.engine.mode == "none"


# --------------------------------------------------------------------------- menu actions


def _action(app_instance, key: str):
    """The menu action whose text is the English translation of `key`."""
    from tintaview.i18n import t

    label = t(key)
    return next(a for a in app_instance._menu.actions() if a.text() == label)


def test_the_menu_offers_pause_logs_and_diagnostics(tray):
    """The two things a stuck user needs (where the logs are, what `doctor` says) plus
    the one thing a user recording their screen needs."""
    app_instance, _server = tray
    for key in ("tray.menu.pause_lighting", "tray.menu.open_logs", "tray.menu.diagnostics"):
        assert _action(app_instance, key) is not None


def test_pause_action_drives_the_controller(tray_with_controller):
    app_instance, server = tray_with_controller
    app_instance._menu = app_instance._build_menu()  # rebuilt now the controller exists
    action = _action(app_instance, "tray.menu.pause_lighting")
    assert action.isCheckable()
    assert action.isChecked() is False

    action.setChecked(True)
    assert server.controller.paused is True
    action.setChecked(False)
    assert server.controller.paused is False


def test_pause_state_survives_a_menu_rebuild(tray_with_controller):
    """The menu is rebuilt wholesale on a language change, so the check mark has to be
    re-read from the controller rather than remembered by the old menu."""
    from tintaview.i18n import t

    app_instance, server = tray_with_controller
    server.controller.paused = True

    app_instance._menu = app_instance._build_menu()

    assert _action(app_instance, "tray.menu.pause_lighting").isChecked() is True
    assert t("tray.menu.pause_lighting") == "Pause lighting"  # the fixture runs in English


def test_pause_is_a_no_op_without_a_controller(tray):
    """`TrayApp` is documented as working against any stand-in for a StatusServer."""
    app_instance, _server = tray
    app_instance._set_paused(True)  # must not raise


def test_open_logs_reports_a_failure_instead_of_raising(tray, monkeypatch):
    app_instance, _server = tray
    shown: list[str] = []
    monkeypatch.setattr(
        tray_mod.QtGui.QDesktopServices, "openUrl", staticmethod(lambda url: False)
    )
    monkeypatch.setattr(
        tray_mod.QtWidgets.QMessageBox, "information",
        staticmethod(lambda *a, **k: shown.append(a[-1])),
    )

    app_instance._open_logs()

    assert shown and "logs" in shown[0].lower()


def test_diagnostics_shows_a_placeholder_then_the_report(tray, monkeypatch):
    """The dialog opens on "running…" rather than after the run: `doctor` probes the
    daemon, the engine and every agent's hooks over the network, which takes seconds."""
    app_instance, _server = tray
    monkeypatch.setattr(tray_mod.DoctorWorker, "fetch", lambda self: None)

    app_instance._run_diagnostics()
    view = app_instance._doctor_dialog._view
    assert "Running diagnostics" in view.toPlainText()

    app_instance._show_doctor_report("DAEMON  ok\nENGINE  ok")
    assert view.toPlainText() == "DAEMON  ok\nENGINE  ok"
    app_instance._doctor_dialog.close()


def test_diagnostics_says_so_when_the_report_is_empty(tray, monkeypatch):
    app_instance, _server = tray
    monkeypatch.setattr(tray_mod.DoctorWorker, "fetch", lambda self: None)

    app_instance._run_diagnostics()
    app_instance._show_doctor_report("")

    assert "tintaview doctor" in app_instance._doctor_dialog._view.toPlainText()
    app_instance._doctor_dialog.close()


def test_doctor_worker_runs_non_interactively(monkeypatch):
    """The regression behind "Could not run diagnostics": `doctor -v` offers a live
    hook test, and with the daemon reachable — always true from the tray, which *is*
    the daemon — it reached a prompt. Under pythonw `sys.stdin` is None, so that raised;
    from a terminal-launched tray it would have hung instead."""
    seen: dict = {}

    def fake_run_doctor(verbose=False, paint=False, interactive=None):
        seen.update(verbose=verbose, paint=paint, interactive=interactive)
        print("ENGINE  ok")
        return 0

    monkeypatch.setattr("tintaview.install.doctor.run_doctor", fake_run_doctor)
    worker = tray_mod.DoctorWorker()
    reports: list[str] = []
    worker.report_ready.connect(reports.append)

    worker._run()  # synchronous, no thread

    assert seen == {"verbose": True, "paint": False, "interactive": False}
    assert reports == ["ENGINE  ok"]


def test_doctor_worker_reports_what_actually_broke(monkeypatch):
    """A generic "couldn't run it" sends the user to a log they have to find first —
    the same problem the Open logs folder item exists to solve."""
    def boom(**kwargs):
        raise RuntimeError("input(): lost sys.stdin")

    monkeypatch.setattr("tintaview.install.doctor.run_doctor", boom)
    worker = tray_mod.DoctorWorker()
    reports: list[str] = []
    worker.report_ready.connect(reports.append)

    worker._run()

    assert len(reports) == 1
    assert "lost sys.stdin" in reports[0]  # the real cause, not a shrug
    assert "RuntimeError" in reports[0]


def test_doctor_worker_keeps_a_partial_report_when_it_breaks_midway(monkeypatch):
    def half_then_boom(**kwargs):
        print("ENVIRONMENT  ok")
        raise RuntimeError("fell over")

    monkeypatch.setattr("tintaview.install.doctor.run_doctor", half_then_boom)
    worker = tray_mod.DoctorWorker()
    reports: list[str] = []
    worker.report_ready.connect(reports.append)

    worker._run()

    assert "ENVIRONMENT  ok" in reports[0]  # whatever it managed before breaking
    assert "fell over" in reports[0]


# --------------------------------------------------------------------------- second launch


def test_the_server_gets_a_show_hook(tray):
    """A second `tintaview` launch pops this instance's panel rather than exiting in
    silence — from a Start-menu shortcut there is no console to read a message in."""
    app_instance, server = tray
    assert callable(server.on_show)

    server.on_show()  # what StatusServer.request_show() calls, from an HTTP thread
    app_instance.flyout.hide()


def test_show_requested_opens_the_flyout(tray):
    app_instance, _server = tray
    assert not app_instance.flyout.isVisible()

    app_instance._on_show_requested()

    assert app_instance.flyout.isVisible()
    app_instance.flyout.hide()


# --------------------------------------------------------------------------- running tool


def test_state_poll_passes_the_running_tool_to_the_flyout(tray):
    app_instance, server = tray
    server.set({
        "effective": "working",
        "agents": {"claude": {"effective": "working", "count": 1, "tool": "Bash"}},
        "count": 1,
    })

    app_instance._poll_state()

    assert app_instance.flyout._tools == {"claude": "Bash"}


def test_flyout_paints_a_running_tool(qapp):
    """The tool name is drawn beside the title, after the status dot — it must not
    change the section's height, and a long one must not escape the card."""
    flyout = Flyout()
    flyout.set_results(_sample_results())
    height_without = flyout.height()

    flyout.set_status({"claude": "working"}, {"claude": "Bash"})
    assert flyout.height() == height_without
    flyout.render(QtGui.QPixmap(flyout.size()))  # must not raise

    flyout.set_status({"claude": "working"}, {"claude": "A" * 300})
    assert flyout.height() == height_without
    flyout.render(QtGui.QPixmap(flyout.size()))


def test_flyout_set_status_still_accepts_a_status_map_alone(qapp):
    """The tools argument is optional — nothing that only knows about statuses breaks."""
    flyout = Flyout()
    flyout.set_results(_sample_results())
    flyout.set_status({"claude": "idle"})
    assert flyout._tools == {}
    flyout.render(QtGui.QPixmap(flyout.size()))


# --------------------------------------------------------------------------- off the GUI thread


class _RecordingUpdateModule:
    """Stand-in for `tintaview.install.update`, recording which thread called it.

    The whole point of the tests below: `latest_release()` is an HTTPS call with a 10 s
    timeout and `run_update()` on Linux/macOS is a synchronous `sh install.sh` that
    rebuilds the venv. Either one on the GUI thread freezes the tray, the flyout and the
    broker's own Qt callbacks with nothing on screen to explain why.
    """

    CHANNEL_STABLE = "stable"

    def __init__(self, release: dict | None) -> None:
        self._release = release
        self.check_threads: list[threading.Thread] = []
        self.update_threads: list[threading.Thread] = []

    def latest_release(self, channel: str = "stable"):
        self.check_threads.append(threading.current_thread())
        return self._release

    def compare_versions(self, a: str, b: str) -> int:
        return -1  # "b is newer", always: the release below is what's under test

    def run_update(self, check_only: bool = False, channel: str | None = None) -> int:
        self.update_threads.append(threading.current_thread())
        return 0


def _install_fake_update_module(monkeypatch, fake) -> None:
    """Make `from tintaview.install import update as update_mod` yield `fake`.

    The tray imports it lazily inside the worker (so a headless build without it still
    starts), so patching the attribute on the package is what the import actually reads.
    """
    import tintaview.install

    monkeypatch.setattr(tintaview.install, "update", fake, raising=False)
    monkeypatch.setitem(sys.modules, "tintaview.install.update", fake)


def test_manual_update_check_never_runs_on_the_calling_thread(qapp, monkeypatch):
    fake = _RecordingUpdateModule({"tag_name": "v9.9.9", "body": "Notes"})
    _install_fake_update_module(monkeypatch, fake)

    worker = tray_mod.ManualUpdateWorker()
    seen: list[tuple] = []
    worker.check_ready.connect(lambda *a: seen.append(a))

    caller = threading.current_thread()
    assert worker.check("stable") is True
    _drain(worker)

    assert fake.check_threads, "latest_release() was never called"
    assert all(t is not caller for t in fake.check_threads), (
        "latest_release() ran on the thread that asked for the check"
    )
    qapp.processEvents()  # the signal is queued across the thread boundary
    assert seen and seen[0][0] == tray_mod.ManualUpdateWorker.OUTCOME_AVAILABLE
    assert seen[0][1] == "9.9.9"


def test_manual_update_install_never_runs_on_the_calling_thread(qapp, monkeypatch):
    fake = _RecordingUpdateModule({"tag_name": "v9.9.9"})
    _install_fake_update_module(monkeypatch, fake)

    worker = tray_mod.ManualUpdateWorker()
    codes: list[int] = []
    worker.install_done.connect(codes.append)

    caller = threading.current_thread()
    assert worker.install("stable") is True
    _drain(worker)

    assert fake.update_threads, "run_update() was never called"
    assert all(t is not caller for t in fake.update_threads), (
        "run_update() ran on the thread that asked for the install"
    )
    qapp.processEvents()
    assert codes == [0]


def test_check_updates_menu_item_touches_no_network_on_the_gui_thread(tray, monkeypatch):
    """The menu item itself must return immediately — the whole reason this moved."""
    app_instance, _server = tray
    fake = _RecordingUpdateModule({"tag_name": "v9.9.9"})
    _install_fake_update_module(monkeypatch, fake)
    monkeypatch.setattr(
        QtWidgets.QSystemTrayIcon, "showMessage", lambda self, *a, **k: None
    )
    # The check's reply is a queued signal into `_on_manual_check`, which opens a modal
    # message box. Stub both boxes and flush the queue here, so the dialog can never be
    # delivered (and block on its own nested event loop) inside some later test.
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "information", staticmethod(lambda *a, **k: None)
    )
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "question",
        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.No),
    )

    gui_thread = threading.current_thread()
    app_instance._check_updates()
    _drain(app_instance._manual_update_worker)
    QtWidgets.QApplication.instance().processEvents()

    assert fake.check_threads
    assert all(t is not gui_thread for t in fake.check_threads)


def _drain(worker, timeout: float = 5.0) -> None:
    """Wait for a `_GuardedWorker`'s in-flight run to finish."""
    assert worker._inflight.acquire(timeout=timeout), "the worker never finished"
    worker._inflight.release()


def test_workers_drop_a_second_request_while_one_is_running(qapp):
    """Repeated "Refresh usage" clicks used to stack a thread — and a Cursor RPC against
    a ~300 MB state.vscdb — each. Two overlapping DoctorWorkers were worse: their
    process-global `redirect_stdout` unwinds in the wrong order and leaves `sys.stdout`
    bound to a StringIO nobody ever reads again."""
    started = threading.Event()
    release = threading.Event()
    runs: list[int] = []

    class _Slow(tray_mod._GuardedWorker):
        def _run(self) -> None:
            runs.append(1)
            started.set()
            release.wait(5.0)

    worker = _Slow()
    worker.fetch()
    assert started.wait(5.0)

    worker.fetch()  # must be a no-op while the first is still running
    worker.fetch()
    assert runs == [1]

    release.set()
    _drain(worker)
    assert runs == [1]

    worker.fetch()  # ...and the guard clears once it finishes
    _drain(worker)
    assert runs == [1, 1]


def test_engine_reset_and_reapply_run_off_the_gui_thread(tray_with_controller):
    """A full engine close + open + paint is seconds of blocking I/O; on the GUI thread
    it froze the tray every time Settings was accepted."""
    app_instance, server = tray_with_controller
    seen: list[threading.Thread] = []
    server.controller.reset_engine = lambda: seen.append(threading.current_thread())

    app_instance._apply_settings(_accepted_copy(app_instance, **{"engine.mode": "openrgb"}))
    _join_lighting(app_instance)

    assert seen and seen[0] is not threading.current_thread()


def test_engine_refresh_does_not_overlap_itself(tray_with_controller):
    app_instance, server = tray_with_controller
    release = threading.Event()
    started = threading.Event()

    def slow_reset() -> None:
        server.controller.resets += 1
        started.set()
        release.wait(5.0)

    server.controller.reset_engine = slow_reset

    app_instance._refresh_lighting(reset=True)
    assert started.wait(5.0)
    app_instance._refresh_lighting(reset=True)  # dropped, not queued
    release.set()
    _join_lighting(app_instance)

    assert server.controller.resets == 1


# --------------------------------------------------------------------------- repaint guards


def test_the_icon_is_not_re_set_on_every_poll(tray, monkeypatch):
    """`_poll_state` runs every 1.5 s; `setIcon` makes the shell rebuild and repaint the
    tray item, so re-setting an identical icon is pure cost for the whole session."""
    app_instance, server = tray
    calls: list[object] = []
    monkeypatch.setattr(
        QtWidgets.QSystemTrayIcon, "setIcon", lambda self, icon: calls.append(icon)
    )
    monkeypatch.setattr(
        QtWidgets.QSystemTrayIcon, "setToolTip", lambda self, text: calls.append(text)
    )

    payload = {
        "effective": "idle",
        "agents": {"claude": {"effective": "idle", "count": 1}},
        "count": 1,
    }
    server.set(payload)
    app_instance._poll_state()
    assert len(calls) == 2  # one icon, one tooltip

    for _ in range(5):
        app_instance._poll_state()
    assert len(calls) == 2, "an unchanged state re-set the icon and tooltip"

    server.set({"effective": "none", "agents": {}, "count": 0})
    app_instance._poll_state()
    assert len(calls) == 4  # a real change does get through


def test_a_colour_change_still_repaints_an_unchanged_status(tray_with_controller):
    """The guard keys on what would be *drawn*, not just on the status name — otherwise
    picking a new idle colour in Settings did nothing until the next session opened."""
    app_instance, server = tray_with_controller
    server.set({
        "effective": "confirm",
        "agents": {"claude": {"effective": "confirm", "count": 1}},
        "count": 1,
    })
    app_instance._poll_state()
    before = app_instance.tray.icon().cacheKey()

    app_instance._apply_settings(_accepted_copy(app_instance, **{"colors.confirm": "#123456"}))
    _join_lighting(app_instance)
    app_instance.blink_timer.stop()
    app_instance._on_blink()

    assert app_instance.tray.icon().cacheKey() != before


def test_flyout_set_status_skips_the_repaint_when_nothing_changed(qapp, monkeypatch):
    flyout = Flyout()
    flyout.set_results(_sample_results())
    repaints: list[int] = []
    monkeypatch.setattr(Flyout, "update", lambda self, *a, **k: repaints.append(1))

    flyout.set_status({"claude": "working"}, {"claude": "Bash"})
    assert repaints == [1]

    flyout.set_status({"claude": "working"}, {"claude": "Bash"})
    flyout.set_status({"claude": "working"}, {"claude": "Bash"})
    assert repaints == [1], "an unchanged status map repainted the whole card"

    flyout.set_status({"claude": "idle"}, {"claude": "Bash"})
    assert repaints == [1, 1]


# --------------------------------------------------------------------------- working pulse


def test_the_pulse_is_quantised_and_cached(qapp):
    """The breathe used to be continuous: every 100 ms tick got a distinct brightness, so
    nothing could be cached and the tray rebuilt nine pixmaps and called `setIcon` ten
    times a second, forever, for as long as an agent was working."""
    steps = {icons.pulse_step(x / 1000.0) for x in range(0, 7000)}  # two full periods
    assert steps == set(range(icons.PULSE_STEPS))

    # Quantised colours land in the one icon cache, so a whole cycle costs PULSE_STEPS
    # renders and not one per tick.
    a = icons.pulse_icon_for_step((0, 200, 0), 5)
    b = icons.pulse_icon_for_step((0, 200, 0), 5)
    assert a is b
    assert icons.pulse_icon_for_step((0, 200, 0), 6) is not a

    # The period is unchanged — the tick got slower, the breathe did not.
    assert icons.PULSE_PERIOD_S == 3.5
    assert tray_mod.ANIM_TICK_MS == 200


def test_the_pulse_skips_seticon_while_the_step_is_unchanged(tray, monkeypatch):
    app_instance, _server = tray
    calls: list[object] = []
    monkeypatch.setattr(
        QtWidgets.QSystemTrayIcon, "setIcon", lambda self, icon: calls.append(icon)
    )
    monkeypatch.setattr(tray_mod.icons, "pulse_step", lambda now: 7)

    app_instance._update_anim_icon()
    app_instance._update_anim_icon()
    app_instance._update_anim_icon()

    assert len(calls) == 1


# --------------------------------------------------------------------------- hook drift


class _FakeAdapter:
    def __init__(self, key: str, name: str) -> None:
        self.key = key
        self.display_name = name


def _fake_hooks_module(states: dict[str, str]):
    class _Hooks:
        STATUS_INSTALLED = "installed"
        STATUS_MISSING = "missing"
        STATUS_PARTIAL = "partial"
        STATUS_STALE_PATH = "stale-path"

        def __init__(self) -> None:
            self.checked: list = []

        def status(self, adapter, hook_bin, scope="user", project_dir=None):
            self.checked.append(adapter)
            return states[adapter.key]

    return _Hooks()


def test_hook_drift_reports_only_the_statuses_the_wizard_can_fix(qapp, monkeypatch):
    """`hooks.status()` may grow values beyond the four it has today — an unreadable
    config file, say — and none of those are a reason to send someone into a
    diff-and-confirm install flow that cannot address them."""
    import tintaview.agents.base as agents_base
    import tintaview.install
    import tintaview.install.wsl as wsl_mod

    adapters = {
        "claude": _FakeAdapter("claude", "Claude Code"),
        "codex": _FakeAdapter("codex", "Codex CLI"),
        "cursor": _FakeAdapter("cursor", "Cursor"),
        "jetbrains": None,  # stats-only: no adapter at all, and that is expected
    }
    hooks = _fake_hooks_module({
        "claude": "installed",
        "codex": "missing",
        "cursor": "config-unreadable",  # a value this build has never seen
    })
    monkeypatch.setattr(agents_base, "get", adapters.get)
    # The tray does `from tintaview.install import hooks`, which reads the attribute on
    # the already-imported package — patching sys.modules alone would not be seen.
    monkeypatch.setattr(tintaview.install, "hooks", hooks)
    monkeypatch.setattr(wsl_mod, "configured_adapter", lambda cfg, adapter: adapter)

    cfg = Config()
    cfg.enabled_agents = ["claude", "codex", "cursor", "jetbrains"]
    worker = tray_mod.HookDriftWorker(cfg)
    seen: list[list] = []
    worker.drift_ready.connect(seen.append)

    worker._run()

    assert seen == [["Codex CLI"]]


def test_hook_drift_resolves_the_adapter_through_the_configured_home(qapp, monkeypatch):
    """AGENTS.md, "WSL split install": a bare adapter answers from C:\\Users\\you, which is
    the wrong side of the boundary, and every agent is then reported as broken."""
    import tintaview.agents.base as agents_base
    import tintaview.install
    import tintaview.install.wsl as wsl_mod

    adapter = _FakeAdapter("claude", "Claude Code")
    remote = _FakeAdapter("claude", "Claude Code (remote)")
    hooks = _fake_hooks_module({"claude": "installed"})
    monkeypatch.setattr(agents_base, "get", lambda key: adapter if key == "claude" else None)
    monkeypatch.setattr(tintaview.install, "hooks", hooks)
    monkeypatch.setattr(wsl_mod, "configured_adapter", lambda cfg, a: remote)

    cfg = Config()
    cfg.enabled_agents = ["claude"]
    tray_mod.HookDriftWorker(cfg)._run()

    assert hooks.checked == [remote]


def test_hook_drift_balloons_once_per_state_change_and_offers_the_wizard(tray, monkeypatch):
    """The check runs on the 5-minute usage cadence; a balloon every five minutes for a
    condition the user has already chosen not to fix is how a tray icon gets muted."""
    app_instance, _server = tray
    balloons: list[tuple] = []
    monkeypatch.setattr(
        QtWidgets.QSystemTrayIcon, "showMessage",
        lambda self, title, message, *a, **k: balloons.append((title, message)),
    )
    assert app_instance._hooks_action.isVisible() is False

    app_instance._on_hook_drift(["Codex CLI"])
    assert len(balloons) == 1
    assert "Codex CLI" in balloons[0][1]
    assert app_instance._hooks_action.isVisible() is True

    app_instance._on_hook_drift(["Codex CLI"])  # same state, no second balloon
    assert len(balloons) == 1

    app_instance._on_hook_drift([])  # fixed: menu item goes away, silently
    assert len(balloons) == 1
    assert app_instance._hooks_action.isVisible() is False

    app_instance._on_hook_drift(["Cursor"])  # a new problem does balloon again
    assert len(balloons) == 2


def test_the_drift_check_rides_the_usage_cadence(tray):
    """One slow loop, not a timer of its own: an agent's config file changes about as
    often as its quota does."""
    app_instance, _server = tray
    calls: list[int] = []
    app_instance._drift_worker.fetch = lambda: calls.append(1)  # type: ignore[method-assign]
    app_instance.usage_timer.timeout.emit()
    assert calls == [1]


# --------------------------------------------------------------------------- second launch / quit


def test_the_server_gets_a_quit_hook(tray, monkeypatch):
    """`GET /quit` runs on an HTTP worker thread, so it may only ever *signal* the GUI
    thread — the same rule `/show` follows."""
    app_instance, server = tray
    assert callable(server.on_quit)
    quits: list[int] = []
    monkeypatch.setattr(QtWidgets.QApplication, "quit", lambda self: quits.append(1))

    server.on_quit()  # what StatusServer calls, from an HTTP thread
    QtWidgets.QApplication.instance().processEvents()

    assert quits == [1]
