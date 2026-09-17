"""디스패처 슬롯 생성 — market_hours.ALL_WINDOWS → Personal Assistant `gha-schedules.json` 항목.

2026-09-17 부터 이 저장소의 예약 실행은 GitHub `schedule:` 크론이 아니라 Personal Assistant 의
tick 디스패처가 맡는다: Cloud Scheduler(5분) → PA `apps/orchestrator/config/gha-schedules.json` 의
`atr-*` 항목 → 이 저장소 `atr_monitor.yml` 을 `workflow_dispatch(job=auto)` 로 깨운다.

규칙 — 창(window)마다 창 시작 직후 **1회**:
  1. 창 시작 시각을 5분 격자로 올린다(09:07 → 09:10). tick 이 5분 격자에서 돌므로 슬롯을 격자에
     맞추면 슬롯→디스패치 지연이 초 단위가 된다. 올림 폭은 최대 4분이라 최단 창(25분)에 여유가 있다.
  2. 겨울·여름 프로브 주로 UTC 로 바꾼다. America/New_York 창은 EDT·EST 가 1시간 달라 항목이 둘
     생기고(`-edt`/`-est`), 어느 날이든 하나만 창 안에 떨어진다 — 다른 하나는 창 밖이라 실행기가
     전 종목 stop_check 만 하고 끝난다(무해). Asia/Seoul 창은 둘이 같아 항목 하나.
  3. UTC (시, 분) 이 같은 창은 한 항목으로 합친다(요일 합집합). 디스패처는 같은 워크플로의 같은 분
     슬롯을 하나만 보내고 나머지를 SUPERSEDED 로 남기므로, 따로 내면 매일 SUPERSEDED 행이 쌓인다.
     `job=auto` 한 번으로 `due_windows()` 가 두 창을 다 돌려준다.

창을 고치면:  python dispatcher_schedules.py  →  dispatcher_schedules.json 을 PA 설정 파일에 반영.
tests/test_dispatcher_schedules.py 가 JSON 과 생성 결과의 일치를 강제한다.
"""
from __future__ import annotations

import datetime as dt
import json
import os

import market_hours as mh

REPO = "Lukedino/ATR_Monitoring"
WORKFLOW = "atr_monitor.yml"
GRID_MIN = 5
OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dispatcher_schedules.json")

# 프로브 주(월요일). 겨울 = EST, 여름 = EDT. 둘 다 2026 년 DST 전환일(03-08·11-01)에서 멀다.
PROBE_WEEKS = (dt.date(2026, 1, 12), dt.date(2026, 7, 13))
UTC = dt.timezone.utc
NEW_YORK_KEY = "America/New_York"


def _rounded_local_start(window: mh.Window, day: dt.date) -> dt.datetime:
    """창 시작을 5분 격자로 올린 지역 시각(timezone-aware)."""
    local = dt.datetime.combine(day, window.start, tzinfo=window.tz)
    return local + dt.timedelta(minutes=(-local.minute) % GRID_MIN)


def _dst_suffix(local: dt.datetime) -> str:
    return "edt" if local.dst() else "est"


def _tz_label(window: mh.Window) -> str:
    return "ET" if window.tz.key == NEW_YORK_KEY else "KST"


def _dow_field(dows: set[int]) -> str:
    """PA cron.ts 규칙: 0=일요일. 7일 전부면 `*`, 연속이면 `a-b`, 아니면 쉼표 목록."""
    if len(dows) == 7:
        return "*"
    s = sorted(dows)
    if len(s) > 1 and s == list(range(s[0], s[-1] + 1)):
        return f"{s[0]}-{s[-1]}"
    return ",".join(str(d) for d in s)


def build_entries() -> list[dict]:
    """ALL_WINDOWS → PA GhaScheduleSchema 항목 목록(UTC 시각 순)."""
    keyed: dict[tuple[int, int], dict] = {}
    for w in mh.ALL_WINDOWS:
        is_ny = w.tz.key == NEW_YORK_KEY
        for week in PROBE_WEEKS:
            for d in sorted(w.weekdays):
                local = _rounded_local_start(w, week + dt.timedelta(days=d))
                u = local.astimezone(UTC)
                bucket = keyed.setdefault((u.hour, u.minute), {"parts": [], "labels": [], "dows": set()})
                part = w.name.replace("_", "-") + (f"-{_dst_suffix(local)}" if is_ny else "")
                if part not in bucket["parts"]:
                    bucket["parts"].append(part)
                    dst = f" {_dst_suffix(local).upper()}" if is_ny else ""
                    bucket["labels"].append(f"{w.name}({w.start:%H:%M} {_tz_label(w)}{dst})")
                bucket["dows"].add((u.weekday() + 1) % 7)   # Python 월=0 → cron 일=0

    entries = []
    for (hour, minute), b in sorted(keyed.items()):
        entries.append({
            "id": "atr-" + "-".join(b["parts"]),
            "repo": REPO,
            "workflow": WORKFLOW,
            "ref": "main",
            "cron": f"{minute} {hour} * * {_dow_field(b['dows'])}",
            "inputs": {"job": "auto"},
            "enabled": True,
            "note": "ATR 창 " + ", ".join(b["labels"]) + " 시작 직후 1회 · job=auto",
        })
    return entries


def main() -> None:
    entries = build_entries()
    with open(OUTPUT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"{len(entries)} entries -> {OUTPUT}")


if __name__ == "__main__":
    main()
