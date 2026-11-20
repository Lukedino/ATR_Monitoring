"""종목 하나의 일시적 데이터 공백(ATR 계산 불가)이 실행 전체를 '실패' 로 만들지 않는다.

2026-11-19 야후가 한 종목 이력을 하루 동안 짧게 줘서 16회 실행이 전부 종료코드 1 이었다.
이력 부족(insufficient_history)만 종목별 연속 기록을 상태에 남기고, 연속 DATA_GAP_FAIL_AFTER 회 또는
DATA_GAP_MAX_HOURS 시간 중 먼저 닿는 쪽부터 실패로 올린다. 한 실행에 DATA_GAP_MAX_SYMBOLS 종목을
넘으면 유예 없이 전부 실패다. 예외·가격 모순은 지금처럼 즉시 실패다. 심볼은 전부 가명이다.
"""
import json
import types
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

import config
import monitor
import state_validation
import stop_manager as sm
from state_validation import StateValidationError, validate_state

GOOD, GAP = "SYM-0A01", "SYM-0B02"
EXTRA = ["SYM-0C03", "SYM-0D04", "SYM-0E05"]
GONE = "SYM-0F06"
T0 = datetime(2026, 1, 12, 12, 0, tzinfo=timezone.utc)


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
    monkeypatch.setattr(monitor, "ALL_SYMBOLS", [GOOD, GAP, *EXTRA])
    monkeypatch.setattr(monitor, "_is_market_active_for_triggers", lambda s: True)
    monkeypatch.setattr(monitor, "check_immediate_triggers",
                        lambda *a, **k: types.SimpleNamespace(has_trigger=False, triggers=[]))
    clock = {"now": T0}
    monkeypatch.setattr(monitor, "_now_utc", lambda: clock["now"])
    outcome = {}   # symbol → "gap"(이력 5봉) | "corrupt" | "empty"(시세 없음) | Exception | None(성공)

    def frame(symbol):
        kind = outcome.get(symbol)
        if kind == "empty":
            return pd.DataFrame()
        return _bars(5 if kind == "gap" else 30)
    monkeypatch.setattr(monitor, "fetch_portfolio", lambda syms: {s: frame(s) for s in syms})

    def calc(symbol, df, period):
        result = outcome.get(symbol)
        if isinstance(result, Exception):
            raise result
        if result in ("gap", "corrupt"):
            return None
        return types.SimpleNamespace(symbol=symbol, stop_level=90.0, current_close=100.0, highest_high=110.0)
    monkeypatch.setattr(monitor, "calc_chandelier_stop", calc)
    return types.SimpleNamespace(state=state, outcome=outcome, clock=clock, monkeypatch=monkeypatch)


def _gaps(env):
    return json.loads(env.state.read_text(encoding="utf-8")).get("data_gaps", {})


def _run(env, symbols=(GOOD, GAP), *, after=timedelta(minutes=30)):
    result = monitor.job_stop_check(list(symbols))
    env.clock["now"] += after
    return result


def test_thresholds():
    assert (config.DATA_GAP_FAIL_AFTER, config.DATA_GAP_MAX_HOURS, config.DATA_GAP_MAX_SYMBOLS) == (3, 3, 2)


# ── 연속 횟수 ────────────────────────────────────────────────────────────────
def test_first_and_second_gap_only_warn_and_the_run_stays_clean(env, caplog):
    env.outcome[GAP] = "gap"
    for expected in (1, 2):
        assert _run(env) is not None
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
    _run(env)
    monitor.run_github_actions_mode()     # 두 번째 공백도 종료코드 0 — SystemExit 없음
    assert _gaps(env)[GAP]["count"] == 2


def test_third_consecutive_gap_is_a_problem(env):
    env.outcome[GAP] = "gap"
    for _ in range(3):
        _run(env)
    assert len(monitor._problems) == 1
    assert monitor._problems[0].startswith("종목 처리 실패 1/2 (데이터 부족 1종목")
    assert GAP not in monitor._problems[0]
    assert _gaps(env)[GAP]["count"] == 3


def test_success_clears_the_streak_and_keeps_since(env):
    env.outcome[GAP] = "gap"
    _run(env)
    since = _gaps(env)[GAP]["since"]
    _run(env)
    assert _gaps(env)[GAP]["since"] == since == T0.isoformat(timespec="minutes")
    env.outcome[GAP] = None
    _run(env)
    assert GAP not in _gaps(env)
    env.outcome[GAP] = "gap"
    _run(env)
    assert _gaps(env)[GAP]["count"] == 1
    assert monitor._problems == []


# ── 경과 시간 상한 ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("gap, problem", [
    (timedelta(hours=3) - timedelta(minutes=1), False),
    (timedelta(hours=3), True),
    (timedelta(hours=6), True),       # 주말 크립토 슬롯 간격 — 둘째 실행에서 바로 실패
])
def test_second_gap_fails_once_the_streak_is_three_hours_old(env, gap, problem):
    env.outcome[GAP] = "gap"
    _run(env, after=gap)
    _run(env)
    assert _gaps(env)[GAP]["count"] == 2
    assert bool(monitor._problems) is problem


