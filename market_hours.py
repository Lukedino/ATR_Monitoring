"""시장별 트리거 알림 활성 시간 판정.

UTC 시(hour)를 손으로 계산해 박아두면 두 가지가 조용히 깨진다. 둘 다 2026-09-13 에 실측됐다:

  ① KR 이 KST 18:00 에 끊긴다. 2026-09-14 부터 KRX 애프터마켓이 16:00~20:00 실시간 거래로
     신설되고 기존 시간외단일가(~18:00)가 폐지되므로, KST 18:00~20:00 의 트리거 알림이
     사라진다. 손절선 갱신은 되지만 알림만 빠지기 때문에 겉보기로는 정상이다.
  ② US 프리마켓이 서머타임 기간에 1시간 늦게 열린다. 구 주석의 "ET 04:00 ← UTC 09:00" 은
     EST(UTC-5) 기준이라, EDT(3~11월, UTC-4)에는 ET 05:00 에야 열렸다.

그래서 UTC 산술 대신 지역 시간대로 변환해 판정한다. 부수 효과로 주말 경계 특례
(일요일 UTC 23시부터 KR 활성, 토요일 UTC 01시까지 US 활성)가 통째로 사라진다 —
지역 시간대에서 요일을 보면 금요일 애프터마켓은 그냥 금요일이다.

이 게이트는 알림 발송 여부만 정한다. 데이터 수집과 Stop 갱신은 게이트와 무관하게 돌아간다.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

SEOUL    = ZoneInfo("Asia/Seoul")
NEW_YORK = ZoneInfo("America/New_York")

# KR: 장전 08:00 ~ 애프터마켓 종료 20:00 (정규장 09:00~15:30, 애프터 16:00~20:00)
KR_ACTIVE = (dt.time(8, 0), dt.time(20, 0))

# US: 프리마켓 04:00 ~ 애프터마켓 종료 20:00 ET (정규장 09:30~16:00)
US_ACTIVE = (dt.time(4, 0), dt.time(20, 0))

_MARKET_RULES = {
    "KR":  (SEOUL,    KR_ACTIVE),
    "US":  (NEW_YORK, US_ACTIVE),
    "ETF": (NEW_YORK, US_ACTIVE),
}


def is_market_active(market: str, now_utc: dt.datetime) -> bool:
    """해당 시장이 지금 트리거 알림을 보낼 수 있는 시간대인지.

    Parameters
    ----------
    market  : "KR" | "US" | "ETF" | "Crypto" (config.get_market_type 의 반환값)
    now_utc : timezone-aware datetime. 지역 시간대로 변환해 판정한다.
    """
    if market == "Crypto":
        return True   # 24/7

    rule = _MARKET_RULES.get(market)
    if rule is None:
        return True   # 분류 불가한 심볼은 게이트로 막지 않는다 (알림 누락보다 과다가 낫다)

    tz, (start, end) = rule
    local = now_utc.astimezone(tz)
    if local.weekday() >= 5:      # 지역 시간대 기준 주말
        return False
    return start <= local.time() < end


# ─────────────────────────────────────────────────────────────
# 실행 창 (window)
#
# GHA schedule 이벤트가 배달률 18% 로 드롭되므로(2026-09-12 실측) "정각 실행" 을 전제할 수 없다.
# 대신 창 안에서 오늘 아직 안 했으면 하는 멱등 구조로 간다 — 몇 분 밀려도, 중복 발사돼도
# 안전하고 창 안 재시도가 공짜로 생긴다.
#
# 2026-09-17 부터 트리거는 Personal Assistant 디스패처다(창마다 시작 직후 1회, dispatcher_schedules.py).
# 창 멱등·안전망 구조는 그대로 둔다 — 중복 발사·수동 실행에도 안전해야 하는 건 변하지 않는다.
#
# 창은 전부 지역 시각으로 정의한다. KR·크립토는 KST 고정(서머타임 없음), US 는 ET 이라
# America/New_York 변환이 서머타임을 자동 처리한다.
# ─────────────────────────────────────────────────────────────
from dataclasses import dataclass, field  # noqa: E402

WEEKDAYS = frozenset({0, 1, 2, 3, 4})
EVERYDAY = frozenset(range(7))


@dataclass(frozen=True)
class Window:
    name:         str
    market:       str          # "KR" | "US" | "Crypto"
    tz:           ZoneInfo
    start:        dt.time
    duration_min: int
    action:       str          # "stop_check" | "weekly_report"
    weekdays:     frozenset    # 지역 시각 기준 요일 (0=월)
    brief:        bool = False # 종가 창만 일일 요약을 덧붙인다

    def local_date(self, now_utc: dt.datetime) -> dt.date:
        """멱등성 키로 쓸 날짜. UTC 날짜를 쓰면 경계에서 하루가 어긋난다."""
        return now_utc.astimezone(self.tz).date()

    def is_due(self, now_utc: dt.datetime) -> bool:
        local = now_utc.astimezone(self.tz)
        if local.weekday() not in self.weekdays:
            return False
        end = (dt.datetime.combine(local.date(), self.start)
               + dt.timedelta(minutes=self.duration_min)).time()
        return self.start <= local.time() < end


def _w(name, market, tz, hh, mm, dur, action="stop_check", weekdays=WEEKDAYS, brief=False):
    return Window(name, market, tz, dt.time(hh, mm), dur, action, weekdays, brief)


ALL_WINDOWS: tuple[Window, ...] = (
    # KR — 정규장 09:00~15:30, 애프터마켓 16:00~20:00 (2026-09-14 신설)
    _w("kr_open",        "KR", SEOUL,  9,  7, 30),
    _w("kr_late",        "KR", SEOUL, 14, 57, 30),
    _w("kr_close",       "KR", SEOUL, 15, 35, 25, brief=True),
    _w("kr_after",       "KR", SEOUL, 17,  0, 30),
    _w("kr_after_close", "KR", SEOUL, 19, 30, 30),

    # US — 프리 04:00~, 정규장 09:30~16:00, 애프터 ~20:00 ET
    _w("us_open",        "US", NEW_YORK,  9, 37, 30),
    _w("us_late",        "US", NEW_YORK, 15, 27, 30),
    _w("us_close",       "US", NEW_YORK, 16,  5, 25, brief=True),
    _w("us_after_close", "US", NEW_YORK, 19, 30, 30),

    # Crypto — 24/7 이라 장 개념이 없어 KST 6시간 균등
    _w("crypto_1", "Crypto", SEOUL,  3,  7, 30, weekdays=EVERYDAY),
    _w("crypto_2", "Crypto", SEOUL,  9,  7, 30, weekdays=EVERYDAY),
    _w("crypto_3", "Crypto", SEOUL, 15,  7, 30, weekdays=EVERYDAY),
    _w("crypto_4", "Crypto", SEOUL, 21,  7, 30, weekdays=EVERYDAY),

    # 주간 리포트 — 기존 동작 유지 (금 18:00 KST / 토 08:00 KST)
    _w("kr_weekly", "KR", SEOUL, 18, 0, 30, action="weekly_report", weekdays=frozenset({4})),
    _w("us_weekly", "US", SEOUL,  8, 0, 30, action="weekly_report", weekdays=frozenset({5})),
)


def due_windows(now_utc: dt.datetime) -> list[Window]:
    """지금 시각에 해당하는 창 전부. 겹치는 창(KR 장초반 + 크립토 2번)은 둘 다 돌려준다."""
    return [w for w in ALL_WINDOWS if w.is_due(now_utc)]
