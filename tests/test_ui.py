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

from PySide6 import QtGui, QtWidgets  # noqa: E402

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


def test_flyout_handles_empty_results(qapp):
    flyout = Flyout()
    flyout.set_results({})
    pixmap = QtGui.QPixmap(flyout.size())
    flyout.render(pixmap)
    assert not pixmap.isNull()


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
    tooltip = app_instance.tray.toolTip()
    assert "Claude Code: idle (1 session)" in tooltip
    assert "Codex CLI: no session" in tooltip
    # One agent per line: joined into a single line, three enabled agents wrapped
    # mid-entry and stranded an agent's name away from its status.
    assert tooltip.splitlines() == ["Claude Code: idle (1 session)", "Codex CLI: no session"]
    assert not app_instance.blink_timer.isActive()

    server.set({
        "effective": "working",
        "agents": {"claude": {"effective": "working", "count": 2},
                   "codex": {"effective": "none", "count": 0}},
        "count": 2,
    })
    app_instance._poll_state()
    working_icon = app_instance.tray.icon().cacheKey()
    assert "Claude Code: working (2 sessions)" in app_instance.tray.toolTip()
    assert working_icon != idle_icon

    server.set({
        "effective": "confirm",
        "agents": {"claude": {"effective": "confirm", "count": 1},
                   "codex": {"effective": "none", "count": 0}},
        "count": 1,
    })
    app_instance._poll_state()
    assert "Claude Code: needs confirmation" in app_instance.tray.toolTip()
    assert app_instance.blink_timer.isActive()
    assert app_instance.tray.icon().cacheKey() == icons.state_icon(confirm_rgb, 128).cacheKey()
    assert app_instance.tray.icon().cacheKey() != working_icon

    server.set({"effective": "none", "agents": {}, "count": 0})
    app_instance._poll_state()
    assert not app_instance.blink_timer.isActive()
    assert "no session" in app_instance.tray.toolTip().lower()
    # "no session" is the mark in the LOGO's colours, not a fourth status tint: at rest
    # the tray is just the TintaView logo, and any single-colour icon means something is
    # happening. It shares the status icons' geometry exactly, so it still reads as the
    # same mark rather than a different icon.
    assert app_instance.tray.icon().cacheKey() == icons.brand_icon(128).cacheKey()
    none_rgb = app_instance._cfg.colors.rgb("none")
    assert app_instance.tray.icon().cacheKey() != icons.state_icon(none_rgb, 128).cacheKey()


def test_tray_sound_toggle_persists(tray, tmp_path):
    app_instance, _ = tray
    actions = app_instance.tray.contextMenu().actions()
    sound_action = next(a for a in actions if a.text() == "Sound on confirm")
    assert sound_action.isChecked() is False

    sound_action.trigger()  # toggles the checkbox and fires `toggled`

    assert app_instance._cfg.ui.chime_on_confirm is True
    assert (tmp_path / "config.toml").exists()


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
