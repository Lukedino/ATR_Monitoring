"""종목 하나의 일시적 데이터 공백(ATR 계산 불가)이 실행 전체를 '실패' 로 만들지 않는다.

2026-11-19 야후가 한 종목 이력을 하루 동안 짧게 줘서 16회 실행이 전부 종료코드 1 이었다.
ATR 계산 불가(None)는 종목별 연속 횟수를 상태에 세고, DATA_GAP_FAIL_AFTER 회째부터 실패로 올린다.
예외는 지금처럼 즉시 실패다. 심볼은 전부 합성 값이다.
"""
import json
import types
from datetime import datetime, timezone

import pandas as pd
import pytest

import config
import monitor
import stop_manager as sm
from state_validation import StateValidationError, validate_state

GOOD, GAP = "SYNA", "SYNB"


def _bars(rows=30):
    idx = pd.date_range(end="2026-01-12", periods=rows, freq="D")
    return pd.DataFrame({"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0, "Volume": 1000.0}, index=idx)


class FakeTelegram:
    def __init__(self):
        self.messages = []

    def send_message(self, text, *a, **k):
        self.messages.append(text)
        return True

    send_long_message = send_message

    def send_photo(self, *a, **k):
        return True

    def __getattr__(self, name):
        if name.startswith("fmt_"):
            return lambda *a, **k: f"{name}:{a[0] if a else ''}"
        raise AttributeError(name)


@pytest.fixture
def env(tmp_path, monkeypatch):
    state = tmp_path / "stop_levels.json"
    state.write_text(json.dumps({"positions": {}, "alert_log": {}}), encoding="utf-8")
    monkeypatch.setattr(sm, "DATA_FILE", state)
    monkeypatch.setattr(monitor, "tg", FakeTelegram())
    monkeypatch.setattr(monitor, "_problems", [])
    monkeypatch.setattr(monitor, "ALL_SYMBOLS", [GOOD, GAP])
    monkeypatch.setattr(monitor, "_is_market_active_for_triggers", lambda s: True)
    monkeypatch.setattr(monitor, "check_immediate_triggers",
                        lambda *a, **k: types.SimpleNamespace(has_trigger=False, triggers=[]))
    outcome = {}   # symbol → "gap"(이력 5봉) | "corrupt" | Exception | None(성공)
    monkeypatch.setattr(monitor, "fetch_portfolio",
                        lambda syms: {s: _bars(5 if outcome.get(s) == "gap" else 30) for s in syms})

    def calc(symbol, df, period):
        result = outcome.get(symbol)
        if isinstance(result, Exception):
            raise result
        if result in ("gap", "corrupt"):
            return None
        return types.SimpleNamespace(symbol=symbol, stop_level=90.0, current_close=100.0, highest_high=110.0)
    monkeypatch.setattr(monitor, "calc_chandelier_stop", calc)
    return types.SimpleNamespace(state=state, outcome=outcome, monkeypatch=monkeypatch)


def _gaps(env):
    return json.loads(env.state.read_text(encoding="utf-8")).get("data_gaps", {})


def test_threshold_is_three():
    assert config.DATA_GAP_FAIL_AFTER == 3


def test_first_and_second_gap_only_warn_and_the_run_stays_clean(env, caplog):
    env.outcome[GAP] = "gap"
    for expected in (1, 2):
        assert monitor.job_stop_check([GOOD, GAP]) is not None
        assert monitor._problems == []
        assert _gaps(env)[GAP]["count"] == expected
    assert GOOD not in _gaps(env)
    assert any("연속 2/3회" in r.getMessage() for r in caplog.records)


def test_clean_run_after_two_gaps_exits_zero(env):
    env.outcome[GAP] = "gap"
    env.monkeypatch.setattr(monitor.drive_state, "from_env", lambda path: None)
    env.monkeypatch.setattr(monitor, "_portfolio_problem", lambda: None)
    env.monkeypatch.setattr(monitor, "run_due_windows", lambda now=None: monitor.job_stop_check([GOOD, GAP]))
    env.monkeypatch.setenv("GHA_JOB", "auto")
    monitor.job_stop_check([GOOD, GAP])
    monitor.run_github_actions_mode()     # 두 번째 공백도 종료코드 0 — SystemExit 없음
    assert _gaps(env)[GAP]["count"] == 2


def test_third_consecutive_gap_is_a_problem(env):
    env.outcome[GAP] = "gap"
    monitor.job_stop_check([GOOD, GAP])
    monitor.job_stop_check([GOOD, GAP])
    monitor.job_stop_check([GOOD, GAP])
    assert len(monitor._problems) == 1
    assert "종목 처리 실패 1/2" in monitor._problems[0]
    assert "연속" in monitor._problems[0]
    assert GAP not in monitor._problems[0]
    assert _gaps(env)[GAP]["count"] == 3


def test_success_clears_the_streak_and_keeps_since(env):
    env.outcome[GAP] = "gap"
    monitor.job_stop_check([GOOD, GAP])
    since = _gaps(env)[GAP]["since"]
    monitor.job_stop_check([GOOD, GAP])
    assert _gaps(env)[GAP]["since"] == since
    env.outcome[GAP] = None
    monitor.job_stop_check([GOOD, GAP])
    assert GAP not in _gaps(env)
    env.outcome[GAP] = "gap"
    monitor.job_stop_check([GOOD, GAP])
    assert _gaps(env)[GAP]["count"] == 1
    assert monitor._problems == []


def test_exception_is_still_an_immediate_failure_and_does_not_count_as_a_gap(env):
    env.outcome[GAP] = ValueError("bug")
    monitor.job_stop_check([GOOD, GAP])
    assert monitor._problems == ["종목 처리 실패 1/2"]
    assert GAP not in _gaps(env)


def test_none_for_a_reason_other_than_short_history_is_still_an_immediate_failure(env):
    env.outcome[GAP] = "corrupt"          # 30봉 정상 이력인데 계산 불가 — 데이터 부족이 아니다
    monitor.job_stop_check([GOOD, GAP])
    assert monitor._problems == ["종목 처리 실패 1/2"]
    assert GAP not in _gaps(env)


def test_only_this_runs_symbols_are_touched_and_unknown_symbols_are_pruned(env):
    env.state.write_text(json.dumps({"positions": {}, "data_gaps": {
        GOOD: {"count": 2, "since": "2026-01-10T00:00+00:00"},
        "SYNGONE": {"count": 1, "since": "2026-01-10T00:00+00:00"},
    }}), encoding="utf-8")
    env.outcome[GAP] = "gap"
    monitor.job_stop_check([GAP])           # GOOD 은 이번 실행 대상이 아니다
    gaps = _gaps(env)
    assert gaps[GOOD]["count"] == 2
    assert gaps[GAP]["count"] == 1
    assert "SYNGONE" not in gaps


def test_gap_record_save_failure_falls_back_to_failing(env):
    env.outcome[GAP] = "gap"

    def broken(*a, **k):
        raise StateValidationError("State write failed; existing file was preserved")
    env.monkeypatch.setattr(monitor, "record_data_gaps", broken)
    monitor.job_stop_check([GOOD, GAP])
    assert any("종목 처리 실패 1/2" in p for p in monitor._problems)
    assert any("데이터 공백 기록 저장 실패" in p for p in monitor._problems)


def test_no_write_when_nothing_changes(env):
    before = env.state.read_bytes()
    monitor.job_stop_check([GOOD, GAP])
    assert env.state.read_bytes() == before


# ── 상태 검증 ────────────────────────────────────────────────────────────────
def test_old_state_without_the_section_still_validates():
    assert validate_state({"positions": {}, "alert_log": {}})


def test_valid_section_validates():
    validate_state({"positions": {}, "data_gaps": {GAP: {"count": 1, "since": "2026-01-12T12:00+00:00"}}})


@pytest.mark.parametrize("section", [
    [],
    {GAP: []},
    {GAP: {"since": "2026-01-12T12:00+00:00"}},
    {GAP: {"count": 0, "since": "2026-01-12T12:00+00:00"}},
    {GAP: {"count": -1, "since": "2026-01-12T12:00+00:00"}},
    {GAP: {"count": 1.0, "since": "2026-01-12T12:00+00:00"}},
    {GAP: {"count": True, "since": "2026-01-12T12:00+00:00"}},
    {GAP: {"count": "1", "since": "2026-01-12T12:00+00:00"}},
    {GAP: {"count": 1}},
    {GAP: {"count": 1, "since": "not a time"}},
    {GAP: {"count": 1, "since": ""}},
    {"": {"count": 1, "since": "2026-01-12T12:00+00:00"}},
])
def test_malformed_section_is_rejected(section):
    with pytest.raises(StateValidationError):
        validate_state({"positions": {}, "data_gaps": section})


def test_record_data_gaps_counts_clears_and_prunes(env):
    now = datetime(2026, 1, 12, 12, 0, tzinfo=timezone.utc)
    assert sm.record_data_gaps([GAP], [GOOD], [GOOD, GAP], now_utc=now) == {GAP: 1}
    assert sm.record_data_gaps([GAP], [GOOD], [GOOD, GAP], now_utc=now) == {GAP: 2}
    assert _gaps(env)[GAP] == {"count": 2, "since": "2026-01-12T12:00+00:00"}
    assert sm.record_data_gaps([], [GAP], [GOOD, GAP], now_utc=now) == {}
    assert _gaps(env) == {}
