"""Localised wording shared by every usage provider.

The five providers each derive a reset time from a different undocumented field
(`resets_at`, `resets_in_seconds`, `billingCycleEnd`, `nextRefill`, `quota_reset_date_utc`),
but they all render it as the same handful of phrases — and before this module each kept
its own copy of the weekday and month abbreviations, which is four places to translate a
word that appears once on screen.

So: parsing stays with the provider that knows its own payload, and the *text* lives
here. `stats/providers/*` must not build a user-visible date string by hand.

And it is worded **when the row is drawn**, not when it was fetched: a provider hands a
row the reset *instant* (`UsageRow.reset_at`) plus one of the `RESET_*` styles below,
and `reset_row_text` does the rest. A provider that worded its own reset time froze
"Resets in 2 hr 59 min" into `usage_cache.json` and counted it down to nothing.

Nothing here raises: a bad value produces an empty string, because this text lands in a
flyout row's right-hand slot where a wrong-looking value reads as a usage figure (see
`providers/codex._reset_epoch` for the incident that rule comes from).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from tintaview.i18n import t

#: How a row's `UsageRow.reset_at` is worded. A provider names the style its own API's
#: window deserves and `reset_row_text` does the rest, so the wording still lives here
#: while the *instant* lives on the row — which is the point of the split: a provider
#: that worded its reset time at fetch time froze "Resets in 2 hr 59 min" into
#: `usage_cache.json`, where it went on counting down to a moment three days past.
RESET_RELATIVE = "relative"  # "Resets in 3 hr 23 min", then "Resets Fri 3:59 PM"
RESET_RELATIVE_WEEK = "relative_week"  # as above, but a bare date once a week out
RESET_DATE = "date"  # always "Resets 14 Sep" — for a monthly billing cycle
RESET_DAYS = "days"  # "Resets in 24d" / "Resets today" — whole days only

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


def reset_in_days_text(dt: datetime) -> str:
    """"Resets in 24d", or "Resets today" for anything already due.

    Whole days, because the API this wording exists for (GitHub Copilot's) reports only
    a reset *date*: an hours-and-minutes countdown off a date implies a precision the
    payload doesn't have.
    """
    seconds = (dt - datetime.now(UTC)).total_seconds()
    if seconds <= 0:
        return t("usage.reset.today")
    return t("usage.reset.in_days", days=math.ceil(seconds / 86400))


def cache_age_text(age_s: float) -> str:
    """"Couldn't refresh — usage from 6 hr ago", for rows served out of the cache.

    Coarse on purpose, and abbreviated in the same style as the reset phrases above:
    the point is "these numbers are old and roughly how old", not a precise duration.
    Anything under a minute still says "1 min" rather than "0 min" — a substitution
    that recent is not shown at all (see `StatsService._staleness_notice`), so a zero
    here could only ever be a rounding artefact.
    """
    # Hours all the way to two days, not one. `StatsService.MAX_CACHE_GRACE_S` is 48 h,
    # so a 24-hour boundary would make "1d ago" the only thing the days form could ever
    # say, and it would say it for everything from 24 to 48 hours old — collapsing the
    # distinction precisely where the reader is deciding whether to trust the number.
    # The days form is kept for a clock that jumped, or a wider ceiling later.
    if age_s >= 2 * 86400:
        return t("usage.cache.stale_days", days=int(age_s // 86400))
    if age_s >= 3600:
        return t("usage.cache.stale_hours", hours=int(age_s // 3600))
    return t("usage.cache.stale_minutes", minutes=max(1, int(age_s // 60)))


def reset_row_text(reset_at: float, style: str = RESET_RELATIVE) -> str:
    """A row's right-hand reset text, worded from `reset_at` (epoch seconds) **now**.

    Called at render time, not fetch time — the whole reason `UsageRow.reset_at` holds
    an instant instead of a sentence. A row that worded its own reset time when it was
    fetched was already up to one poll interval stale the moment it was drawn, and a
    cached one stayed frozen for as long as the cache stood in: the flyout showed
    "Resets in 2 hr 59 min" for three days, off a window that had closed on the Friday.

    An unknown instant (0.0) or an unrepresentable one is "", per this module's rule
    that a bad value never reaches a row's right-hand slot looking like a usage figure.
    """
    if not reset_at:
        return ""
    try:
        dt = datetime.fromtimestamp(reset_at, UTC)
    except (OverflowError, OSError, ValueError):
        return ""
    if style == RESET_DATE:
        return reset_at_date(dt.astimezone())
    if style == RESET_DAYS:
        return reset_in_days_text(dt)
    if style == RESET_RELATIVE_WEEK:
        # Beyond a week a weekday name is ambiguous at best — "Resets Fri" for a
        # monthly budget four weeks out reads as *this* Friday.
        return reset_text(dt, date_after_days=6)
    return reset_text(dt)
