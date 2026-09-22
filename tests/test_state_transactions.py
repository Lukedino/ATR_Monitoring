"""Complete state operations and monitor jobs share one local transaction."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, timezone
import threading
from types import SimpleNamespace

import pandas as pd
import pytest

import drive_state
import monitor
import state_validation as validation
import stop_manager
from state_lock import StateLockError, state_transaction


@pytest.fixture
def local_state(tmp_path, monkeypatch):
    path = tmp_path / "synthetic-state.json"
    path.write_text('{"positions": {}, "alert_log": {}}', encoding="utf-8")
    monkeypatch.setattr(stop_manager, "DATA_FILE", path)
    monkeypatch.setattr(monitor, "STATE_FILE", path)
    monkeypatch.setattr(monitor, "_problems", [])
    return path


def assert_other_thread_can_enter(path, expected):
    def attempt():
        try:
            with state_transaction(path, timeout=0.025):
                return True
        except StateLockError:
            return False
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(attempt).result(timeout=2) is expected


def test_all_nested_state_apis_share_dynamic_path_and_accept_keyword_arguments(local_state, tmp_path, monkeypatch):
    other = tmp_path / "other-state.json"
    for path, symbol in ((local_state, "SYNTH-FIRST"), (other, "SYNTH-SECOND")):
        monkeypatch.setattr(stop_manager, "DATA_FILE", path)
        with state_transaction(path):
            validation.write_state(path=path, data={"positions": {}})
            stop_manager.add_position(symbol, 100, 90)
            stop_manager.mark_trigger_sent(symbol, ["STOP NEAR"], 100, 90)
            stop_manager.mark_window_done("synthetic-window", date(2026, 9, 22))
            assert set(validation.read_state(path=path)["positions"]) == {symbol}
            assert_other_thread_can_enter(path, False)
        assert_other_thread_can_enter(path, True)
    assert set(validation.read_state(local_state)["positions"]) == {"SYNTH-FIRST"}
    assert set(validation.read_state(other)["positions"]) == {"SYNTH-SECOND"}


def test_mutator_holds_transaction_between_read_and_save(local_state, monkeypatch):
    original_load = stop_manager.load_all
    first_read, release, second_attempt = threading.Event(), threading.Event(), threading.Event()
    reads = []
    def pause_first_read():
        records = original_load()
        reads.append(threading.current_thread().name)
        if len(reads) == 1:
            first_read.set()
            assert release.wait(3)
        return records
    monkeypatch.setattr(stop_manager, "load_all", pause_first_read)
    def second():
        second_attempt.set()
        return stop_manager.add_position("SYNTH-SECOND", 100, 90)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(stop_manager.add_position, "SYNTH-FIRST", 100, 90)
        try:
            assert first_read.wait(2)
            later = pool.submit(second)
            assert second_attempt.wait(2)
            assert_other_thread_can_enter(local_state, False)
            assert len(reads) == 1
        finally:
            release.set()
        first.result(timeout=3)
        later.result(timeout=3)
    assert set(validation.read_state(local_state)["positions"]) == {"SYNTH-FIRST", "SYNTH-SECOND"}


@pytest.fixture
def runtime(local_state, monkeypatch):
    calls, messages = [], []
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GHA_JOB", "stop_check")
    monkeypatch.setattr(monitor, "_portfolio_problem", lambda: None)
    monkeypatch.setattr(monitor, "tg", SimpleNamespace(
        send_message=lambda text, **kwargs: messages.append(text) or True))
    return calls, messages


@pytest.mark.parametrize("job_fails", [False, True])
def test_gha_transaction_spans_pull_job_and_push_even_after_job_failure(local_state, runtime, monkeypatch, job_fails):
    calls, messages = runtime
    def phase(name):
        assert_other_thread_can_enter(local_state, False)
        calls.append(name)
    def pull():
        phase("pull")
        # Real nested state access must not reacquire a conflicting OS handle.
        validation.write_state(local_state, {"positions": {}})
        return {"positions": {}}
    def job():
        phase("job")
        stop_manager.add_position("SYNTH-SAVED", 100, 90)
        if job_fails:
            raise ValueError("SYNTHETIC-PRIVATE")
    def push():
        phase("push")
        assert "SYNTH-SAVED" in validation.read_state(local_state)["positions"]
    monkeypatch.setattr(monitor.drive_state, "from_env", lambda path: SimpleNamespace(pull=pull, push=push))
    monkeypatch.setattr(monitor, "job_stop_check", job)
    if job_fails:
        with pytest.raises(SystemExit) as stopped:
            monitor.run_github_actions_mode()
        assert stopped.value.code == 1
        assert "SYNTHETIC-PRIVATE" not in str(messages)
    else:
        monitor.run_github_actions_mode()
    assert calls == ["pull", "job", "push"]
    assert_other_thread_can_enter(local_state, True)


def test_gha_lock_acquisition_failure_never_pulls_runs_or_pushes(runtime, monkeypatch, caplog):
    calls, messages = runtime
    @contextmanager
    def unavailable(path):
        raise StateLockError("SYNTHETIC-PRIVATE-PATH")
        yield
    monkeypatch.setattr(monitor, "state_transaction", unavailable)
    monkeypatch.setattr(monitor.drive_state, "from_env", lambda path: calls.append("initialize"))
    monkeypatch.setattr(monitor, "job_stop_check", lambda: calls.append("job"))
    with pytest.raises(SystemExit) as stopped:
        monitor.run_github_actions_mode()
    assert stopped.value.code == 1
    assert calls == []
    assert len(messages) == 1 and "잠금" in messages[0]
    assert "SYNTHETIC-PRIVATE" not in str(messages) + caplog.text


def test_pull_failure_skips_job_and_push_and_releases_transaction(local_state, runtime, monkeypatch):
    calls, _ = runtime
    def pull():
        assert_other_thread_can_enter(local_state, False)
        calls.append("pull")
        raise drive_state.StateSyncError("Synthetic download failure")
    monkeypatch.setattr(monitor.drive_state, "from_env", lambda path: SimpleNamespace(
        pull=pull, push=lambda: calls.append("push")))
    monkeypatch.setattr(monitor, "job_stop_check", lambda: calls.append("job"))
    with pytest.raises(SystemExit) as stopped:
        monitor.run_github_actions_mode()
    assert stopped.value.code == 1
    assert calls == ["pull"]
    assert_other_thread_can_enter(local_state, True)


@pytest.mark.parametrize("job", ["stop", "trigger", "report", "windows"])
def test_local_jobs_hold_the_same_state_path_transaction(local_state, monkeypatch, job):
    checked = []
    def during_work(*args):
        assert_other_thread_can_enter(local_state, False)
        checked.append(True)
        return {}
    monkeypatch.setattr(monitor, "fetch_portfolio", during_work)
    monkeypatch.setattr(monitor, "load_stops", lambda: {})
    if job == "stop":
        monitor.job_stop_check([])
    elif job == "trigger":
        monitor.job_trigger_check()
    elif job == "report":
        with pytest.raises(RuntimeError, match="수집 실패"):
            monitor._run_daily_report(["SYNTH-REPORT"], "Synthetic report")
    else:
        monkeypatch.setattr(monitor, "job_stop_check", during_work)
        monkeypatch.setattr(monitor.market_hours, "due_windows", lambda now: [])
        monitor.run_due_windows(datetime(2026, 9, 22, tzinfo=timezone.utc))
    assert checked == [True]
    assert_other_thread_can_enter(local_state, True)


@pytest.mark.parametrize("kind", ["trigger", "stop"])
def test_concurrent_jobs_do_not_repeat_send_before_mark_or_stop_commit(local_state, monkeypatch, kind):
    symbol = "SYNTH-LOCKED"
    frame = pd.DataFrame({"Close": [100.0] * 22}, index=pd.date_range("2026-01-01", periods=22))
    sending, release, second_attempt = threading.Event(), threading.Event(), threading.Event()
    messages, fetches = [], []
    def fetch(symbols):
        fetches.append(True)
        return {symbol: frame}
    def send(text, **kwargs):
        messages.append(text)
        sending.set()
        assert release.wait(3)
        return True
    monkeypatch.setattr(monitor, "ALL_SYMBOLS", [symbol])
    monkeypatch.setattr(monitor, "fetch_portfolio", fetch)
    monkeypatch.setattr(monitor, "_is_market_active_for_triggers", lambda symbol: True)
    monkeypatch.setattr(monitor, "_send_chart_quietly", lambda *args: None)
    monkeypatch.setattr(monitor, "tg", SimpleNamespace(
        send_message=send, fmt_trigger_alert=lambda *args: "Synthetic trigger",
        fmt_stop_update=lambda pending: "Synthetic stop update"))
    if kind == "trigger":
        monkeypatch.setattr(monitor, "check_immediate_triggers", lambda *args: SimpleNamespace(
            has_trigger=True, triggers=["STOP NEAR"]))
        run = monitor.job_trigger_check
    else:
        stop_manager.add_position(symbol, 100, 90)
        monkeypatch.setattr(monitor, "check_immediate_triggers", lambda *args: SimpleNamespace(has_trigger=False))
        monkeypatch.setattr(monitor, "calc_chandelier_stop", lambda *args: SimpleNamespace(
            symbol=symbol, stop_level=95.0, current_close=100.0, highest_high=110.0))
        run = monitor.job_stop_check
    def second():
        second_attempt.set()
        return run()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run)
        try:
            assert sending.wait(2)
            later = pool.submit(second)
            assert second_attempt.wait(2)
            assert_other_thread_can_enter(local_state, False)
            assert len(fetches) == 1
        finally:
            release.set()
        first.result(timeout=3)
        later.result(timeout=3)
    assert len(messages) == 1
    if kind == "trigger":
        assert symbol in validation.read_state(local_state)["alert_log"]
    else:
        assert stop_manager.get_position(symbol).current_stop == 95.0
    assert not monitor._problems


def test_window_check_and_completion_remain_inside_one_transaction(local_state, monkeypatch):
    phases = []
    now = datetime(2026, 9, 22, tzinfo=timezone.utc)
    window = SimpleNamespace(name="synthetic-brief", action="stop_check", brief=True,
                             market="KR", local_date=lambda instant: instant.date())
    def check(name, day):
        assert_other_thread_can_enter(local_state, False)
        phases.append("check")
        return False
    def extra(selected, result):
        assert_other_thread_can_enter(local_state, False)
        phases.append("send")
    def mark(name, day):
        assert_other_thread_can_enter(local_state, False)
        phases.append("mark")
        stop_manager.mark_window_done(name, day)
    monkeypatch.setattr(monitor, "job_stop_check", lambda: object())
    monkeypatch.setattr(monitor.market_hours, "due_windows", lambda instant: [window])
    monkeypatch.setattr(monitor, "is_window_done", check)
    monkeypatch.setattr(monitor, "_run_window_extra", extra)
    monkeypatch.setattr(monitor, "mark_window_done", mark)
    monitor.run_due_windows(now)
    assert phases == ["check", "send", "mark"]
    assert validation.read_state(local_state)["done_windows"] == {"2026-09-22": ["synthetic-brief"]}
