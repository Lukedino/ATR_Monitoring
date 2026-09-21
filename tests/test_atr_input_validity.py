"""Synthetic ATR input and freshness regressions; no quotes, state or notifications."""
import math
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import atr_calculator as A


def bars(rows=80):
    index = pd.date_range("2025-01-01", periods=rows, freq="D")
    width = np.where(np.arange(rows) < rows // 2, 1.0, 5.0)
    return pd.DataFrame({"Open": 100.0, "High": 100.0 + width,
                         "Low": 100.0 - width, "Close": 100.0,
                         "Volume": 1000.0}, index=index)


@pytest.mark.parametrize("position", [0, 39, 79])
@pytest.mark.parametrize("column", ["High", "Low", "Close"])
@pytest.mark.parametrize("invalid,reason", [
    (float("nan"), "non_finite_prices"), (float("inf"), "non_finite_prices"),
    (-float("inf"), "non_finite_prices"), (None, "non_finite_prices"),
    (0.0, "non_positive_prices"), (-2.0, "non_positive_prices"),
    ("synthetic-invalid", "non_numeric_prices"), (True, "non_numeric_prices"),
])
def test_invalid_price_at_any_history_position_cannot_produce_a_stop(position, column, invalid, reason):
    frame = bars().astype(object)
    frame.iloc[position, frame.columns.get_loc(column)] = invalid
    original = frame.copy(deep=True)
    assert A.atr_input_issue(frame) == reason
    assert A.calc_true_range(frame).empty
    assert A.calc_atr(frame).empty
    assert A.calc_atr_pct(frame).empty
    assert A.calc_chandelier_stop("SYM-ATR", frame) is None
    assert A.summarize_portfolio_atr({"SYM-ATR": frame}).empty
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("column,value", [("High", 90.0), ("Low", 110.0), ("Close", 80.0)])
def test_inconsistent_ohlc_is_unusable_even_when_every_value_is_finite(column, value):
    frame = bars()
    frame.loc[frame.index[39], column] = value
    assert A.atr_input_issue(frame) == "inconsistent_prices"
    assert A.calc_atr(frame).empty
    assert A.calc_chandelier_stop("SYM-ATR", frame) is None


@pytest.mark.parametrize("rows", [0, 1, 10, 13, 14, 20])
def test_short_history_never_produces_a_partial_chandelier_result(rows):
    frame = bars(rows)
    assert A.atr_input_issue(frame) == ("empty_input" if rows == 0 else "insufficient_history")
    assert A.calc_chandelier_stop("SYM-ATR", frame) is None
    assert A.summarize_portfolio_atr({"SYM-ATR": frame}).empty
    if rows < 14:
        assert A.calc_atr(frame).empty
        assert A.calc_atr_pct(frame).empty


def reference_wilder(frame, period):
    true_ranges = []
    for position, row in enumerate(frame.itertuples()):
        values = [row.High - row.Low]
        if position:
            previous_close = float(frame.iloc[position - 1]["Close"])
            values.extend([abs(row.High - previous_close), abs(row.Low - previous_close)])
        true_ranges.append(max(values))
    expected = [float("nan")] * (period - 1)
    expected.append(sum(true_ranges[:period]) / period)
    for value in true_ranges[period:]:
        expected.append((expected[-1] * (period - 1) + value) / period)
    return np.array(expected)


@pytest.mark.parametrize("period", [2, 7, 14, 30])
def test_normal_wilder_initialization_recurrence_and_stop_stay_unchanged(period):
    frame = bars()
    expected = reference_wilder(frame, period)
    atr = A.calc_atr(frame, period)
    np.testing.assert_allclose(atr.to_numpy(), expected, rtol=1e-14, equal_nan=True)
    np.testing.assert_allclose(A.calc_atr_pct(frame, period).to_numpy(), expected,
                               rtol=1e-14, equal_nan=True)  # Close is exactly 100.
    result = A.calc_chandelier_stop("SYM-ATR", frame, period)
    assert result is not None
    assert result.atr == round(expected[-1], 4)
    assert result.multiple == A.get_atr_multiple("SYM-ATR", expected[-1])
    assert result.stop_level == round(105.0 - expected[-1] * result.multiple, 4)
    assert result.current_close == 100.0
    assert result.ema_21 == 100.0


def test_reported_mid_history_nan_reproduction_never_reuses_the_older_low_atr():
    frame = bars()
    healthy = A.calc_chandelier_stop("SYM-ATR", frame)
    assert healthy.atr == pytest.approx(9.5872, abs=.0001)
    frame.loc[frame.index[40], ["High", "Low"]] = np.nan
    assert A.calc_chandelier_stop("SYM-ATR", frame) is None
    assert A.calc_atr(frame).empty


@pytest.mark.parametrize("defect", ["nan_tail", "inf_tail", "negative_tail", "shortened_index", "wrong_index"])
def test_chandelier_does_not_fall_back_when_latest_computed_atr_is_unusable(monkeypatch, defect):
    frame = bars()
    series = A.calc_atr(frame)
    if defect == "nan_tail":
        series.iloc[-1] = np.nan
    elif defect == "inf_tail":
        series.iloc[-1] = np.inf
    elif defect == "negative_tail":
        series.iloc[-1] = -1.0
    elif defect == "shortened_index":
        series = series.iloc[:-1]
    else:
        series.index = series.index + pd.Timedelta(days=1)
    monkeypatch.setattr(A, "calc_atr", lambda *args, **kwargs: series.copy())
    assert A.calc_chandelier_stop("SYM-ATR", frame) is None
    assert A.summarize_portfolio_atr({"SYM-ATR": frame}).empty


@pytest.mark.parametrize("defect", [np.nan, np.inf, -1.0])
def test_spike_detection_does_not_collapse_invalid_tail_or_internal_history(defect):
    series = pd.Series([1.0] * 30 + [5.0])
    assert A.is_atr_spike(series)
    for position in (-1, -10):
        damaged = series.copy()
        damaged.iloc[position] = defect
        assert not A.is_atr_spike(damaged)


def test_summary_keeps_healthy_symbols_and_excludes_uncomputable_inputs():
    invalid = bars()
    invalid.loc[invalid.index[39], "Close"] = np.nan
    summary = A.summarize_portfolio_atr({"SYM-VALID": bars(), "SYM-INVALID": invalid,
                                       "SYM-SHORT": bars(10)})
    assert summary["Symbol"].tolist() == ["SYM-VALID"]
    assert math.isfinite(summary.iloc[0]["ATR"])
    assert math.isfinite(summary.iloc[0]["StopLevel"])


def test_hlc_only_numeric_text_and_flat_prices_keep_supported_calculation_contracts():
    frame = bars().drop(columns=["Open", "Volume"]).astype(str)
    assert A.atr_input_issue(frame) is None
    assert A.calc_chandelier_stop("SYM-ATR", frame).atr == pytest.approx(9.5872, abs=.0001)
    flat = bars()
    flat[["Open", "High", "Low", "Close"]] = 100.0
    result = A.calc_chandelier_stop("SYM-ATR", flat)
    assert result.atr == 0.0  # Zero volatility is valid; zero prices are not.
    assert result.stop_level == 100.0


@pytest.mark.parametrize("opening", [np.nan, 0.0, 120.0])
def test_open_is_not_an_atr_input_and_does_not_block_valid_hlc(opening):
    frame = bars()
    reference = A.calc_atr(frame)
    expected_stop = A.calc_chandelier_stop("SYM-ATR", frame)
    frame["Open"] = opening
    assert A.atr_input_issue(frame) is None
    pd.testing.assert_series_equal(A.calc_atr(frame), reference)
    assert A.calc_chandelier_stop("SYM-ATR", frame) == expected_stop
    assert not A.summarize_portfolio_atr({"SYM-ATR": frame}).empty


def test_collector_current_quote_without_open_retains_valid_atr_and_stop():
    from data_collector import _sync_latest_quote

    frame = bars()
    today = (frame.index[-1] + pd.Timedelta(days=1)).date()
    now = pd.Timestamp(today, tz="America/New_York") + pd.Timedelta(hours=12)
    metadata = {"regularMarketTime": now.timestamp(), "regularMarketPrice": 103.0,
                "regularMarketDayHigh": 105.0, "regularMarketDayLow": 95.0,
                "regularMarketVolume": 1000}
    ticker = SimpleNamespace(get_history_metadata=lambda: metadata)
    collected = _sync_latest_quote(ticker, "SYM-ATR", frame, now_utc=now.to_pydatetime())
    assert len(collected) == len(frame) + 1
    assert pd.isna(collected["Open"].iloc[-1])
    assert A.atr_input_issue(collected) is None
    expected = reference_wilder(collected, 14)
    np.testing.assert_allclose(A.calc_atr(collected).to_numpy(), expected,
                               rtol=1e-14, equal_nan=True)
    result = A.calc_chandelier_stop("SYM-ATR", collected)
    assert result is not None
    assert result.current_close == 103.0
    assert result.atr == round(expected[-1], 4)


@pytest.mark.parametrize("defect", ["duplicate", "reverse", "missing"])
def test_ambiguous_bar_index_is_rejected_without_guessing_order(defect):
    frame = bars()
    if defect == "duplicate":
        frame.index = pd.DatetimeIndex([frame.index[0], *frame.index[:-1]])
    elif defect == "reverse":
        frame = frame.iloc[::-1]
    else:
        frame.index = pd.DatetimeIndex([pd.NaT, *frame.index[1:]])
    assert A.atr_input_issue(frame) == "invalid_index"
    assert A.calc_chandelier_stop("SYM-ATR", frame) is None


def test_missing_column_and_non_frame_return_the_existing_failure_shapes():
    for frame, reason in [(bars().drop(columns="Low"), "missing_price_columns"),
                          (None, "empty_input")]:
        assert A.atr_input_issue(frame) == reason
        assert A.calc_atr(frame).empty
        assert A.calc_atr_pct(frame).empty
        assert A.calc_chandelier_stop("SYM-ATR", frame) is None


@pytest.mark.parametrize("period", [0, -1, 1.5, True])
def test_invalid_period_is_a_clean_failure_without_redefining_the_strategy(period):
    assert A.atr_input_issue(bars(), period) == "invalid_period"
    assert A.calc_atr(bars(), period).empty
    assert A.calc_chandelier_stop("SYM-ATR", bars(), period) is None
