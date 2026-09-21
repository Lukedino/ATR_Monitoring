"""종가 창의 일일 요약 포매터.

주간 리포트는 그대로 두고(바차트 + Chandelier 전 종목 나열 + 종목별 미니차트 N장),
매일 종가에는 텍스트 1건만 보낸다. 이미지가 없으니 Telegram rate limit 대기(장당 4초)도
없고 실행이 빠르다.

2026-09-21 소유자 결정: 이탈·근접 종목은 **전부** 나열한다. "… 외 28개" 로 접으면 어느 종목을
손봐야 하는지 알 수 없어 요약의 쓸모가 없다. 여유 종목은 여전히 개수만 — 할 일이 없는 종목이다.
길어지면 send_long_message 가 줄 단위로 나눠 보낸다.

같은 날 찾은 회귀: 09-15 에 stop_check 을 전 종목으로 넓히면서 요약도 전 종목 결과를 그대로
받아, "KR 종가 요약" 에 미국 종목이 섞였다. 요약은 창의 시장 종목만 센다. 크립토는 전용 종가 창이
없어 US 요약에 붙이고 제목도 "US/크립토 종가 요약" 으로 맞춘다 — 어느 요약에서도 빠지는 보유가 없게.
"""
import os
import sys
import types

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from atr_calculator import ChandelierResult  # noqa: E402
import telegram_bot as tg  # noqa: E402
import monitor  # noqa: E402


def _ch(symbol: str, dist_pct: float, market: str = "US") -> ChandelierResult:
    """현재가 100 기준으로 Stop 거리만 맞춘 결과 객체."""
    close = 100.0
    stop  = close * (1 - dist_pct / 100)   # dist 가 음수면 stop > close → 이탈
    return ChandelierResult(
        symbol=symbol, highest_high=120.0, atr=2.0, atr_pct=2.0, multiple=3.0,
        stop_level=stop, current_close=close, dist_to_stop_pct=dist_pct, market=market,
    )


BREACHED = _ch("AAPL", -2.0)     # stop 102 > close 100
NEAR     = _ch("NVDA", 3.0)      # stop 97, 거리 3% → 근접
SAFE     = _ch("MSFT", 20.0)     # stop 80, 거리 20% → 여유


def test_breached_and_near_are_counted_separately():
    """is_near_stop 은 이탈을 포함한다 — 그대로 세면 이탈 종목이 두 번 잡힌다."""
    out = tg.fmt_daily_brief("US 종가 요약", "2026-09-14", [BREACHED, NEAR, SAFE])
    assert "이탈 1" in out
    assert "근접 1" in out
    assert "여유 1" in out


def test_breached_symbols_are_named():
    out = tg.fmt_daily_brief("US 종가 요약", "2026-09-14", [BREACHED, SAFE])
    assert "AAPL" in out


def test_near_symbols_are_named():
    out = tg.fmt_daily_brief("US 종가 요약", "2026-09-14", [NEAR, SAFE])
    assert "NVDA" in out


def test_safe_symbols_are_not_named():
    """여유 종목까지 나열하면 요약이 아니다 — 개수만 남긴다."""
    out = tg.fmt_daily_brief("US 종가 요약", "2026-09-14", [BREACHED, SAFE])
    assert "MSFT" not in out


def test_every_breached_and_near_name_is_listed():
    """접으면("… 외 N개") 어느 종목을 손봐야 하는지 모른다 — 전부 나열한다."""
    breached = [_ch(f"BRK{i}", -float(i + 1)) for i in range(33)]
    near     = [_ch(f"NR{i}", 3.0) for i in range(16)]
    out = tg.fmt_daily_brief("US 종가 요약", "2026-09-14", breached + near)
    lines = set(out.split("\n"))
    assert all(f"  • BRK{i}" in lines for i in range(33))
    assert all(f"  • NR{i}" in lines for i in range(16))
    assert "외 " not in out


def test_names_are_listed_one_per_line():
    """한 줄에 쉼표로 이으면 긴 종목명(회사명 | 티커)이 뒤엉켜 읽히지 않는다 — 종목마다 한 줄."""
    many = [_ch(f"TCK{i}", -float(i + 1)) for i in range(7)]
    lines = tg.fmt_daily_brief("US 종가 요약", "2026-09-14", many).split("\n")
    head = lines.index("🔴 손절 이탈 7")
    items = lines[head + 1: head + 1 + 7]
    assert all(l.startswith("  • ") and l.count("TCK") == 1 for l in items)
    assert "TCK6" in items[0]                # Stop 거리가 가까운(더 깊이 이탈한) 순
    assert "TCK0" in items[-1]
    assert lines[head + 1 + 7] == "🟢 여유 0"


def test_clean_day_omits_empty_lines():
    """이탈·근접이 없으면 그 줄은 빼서 더 짧게."""
    out = tg.fmt_daily_brief("US 종가 요약", "2026-09-14", [SAFE])
    assert "이탈" not in out
    assert "근접" not in out
    assert "여유 1" in out


def test_spike_and_update_counts_are_shown():
    out = tg.fmt_daily_brief("US 종가 요약", "2026-09-14", [SAFE],
                             spike_count=2, updated_count=4)
    assert "스파이크 2" in out
    assert "Stop 갱신 4" in out


def test_zero_counts_are_omitted():
    out = tg.fmt_daily_brief("US 종가 요약", "2026-09-14", [SAFE],
                             spike_count=0, updated_count=0)
    assert "스파이크" not in out
    assert "Stop 갱신" not in out


