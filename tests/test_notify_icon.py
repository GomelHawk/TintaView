"""`icons.notify_icon` — the speech bubble drawn over the tray icon."""

from __future__ import annotations

import pytest
from PySide6 import QtWidgets

from tintaview.ui import icons


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_bubble_is_drawn_in_the_upper_right_only(qapp):
    source = icons.state_icon((255, 0, 19))
    badged = icons.notify_icon(source)
    for px in (16, 32, 128):
        before = source.pixmap(px, px).toImage()
        after = badged.pixmap(px, px).toImage()
        assert after.width() == px
        if px >= 32:  # at 16 px the 1 px outline leaves too little white to sample
            # Upper right, between the dots and the top: the bubble's own white...
            c = after.pixelColor(int(px * 0.82), int(px * 0.1))
            assert min(c.red(), c.green(), c.blue()) > 200
        # ...inside a black outline along the top edge.
        edge = after.pixelColor(int(px * 0.82), 0)
        assert edge.alpha() > 100 and max(edge.red(), edge.green(), edge.blue()) < 90
        # Lower left is the untouched mark.
        assert after.pixelColor(int(px * 0.2), int(px * 0.8)) == before.pixelColor(
            int(px * 0.2), int(px * 0.8))


def test_bubble_and_shield_show_together(qapp):
    both = icons.keep_awake_icon(icons.notify_icon(icons.brand_icon())).pixmap(32, 32).toImage()
    bubble = both.pixelColor(int(32 * 0.82), int(32 * 0.1))
    shield = both.pixelColor(int(32 * 0.75), int(32 * 0.55))
    assert min(bubble.red(), bubble.green(), bubble.blue()) > 200
    assert shield.green() > shield.red() and shield.green() > 150


def test_bubble_icons_are_cached_per_source(qapp):
    source = icons.state_icon((1, 2, 3))
    first = icons.notify_icon(source)
    assert icons.notify_icon(source) is first