# ── 동시 다발 상한 ───────────────────────────────────────────────────────────
def test_two_simultaneous_gaps_are_still_tolerated(env):
    env.outcome.update({GAP: "gap", EXTRA[0]: "gap"})
    _run(env, [GOOD, GAP, *EXTRA])
    assert monitor._problems == []
    assert {_gaps(env)[s]["count"] for s in (GAP, EXTRA[0])} == {1}


def test_more_than_two_simultaneous_gaps_fail_at_once_but_are_still_counted(env):
    env.outcome.update({GAP: "gap", EXTRA[0]: "gap", EXTRA[1]: "gap"})
    _run(env, [GOOD, GAP, *EXTRA])
    assert len(monitor._problems) == 1
    assert monitor._problems[0].startswith("종목 처리 실패 3/5 (데이터 부족 3종목")
    assert {_gaps(env)[s]["count"] for s in (GAP, *EXTRA[:2])} == {1}
    # 다음 실행에 하나만 남으면 그 종목의 연속 기록은 이어진다(2회째 — 아직 유예).
    env.outcome.update({EXTRA[0]: None, EXTRA[1]: None})
    monitor._problems.clear()
    _run(env, [GOOD, GAP, *EXTRA])
    assert monitor._problems == []
    assert _gaps(env) == {GAP: {"count": 2, "since": T0.isoformat(timespec="minutes")}}


# ── 즉시 실패로 남는 경로 ────────────────────────────────────────────────────
def test_exception_is_still_an_immediate_failure_and_does_not_count_as_a_gap(env):
    env.outcome[GAP] = ValueError("bug")
    _run(env)
    assert monitor._problems == ["종목 처리 실패 1/2"]
    assert GAP not in _gaps(env)


def test_none_for_a_reason_other_than_short_history_is_still_an_immediate_failure(env):
    env.outcome[GAP] = "corrupt"          # 30봉 정상 이력인데 계산 불가 — 데이터 부족이 아니다
    _run(env)
    assert monitor._problems == ["종목 처리 실패 1/2"]
    assert GAP not in _gaps(env)


def test_real_state_write_failure_falls_back_to_failing(env):
    env.outcome[GAP] = "gap"
    before = env.state.read_bytes()

    def refuse(*a, **k):
        raise OSError("disk full")
    env.monkeypatch.setattr(state_validation.os, "replace", refuse)
    _run(env)
    assert any(p.startswith("종목 처리 실패 1/2") for p in monitor._problems)
    assert "데이터 공백 기록 저장 실패: StateValidationError" in monitor._problems
    assert env.state.read_bytes() == before


# ── 기록 범위 ────────────────────────────────────────────────────────────────
def test_only_this_runs_symbols_are_touched_and_unknown_symbols_are_pruned(env):
    env.state.write_text(json.dumps({"positions": {}, "data_gaps": {
        GOOD: {"count": 2, "since": "2026-01-12T11:00+00:00"},
        GONE: {"count": 1, "since": "2026-01-12T11:00+00:00"},
    }}), encoding="utf-8")
    env.outcome[GAP] = "gap"
    _run(env, [GAP])                      # GOOD 은 이번 실행 대상이 아니다
    gaps = _gaps(env)
    assert gaps[GOOD]["count"] == 2
    assert gaps[GAP]["count"] == 1
    assert GONE not in gaps


def test_missing_quote_keeps_the_streak_untouched(env):
    env.state.write_text(json.dumps({"positions": {}, "data_gaps": {
        GAP: {"count": 2, "since": "2026-01-12T11:30+00:00"},
    }}), encoding="utf-8")
    env.outcome[GAP] = "empty"            # 5종목 중 4종목 수집 = 80% — 수집률 검사는 통과
    assert _run(env, [GOOD, GAP, *EXTRA]) is not None
    assert monitor._problems == []
    assert _gaps(env)[GAP] == {"count": 2, "since": "2026-01-12T11:30+00:00"}


def test_no_write_when_nothing_changes(env):
    before = env.state.read_bytes()
    _run(env)
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
    assert sm.record_data_gaps([GAP], [GOOD], [GOOD, GAP], now_utc=T0) == {
        GAP: {"count": 1, "since": "2026-01-12T12:00+00:00"}}
    assert sm.record_data_gaps([GAP], [GOOD], [GOOD, GAP], now_utc=T0 + timedelta(hours=1))[GAP] == {
        "count": 2, "since": "2026-01-12T12:00+00:00"}
    assert sm.record_data_gaps([], [GAP], [GOOD, GAP], now_utc=T0) == {}
    assert _gaps(env) == {}
