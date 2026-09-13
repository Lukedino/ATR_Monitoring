"""market_hours — 시장별 트리거 알림 활성 시간 판정.

기존 구현은 monitor.py 안에서 UTC 시(hour)를 손으로 계산해 박아뒀고, 그래서 두 가지가 깨져 있었다
(2026-09-13 점검):

  ① KR 이 KST 18:00 에 끊긴다. 2026-09-14 부터 KRX 애프터마켓이 16:00~20:00 실시간 거래로
     신설되고 기존 시간외단일가(~18:00)가 폐지되므로, KST 18:00~20:00 의 트리거 알림이
     조용히 사라진다.
  ② US 프리마켓이 서머타임 기간에 1시간 늦게 열린다. 주석의 "ET 04:00 ← UTC 09:00" 은
     EST(UTC-5) 기준이라, EDT(3~11월, UTC-4)에는 ET 05:00 에야 열린다.

둘 다 UTC 산술을 지역 시간대로 바꾸면 사라진다. 주말 경계(일요일 UTC 23시부터 KR 활성,
토요일 UTC 01시까지 US 활성) 특례도 지역 시간대에서 요일을 보면 저절로 해소된다.
"""
import datetime as dt
import os
import sys
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import market_hours as mh  # noqa: E402

SEOUL = ZoneInfo("Asia/Seoul")
NEW_YORK = ZoneInfo("America/New_York")

# 2026-09-11 금 / 2026-09-12 토 / 2026-09-13 일 / 2026-09-14 월 (모두 뉴욕 EDT)
# 2026-01-16 금 (뉴욕 EST)


def _utc(tz: ZoneInfo, *args: int) -> dt.datetime:
    """지역 시각을 UTC 로 바꾼다. 테스트가 UTC 산술을 흉내내지 않도록."""
    return dt.datetime(*args, tzinfo=tz).astimezone(dt.timezone.utc)


# ── KR ──────────────────────────────────────────────────────
def test_kr_after_market_is_active_until_20_kst():
    """① 2026-09-14 부터 KRX 애프터마켓이 20:00 까지다. 기존 구현은 18:00 에 끊겼다."""
    assert mh.is_market_active("KR", _utc(SEOUL, 2026, 9, 14, 19, 0)) is True


def test_kr_inactive_after_after_market_close():
    assert mh.is_market_active("KR", _utc(SEOUL, 2026, 9, 14, 20, 30)) is False


def test_kr_active_during_regular_session():
    assert mh.is_market_active("KR", _utc(SEOUL, 2026, 9, 14, 9, 30)) is True


def test_kr_premarket_active_from_08_kst():
    assert mh.is_market_active("KR", _utc(SEOUL, 2026, 9, 14, 8, 30)) is True


def test_kr_inactive_before_premarket():
    assert mh.is_market_active("KR", _utc(SEOUL, 2026, 9, 14, 7, 30)) is False


def test_kr_inactive_on_saturday():
    assert mh.is_market_active("KR", _utc(SEOUL, 2026, 9, 12, 10, 0)) is False


# ── US ──────────────────────────────────────────────────────
def test_us_premarket_active_during_dst():
    """② EDT 의 ET 04:30 은 UTC 08:30 이라 기존 구현이 비활성으로 판정했다."""
    assert mh.is_market_active("US", _utc(NEW_YORK, 2026, 9, 11, 4, 30)) is True


def test_us_premarket_active_during_standard_time():
    """EST 에서도 같아야 한다 — 기존 구현이 유일하게 맞던 구간이라 회귀 방지."""
    assert mh.is_market_active("US", _utc(NEW_YORK, 2026, 1, 16, 4, 30)) is True


def test_us_inactive_before_premarket_open():
    assert mh.is_market_active("US", _utc(NEW_YORK, 2026, 9, 11, 3, 30)) is False


def test_us_after_market_active_until_20_et():
    assert mh.is_market_active("US", _utc(NEW_YORK, 2026, 9, 11, 19, 30)) is True


def test_us_inactive_after_after_market_close():
    assert mh.is_market_active("US", _utc(NEW_YORK, 2026, 9, 11, 20, 30)) is False


def test_us_friday_after_market_active_even_when_utc_is_saturday():
    """EST 의 금요일 19:30 ET 는 UTC 로 토요일이다. 요일은 지역 시간대에서 봐야 한다."""
    moment = _utc(NEW_YORK, 2026, 1, 16, 19, 30)
    assert moment.weekday() == 5  # UTC 로는 토요일
    assert mh.is_market_active("US", moment) is True


def test_us_inactive_on_sunday():
    assert mh.is_market_active("US", _utc(NEW_YORK, 2026, 9, 13, 12, 0)) is False


def test_etf_follows_us_hours():
    assert mh.is_market_active("ETF", _utc(NEW_YORK, 2026, 9, 11, 4, 30)) is True


# ── Crypto ──────────────────────────────────────────────────
def test_crypto_is_always_active():
    assert mh.is_market_active("Crypto", _utc(SEOUL, 2026, 9, 13, 3, 0)) is True
    assert mh.is_market_active("Crypto", _utc(SEOUL, 2026, 9, 14, 23, 0)) is True
