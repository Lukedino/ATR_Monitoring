"""진행 중인 봉이 확정 이력의 ATR/Chandelier 계산을 막지 않는지 검증한다.

2026-09-22 19:30Z 실행의 `history_normalized.close_above_high.latest.first_observed`
진단으로, 실패 원인이 공급자가 준 마지막 봉 하나임을 확인했다. 장중에는 High 가 아직
갱신되지 않은 상태에서 Close(현재가)가 그 값을 넘을 수 있고, 장 마감 후에는 사라진다.

계약: 모순이 **마지막 행에만** 있으면 그 미확정 봉을 빼고 확정 이력으로 계산한다.
과거 행에 모순이 있으면 공급자 자료 오류이므로 기존대로 거절한다. 현재가는 잘라내기와
무관하게 항상 원본 마지막 행이어야 한다 — 옛 종가로 Stop 거리를 재면 위험 신호를 놓친다.
"""
import numpy as np
import pandas as pd
import pytest

from atr_calculator import (atr_input_issue, calc_atr, calc_atr_pct, calc_chandelier_stop,
                            summarize_portfolio_atr)
from config import ATR_PERIOD

HH_WINDOW = 20
MIN_ROWS = max(ATR_PERIOD, HH_WINDOW) + 1


def bars(rows=40):
    close = np.arange(rows, dtype=float) + 100.0
    frame = pd.DataFrame(
        {"High": close + 2.0, "Low": close - 2.0, "Close": close,
         "Open": close - 0.5, "Volume": np.full(rows, 1000.0)},
        index=pd.date_range("2026-01-01", periods=rows),
    )
    frame.index.name = "Date"
    return frame


def break_row(frame, position, *, above=True):
    """해당 행의 Close 를 High 위(또는 Low 아래)로 밀어 관계 모순을 만든다."""
    frame = frame.copy()
    label = frame.index[position]
    column, offset = ("High", 5.0) if above else ("Low", -5.0)
    frame.loc[label, "Close"] = float(frame[column].iloc[position]) + offset
    return frame


@pytest.mark.parametrize("above", [True, False])
def test_unconfirmed_last_bar_no_longer_blocks_the_calculation(above):
    frame = break_row(bars(), -1, above=above)
    original = frame.copy(deep=True)

    assert atr_input_issue(frame) is None
    result = calc_chandelier_stop("AAPL", frame)

    assert result is not None
    pd.testing.assert_frame_equal(frame, original)


def test_confirmed_history_supplies_the_stop_and_the_raw_bar_supplies_current_price():
    frame = break_row(bars(), -1, above=True)
    confirmed = frame.iloc[:-1]

    result = calc_chandelier_stop("AAPL", frame)

    assert result is not None
    # Highest High 와 EMA 는 확정된 봉까지만 본다.
    assert result.highest_high == pytest.approx(
        round(float(confirmed["High"].iloc[-HH_WINDOW:].max()), 4))
    # 현재가는 원본 마지막 행 그대로 — 전일 종가로 대체하지 않는다.
    assert result.current_close == pytest.approx(round(float(frame["Close"].iloc[-1]), 4))
    # 두 값이 같은 행에서 왔다면 잘라내기가 현재가까지 물러난 것이다.
    assert result.current_close != pytest.approx(round(float(confirmed["Close"].iloc[-1]), 4))


def test_clean_frame_keeps_the_existing_result():
    frame = bars()
    result = calc_chandelier_stop("AAPL", frame)

    assert result is not None
    assert result.highest_high == pytest.approx(
        round(float(frame["High"].iloc[-HH_WINDOW:].max()), 4))
    assert result.current_close == pytest.approx(round(float(frame["Close"].iloc[-1]), 4))
    assert atr_input_issue(frame) is None


@pytest.mark.parametrize("positions", [(5,), (5, -1), (0,), (-2,)])
def test_inconsistency_outside_the_last_bar_is_still_rejected(positions):
    frame = bars()
    for position in positions:
        frame = break_row(frame, position, above=True)

    assert atr_input_issue(frame) == "inconsistent_prices"
    assert calc_chandelier_stop("AAPL", frame) is None


def test_trimming_that_leaves_too_little_history_is_reported_as_insufficient():
    frame = break_row(bars(MIN_ROWS), -1, above=True)

    assert atr_input_issue(frame) == "insufficient_history"
    assert calc_chandelier_stop("AAPL", frame) is None


def test_single_inconsistent_bar_is_not_silently_accepted():
    frame = break_row(bars(1), -1, above=True)

    assert atr_input_issue(frame) is not None
    assert calc_chandelier_stop("AAPL", frame) is None


def blank_high_low(frame, position):
    """수집기가 공급자의 High/Low=0 을 NaN 으로 바꾼 부분 봉 모양을 만든다."""
    frame = frame.copy()
    label = frame.index[position]
    frame.loc[label, ["Open", "High", "Low"]] = np.nan
    return frame


