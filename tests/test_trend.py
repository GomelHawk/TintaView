"""Tests for `tintaview.stats.trend` — the burn-rate projection under a usage section.

The projection is crude by design (a straight line through a handful of samples), so
what is pinned here is mostly the set of cases where it must say **nothing**: too little
history, a window that is flat or falling, an ETA past the horizon, and an ETA later
than the window's own reset. A wrong "empties in ~10 min" on a card someone plans their
afternoon around is worse than no line at all.
"""

from __future__ import annotations

import json

import pytest

from tintaview.stats.model import UsageResult, UsageRow
from tintaview.stats.trend import MIN_SPAN_S, SAMPLE_TTL_S, TrendStore

NOW = 1_700_000_000.0


@pytest.fixture
def store(tmp_path):
    return TrendStore(tmp_path / "usage_trend.json")


def _result(pct: float, *, label: str = "5-hour limit", reset_at: float = 0.0,
            source: str = "official", agent: str = "claude") -> UsageResult:
    return UsageResult(
        agent=agent,
        source=source,
        rows=[UsageRow(label=label, pct=pct, reset_at=reset_at)],
    )


def _feed(store, points, **kwargs):
    """Record `(offset_seconds, pct)` pairs relative to NOW."""
    for offset, pct in points:
        store.record([_result(pct, **kwargs)], now=NOW + offset)


def test_a_climbing_window_projects_when_it_will_empty(store):
    # 40% -> 60% in an hour is 20 points/hour, with 40 points left to spend.
    _feed(store, [(0, 40.0), (3600, 60.0)])

    eta = store.eta("claude", UsageRow(label="5-hour limit", pct=60.0), now=NOW + 3600)

    assert eta == pytest.approx(2 * 3600, rel=0.01)


def test_too_little_history_says_nothing(store):
    _feed(store, [(0, 40.0), (MIN_SPAN_S - 60, 60.0)])

    assert store.eta("claude", UsageRow(label="5-hour limit", pct=60.0),
                     now=NOW + MIN_SPAN_S - 60) is None


def test_a_flat_or_falling_window_says_nothing(store):
    """A reset *lowers* the percentage, which is a negative rate, not a forecast."""
    _feed(store, [(0, 60.0), (3600, 60.0)])
    assert store.eta("claude", UsageRow(label="5-hour limit", pct=60.0), now=NOW + 3600) is None

    _feed(store, [(7200, 5.0)])
    assert store.eta("claude", UsageRow(label="5-hour limit", pct=5.0), now=NOW + 7200) is None


def test_an_eta_beyond_the_horizon_says_nothing(store):
    """"Empties in 19 hours" is not news on a card you glance at."""
    _feed(store, [(0, 10.0), (3600, 11.0)])  # 1 point/hour, 89 to go

    assert store.eta("claude", UsageRow(label="5-hour limit", pct=11.0), now=NOW + 3600) is None


def test_a_window_that_resets_before_it_empties_says_nothing(store):
    """The limit isn't going to run out — and the row already says when it resets."""
    _feed(store, [(0, 40.0), (3600, 60.0)])  # would empty in ~2 h
    row = UsageRow(label="5-hour limit", pct=60.0, reset_at=NOW + 3600 + 1800)  # in 30 min

    assert store.eta("claude", row, now=NOW + 3600) is None


def test_cached_readings_are_not_recorded(store):
    """A cached result is the same numbers again; recording them would flatten the rate
    towards zero for as long as the outage lasted."""
    _feed(store, [(0, 40.0)])
    _feed(store, [(1800, 40.0), (3600, 40.0)], source="cache")
    _feed(store, [(3600, 60.0)])

    eta = store.eta("claude", UsageRow(label="5-hour limit", pct=60.0), now=NOW + 3600)

    assert eta == pytest.approx(2 * 3600, rel=0.01), "a cache substitution skewed the rate"


def test_samples_expire_and_the_file_stays_small(store, tmp_path):
    _feed(store, [(0, 10.0), (60, 11.0)])
    _feed(store, [(SAMPLE_TTL_S + 3600, 80.0), (SAMPLE_TTL_S + 7200, 90.0)])

    stored = json.loads((tmp_path / "usage_trend.json").read_text(encoding="utf-8"))
    kept = stored["claude"]["5-hour limit"]

    assert [pct for _ts, pct in kept] == [80.0, 90.0]


def test_series_are_keyed_by_label_not_by_position(store):
    """Providers reorder rows; an index would chart one window against another's pace."""
    weekly = UsageRow(label="Weekly limit", pct=30.0)
    _feed(store, [(0, 40.0), (3600, 60.0)])
    _feed(store, [(0, 29.0), (3600, 30.0)], label="Weekly limit")

    assert store.eta("claude", weekly, now=NOW + 3600) is None  # 1 point/hour: beyond the horizon
    assert store.eta("claude", UsageRow(label="5-hour limit", pct=60.0), now=NOW + 3600) is not None


def test_soonest_picks_the_row_that_stops_your_work_first(store):
    _feed(store, [(0, 40.0), (3600, 60.0)])  # ~2 h
    _feed(store, [(0, 40.0), (3600, 80.0)], label="Weekly limit")  # ~30 min

    result = UsageResult(agent="claude", rows=[
        UsageRow(label="5-hour limit", pct=60.0),
        UsageRow(label="Weekly limit", pct=80.0),
    ])
    soonest = store.soonest(result, now=NOW + 3600)

    assert soonest is not None
    assert soonest[0].label == "Weekly limit"


def test_a_corrupt_trend_file_is_treated_as_no_history(tmp_path):
    path = tmp_path / "usage_trend.json"
    path.write_text("{ not json", encoding="utf-8")

    store = TrendStore(path)
    _feed(store, [(0, 40.0), (3600, 60.0)])

    assert store.eta("claude", UsageRow(label="5-hour limit", pct=60.0), now=NOW + 3600)


def test_recording_never_raises(store, monkeypatch):
    """A trend is a nicety; it must never take a stats poll down with it."""
    monkeypatch.setattr(store, "_save", lambda: (_ for _ in ()).throw(OSError("read-only")))

    store.record([_result(40.0)], now=NOW)  # must not raise
