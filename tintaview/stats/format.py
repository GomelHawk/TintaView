"""Localised wording shared by every usage provider.

The five providers each derive a reset time from a different undocumented field
(`resets_at`, `resets_in_seconds`, `billingCycleEnd`, `nextRefill`, `quota_reset_date_utc`),
but they all render it as the same handful of phrases — and before this module each kept
its own copy of the weekday and month abbreviations, which is four places to translate a
word that appears once on screen.

So: parsing stays with the provider that knows its own payload, and the *text* lives
here. `stats/providers/*` must not build a user-visible date string by hand.

Nothing here raises: a bad value produces an empty string, because this text lands in a
flyout row's right-hand slot where a wrong-looking value reads as a usage figure (see
`providers/codex._fmt_reset` for the incident that rule comes from).
"""

from __future__ import annotations

from datetime import UTC, datetime

from tintaview.i18n import t

#: Catalogue key suffixes, in `datetime`'s own order: `weekday()` is Monday-based and
#: `month` is 1-based.
_WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_MONTH_KEYS = ("jan", "feb", "mar", "apr", "may", "jun",
               "jul", "aug", "sep", "oct", "nov", "dec")


def weekday_name(dt: datetime) -> str:
    """Abbreviated weekday. Not `strftime("%a")`, which is locale-dependent (and so
    English on almost every machine no matter what the user picked here)."""
    return t(f"usage.weekday.{_WEEKDAY_KEYS[dt.weekday()]}")


def month_name(dt: datetime) -> str:
    return t(f"usage.month.{_MONTH_KEYS[dt.month - 1]}")


def date_text(dt: datetime) -> str:
    """A bare day+month ("14 Sep") for composing into a larger phrase."""
    return t("usage.date", day=dt.day, month=month_name(dt))


def reset_at_date(dt: datetime) -> str:
    """"Resets 14 Sep" — for a window far enough out that a weekday is ambiguous."""
    return t("usage.reset.at_date", day=dt.day, month=month_name(dt))


def reset_at_time(dt: datetime) -> str:
    """"Resets Fri 3:59 PM".

    Both a 12- and a 24-hour rendering of the hour are passed in, plus the AM/PM word,
    and the catalogue entry decides which to use: a 12-hour clock is the natural
    reading in English and the wrong one in German, Polish or Russian.
    """
    return t(
        "usage.reset.at_time",
        weekday=weekday_name(dt),
        hour12=dt.hour % 12 or 12,
        # Zero-padded: every 24-hour catalogue reads "09:05", not "9:05". (hour12 is
        # deliberately NOT padded — a 12-hour clock writes "9:05 PM".)
        hour24=f"{dt.hour:02d}",
        minute=f"{dt.minute:02d}",
        ampm=t("usage.ampm.am") if dt.hour < 12 else t("usage.ampm.pm"),
    )


def reset_text(dt: datetime, *, date_after_days: int | None = None) -> str:
    """How long until `dt`, worded by how far away it is.

    Within the day it stays relative ("Resets in 3 hr 23 min") because that is the form
    someone waiting on a limit actually reads; past that it becomes a clock time. With
    `date_after_days`, anything that far out becomes a date instead — a weekday name for
    a monthly budget four weeks away reads as *this* week (see `providers/codex`).
    """
    secs = int((dt - datetime.now(UTC)).total_seconds())
    if secs <= 0:
        return t("usage.reset.now")
    if secs < 86400:
        hours, rem = divmod(secs, 3600)
        minutes = rem // 60
        if hours:
            return t("usage.reset.in_hours_minutes", hours=hours, minutes=minutes)
        return t("usage.reset.in_minutes", minutes=minutes)
    local = dt.astimezone()
    if date_after_days is not None and secs >= date_after_days * 86400:
        return reset_at_date(local)
    return reset_at_time(local)
