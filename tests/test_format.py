"""Shared row wording for the usage providers (`tintaview.stats.format`).

`stats/format.py` exists so five providers don't each keep their own weekday table, and
so the reset column can be worded per language. What is easy to get wrong here is the
*clock*: `usage.reset.at_time` is handed both a 12- and a 24-hour rendering of the hour
and each catalogue picks one, so a padding bug in one of them is invisible in English.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tintaview import i18n
from tintaview.stats import format as fmt


@pytest.fixture(autouse=True)
def _restore_language():
    """Language is process-global; a leak fails a test in another module entirely."""
    previous = i18n.current_language()
    try:
        yield
    finally:
        i18n.set_language(previous)


def _at(hour: int, minute: int) -> datetime:
    return datetime(2026, 9, 4, hour, minute, tzinfo=UTC)  # a Friday


# --------------------------------------------------------------------------- the clock


@pytest.mark.parametrize("language", ["de", "ru", "pl", "uk", "be", "es", "it"])
def test_twenty_four_hour_catalogues_pad_the_hour(language):
    """09:05, never 9:05.

    Every non-English catalogue renders `{hour24}:{minute}`; the hour was interpolated
    unpadded while the minute was padded, so a single-digit hour produced a time that
    reads as broken in exactly the languages nobody on the team checks.
    """
    i18n.set_language(language)
    text = fmt.reset_at_time(_at(9, 5))
    assert "09:05" in text, text
    assert "9:05" not in text.replace("09:05", "")


def test_english_keeps_an_unpadded_twelve_hour_clock():
    """The 12-hour form must NOT gain a leading zero — "09:05 PM" is not how it reads."""
    i18n.set_language("en")
    assert "9:05 PM" in fmt.reset_at_time(_at(21, 5))
    assert "12:00 AM" in fmt.reset_at_time(_at(0, 0))
    assert "12:00 PM" in fmt.reset_at_time(_at(12, 0))


@pytest.mark.parametrize("hour,expected", [(0, "00:"), (9, "09:"), (13, "13:"), (23, "23:")])
def test_hour24_is_two_digits_across_the_whole_day(hour, expected):
    i18n.set_language("de")
    assert expected in fmt.reset_at_time(_at(hour, 7))


# --------------------------------------------------------------------------- wording


def test_weekday_and_month_come_from_the_catalogue_not_strftime():
    """`strftime("%a")` is the C locale, not the user's choice — always English."""
    i18n.set_language("ru")
    assert fmt.weekday_name(_at(12, 0)) == i18n.t("usage.weekday.fri")
    assert fmt.month_name(_at(12, 0)) == i18n.t("usage.month.sep")


def test_reset_text_stays_relative_within_the_day():
    i18n.set_language("en")
    soon = datetime.now(UTC) + timedelta(hours=3, minutes=23)
    text = fmt.reset_text(soon)
    assert "3" in text and "hr" in text.lower() or "hour" in text.lower()


def test_reset_text_in_the_past_says_now():
    i18n.set_language("en")
    assert fmt.reset_text(datetime.now(UTC) - timedelta(minutes=1)) == i18n.t("usage.reset.now")


def test_reset_text_becomes_a_date_past_date_after_days():
    """A weekday name for a monthly budget four weeks out reads as *this* week."""
    i18n.set_language("en")
    far = datetime.now(UTC) + timedelta(days=28)
    text = fmt.reset_text(far, date_after_days=6)
    assert text == fmt.reset_at_date(far.astimezone())
    assert fmt.weekday_name(far.astimezone()) not in text
