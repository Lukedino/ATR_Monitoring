"""창 실행기 — 어떤 창을 돌리고 무엇을 건너뛰는가.

GHA 트리거가 창 안에 여러 번 떨어질 수도, 한 번도 안 떨어질 수도 있다(배달률 18%).
그래서 실행기는 두 가지를 보장해야 한다:
  - 이미 한 창은 다시 하지 않는다 (중복 알림 방지)
  - 실패한 창은 완료로 기록하지 않는다 → 같은 창 안 다음 틱이 재시도한다
"""
import datetime as dt
import json
import os
import sys
from zoneinfo import ZoneInfo

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import monitor  # noqa: E402
import stop_manager as sm  # noqa: E402

SEOUL = ZoneInfo("Asia/Seoul")


def _utc(*args: int) -> dt.datetime:
    return dt.datetime(*args, tzinfo=SEOUL).astimezone(dt.timezone.utc)


KR_OPEN_TIME = _utc(2026, 9, 14, 9, 20)   # kr_open 과 crypto_2 가 겹치는 시각
QUIET_TIME   = _utc(2026, 9, 14, 11, 0)   # 어떤 창도 아닌 시각


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    path = tmp_path / "stop_levels.json"
    path.write_text(json.dumps({"positions": {}, "alert_log": {}}), encoding="utf-8")
    monkeypatch.setattr(sm, "DATA_FILE", path)
    return path


@pytest.fixture
def recorded(monkeypatch):
    """창 본체 실행을 가로채 이름만 기록한다 — 네트워크·텔레그램을 타지 않도록."""
    calls: list[str] = []
    monkeypatch.setattr(monitor, "_run_window", lambda w: calls.append(w.name))
    return calls


def test_no_due_window_runs_nothing(isolated_state, recorded):
    monitor.run_due_windows(now_utc=QUIET_TIME)
    assert recorded == []


def test_due_window_runs(isolated_state, recorded):
    monitor.run_due_windows(now_utc=KR_OPEN_TIME)
    assert "kr_open" in recorded


def test_due_window_is_marked_done(isolated_state, recorded):
    monitor.run_due_windows(now_utc=KR_OPEN_TIME)
    assert sm.is_window_done("kr_open", dt.date(2026, 9, 14)) is True


def test_already_done_window_is_skipped(isolated_state, recorded):
    sm.mark_window_done("kr_open", dt.date(2026, 9, 14))
    monitor.run_due_windows(now_utc=KR_OPEN_TIME)
    assert "kr_open" not in recorded


def test_overlapping_windows_both_run(isolated_state, recorded):
    """09:07 에 KR 장초반과 크립토 2번이 겹친다 — 대상 종목이 달라 둘 다 돌아야 한다."""
    monitor.run_due_windows(now_utc=KR_OPEN_TIME)
    assert "kr_open" in recorded and "crypto_2" in recorded


def test_second_tick_in_same_window_does_not_rerun(isolated_state, recorded):
    """트리거가 창 안에 두 번 떨어져도 알림은 한 번이어야 한다."""
    monitor.run_due_windows(now_utc=KR_OPEN_TIME)
    recorded.clear()
    monitor.run_due_windows(now_utc=_utc(2026, 9, 14, 9, 30))
    assert recorded == []


def test_failed_window_is_not_marked_done(isolated_state, monkeypatch):
    """실패를 완료로 기록하면 그날 그 창은 영영 안 돈다 — 창 안 재시도가 살아 있어야 한다."""
    def boom(w):
        raise RuntimeError("야후 조회 실패")
    monkeypatch.setattr(monitor, "_run_window", boom)

    monitor.run_due_windows(now_utc=KR_OPEN_TIME)

    assert sm.is_window_done("kr_open", dt.date(2026, 9, 14)) is False


def test_one_failing_window_does_not_block_the_other(isolated_state, monkeypatch):
    """겹친 창 하나가 실패해도 나머지는 돌아야 한다."""
    ran: list[str] = []

    def selective(w):
        if w.name == "kr_open":
            raise RuntimeError("야후 조회 실패")
        ran.append(w.name)

    monkeypatch.setattr(monitor, "_run_window", selective)
    monitor.run_due_windows(now_utc=KR_OPEN_TIME)

    assert "crypto_2" in ran
    assert sm.is_window_done("crypto_2", dt.date(2026, 9, 14)) is True
    assert sm.is_window_done("kr_open", dt.date(2026, 9, 14)) is False
