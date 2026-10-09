"""The bell in the usage panel's title bar: the last `notify_user` messages."""

from __future__ import annotations

import pytest
from PySide6 import QtCore, QtGui, QtWidgets

from tintaview.ui import flyout as flyout_mod
from tintaview.ui.flyout import Flyout, NotifyPopup
from tintaview.ui.notify_log import MAX_ENTRIES, NotifyEntry, NotifyLog, age_text


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


# --------------------------------------------------------------------------- model


def test_newest_first_capped_and_unread_until_seen():
    log = NotifyLog()
    assert log.entries == [] and not log.unread
    for i in range(MAX_ENTRIES + 3):
        log.add(NotifyEntry("claude", f"m{i}"))
    assert len(log.entries) == MAX_ENTRIES
    assert log.entries[0].message == f"m{MAX_ENTRIES + 2}"  # newest first
    assert log.unread
    log.mark_seen()
    assert not log.unread and log.entries  # seen is not cleared
    log.add(NotifyEntry("codex", "again"))
    assert log.unread
    log.clear()
    assert log.entries == [] and not log.unread


@pytest.mark.parametrize("cwd, project", [
    ("/home/me/TintaView", "TintaView"),
    ("/home/me/TintaView/", "TintaView"),
    (r"C:\Users\me\billing-service", "billing-service"),
    ("", ""),
])
def test_project_is_the_folder_name(cwd, project):
    assert NotifyEntry("claude", "x", cwd=cwd).project == project


@pytest.mark.parametrize("age, text", [
    (5, "just now"), (60, "1 min ago"), (59 * 60, "59 min ago"),
    (3600, "1 hr ago"), (47 * 3600, "47 hr ago"), (3 * 86400, "3d ago"),
])
def test_age_is_worded_coarsely(age, text):
    assert age_text(1000.0, now=1000.0 + age) == text


# --------------------------------------------------------------------------- card


def _card(qapp) -> Flyout:
    card = Flyout()
    card.resize(flyout_mod.CARD_W, 200)
    return card


def test_the_bell_sits_left_of_the_gear(qapp):
    card = _card(qapp)
    gear, _close = card._top_bar_rects()
    bell = card._bell_rect()
    assert bell.right() < gear.left()
    assert bell.top() == gear.top() and bell.width() == gear.width()


def test_tooltip_says_none_yet_then_peeks_at_the_newest(qapp):
    card = _card(qapp)
    assert card._bell_tooltip() == "No notifications yet"
    card.notify_log.add(NotifyEntry("codex", "older"))
    card.notify_log.add(NotifyEntry("claude", "Migration finished:\n142 tables " + "x" * 200))
    tip = card._bell_tooltip()
    assert tip.startswith("Claude Code · just now — Migration finished: 142 tables")
    assert tip.endswith("…") and "\n" not in tip


def test_clicking_an_empty_bell_does_nothing(qapp):
    card = _card(qapp)
    card._open_notifications()
    assert card.findChild(NotifyPopup) is None


def test_opening_the_list_marks_it_seen_and_shows_every_message(qapp):
    card = _card(qapp)
    card.notify_log.add(NotifyEntry("claude", "first <b>not markup</b>", cwd="/p/TintaView"))
    card.notify_log.add(NotifyEntry("copilot", "second"))
    card._open_notifications()
    popup = card.findChild(NotifyPopup)
    try:
        assert isinstance(popup, NotifyPopup) and popup.isVisible()
        assert not card.notify_log.unread
        labels = {lbl.text(): lbl for lbl in popup.findChildren(QtWidgets.QLabel)}
        assert "first <b>not markup</b>" in labels and "second" in labels and "TintaView" in labels
        message = labels["first <b>not markup</b>"]
        assert message.textFormat() == QtCore.Qt.PlainText  # an agent's text, never HTML
        assert message.textInteractionFlags() & QtCore.Qt.TextSelectableByMouse
    finally:
        popup.close()


def test_clear_empties_the_list_and_grays_the_bell(qapp):
    card = _card(qapp)
    card.notify_log.add(NotifyEntry("claude", "done"))
    card._open_notifications()
    popup = card.findChild(NotifyPopup)
    clear = next(b for b in popup.findChildren(QtWidgets.QPushButton) if b.text() == "Clear")
    clear.click()
    assert card.notify_log.entries == []
    assert card._bell_tooltip() == "No notifications yet"


def test_an_opened_list_leaves_no_reference_cycle(qapp):
    """The CI crash (Windows access violation, macOS segfault in shiboken's
    `mainThreadDeletionHandler`, PySide6 6.12): card and popup referenced each other,
    so only the cycle collector could free them — on whatever thread it ran — and the
    deferred Qt delete then hit a popup that had already deleted itself on close.
    Without a cycle the last reference going away frees the card at once, right here
    on the main thread, which is all this checks: `gc` is off while it runs."""
    import gc
    import weakref

    gc.collect()
    gc.disable()
    try:
        card = _card(qapp)
        card.notify_log.add(NotifyEntry("claude", "done"))
        card._open_notifications()
        card.findChild(NotifyPopup).close()
        ref = weakref.ref(card)
        del card
        assert ref() is None, "the card is only freeable by the cycle collector"
    finally:
        gc.enable()


def _bell_pixels(card: Flyout) -> set[str]:
    image = QtGui.QImage(card.size(), QtGui.QImage.Format_ARGB32)
    image.fill(0)
    card.render(image)
    r = card._bell_rect().toAlignedRect()
    return {image.pixelColor(x, y).name() for x in range(r.left(), r.right())
            for y in range(r.top(), r.bottom())}


def test_the_bell_is_gray_then_white_then_dotted(qapp):
    card = _card(qapp)
    faint, text, dot = (flyout_mod.FAINT.name(), flyout_mod.TEXT.name(),
                        flyout_mod.BELL_DOT.name())
    empty = _bell_pixels(card)
    assert faint in empty and text not in empty
    card.notify_log.add(NotifyEntry("claude", "done"))
    unread = _bell_pixels(card)
    assert text in unread and dot in unread
    card.notify_log.mark_seen()
    seen = _bell_pixels(card)
    assert text in seen and dot not in seen
