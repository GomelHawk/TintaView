"""`icons.keep_awake_icon` — the green shield drawn over the tray icon."""

from __future__ import annotations

import pytest
from PySide6 import QtWidgets

from tintaview.ui import icons


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_shield_is_drawn_in_the_lower_right_only(qapp):
    source = icons.state_icon((255, 0, 19))
    badged = icons.keep_awake_icon(source)
    for px in (16, 32, 128):
        before = source.pixmap(px, px).toImage()
        after = badged.pixmap(px, px).toImage()
        assert after.width() == px
        # Lower right: the shield's own green.
        c = after.pixelColor(int(px * 0.75), int(px * 0.55))
        assert c.green() > c.red() and c.green() > 150
        # Upper left is the untouched mark.
        assert after.pixelColor(int(px * 0.2), int(px * 0.2)) == before.pixelColor(
            int(px * 0.2), int(px * 0.2))


def test_the_same_shield_whatever_the_status(qapp):
    """Static and one colour: the shield is not tinted by the status underneath."""
    px = 64
    spot = (int(px * 0.75), int(px * 0.55))
    colours = {
        icons.keep_awake_icon(icons.state_icon(rgb)).pixmap(px, px).toImage().pixelColor(*spot).name()
        for rgb in ((255, 0, 19), (255, 187, 0), (76, 0, 5))
    }
    colours.add(icons.keep_awake_icon(icons.brand_icon()).pixmap(px, px).toImage()
                .pixelColor(*spot).name())
    assert len(colours) == 1


def test_badged_icons_are_cached_and_the_source_is_untouched(qapp):
    source = icons.state_icon((10, 200, 10))
    raw = source.pixmap(48, 48).toImage()
    first = icons.keep_awake_icon(source)
    assert icons.keep_awake_icon(source) is first
    assert source.pixmap(48, 48).toImage() == raw
