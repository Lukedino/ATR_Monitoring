"""종가 창의 일일 요약 포매터.

주간 리포트는 그대로 두고(바차트 + Chandelier 전 종목 나열 + 종목별 미니차트 N장),
매일 종가에는 텍스트 1건만 보낸다. 이미지가 없으니 Telegram rate limit 대기(장당 4초)도
없고 실행이 빠르다.

보유가 78종목이라 "간략" 을 지키려면 이름 나열에 상한이 필요하다 — 근접이 20종목이면
전부 나열하는 순간 요약이 아니게 된다.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from atr_calculator import ChandelierResult  # noqa: E402
import telegram_bot as tg  # noqa: E402


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


def test_long_name_lists_are_truncated():
    many = [_ch(f"TCK{i}", 3.0) for i in range(12)]
    out = tg.fmt_daily_brief("US 종가 요약", "2026-09-14", many)
    assert "외 " in out                      # "외 N개" 로 접힌다
    assert out.count("TCK") <= tg.BRIEF_NAME_LIMIT


def test_names_are_listed_one_per_line():
    """한 줄에 쉼표로 이으면 긴 종목명(회사명 | 티커)이 뒤엉켜 읽히지 않는다 — 종목마다 한 줄."""
    many = [_ch(f"TCK{i}", -float(i + 1)) for i in range(7)]
    lines = tg.fmt_daily_brief("US 종가 요약", "2026-09-14", many).split("\n")
    head = lines.index("🔴 손절 이탈 7")
    items = lines[head + 1: head + 1 + tg.BRIEF_NAME_LIMIT]
    assert all(l.startswith("  • ") and l.count("TCK") == 1 for l in items)
    assert "TCK6" in items[0]                # Stop 거리가 가까운(더 깊이 이탈한) 순
    assert lines[head + 1 + tg.BRIEF_NAME_LIMIT] == "  … 외 2개"


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


def test_brief_has_no_chart_and_stays_short():
    """요약은 텍스트 1건이다 — 길어지면 send_long_message 분할이 필요해져 취지가 깨진다."""
    many = [_ch(f"TCK{i}", 3.0) for i in range(78)]
    out = tg.fmt_daily_brief("US 종가 요약", "2026-09-14", many, spike_count=3, updated_count=9)
    assert len(out) < 800
