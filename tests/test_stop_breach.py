"""등록 Stop 이탈 알림 (2026-09-19 심층 검토 ATR-06, 2026-09-20 소유자 결정으로 추가).

예전 트리거 4 는 `0 < dist <= 5%`(근접)만 봤다. 종가가 Stop **아래**로 내려가면 아무것도 붙지 않아,
전날 '근접' 알림을 받은 종목이 다음 날 -5% 미만 하락으로 Stop 을 깨면 정작 이탈 당일은 무음이었다.
"""
import json
from datetime import datetime, timezone

import pandas as pd
import pytest

import atr_calculator
import market_dates
import stop_manager as sm


@pytest.fixture(autouse=True)
def fixed_market_clock(monkeypatch):
    original = market_dates.utc_now
    fixed_now = datetime(2026, 1, 12, 18, tzinfo=timezone.utc)
    monkeypatch.setattr(market_dates, "utc_now",
                        lambda now_utc=None: fixed_now if now_utc is None else original(now_utc))
    monkeypatch.setattr(sm, "utc_now", market_dates.utc_now)


def bars(last_close, base=100.0, rows=30):
    idx = pd.date_range(end=pd.Timestamp("2026-01-12"), periods=rows, freq="D")
    df = pd.DataFrame({"Open": base, "High": base * 1.005, "Low": base * 0.995, "Close": base, "Volume": 1000.0}, index=idx)
    df.iloc[-1, df.columns.get_loc("Close")] = last_close
    df.iloc[-1, df.columns.get_loc("Open")] = base                       # 갭 없음
    df.iloc[-1, df.columns.get_loc("Low")] = min(last_close, base * 0.995)
    df.iloc[-1, df.columns.get_loc("High")] = max(last_close, base * 1.005)
    return df


def kinds(result):
    return [" ".join(t.split()[:2]) for t in result.triggers]


def test_close_below_the_registered_stop_is_a_breach():
    result = atr_calculator.check_immediate_triggers("AAA", bars(97.0), current_stop=98.0)   # -3% 라 SURGE DOWN 아님
    assert kinds(result) == ["STOP BREACH"]
    assert "98" in result.triggers[0]


def test_close_exactly_at_the_stop_counts_as_a_breach():
    assert kinds(atr_calculator.check_immediate_triggers("AAA", bars(98.0), 98.0)) == ["STOP BREACH"]


def test_close_just_above_the_stop_is_still_near():
    assert kinds(atr_calculator.check_immediate_triggers("AAA", bars(99.0), 98.0)) == ["STOP NEAR"]


def test_far_above_the_stop_is_quiet():
    assert atr_calculator.check_immediate_triggers("AAA", bars(100.0), 90.0).triggers == []


def test_a_stop_far_above_the_price_asks_for_a_data_check_instead():
    """4:1 분할 뒤 조정 시세(100)와 옛 Stop(400) — '이탈' 이 아니라 Stop 재등록이 필요한 상황이다."""
    result = atr_calculator.check_immediate_triggers("AAA", bars(100.0), current_stop=400.0)
    assert kinds(result) == ["STOP MISMATCH"]


def test_no_registered_stop_means_no_stop_trigger():
    assert atr_calculator.check_immediate_triggers("AAA", bars(97.0), None).triggers == []


@pytest.fixture
def alert_state(tmp_path, monkeypatch):
    path = tmp_path / "stop_levels.json"
    path.write_text(json.dumps({"positions": {}, "alert_log": {}}), encoding="utf-8")
    monkeypatch.setattr(sm, "DATA_FILE", path)
    return path


def test_near_turning_into_breach_the_same_day_is_sent_again(alert_state):
    near = ["STOP NEAR 1.0% (Stop=98.00)"]
    sm.mark_trigger_sent("AAA", near, 99.0, 98.0)
    assert sm.should_send_trigger_alert("AAA", near, 99.0, 98.0) is False                     # 같은 조건은 중복
    assert sm.should_send_trigger_alert("AAA", ["STOP BREACH 1.0% 하회 (Stop=98.00)"], 97.0, 98.0) is True


def test_the_same_breach_is_not_repeated_within_the_day(alert_state):
    breach = ["STOP BREACH 1.0% 하회 (Stop=98.00)"]
    sm.mark_trigger_sent("AAA", breach, 97.0, 98.0)
    assert sm.should_send_trigger_alert("AAA", ["STOP BREACH 1.2% 하회 (Stop=98.00)"], 96.8, 98.0) is False


# ── 알림 문구: 근접·이탈·불일치를 구분해 안내한다 ─────────────────────────────
import telegram_bot


def test_breach_alert_says_the_stop_is_broken_not_approaching():
    text = telegram_bot.fmt_trigger_alert("AAA", ["STOP BREACH 1.0% 하회 (Stop=98.00) — 손절선 이탈"], 97.0, 98.0)
    assert "이탈" in text and "임박" not in text


def test_mismatch_alert_asks_for_re_registration_not_a_sale():
    text = telegram_bot.fmt_trigger_alert("AAA", ["STOP MISMATCH 등록 Stop 이 현재가의 4.0배 (Stop=400.00)"], 100.0, 400.0)
    assert "재등록" in text and "매도" not in text


def test_near_alert_keeps_its_wording():
    text = telegram_bot.fmt_trigger_alert("AAA", ["STOP NEAR 1.0% (Stop=98.00)"], 99.0, 98.0)
    assert "임박" in text
