"""창 실행기.

**2026-09-15 실측으로 설계를 한 번 고쳤다.** 처음에는 창 안에서만 stop_check 을 돌렸는데,
배달된 런 10건 중 창(25~30분)에 들어간 건 1건뿐이었고 월요일 하루 KR 창이 하나도 돌지
않았다. 배달률이 7% 라 좁은 창을 요구하면 대부분의 날에 아무것도 안 돈다.

그래서 역할을 나눴다:
  - **전 종목 stop_check 은 창과 무관하게 항상 돈다** — 교체 전과 같은 안전망.
    트리거 알림을 창에 가두면 안 된다.
  - **창은 그 위에 얹는 것만 정한다** — 종가 요약, 주간 리포트. 하루 1회 멱등.

stop_check 전용 창(kr_open 등)은 안전망이 이미 덮으므로 실행기가 건너뛴다. 창 정의 자체는
남겨 둔다 — 트리거가 신뢰할 수 있게 되면(Cloud Scheduler) 다시 의미가 생긴다.
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


QUIET_TIME = _utc(2026, 9, 14, 11, 0)    # 어떤 창도 아닌 시각
OPEN_TIME  = _utc(2026, 9, 14, 9, 20)    # kr_open + crypto_2 — 둘 다 stop_check 전용
CLOSE_TIME = _utc(2026, 9, 14, 15, 40)   # kr_close — 종가 요약이 붙는 창
D = dt.date(2026, 9, 14)


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    path = tmp_path / "stop_levels.json"
    path.write_text(json.dumps({"positions": {}, "alert_log": {}}), encoding="utf-8")
    monkeypatch.setattr(sm, "DATA_FILE", path)
    return path


@pytest.fixture
def stub(monkeypatch):
    """네트워크·텔레그램을 타지 않도록 본체를 가로채고 호출만 기록한다."""
    rec = {"stop_check": [], "extra": []}
    monkeypatch.setattr(monitor, "job_stop_check",
                        lambda symbols=None: rec["stop_check"].append(symbols))
    monkeypatch.setattr(monitor, "_run_window_extra",
                        lambda w, result: rec["extra"].append(w.name))
    return rec


# ── 안전망: stop_check 은 항상 돈다 ─────────────────────────
def test_stop_check_runs_even_when_no_window_is_due(isolated_state, stub):
    monitor.run_due_windows(now_utc=QUIET_TIME)
    assert stub["stop_check"] == [None], "창이 없어도 전 종목을 봐야 한다"


def test_stop_check_runs_once_even_with_overlapping_windows(isolated_state, stub):
    """겹친 창이 둘이어도 전 종목 체크는 한 번 — 야후를 두 번 칠 이유가 없다."""
    monitor.run_due_windows(now_utc=OPEN_TIME)
    assert len(stub["stop_check"]) == 1


def test_stop_check_covers_all_symbols_not_just_one_market(isolated_state, stub):
    """시장별로 좁히면 그 시장 창이 안 떨어진 날 그 시장이 통째로 빈다."""
    monitor.run_due_windows(now_utc=CLOSE_TIME)
    assert stub["stop_check"][0] is None


# ── 창은 덧붙이는 것만 ──────────────────────────────────────
def test_stop_check_only_windows_add_nothing(isolated_state, stub):
    """kr_open 은 안전망이 이미 덮는다 — 따로 실행할 게 없다."""
    monitor.run_due_windows(now_utc=OPEN_TIME)
    assert stub["extra"] == []


def test_brief_window_adds_the_daily_summary(isolated_state, stub):
    monitor.run_due_windows(now_utc=CLOSE_TIME)
    assert stub["extra"] == ["kr_close"]


def test_brief_window_is_marked_done(isolated_state, stub):
    monitor.run_due_windows(now_utc=CLOSE_TIME)
    assert sm.is_window_done("kr_close", D) is True


def test_brief_is_sent_once_per_day(isolated_state, stub):
    """창 안에 런이 두 번 떨어져도 요약은 하루 한 번."""
    monitor.run_due_windows(now_utc=CLOSE_TIME)
    stub["extra"].clear()
    monitor.run_due_windows(now_utc=_utc(2026, 9, 14, 15, 50))
    assert stub["extra"] == []


def test_stop_check_still_runs_on_the_second_tick(isolated_state, stub):
    """요약은 하루 한 번이지만 손절 체크는 매 런 돌아야 한다."""
    monitor.run_due_windows(now_utc=CLOSE_TIME)
    monitor.run_due_windows(now_utc=_utc(2026, 9, 14, 15, 50))
    assert len(stub["stop_check"]) == 2


# ── 실패 격리 ───────────────────────────────────────────────
def test_failed_extra_is_not_marked_done(isolated_state, monkeypatch):
    """실패를 완료로 적으면 그날 요약이 영영 안 간다."""
    monkeypatch.setattr(monitor, "job_stop_check", lambda symbols=None: None)

    def boom(w, result):
        raise RuntimeError("텔레그램 실패")
    monkeypatch.setattr(monitor, "_run_window_extra", boom)

    monitor.run_due_windows(now_utc=CLOSE_TIME)
    assert sm.is_window_done("kr_close", D) is False


def test_failed_stop_check_does_not_block_the_extra(isolated_state, monkeypatch):
    """손절 체크가 실패해도 요약 시도는 해야 한다."""
    extras: list = []

    def boom(symbols=None):
        raise RuntimeError("야후 조회 실패")
    monkeypatch.setattr(monitor, "job_stop_check", boom)
    monkeypatch.setattr(monitor, "_run_window_extra", lambda w, r: extras.append(w.name))

    monitor.run_due_windows(now_utc=CLOSE_TIME)
    assert extras == ["kr_close"]
