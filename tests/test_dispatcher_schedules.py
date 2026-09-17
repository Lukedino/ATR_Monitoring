"""디스패처 슬롯 생성 규칙 — 창 정의(market_hours.ALL_WINDOWS)에서 Personal Assistant 설정 항목을 만든다.

2026-09-17 부터 예약 실행은 GitHub `schedule:` 크론이 아니라 Personal Assistant 의 tick 디스패처
(Cloud Scheduler 5분 → workflow_dispatch job=auto)가 맡는다. 크론 배달률이 7% 라(2026-09-15 실측)
창 안에 틱이 하나도 안 떨어지는 날이 잦았고, 09-15 에 kr_close 종가 요약이 실제로 유실됐다.

디스패처는 정시에 확실히 부르므로 창마다 창 시작 직후 **1회**면 된다. 이 테스트는
  ① 모든 창이 서머타임 양쪽에서 시작 +4분 안의 슬롯을 정확히 하나 갖고,
  ② 그 슬롯 +3분(체크아웃·pip·폰트 설치)이 창 안에 있으며,
  ③ 항목이 PA 스키마(GhaScheduleSchema)에 맞고,
  ④ 커밋된 dispatcher_schedules.json 이 생성 결과와 같다(손으로 고치면 여기서 깨진다)
를 고정한다. 창을 고치면 `python dispatcher_schedules.py` 로 JSON 을 다시 만들고 PA 설정에 반영한다.
"""
import datetime as dt
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import dispatcher_schedules as ds  # noqa: E402
import market_hours as mh  # noqa: E402

UTC = dt.timezone.utc
ENTRIES = ds.build_entries()
BY_ID = {e["id"]: e for e in ENTRIES}

# Personal Assistant packages/schemas/src/gha.ts GhaScheduleSchema.id
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")


def _expand_dow(field: str) -> set[int]:
    if field == "*":
        return set(range(7))
    out: set[int] = set()
    for part in field.split(","):
        if "-" in part:
            a, b = part.split("-")
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


def _fires_at(entry: dict, u: dt.datetime) -> bool:
    """PA cron.ts 와 같은 규칙: 분·시·요일(0=일요일) 을 UTC 로 대조."""
    minute, hour, _dom, _mon, dow = entry["cron"].split()
    return u.minute == int(minute) and u.hour == int(hour) and (u.weekday() + 1) % 7 in _expand_dow(dow)


def _probe_days(window):
    for week in ds.PROBE_WEEKS:
        for d in sorted(window.weekdays):
            yield week + dt.timedelta(days=d)


def _window_start(window, day: dt.date) -> dt.datetime:
    return dt.datetime.combine(day, window.start, tzinfo=window.tz)


def _hits_inside(window, day: dt.date) -> list[tuple[int, str]]:
    """창 안(시작 ≤ t < 끝)에서 발화하는 (창 시작 후 분, 항목 id)."""
    start = _window_start(window, day)
    hits = []
    for m in range(window.duration_min):
        u = (start + dt.timedelta(minutes=m)).astimezone(UTC)
        hits.extend((m, e["id"]) for e in ENTRIES if _fires_at(e, u))
    return hits


# ── ① 창당 1회, 시작 +4분 안 ────────────────────────────────
def test_every_window_gets_exactly_one_slot_within_four_minutes_of_start():
    for w in mh.ALL_WINDOWS:
        for day in _probe_days(w):
            early = [(m, i) for m, i in _hits_inside(w, day) if m <= 4]
            assert len(early) == 1, f"{w.name} {day}: 시작 +4분 안 슬롯 {early}"


def test_rounding_never_moves_a_slot_before_the_window_opens():
    """올림이므로 슬롯은 창 시작과 같거나 뒤다 — 앞이면 실행기가 '창 없음' 으로 본다."""
    for w in mh.ALL_WINDOWS:
        for day in _probe_days(w):
            start = _window_start(w, day)
            before = (start - dt.timedelta(minutes=1)).astimezone(UTC)
            assert not any(_fires_at(e, before) for e in ENTRIES), f"{w.name} {day}: 창 열리기 1분 전에 발화"


# ── ② 시작 지연을 더해도 창 안 ───────────────────────────────
STARTUP_LATENCY_MIN = 3   # checkout + pip cache + fonts-nanum, 실측 1~2분


