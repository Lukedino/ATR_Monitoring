"""진행 중인 마지막 봉이 있어도 알림 차트가 그려지는지 검증한다.

f2bce3a 는 마지막 행에만 H/L/C 모순이 있으면 계산기가 확정 이력으로 Stop 을 계산하게 했다.
그 뒤로 그런 종목도 트리거 알림이 나가는데, 차트는 여전히 원본 프레임으로 ATR 을 계산해
`calc_atr` 가 빈 시리즈를 돌려주고 `_rolling_chandelier_series` 가 IndexError 를 냈다.
2026-09-23~26 운영 로그의 차트 누락 33건이 전부 이 IndexError 였고, 해당 실행마다
`close_*.latest` 진단이 함께 찍혔다.

계약: 차트의 지표(ATR·ATR%·Stop 궤적·EMA)는 계산기와 같은 확정 이력을 쓰고, 가격선의
마지막 점은 원본 현재가를 그대로 둔다 — 알림의 근거가 그 현재가다.
"""
import numpy as np
import pandas as pd
import pytest

import monitor
import visualizer
from atr_calculator import calc_chandelier_stop

SYMBOL = "ZZQ-USD"   # 합성 심볼 — 실제 보유 종목을 적지 않는다


def bars(rows=80):
    close = np.linspace(100.0, 140.0, rows)
    frame = pd.DataFrame(
        {"Open": close - 0.5, "High": close + 1.0, "Low": close - 1.0,
         "Close": close, "Volume": np.full(rows, 1000.0)},
        index=pd.date_range("2026-06-01", periods=rows),
    )
    frame.index.name = "Date"
    return frame


def break_last(frame, *, above=True):
    """마지막 행의 Close 를 High 위(또는 Low 아래)로 밀어 진행 중인 봉을 흉내 낸다."""
    frame = frame.copy()
    column, offset = ("High", 3.0) if above else ("Low", -3.0)
    frame.iloc[-1, frame.columns.get_loc("Close")] = float(frame[column].iloc[-1]) + offset
    return frame


@pytest.mark.parametrize("above", [True, False])
def test_chart_renders_when_only_the_last_bar_is_unconfirmed(above):
    frame = break_last(bars(), above=above)
    original = frame.copy(deep=True)
    assert calc_chandelier_stop(SYMBOL, frame) is not None   # 알림이 나가는 조건

    png = visualizer.plot_atr_chart(SYMBOL, frame, as_bytes=True)

    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pd.testing.assert_frame_equal(frame, original)


def test_stop_trail_comes_from_confirmed_history():
    frame = break_last(bars(), above=True)

    trail = visualizer._rolling_chandelier_series(SYMBOL, frame)

    assert trail.index.equals(frame.index)
    assert trail.iloc[:-1].notna().any()
    confirmed = visualizer._rolling_chandelier_series(SYMBOL, frame.iloc[:-1])
    pd.testing.assert_series_equal(trail.iloc[:-1], confirmed)


def test_alert_path_sends_the_chart_for_an_unconfirmed_bar(monkeypatch):
    sent = []
    monkeypatch.setattr(monitor.tg, "send_photo",
                        lambda image, caption="": sent.append((image, caption)) or True)

    monitor._send_chart_quietly(SYMBOL, break_last(bars()), None, "synthetic")

    assert len(sent) == 1
    assert sent[0][0][:8] == b"\x89PNG\r\n\x1a\n"
