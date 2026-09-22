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

from atr_calculator import atr_input_issue, calc_chandelier_stop
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


def test_other_input_failures_keep_their_own_reason_codes():
    empty = pd.DataFrame(columns=["High", "Low", "Close"])
    assert atr_input_issue(empty) == "empty_input"

    negative = bars()
    negative.loc[negative.index[3], "Low"] = -1.0
    assert atr_input_issue(negative) == "non_positive_prices"

    short = bars(MIN_ROWS - 1)
    assert atr_input_issue(short) == "insufficient_history"