def test_slot_plus_startup_latency_stays_inside_the_window():
    for w in mh.ALL_WINDOWS:
        for day in _probe_days(w):
            for m, entry_id in _hits_inside(w, day):
                if m <= 4:
                    assert m + STARTUP_LATENCY_MIN < w.duration_min, f"{w.name} {entry_id}: {m}+{STARTUP_LATENCY_MIN} ≥ {w.duration_min}"


# ── ③ PA 스키마 불변식 ──────────────────────────────────────
def test_entries_match_personal_assistant_schema():
    assert len(ENTRIES) == 18   # 창을 더하면 이 수와 PA gha-schedules.json 을 같이 갱신
    assert len(BY_ID) == len(ENTRIES)
    for e in ENTRIES:
        assert set(e) == {"id", "repo", "workflow", "ref", "cron", "inputs", "enabled", "note"}, e["id"]
        assert ID_RE.match(e["id"]), e["id"]
        assert e["repo"] == "Lukedino/ATR_Monitoring"
        assert e["workflow"] == "atr_monitor.yml"
        assert e["ref"] == "main"
        assert e["inputs"] == {"job": "auto"}
        assert e["enabled"] is True
        assert 0 < len(e["note"]) <= 200, e["id"]
        assert len(e["cron"].split()) == 5, e["cron"]


def test_no_two_entries_share_a_utc_minute():
    """같은 워크플로의 같은 분 슬롯은 디스패처가 하나만 보내고 나머지를 SUPERSEDED 로 남긴다 — 합쳐서 낸다."""
    minutes = [tuple(e["cron"].split()[:2]) for e in ENTRIES]
    assert len(set(minutes)) == len(minutes)


def test_entries_are_sorted_by_utc_time():
    keys = [(int(e["cron"].split()[1]), int(e["cron"].split()[0])) for e in ENTRIES]
    assert keys == sorted(keys)


# ── ④ 드리프트 가드 ─────────────────────────────────────────
def test_committed_json_matches_generator():
    with open(ds.OUTPUT, encoding="utf-8") as f:
        assert json.load(f) == ENTRIES, "python dispatcher_schedules.py 로 다시 생성하고 PA 설정에 반영할 것"


# ── ⑤ 서머타임: New_York 창은 EDT·EST 두 변형, 날마다 하나만 창 안 ──
def _ny_windows():
    return [w for w in mh.ALL_WINDOWS if w.tz.key == "America/New_York"]


def test_new_york_windows_have_both_dst_variants():
    assert _ny_windows(), "US 창이 없으면 이 테스트를 지운다"
    for w in _ny_windows():
        base = "atr-" + w.name.replace("_", "-")
        assert f"{base}-edt" in BY_ID and f"{base}-est" in BY_ID, w.name


def test_only_one_dst_variant_lands_inside_the_window_each_day():
    for w in _ny_windows():
        base = "atr-" + w.name.replace("_", "-")
        for day in _probe_days(w):
            inside = {i for _m, i in _hits_inside(w, day) if i in (f"{base}-edt", f"{base}-est")}
            expected = f"{base}-edt" if _window_start(w, day).dst() else f"{base}-est"
            assert inside == {expected}, f"{w.name} {day}: {inside}"


# ── ⑥ 요일 이동·합침 사례 고정 ──────────────────────────────
def test_kr_open_and_crypto_2_merge_into_one_daily_entry():
    """kr_open(월~금 09:07 KST) 과 crypto_2(매일 09:07 KST) 는 같은 00:10Z — 합쳐서 매일 하나."""
    assert BY_ID["atr-kr-open-crypto-2"]["cron"] == "10 0 * * *"
    assert "atr-kr-open" not in BY_ID and "atr-crypto-2" not in BY_ID


def test_us_weekly_saturday_kst_is_friday_utc():
    assert BY_ID["atr-us-weekly"]["cron"] == "0 23 * * 5"


def test_us_after_close_in_standard_time_crosses_midnight_utc():
    """19:30 EST = 다음날 00:30Z → 요일이 화~토(2-6) 로 민다."""
    assert BY_ID["atr-us-after-close-est"]["cron"] == "30 0 * * 2-6"
    assert BY_ID["atr-us-after-close-edt"]["cron"] == "30 23 * * 1-5"


def test_kr_close_slot_is_the_window_start_itself():
    """15:35 는 이미 5분 격자라 올림이 없다."""
    assert BY_ID["atr-kr-close"]["cron"] == "35 6 * * 1-5"
