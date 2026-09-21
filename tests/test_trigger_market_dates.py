"""Trigger freshness follows daily labels and each market's calendar date."""
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from atr_calculator import check_immediate_triggers


def bars(latest_label, *, last_close=97.0, last_open=100.0):
    frame = pd.DataFrame({
        "Open": [100.0] * 30,
        "High": [101.0] * 30,
        "Low": [96.0] * 30,
        "Close": [100.0] * 30,
        "Volume": [1000.0] * 30,
    })
    # Object labels let malformed/numeric/NaT inputs reach the public boundary.
    labels = list(pd.date_range("2025-11-01", periods=29, freq="D"))
    frame.index = pd.Index(labels + [latest_label], dtype=object)
    frame.iloc[-1, frame.columns.get_loc("Close")] = last_close
    frame.iloc[-1, frame.columns.get_loc("Open")] = last_open
    return frame


def at(iso):
    return datetime.fromisoformat(iso)


@pytest.mark.parametrize("symbol,now,label", [
    ("MOCK", "2026-01-13T00:10:00+00:00", "2026-01-12"),
    ("MOCK", "2026-01-17T00:30:00+00:00", "2026-01-16"),
    ("MOCK", "2026-07-18T00:00:00+00:00", "2026-07-17"),
    ("MOCK", "2026-03-08T04:30:00+00:00", "2026-03-07"),
    ("MOCK", "2026-03-09T04:30:00+00:00", "2026-03-09"),
    ("MOCK", "2026-11-01T04:30:00+00:00", "2026-11-01"),
    ("MOCK", "2026-11-02T04:30:00+00:00", "2026-11-01"),
    ("MOCK.KS", "2026-01-12T23:30:00+00:00", "2026-01-13"),
    ("MOCK.KQ", "2026-01-12T23:30:00+00:00", "2026-01-13"),
    ("MOCK-USD", "2026-01-13T00:10:00+00:00", "2026-01-13"),
    ("MOCK-USDT", "2026-01-12T23:30:00+00:00", "2026-01-12"),
])
def test_current_market_day_allows_breach(symbol, now, label):
    result = check_immediate_triggers(symbol, bars(label), 98.0, now_utc=at(now))
    assert len(result.triggers) == 1
    assert result.triggers[0].startswith("STOP BREACH")


@pytest.mark.parametrize("symbol,now,stale,future", [
    ("MOCK", "2026-01-13T00:10:00+00:00", "2026-01-11", "2026-01-13"),
    ("MOCK.KS", "2026-01-12T23:30:00+00:00", "2026-01-12", "2026-01-14"),
    ("MOCK-USD", "2026-01-13T00:10:00+00:00", "2026-01-12", "2026-01-14"),
])
def test_other_market_day_never_replays_breach(symbol, now, stale, future):
    for label in (stale, future):
        result = check_immediate_triggers(symbol, bars(label), 98.0, now_utc=at(now))
        assert result.triggers == []


@pytest.mark.parametrize("label", [
    pd.NaT, None, float("nan"), 0, 1768176000000000000, True,
    "not-a-date", "2026-13-12", "", "NaT", np.datetime64("NaT"),
])
def test_invalid_daily_label_cannot_bypass_freshness(label):
    result = check_immediate_triggers(
        "MOCK", bars(label), 98.0, now_utc=at("2026-01-13T00:10:00+00:00"))
    assert result.triggers == []


@pytest.mark.parametrize("label", [
    date(2026, 1, 12), datetime(2026, 1, 12), "2026-01-12",
    pd.Timestamp("2026-01-12T00:00:00+00:00"),
    pd.Timestamp("2026-01-12T00:00:00+09:00"),
    pd.Timestamp("2026-01-12T23:30:00-05:00"),
])
def test_daily_labels_keep_their_displayed_date(label):
    # Converting the label to New York or UTC would move these boundary labels.
    result = check_immediate_triggers(
        "MOCK", bars(label), 98.0, now_utc=at("2026-01-13T00:10:00+00:00"))
    assert result.triggers[0].startswith("STOP BREACH")


def test_default_clock_preserves_existing_positional_call(monkeypatch):
    import market_dates
    monkeypatch.setattr(market_dates, "utc_now",
                        lambda now_utc=None: datetime(2026, 1, 13, 0, 10, tzinfo=timezone.utc))
    assert check_immediate_triggers("MOCK", bars("2026-01-12"), 98.0).has_trigger


def test_naive_reference_clock_is_rejected():
    with pytest.raises(ValueError):
        check_immediate_triggers("MOCK", bars("2026-01-12"), 98.0,
                                 now_utc=datetime(2026, 1, 12, 18))


@pytest.mark.parametrize("last_open", [float("nan"), 0.0])
def test_partial_open_still_allows_stop_breach(last_open):
    result = check_immediate_triggers(
        "MOCK", bars("2026-01-12", last_open=last_open), 98.0,
        now_utc=at("2026-01-13T00:10:00+00:00"))
    assert len(result.triggers) == 1
    assert result.triggers[0].startswith("STOP BREACH")
