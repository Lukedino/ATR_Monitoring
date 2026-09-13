"""워크플로 크론이 창 정의와 어긋나지 않는지 대조.

크론은 market_hours 의 창 정의에서 생성했다. 창을 고치고 크론을 안 고치면 그 창은
영영 안 돌고, 아무도 모른다 — 알림이 안 오는 건 "조용한" 실패라 눈에 띄지 않는다.
이 테스트가 둘을 묶어 둔다.
"""
import datetime as dt
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import market_hours as mh  # noqa: E402

WORKFLOW = os.path.join(ROOT, ".github", "workflows", "atr_monitor.yml")

# 서머타임 양쪽을 다 덮도록 겨울·여름 날짜로 확인한다
PROBE_DATES = (dt.date(2026, 1, 15), dt.date(2026, 7, 15))


def _crons() -> list[str]:
    src = open(WORKFLOW, encoding="utf-8").read()
    return re.findall(r"-\s*cron:\s*'([^']+)'", src)


def _expand(field: str) -> set[int]:
    out: set[int] = set()
    for part in field.split(","):
        if "-" in part:
            a, b = part.split("-")
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


def _ticks_inside(w, minutes: set[int], hours: set[int], probe: dt.date) -> int:
    """창 안에 떨어지는 크론 틱 수. 창이 걸치는 시(hour)가 아니라 이걸 봐야 한다 —
    kr_late 는 UTC 05:57~06:27 이라 hour 5 에 3분 걸치지만 그 구간엔 틱이 없고,
    hour 6 의 틱 3개로 충분하다."""
    start_local = dt.datetime.combine(probe, w.start, tzinfo=w.tz)
    count = 0
    for m in range(w.duration_min):
        u = (start_local + dt.timedelta(minutes=m)).astimezone(dt.timezone.utc)
        if u.hour in hours and u.minute in minutes:
            count += 1
    return count


def test_exactly_one_cron():
    """크론 1개 + 창 판정이 설계다. 여러 개면 작업 종류를 크론으로 고르던 옛 방식으로 되돌아간 것."""
    assert len(_crons()) == 1


def test_every_window_gets_redundant_ticks():
    """창마다 틱이 2개 이상이어야 한다.

    배달률이 18% 라 틱 1개짜리 창은 성공률이 18% 다. 중복이 있어야 흡수된다.
    """
    minute_f, hour_f = _crons()[0].split()[:2]
    minutes, hours = _expand(minute_f), _expand(hour_f)
    for w in mh.ALL_WINDOWS:
        for probe in PROBE_DATES:
            n = _ticks_inside(w, minutes, hours, probe)
            assert n >= 2, f"{w.name} ({probe}) 에 틱이 {n}개뿐"


def test_cron_minutes_avoid_round_times():
    """GitHub 문서: 매시 정각이 고부하 구간. 실측에서도 :30 이 :00 보다 37% 더 도착했다."""
    minutes = _expand(_crons()[0].split()[0])
    assert 0 not in minutes
    assert 30 not in minutes


def test_tick_gap_fits_the_shortest_window():
    """가장 짧은 창에도 틱이 최소 1개는 들어가야 한다."""
    minutes = sorted(_expand(_crons()[0].split()[0]))
    gaps = [b - a for a, b in zip(minutes, minutes[1:])] + [60 - minutes[-1] + minutes[0]]
    shortest = min(w.duration_min for w in mh.ALL_WINDOWS)
    assert max(gaps) <= shortest, f"틱 간격 {max(gaps)}분 > 최단 창 {shortest}분"


def test_schedule_runs_delegate_to_auto():
    """예약 실행이 크론 문자열로 작업을 고르면 안 된다 — 그 방식이 커밋 153436e 에서 어긋났다."""
    src = open(WORKFLOW, encoding="utf-8").read()
    assert "GHA_JOB=auto" in src
    assert "github.event.schedule" not in src