# 2026-09-28 00:10Z·18:10Z 실행: 추석 연휴 뒤 국내 23종목이 전부 `non_finite_prices` 로 거절됐다.
# 공급자가 개장 전 Open/High/Low=0·Close=전일 종가인 부분 봉을 주고(3ca1f53), 수집기가 그 0 을
# NaN 으로 바꾼다. 마지막 봉 하나의 결측이므로 관계 모순과 같은 미확정 봉 계약을 따른다.
def test_partial_last_bar_without_high_low_no_longer_blocks_the_calculation():
    frame = blank_high_low(bars(), -1)
    original = frame.copy(deep=True)
    confirmed = frame.iloc[:-1]

    assert atr_input_issue(frame) is None
    result = calc_chandelier_stop("005930.KS", frame)

    assert result is not None
    assert result.highest_high == pytest.approx(
        round(float(confirmed["High"].iloc[-HH_WINDOW:].max()), 4))
    assert result.current_close == pytest.approx(round(float(frame["Close"].iloc[-1]), 4))
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("positions", [(5,), (5, -1), (0,), (-2,)])
def test_missing_prices_outside_the_last_bar_are_still_rejected(positions):
    frame = bars()
    for position in positions:
        frame = blank_high_low(frame, position)

    assert atr_input_issue(frame) == "non_finite_prices"
    assert calc_chandelier_stop("005930.KS", frame) is None


def test_partial_last_bar_without_a_close_is_not_accepted():
    frame = bars()
    frame.loc[frame.index[-1], ["High", "Low", "Close"]] = np.nan

    assert calc_chandelier_stop("005930.KS", frame) is None


def test_provider_pre_open_partial_bar_survives_the_collector_and_the_calculator(monkeypatch):
    """수집기의 0→NaN 변환과 계산기의 결측 거절이 만나는 실제 경로를 합성 응답으로 고정한다."""
    from datetime import datetime
    from types import SimpleNamespace

    import data_collector as collector

    index = pd.bdate_range(end="2026-09-28", periods=60, tz="Asia/Seoul")
    provider = pd.DataFrame({"Open": 100.0, "High": 110.0, "Low": 90.0, "Close": 100.0,
                             "Volume": 1000.0, "Dividends": 0.0, "Stock Splits": 0.0}, index=index)
    partial = pd.DataFrame({"Open": 0.0, "High": 0.0, "Low": 0.0, "Close": 100.0,
                            "Volume": 0.0, "Dividends": 0.0, "Stock Splits": 0.0},
                           index=pd.DatetimeIndex([pd.Timestamp("2026-09-29", tz="Asia/Seoul")]))
    provider = pd.concat([provider, partial])

    ticker = SimpleNamespace(history=lambda **kwargs: provider.copy(deep=True),
                             get_history_metadata=lambda: {},
                             fast_info=SimpleNamespace(last_price=100.0))
    monkeypatch.setattr(collector.yf, "Ticker", lambda symbol: ticker)
    monkeypatch.setattr(collector, "_fetch_naver_kr_price", lambda code: None)

    frame = collector.fetch_ohlcv(
        "005930.KS", now_utc=datetime.fromisoformat("2026-09-28T18:10:00+00:00"))

    assert frame.index[-1].date().isoformat() == "2026-09-29"
    assert atr_input_issue(frame) is None
    assert calc_chandelier_stop("005930.KS", frame) is not None


# 포트폴리오 요약(주간 리포트·종가 요약 스파이크 집계)도 Stop 과 같은 확정 봉 계약을 따른다.
# 원본 프레임으로 ATR 을 계산하던 동안에는 미확정 마지막 봉 종목이 요약에서 조용히 빠졌다.
UNCONFIRMED_LAST_BAR = {
    "inconsistent": lambda frame: break_row(frame, -1, above=True),
    "partial": lambda frame: blank_high_low(frame, -1),
}


@pytest.mark.parametrize("shape", sorted(UNCONFIRMED_LAST_BAR))
def test_portfolio_summary_keeps_a_symbol_whose_last_bar_is_unconfirmed(shape):
    frame = UNCONFIRMED_LAST_BAR[shape](bars())
    original = frame.copy(deep=True)
    confirmed = frame.iloc[:-1]

    summary = summarize_portfolio_atr({"005930.KS": frame, "AAPL": bars()})

    assert sorted(summary["Symbol"]) == ["005930.KS", "AAPL"]
    row = summary.set_index("Symbol").loc["005930.KS"]
    stop = calc_chandelier_stop("005930.KS", frame)
    # ATR·ATR% 는 확정 봉까지, 현재가는 원본 마지막 봉 — Stop 계산과 같은 값이어야 한다.
    assert row["ATR"] == pytest.approx(round(float(calc_atr(confirmed).iloc[-1]), 4))
    assert row["ATR%"] == pytest.approx(round(float(calc_atr_pct(confirmed).iloc[-1]), 2))
    assert row["Close"] == pytest.approx(round(float(frame["Close"].iloc[-1]), 4))
    assert row["StopLevel"] == pytest.approx(round(stop.stop_level, 4))
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("shape", sorted(UNCONFIRMED_LAST_BAR))
def test_portfolio_summary_still_excludes_symbols_with_broken_history(shape):
    frame = UNCONFIRMED_LAST_BAR[shape](bars())
    frame = blank_high_low(frame, 5)

    summary = summarize_portfolio_atr({"005930.KS": frame, "AAPL": bars()})

    assert list(summary["Symbol"]) == ["AAPL"]


def test_other_input_failures_keep_their_own_reason_codes():
    empty = pd.DataFrame(columns=["High", "Low", "Close"])
    assert atr_input_issue(empty) == "empty_input"

    negative = bars()
    negative.loc[negative.index[3], "Low"] = -1.0
    assert atr_input_issue(negative) == "non_positive_prices"

    short = bars(MIN_ROWS - 1)
    assert atr_input_issue(short) == "insufficient_history"
