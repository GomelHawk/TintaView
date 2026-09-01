"""Headless Qt tests for the tray UI: icon tinting/caching/fallback, the flyout's
multi-agent painting, and TrayApp's state -> icon/tooltip mapping.

`QT_QPA_PLATFORM=offscreen` must be set before PySide6 is ever imported (by this
module or anything it imports), so it happens as the very first thing here, ahead
of even `pytest.importorskip`.
"""

from __future__ import annotations

import os

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


def test_state_icon_falls_back_when_asset_missing(qapp, monkeypatch, tmp_path):
    # Point every asset lookup at a directory that doesn't exist, forcing the
    # procedural burst-icon fallback for a (rgb, size) combo not used elsewhere.
    monkeypatch.setattr(icons, "asset_path", lambda name: tmp_path / "missing" / name)

    icon = icons.state_icon((250, 60, 60), size=77)
    assert not icon.isNull()

    image = icon.pixmap(77, 77).toImage()
    # Ray 0 points straight up from centre before rotation — sample a point along
    # it, safely inside the outer radius, and expect it filled with the tint.
    cx = 77 / 2.0
    x, y = int(cx), int(cx - 77 * 0.3)
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

    assert server.controller.resets == 1
    assert server.controller.applied == ["working"]  # the live effective status


def test_apply_settings_reapplies_lighting_after_a_colour_change(tray_with_controller):
    """Same reason, without an engine change: a new colour has to reach the hardware
    now, not at the next status transition."""
    app_instance, server = tray_with_controller

    app_instance._apply_settings(_accepted_copy(app_instance, **{"colors.device.working": "#ABCDEF"}))

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


def test_diagnostics_says_so_when_the_run_failed(tray, monkeypatch):
    app_instance, _server = tray
    monkeypatch.setattr(tray_mod.DoctorWorker, "fetch", lambda self: None)

    app_instance._run_diagnostics()
    app_instance._show_doctor_report("")  # what DoctorWorker emits on an exception

    assert "tintaview doctor" in app_instance._doctor_dialog._view.toPlainText()
    app_instance._doctor_dialog.close()


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
