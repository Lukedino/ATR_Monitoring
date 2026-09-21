"""창 실행 멱등성 — stop_levels.json 의 done_windows 섹션.

GHA 트리거가 언제 몇 번 떨어질지 보장되지 않으므로(배달률 18%, 밀림 최대 4시간),
"창 안에서 오늘 아직 안 했으면 한다" 가 성립하려면 완료 기록이 필요하다.
alert_log 와 같은 방식으로 같은 상태 파일에 얹는다 — Drive 왕복이 이미 그 파일 하나다.

키는 창의 **지역 날짜**다. UTC 날짜를 쓰면 EST 금요일 애프터마켓이 토요일로 기록돼
다음 날 같은 창이 "이미 함" 으로 판정된다.
"""
import datetime as dt
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import stop_manager as sm  # noqa: E402

D1 = dt.date(2026, 9, 14)
D2 = dt.date(2026, 9, 15)


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """상태 파일을 임시 경로로 돌린다 — 실제 운영 파일을 건드리지 않도록."""
    path = tmp_path / "stop_levels.json"
    path.write_text(json.dumps({"positions": {}, "alert_log": {}}), encoding="utf-8")
    monkeypatch.setattr(sm, "DATA_FILE", path)
    return path


def test_window_is_not_done_initially(isolated_state):
    assert sm.is_window_done("kr_open", D1) is False


def test_marked_window_is_done(isolated_state):
    sm.mark_window_done("kr_open", D1)
    assert sm.is_window_done("kr_open", D1) is True


def test_done_state_is_per_date(isolated_state):
    """어제 했다고 오늘 건너뛰면 안 된다."""
    sm.mark_window_done("kr_open", D1)
    assert sm.is_window_done("kr_open", D2) is False


def test_done_state_is_per_window(isolated_state):
    """같은 날이라도 다른 창은 따로다."""
    sm.mark_window_done("kr_open", D1)
    assert sm.is_window_done("kr_close", D1) is False


def test_marking_twice_is_idempotent(isolated_state):
    sm.mark_window_done("kr_open", D1)
    sm.mark_window_done("kr_open", D1)
    raw = json.loads(isolated_state.read_text(encoding="utf-8"))
    assert raw["done_windows"][D1.isoformat()] == ["kr_open"]


def test_multiple_windows_on_same_date(isolated_state):
    sm.mark_window_done("kr_open", D1)
    sm.mark_window_done("crypto_2", D1)
    assert sm.is_window_done("kr_open", D1) is True
    assert sm.is_window_done("crypto_2", D1) is True


def test_old_dates_are_pruned(isolated_state):
    """무한 증식하면 Drive 왕복 파일이 계속 커진다 — 최근 며칠만 남긴다."""
    for i in range(sm.DONE_WINDOW_RETENTION_DAYS + 5):
        sm.mark_window_done("kr_open", D1 + dt.timedelta(days=i))
    raw = json.loads(isolated_state.read_text(encoding="utf-8"))
    assert len(raw["done_windows"]) == sm.DONE_WINDOW_RETENTION_DAYS
    # 가장 오래된 날짜는 사라지고 최신은 남는다
    assert D1.isoformat() not in raw["done_windows"]
    newest = D1 + dt.timedelta(days=sm.DONE_WINDOW_RETENTION_DAYS + 4)
    assert newest.isoformat() in raw["done_windows"]


def test_other_state_sections_are_preserved(isolated_state):
    """positions·alert_log 를 덮어쓰면 포지션과 알림 중복방지가 통째로 날아간다."""
    raw = json.loads(isolated_state.read_text(encoding="utf-8"))
    position = {"symbol": "SYM-0001", "entry_price": 100.0,
                "current_stop": 90.0, "highest_high": 110.0}
    raw["positions"] = {"SYM-0001": position}
    raw["alert_log"] = {"SYM-0001": {"date": "2026-09-14"}}
    isolated_state.write_text(json.dumps(raw), encoding="utf-8")

    sm.mark_window_done("kr_open", D1)

    after = json.loads(isolated_state.read_text(encoding="utf-8"))
    assert after["positions"] == {"SYM-0001": position}
    assert after["alert_log"] == {"SYM-0001": {"date": "2026-09-14"}}
    assert after["done_windows"][D1.isoformat()] == ["kr_open"]
