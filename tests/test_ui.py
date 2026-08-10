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
    # "none" is the mark tinted blue — a fourth state in the same visual family, not
    # the multicolour brand mark (which reads as a different icon rather than a state).
    none_rgb = app_instance._cfg.colors.rgb("none")
    assert app_instance.tray.icon().cacheKey() == icons.state_icon(none_rgb, 128).cacheKey()
    assert app_instance.tray.icon().cacheKey() != icons.brand_icon(128).cacheKey()


def test_tray_sound_toggle_persists(tray, tmp_path):
    app_instance, _ = tray
    actions = app_instance.tray.contextMenu().actions()
    sound_action = next(a for a in actions if a.text() == "Sound on confirm")
    assert sound_action.isChecked() is False

    sound_action.trigger()  # toggles the checkbox and fires `toggled`

    assert app_instance._cfg.ui.chime_on_confirm is True
    assert (tmp_path / "config.toml").exists()
