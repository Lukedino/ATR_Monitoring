"""market_hours 의 실행 창(window) 판정.

GHA schedule 이벤트가 배달률 18% 로 드롭되기 때문에(2026-09-12 실측), "정각에 실행" 을 전제로
설계할 수 없다. 대신 창 안에서 오늘 아직 안 했으면 하는 멱등 구조로 간다 — 몇 분 밀려도,
중복 발사돼도 안전하고 창 안 재시도가 공짜로 생긴다.

창은 전부 지역 시각으로 정의한다. KR·크립토는 KST 고정(서머타임 없음), US 는 ET 이라
`America/New_York` 변환이 서머타임을 자동 처리한다.
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

# 2026-09-11 금(EDT) · 09-12 토 · 09-13 일 · 09-14 월 · 2026-01-16 금(EST)


def _utc(tz: ZoneInfo, *args: int) -> dt.datetime:
    return dt.datetime(*args, tzinfo=tz).astimezone(dt.timezone.utc)


def _names(now_utc: dt.datetime) -> set[str]:
    return {w.name for w in mh.due_windows(now_utc)}


# ── KR 창 ───────────────────────────────────────────────────
def test_kr_open_window_is_due_at_its_start():
    assert "kr_open" in _names(_utc(SEOUL, 2026, 9, 14, 9, 7))


def test_kr_open_window_is_due_mid_window():
    assert "kr_open" in _names(_utc(SEOUL, 2026, 9, 14, 9, 20))


def test_kr_open_window_is_not_due_after_it_closes():
    assert "kr_open" not in _names(_utc(SEOUL, 2026, 9, 14, 9, 40))


def test_kr_close_window_sits_after_regular_session_ends():
    """정규장 마감이 15:30 이므로 종가 창은 그 뒤다."""
    assert "kr_close" in _names(_utc(SEOUL, 2026, 9, 14, 15, 40))


def test_kr_late_window_is_inside_regular_session():
    assert "kr_late" in _names(_utc(SEOUL, 2026, 9, 14, 15, 0))


def test_kr_after_close_window_ends_with_after_market():
    """KRX 애프터마켓이 20:00 에 끝난다."""
    assert "kr_after_close" in _names(_utc(SEOUL, 2026, 9, 14, 19, 45))


def test_kr_windows_do_not_fire_on_weekend():
    due = _names(_utc(SEOUL, 2026, 9, 12, 9, 20))
    assert not any(n.startswith("kr_") for n in due)


# ── US 창 (서머타임 자동 보정) ───────────────────────────────
def test_us_open_window_during_dst():
    assert "us_open" in _names(_utc(NEW_YORK, 2026, 9, 11, 9, 45))


def test_us_open_window_during_standard_time():
    """같은 ET 시각이면 서머타임 여부와 무관하게 같은 창이어야 한다."""
    assert "us_open" in _names(_utc(NEW_YORK, 2026, 1, 16, 9, 45))


def test_us_close_window_is_after_regular_close():
    assert "us_close" in _names(_utc(NEW_YORK, 2026, 9, 11, 16, 10))


def test_us_windows_do_not_fire_on_sunday():
    due = _names(_utc(NEW_YORK, 2026, 9, 13, 9, 45))
    assert not any(n.startswith("us_") for n in due)


# ── 크립토 창 (KST, 6시간 균등) ──────────────────────────────
def test_crypto_windows_are_six_hours_apart():
    starts = sorted(w.start for w in mh.ALL_WINDOWS if w.market == "Crypto")
    assert starts == [dt.time(3, 7), dt.time(9, 7), dt.time(15, 7), dt.time(21, 7)]


def test_crypto_window_fires_on_weekend():
    """크립토는 24/7 이라 주말에도 돈다."""
    assert any(n.startswith("crypto_") for n in _names(_utc(SEOUL, 2026, 9, 12, 15, 20)))


# ── 겹침·공백 ───────────────────────────────────────────────
def test_overlapping_windows_are_all_returned():
    """KR 장초반과 크립토 2번 창이 09:07 로 겹친다 — 둘 다 돌아야 한다."""
    due = _names(_utc(SEOUL, 2026, 9, 14, 9, 20))
    assert "kr_open" in due and "crypto_2" in due


def test_no_window_returns_empty():
    assert mh.due_windows(_utc(SEOUL, 2026, 9, 14, 11, 0)) == []


# ── 주간 리포트 창 (기존 동작 유지) ──────────────────────────
def test_kr_weekly_report_window_on_friday_evening_kst():
    assert "kr_weekly" in _names(_utc(SEOUL, 2026, 9, 11, 18, 10))


def test_us_weekly_report_window_on_saturday_morning_kst():
    assert "us_weekly" in _names(_utc(SEOUL, 2026, 9, 12, 8, 10))


def test_weekly_windows_do_not_fire_on_other_days():
    assert "kr_weekly" not in _names(_utc(SEOUL, 2026, 9, 14, 18, 10))


# ── 창 → 작업 매핑 ──────────────────────────────────────────
def test_close_windows_carry_the_daily_brief():
    (kr_close,) = [w for w in mh.ALL_WINDOWS if w.name == "kr_close"]
    (us_close,) = [w for w in mh.ALL_WINDOWS if w.name == "us_close"]
    assert kr_close.brief is True and us_close.brief is True


def test_non_close_windows_do_not_carry_the_daily_brief():
    for name in ("kr_open", "kr_late", "kr_after", "us_open", "crypto_1"):
        (w,) = [x for x in mh.ALL_WINDOWS if x.name == name]
        assert w.brief is False, name


def test_every_window_has_a_known_action():
    assert {w.action for w in mh.ALL_WINDOWS} == {"stop_check", "weekly_report"}


def test_window_local_date_uses_its_own_timezone():
    """멱등성 키는 창의 지역 날짜여야 한다 — UTC 날짜를 쓰면 경계에서 하루가 어긋난다."""
    # ET 금 19:45 는 EST 에서 UTC 로 토요일이다
    moment = _utc(NEW_YORK, 2026, 1, 16, 19, 45)
    assert moment.date() == dt.date(2026, 1, 17)          # UTC 로는 토요일
    (w,) = [x for x in mh.due_windows(moment) if x.name == "us_after_close"]
    assert w.local_date(moment) == dt.date(2026, 1, 16)   # ET 로는 금요일
