"""Synthetic provider responses only; real quote/network access is never needed."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

import data_collector as collector


def instant(value="2026-01-13T00:10:00+00:00"):
    return datetime.fromisoformat(value)


def bars(last="2026-01-12", *, close=100.0, timezone=None):
    index = pd.date_range(end=last, periods=30, tz=timezone)
    return pd.DataFrame({"Open": 100.0, "High": 110.0, "Low": 90.0,
                         "Close": close, "Volume": 1000.0}, index=index)


def quote(now=None, **changes):
    result = {"regularMarketTime": (now or instant()).timestamp(),
              "regularMarketPrice": 103.0, "regularMarketDayHigh": 105.0,
              "regularMarketDayLow": 95.0, "regularMarketVolume": 2000}
    result.update(changes)
    return result


class Ticker:
    def __init__(self, frame, metadata, *, fast_price=100.0, error=None):
        self.frame = frame
        self.metadata = metadata
        self.fast_price = fast_price
        self.error = error
        self.history_calls = []
        self.metadata_calls = 0
        self.fast_info_calls = 0

    def history(self, **kwargs):
        self.history_calls.append(kwargs)
        return self.frame.copy(deep=True)

    def get_history_metadata(self):
        self.metadata_calls += 1
        if self.error:
            raise self.error
        return self.metadata

    @property
    def fast_info(self):
        self.fast_info_calls += 1
        return SimpleNamespace(last_price=self.fast_price, day_high=999.0,
                               day_low=1.0, open=999.0, last_volume=999999)


@pytest.fixture(autouse=True)
def synthetic_sources_only(monkeypatch):
    monkeypatch.setattr(collector, "_fetch_naver_kr_price", lambda code: None)
    monkeypatch.setattr(collector.yf, "Ticker", lambda symbol: pytest.fail("Use a synthetic ticker"))


@pytest.mark.parametrize("now,last", [
    ("2026-01-13T00:10:00+00:00", "2026-01-12"),  # EST evening
    ("2026-07-14T00:10:00+00:00", "2026-07-13"),  # EDT evening
    ("2026-01-17T00:10:00+00:00", "2026-01-16"),  # Friday ET, Saturday UTC
])
def test_us_evening_updates_its_existing_market_day_instead_of_utc_tomorrow(now, last):
    frame = bars(last)
    original = frame.copy(deep=True)
    ticker = Ticker(frame, quote(instant(now)))
    result = collector._sync_latest_quote(ticker, "SYM-US", frame, now_utc=instant(now))
    assert len(result) == len(frame)
    assert result.index[-1].date().isoformat() == last
    assert result["Close"].iloc[-1] == 103.0
    assert result["High"].iloc[-1] == 110.0  # Preserve earlier same-day extrema.
    assert result["Low"].iloc[-1] == 90.0
    assert ticker.fast_info_calls == 0
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("symbol,now,quote_time,last", [
    ("999991.KS", "2026-01-12T23:30:00+00:00", "2026-01-12T06:00:00+00:00", "2026-01-12"),
    ("SYM-US", "2026-01-19T13:30:00+00:00", "2026-01-16T21:00:00+00:00", "2026-01-16"),
    ("SYM-US", "2026-01-17T17:00:00+00:00", "2026-01-16T21:00:00+00:00", "2026-01-16"),
    ("FAKE-USD", "2026-01-13T00:10:00+00:00", "2026-01-12T23:59:00+00:00", "2026-01-12"),
])
def test_previous_session_quote_cannot_create_a_premarket_holiday_weekend_or_crypto_bar(symbol, now, quote_time, last):
    frame = bars(last)
    ticker = Ticker(frame, quote(instant(quote_time)))
    result = collector._sync_latest_quote(ticker, symbol, frame, now_utc=instant(now))
    assert result is frame
    assert ticker.fast_info_calls == 0


@pytest.mark.parametrize("symbol,now,expected", [
    ("999991.KS", "2026-01-13T00:30:00+00:00", "2026-01-13"),
    ("999992.KQ", "2026-01-13T00:30:00+00:00", "2026-01-13"),
    ("SYM-US", "2026-01-13T00:10:00+00:00", "2026-01-12"),
    ("FAKE-USD", "2026-01-12T23:30:00+00:00", "2026-01-12"),
])
def test_confirmed_current_quote_can_add_one_market_bar_even_at_unchanged_price(symbol, now, expected):
    frame = bars((pd.Timestamp(expected) - pd.Timedelta(days=1)).date())
    original = frame.copy(deep=True)
    frame.attrs["synthetic_test_marker"] = "preserved"
    ticker = Ticker(frame, quote(instant(now), regularMarketPrice=100.0), fast_price=999.0)
    result = collector._sync_latest_quote(ticker, symbol, frame, now_utc=instant(now))
    assert len(result) == len(frame) + 1
    assert result.index[-1].date().isoformat() == expected
    assert result.index.is_unique and result.index.is_monotonic_increasing
    assert pd.isna(result["Open"].iloc[-1])
    assert result["High"].iloc[-1] == 105.0
    assert result["Low"].iloc[-1] == 95.0
    assert result["Close"].iloc[-1] == 100.0
    assert result["Volume"].iloc[-1] == 2000
    assert result.attrs == frame.attrs
    assert ticker.metadata_calls == 1 and ticker.fast_info_calls == 0
    original.attrs = dict(frame.attrs)
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("same_day", [False, True])
@pytest.mark.parametrize("field", ["regularMarketTime", "regularMarketPrice", "regularMarketDayHigh", "regularMarketDayLow"])
def test_missing_timestamp_or_price_field_preserves_both_new_and_existing_bars(field, same_day):
    frame = bars("2026-01-12" if same_day else "2026-01-11")
    metadata = quote()
    del metadata[field]
    ticker = Ticker(frame, metadata)
    assert collector._sync_latest_quote(ticker, "SYM-US", frame, now_utc=instant()) is frame
    assert ticker.fast_info_calls == 0


@pytest.mark.parametrize("field", ["regularMarketTime", "regularMarketPrice", "regularMarketDayHigh", "regularMarketDayLow"])
@pytest.mark.parametrize("value", [None, True, "103", 0, -1, float("nan"), float("inf")])
def test_invalid_quote_fields_never_modify_history(field, value):
    frame = bars("2026-01-11")
    ticker = Ticker(frame, quote(**{field: value}))
    assert collector._sync_latest_quote(ticker, "SYM-US", frame, now_utc=instant()) is frame


@pytest.mark.parametrize("changes", [
    {"regularMarketPrice": 106.0}, {"regularMarketPrice": 94.0},
    {"regularMarketDayHigh": 94.0}, {"regularMarketDayLow": 106.0},
    {"regularMarketTime": instant().timestamp() + 1},
    {"regularMarketTime": 10**100},
    {"regularMarketTime": (instant() - timedelta(days=1)).timestamp()},
])
@pytest.mark.parametrize("last", ["2026-01-11", "2026-01-12"])
def test_inconsistent_future_or_old_quote_preserves_history(changes, last):
    frame = bars(last)
    ticker = Ticker(frame, quote(**changes))
    assert collector._sync_latest_quote(ticker, "SYM-US", frame, now_utc=instant()) is frame


@pytest.mark.parametrize("last", [pd.Timestamp("2026-01-13"), pd.NaT, "not-a-date", 123])
def test_future_or_invalid_last_bar_never_fetches_or_appends_a_quote(last):
    frame = bars()
    frame.index = pd.Index([*frame.index[:-1], last])
    ticker = Ticker(frame, quote())
    assert collector._sync_latest_quote(ticker, "SYM-US", frame, now_utc=instant()) is frame
    assert ticker.metadata_calls == ticker.fast_info_calls == 0


@pytest.mark.parametrize("value", [None, 0, -1, float("nan"), float("inf"), True, "1000", 1.5])
def test_missing_or_invalid_volume_stays_zero_without_borrowing_fast_info(value):
    frame = bars("2026-01-11")
    ticker = Ticker(frame, quote(regularMarketVolume=value))
    result = collector._sync_latest_quote(ticker, "SYM-US", frame, now_utc=instant())
    assert len(result) == len(frame) + 1
    assert result["Volume"].iloc[-1] == 0
    assert ticker.fast_info_calls == 0


def test_same_day_tiny_change_retains_existing_policy():
    frame = bars()
    ticker = Ticker(frame, quote(regularMarketPrice=100.05))
    assert collector._sync_latest_quote(ticker, "SYM-US", frame, now_utc=instant()) is frame


@pytest.mark.parametrize("last", ["2026-01-12", "2026-01-13"])
def test_kr_split_guard_still_preserves_large_price_change(last):
    frame = bars(last)
    now = instant("2026-01-13T01:00:00+00:00")
    ticker = Ticker(frame, quote(now, regularMarketPrice=132.0, regularMarketDayHigh=140.0,
                                regularMarketDayLow=130.0))
    assert collector._sync_latest_quote(ticker, "999991.KS", frame, now_utc=now) is frame


@pytest.mark.parametrize("naver_price,append", [(103.0, True), (107.0, True), (None, True), (120.0, False)])
def test_kr_crosscheck_can_reject_but_never_substitute_unstamped_price(monkeypatch, naver_price, append):
    frame = bars()
    now = instant("2026-01-13T01:00:00+00:00")
    calls = []
    monkeypatch.setattr(collector, "_fetch_naver_kr_price", lambda code: calls.append(code) or naver_price)
    ticker = Ticker(frame, quote(now))
    result = collector._sync_latest_quote(ticker, "999991.KS", frame, now_utc=now)
    assert calls == ["999991"]
    if append:
        assert len(result) == len(frame) + 1
        assert result["Close"].iloc[-1] == 103.0
    else:
        assert result is frame


@pytest.mark.parametrize("metadata", [None, [], {}, {"regularMarketPrice": 103.0}])
def test_unavailable_metadata_is_not_replaced_with_fast_info(metadata):
    frame = bars("2026-01-11")
    ticker = Ticker(frame, metadata)
    assert collector._sync_latest_quote(ticker, "SYM-US", frame, now_utc=instant()) is frame
    assert ticker.fast_info_calls == 0


def test_metadata_exception_logs_only_its_type(caplog):
    frame = bars()
    ticker = Ticker(frame, None, error=ValueError("SYNTHETIC-PRIVATE https://synthetic.invalid/token"))
    assert collector._sync_latest_quote(ticker, "SYM-US", frame, now_utc=instant()) is frame
    assert "ValueError" in caplog.text
    assert "SYNTHETIC-PRIVATE" not in caplog.text
    assert "synthetic.invalid" not in caplog.text


def test_quote_generated_during_request_is_valid_before_receipt_on_same_market_day():
    start = instant()
    frame = bars()
    ticker = Ticker(frame, quote(start + timedelta(seconds=2)))
    result = collector._sync_latest_quote(ticker, "SYM-US", frame, now_utc=start,
                                          receipt_clock=lambda: start + timedelta(seconds=5))
    assert result["Close"].iloc[-1] == 103.0
    assert result.index[-1].date() == frame.index[-1].date()


@pytest.mark.parametrize("quote_offset,receipt_offset", [(6, 5), (0, -1)])
def test_actual_future_quote_or_backward_receipt_clock_preserves_history(quote_offset, receipt_offset):
    start = instant()
    frame = bars()
    ticker = Ticker(frame, quote(start + timedelta(seconds=quote_offset)))
    result = collector._sync_latest_quote(ticker, "SYM-US", frame, now_utc=start,
                                          receipt_clock=lambda: start + timedelta(seconds=receipt_offset))
    assert result is frame


def test_receipt_after_market_midnight_does_not_change_the_starting_bar_date():
    start = instant("2026-01-13T04:59:59+00:00")  # NY January 12, 23:59:59.
    frame = bars()
    ticker = Ticker(frame, quote(start + timedelta(seconds=2)))
    result = collector._sync_latest_quote(ticker, "SYM-US", frame, now_utc=start,
                                          receipt_clock=lambda: start + timedelta(seconds=5))
    assert result is frame


def test_direct_helper_without_injected_time_samples_receipt_after_metadata(monkeypatch):
    start = instant()
    events = []
    clock_values = iter([start, start + timedelta(seconds=5)])
    def clock(value=None):
        assert value is None
        events.append("clock")
        return next(clock_values)
    ticker = Ticker(bars(), quote(start + timedelta(seconds=2)))
    def metadata():
        events.append("metadata")
        return ticker.metadata
    monkeypatch.setattr(collector, "utc_now", clock)
    monkeypatch.setattr(ticker, "get_history_metadata", metadata)
    result = collector._sync_latest_quote(ticker, "SYM-US", ticker.frame)
    assert result["Close"].iloc[-1] == 103.0
    assert events == ["clock", "metadata", "clock"]


@pytest.mark.parametrize("symbol,now,expected,tz", [
    ("SYM-US", "2026-01-13T00:10:00+00:00", "2026-01-12", "America/New_York"),
    ("SYM-US", "2026-07-14T00:10:00+00:00", "2026-07-13", "America/New_York"),
    ("999991.KS", "2026-01-12T23:30:00+00:00", "2026-01-13", "Asia/Seoul"),
    ("FAKE-USD", "2026-01-12T23:30:00+00:00", "2026-01-12", "UTC"),
])
def test_fetch_uses_one_market_date_for_range_quote_and_stale_warning(monkeypatch, symbol, now, expected, tz):
    now = instant(now)
    frame = bars(expected, timezone=tz)
    ticker = Ticker(frame, quote(now))
    monkeypatch.setattr(collector.yf, "Ticker", lambda requested: ticker)
    captured = []
    real_clock = collector.utc_now
    def clock(value=None):
        captured.append(value)
        return now if value is None else real_clock(value)
    monkeypatch.setattr(collector, "utc_now", clock)
    stale = []
    monkeypatch.setattr(collector, "_is_stale", lambda last, today: stale.append((last, today)) or False)
    result = collector.fetch_ohlcv(symbol, lookback_days=10)
    expected_date = pd.Timestamp(expected).date()
    # One clock sample fixes the market date; the second checks receipt time.
    assert captured.count(None) == 2
    assert captured == [None, now, None, now]
    assert ticker.history_calls == [{"start": (expected_date - timedelta(days=10)).isoformat(),
                                     "end": (expected_date + timedelta(days=1)).isoformat(),
                                     "auto_adjust": True}]
    assert stale == [(expected_date, expected_date)]
    assert result.index.tz is None
    assert result.index[-1].date() == expected_date
    assert len(result) == len(frame)


def test_fetch_rejects_naive_clock_before_querying_the_provider():
    with pytest.raises(ValueError):
        collector.fetch_ohlcv("SYM-US", now_utc=datetime(2026, 1, 13))


def test_kr_suffix_correction_preserves_each_markets_adjustment_and_shared_range(monkeypatch):
    now = instant("2026-01-13T01:00:00+00:00")
    first = Ticker(pd.DataFrame(), None)
    alternate = Ticker(bars("2026-01-13", timezone="Asia/Seoul"), quote(now))
    tickers = {"999991.KS": first, "999991.KQ": alternate}
    monkeypatch.setattr(collector.yf, "Ticker", lambda symbol: tickers[symbol])
    result = collector.fetch_ohlcv("999991.KS", lookback_days=10, now_utc=now)
    assert not result.empty
    assert first.history_calls == [{"start": "2026-01-03", "end": "2026-01-14", "auto_adjust": True}]
    assert alternate.history_calls == [{"start": "2026-01-03", "end": "2026-01-14", "auto_adjust": False}]
    assert alternate.metadata_calls == 1


def test_crypto_suffix_resolution_keeps_the_original_symbol_bar_timezone(monkeypatch):
    now = instant("2026-01-13T00:10:00+00:00")
    first = Ticker(pd.DataFrame(), None)
    alternate = Ticker(bars("2026-01-12", timezone="UTC"), quote(now))
    tickers = {"FAKE-USD": first, "FAKE123-USD": alternate}
    monkeypatch.setattr(collector.yf, "Ticker", lambda symbol: tickers[symbol])
    monkeypatch.setattr(collector, "_resolve_yahoo_crypto_ticker", lambda symbol: "FAKE123-USD")
    result = collector.fetch_ohlcv("FAKE-USD", lookback_days=10, now_utc=now)
    assert result.index[-1].date().isoformat() == "2026-01-13"
    assert first.history_calls == alternate.history_calls
    assert alternate.history_calls[0]["auto_adjust"] is True