def test_title_and_data_date_appear():
    out = tg.fmt_daily_brief("KR 종가 요약", "2026-09-14", [SAFE])
    assert "KR 종가 요약" in out
    assert "2026-09-14" in out


def test_empty_result_list_is_handled():
    out = tg.fmt_daily_brief("US 종가 요약", "2026-09-14", [])
    assert out          # 빈 문자열을 보내면 Telegram 이 400 을 준다
    assert "0" in out


# ── 창의 시장 종목만 센다 ────────────────────────────────────
KR_SYM = "999991.KS"


def _bars(last_day: str) -> pd.DataFrame:
    idx = pd.date_range(end=pd.Timestamp(last_day), periods=3, freq="D")
    return pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": 1.0}, index=idx)


def _send_brief(monkeypatch, market: str, ok: bool = True) -> list:
    """보유 = KR 1종목 + US 3종목 + 크립토 1종목. 전 종목 stop_check 결과를 창에 넘기고 나간 본문을 돌려준다."""
    sent: list = []

    def send_long_message(text, *a, **k):
        sent.append(text)
        return ok
    monkeypatch.setattr(monitor, "tg", types.SimpleNamespace(fmt_daily_brief=tg.fmt_daily_brief,
                                                             send_long_message=send_long_message))
    monkeypatch.setattr(monitor, "_BRIEF_SCOPE", {
        "KR": ("KR 종가 요약", [KR_SYM]),
        "US": ("US/크립토 종가 요약", ["AAPL", "NVDA", "MSFT", "BTC-USD"]),
    })
    monkeypatch.setattr(monitor, "summarize_portfolio_atr", lambda m, p: pd.DataFrame(
        {"Symbol": list(m), "Spike": [s == "NVDA" for s in m]}))

    result = monitor.StopCheckResult(
        chandelier      = [_ch(KR_SYM, -1.0, "KR"), BREACHED, NEAR, SAFE, _ch("BTC-USD", -5.0, "Crypto")],
        updated_symbols = ["AAPL"],
        ohlcv_map       = {KR_SYM: _bars("2026-09-21"), "AAPL": _bars("2026-09-18"),
                           "NVDA": _bars("2026-09-18"), "MSFT": _bars("2026-09-18"),
                           "BTC-USD": _bars("2026-09-18")},
    )                                            # 전 종목 최빈 날짜 = 미국 거래일 09-18
    monitor._send_daily_brief(types.SimpleNamespace(market=market), result)
    return sent


def test_kr_brief_excludes_us_holdings(monkeypatch):
    out = "\n".join(_send_brief(monkeypatch, "KR"))
    assert "KR 종가 요약" in out
    assert "보유 1종목" in out
    assert "  • 999991 (KS)" in out
    assert "AAPL" not in out and "NVDA" not in out and "BTC-USD" not in out
    assert "스파이크" not in out and "Stop 갱신" not in out   # 둘 다 미국 종목 몫


def test_kr_brief_uses_the_kr_data_date(monkeypatch):
    """전 종목 최빈값을 쓰면 미국 종목이 많은 날 KR 요약에 미국 거래일이 찍힌다."""
    out = "\n".join(_send_brief(monkeypatch, "KR"))
    assert "2026-09-21" in out and "2026-09-18" not in out


def test_us_brief_carries_crypto_and_excludes_kr_holdings(monkeypatch):
    out = "\n".join(_send_brief(monkeypatch, "US"))
    assert "US/크립토 종가 요약" in out
    assert "보유 4종목" in out
    assert "  • BTC-USD" in out
    assert "999991" not in out
    assert "스파이크 1" in out and "Stop 갱신 1" in out


def test_every_holding_lands_in_exactly_one_brief():
    """크립토 전용 종가 창이 없다 — US 요약에 붙이지 않으면 크립토 보유는 어느 요약에도 안 나온다."""
    scoped = [s for _, symbols in monitor._BRIEF_SCOPE.values() for s in symbols]
    assert sorted(scoped) == sorted(monitor.ALL_SYMBOLS)
    assert set(monitor.CRYPTO_SYMBOLS) <= set(monitor._BRIEF_SCOPE["US"][1])


def test_every_brief_window_has_a_scope():
    """종가 창을 새로 만들고 범위를 빠뜨리면 그 창은 매번 KeyError 로 실패한다."""
    import market_hours
    assert all(w.market in monitor._BRIEF_SCOPE for w in market_hours.ALL_WINDOWS if w.brief)


def test_brief_send_failure_still_raises(monkeypatch):
    import pytest
    with pytest.raises(RuntimeError):
        _send_brief(monkeypatch, "KR", ok=False)


# ── 길어지면 나눠 보내고, 한 조각이라도 실패하면 실패로 알린다 ──────
def test_long_message_reports_a_failed_chunk(monkeypatch):
    results = iter([True, False, True])
    monkeypatch.setattr(tg, "send_message", lambda text, parse_mode="Markdown": next(results))
    text = "\n".join("x" * 100 for _ in range(60))        # 6000자 → 3조각
    assert tg.send_long_message(text) is False


def test_long_message_reports_success(monkeypatch):
    monkeypatch.setattr(tg, "send_message", lambda text, parse_mode="Markdown": True)
    assert tg.send_long_message("짧은 본문") is True
    assert tg.send_long_message("\n".join("x" * 100 for _ in range(60))) is True
