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
