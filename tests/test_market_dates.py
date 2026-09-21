"""Synthetic timezone boundaries; no application config or external data."""

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from market_dates import daily_bar_date, market_date, market_timezone, utc_now


@pytest.mark.parametrize(
    "symbol,instant,expected",
    [
        ("SYM-US", "2026-01-13T00:10:00+00:00", "2026-01-12"),
        ("SYM-US", "2026-07-14T00:10:00+00:00", "2026-07-13"),
        ("SYM-US", "2026-01-17T00:10:00+00:00", "2026-01-16"),
        ("SYM-US", "2026-03-08T04:59:00+00:00", "2026-03-07"),
        ("SYM-US", "2026-03-08T05:00:00+00:00", "2026-03-08"),
        ("SYM-US", "2026-03-09T04:00:00+00:00", "2026-03-09"),
        ("SYM-US", "2026-11-01T04:00:00+00:00", "2026-11-01"),
        ("SYM-US", "2026-11-02T04:59:00+00:00", "2026-11-01"),
        ("SYM-US", "2026-11-02T05:00:00+00:00", "2026-11-02"),
        ("SYM-KR.KS", "2026-01-12T14:59:00+00:00", "2026-01-12"),
        ("SYM-KR.KS", "2026-01-12T15:00:00+00:00", "2026-01-13"),
        ("SYM-KR.KQ", "2026-01-12T23:30:00+00:00", "2026-01-13"),
        ("SYM-COIN-USD", "2026-01-12T23:59:00+00:00", "2026-01-12"),
        ("SYM-COIN-USDT", "2026-01-13T00:00:00+00:00", "2026-01-13"),
    ],
)
def test_market_calendar_boundaries(symbol, instant, expected):
    assert market_date(symbol, datetime.fromisoformat(instant)).isoformat() == expected


def test_case_and_whitespace_do_not_change_market():
    assert market_timezone(" sym-etf.ks ").key == "Asia/Seoul"


def test_injected_clock_normalizes_an_aware_offset():
    supplied = datetime(2026, 1, 13, 9, 0, tzinfo=timezone(timedelta(hours=9)))
    assert utc_now(supplied) == datetime(2026, 1, 13, 0, 0, tzinfo=timezone.utc)
    assert utc_now(supplied).tzinfo is timezone.utc


@pytest.mark.parametrize("invalid", [datetime(2026, 1, 13), pd.NaT, date(2026, 1, 13), "2026-01-13", 0])
def test_clock_rejects_ambiguous_or_invalid_inputs(invalid):
    with pytest.raises(ValueError, match="timezone-aware"):
        utc_now(invalid)


@pytest.mark.parametrize(
    "label",
    [date(2026, 1, 13), datetime(2026, 1, 13), pd.Timestamp("2026-01-13", tz="UTC"),
     pd.Timestamp("2026-01-13", tz="Asia/Seoul"), "2026-01-13", "2026-01-13T00:00:00+09:00"],
)
def test_daily_labels_keep_displayed_date(label):
    assert daily_bar_date(label) == date(2026, 1, 13)


@pytest.mark.parametrize("label", [pd.NaT, None, 0, 20260113, float("nan"), "not-a-date", "2026-02-30"])
def test_invalid_daily_labels_have_no_date(label):
    assert daily_bar_date(label) is None
