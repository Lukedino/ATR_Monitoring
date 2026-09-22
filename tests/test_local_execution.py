"""Local invocation outcomes without provider, Telegram or operational state."""
import builtins
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

import pandas as pd
import pytest
import schedule

import monitor


@pytest.fixture
def local(monkeypatch):
    monkeypatch.setattr(monitor, 'IS_GITHUB_ACTIONS', False)
    monkeypatch.setattr(monitor, '_problems', [])
    monkeypatch.setattr(monitor._config, 'PORTFOLIO_ERROR', '')
    monkeypatch.setitem(sys.modules, 'position_cli', SimpleNamespace(maybe_run=lambda argv: None))
    monkeypatch.setattr(monitor, 'tg', SimpleNamespace(send_message=lambda *a, **k: True,
                                                    send_photo=lambda *a, **k: True))


@pytest.mark.parametrize('flag,job', [('--stop-check', 'job_stop_check'),
    ('--trigger-check', 'job_trigger_check'), ('--kr-report', 'job_kr_daily_report'),
    ('--us-report', 'job_us_daily_report')])
@pytest.mark.parametrize('kind', ['reported', 'exception', 'false', 'success'])
def test_one_shot_cli_reports_real_failure(local, monkeypatch, flag, job, kind):
    def task():
        if kind == 'reported':
            monitor._note_problem('synthetic failure')
        elif kind == 'exception':
            raise RuntimeError('private synthetic payload')
        elif kind == 'false':
            return False
    monkeypatch.setattr(monitor, job, task)
    monkeypatch.setattr(sys, 'argv', ['monitor.py', flag])
    if kind == 'success':
        assert monitor.main() == 0
    else:
        with pytest.raises(SystemExit) as error:
            monitor.main()
        assert error.value.code == 1 and monitor._problems
        assert 'private synthetic payload' not in ' '.join(monitor._problems)


def test_once_keeps_success_after_other_job_failure_and_next_invocation_resets(local, monkeypatch):
    calls = []
    def first():
        calls.append('KR')
        raise OSError('synthetic')
    monkeypatch.setattr(monitor, 'job_kr_daily_report', first)
    monkeypatch.setattr(monitor, 'job_us_daily_report', lambda: calls.append('US'))
    monkeypatch.setattr(sys, 'argv', ['monitor.py', '--once'])
    with pytest.raises(SystemExit):
        monitor.main()
    assert calls == ['KR', 'US']
    monkeypatch.setattr(monitor, 'job_kr_daily_report', lambda: calls.append('KR-OK'))
    assert monitor.main() == 0 and monitor._problems == []


@pytest.mark.parametrize('fault', ['empty', 'invalid', 'render', 'empty_image', 'photo_false', 'photo_exception', 'success'])
def test_explicit_chart_requires_valid_bars_and_confirmed_photo(local, monkeypatch, fault):
    frame = pd.DataFrame() if fault == 'empty' else pd.DataFrame({'Close': [10]})
    monkeypatch.setattr(monitor, 'fetch_ohlcv', lambda symbol: frame)
    monkeypatch.setattr(monitor, 'atr_input_issue', lambda *args: 'invalid' if fault == 'invalid' else '')
    monkeypatch.setattr(monitor, 'load_stops', lambda: {})
    calls = []
    def render(*args, **kwargs):
        calls.append('render')
        if fault == 'render':
            raise ValueError('synthetic')
        return b'' if fault == 'empty_image' else b'png'
    def send(*args, **kwargs):
        calls.append('send')
        if fault == 'photo_exception':
            raise ValueError('synthetic')
        return fault != 'photo_false'
    monkeypatch.setattr(monitor, 'plot_atr_chart', render)
    monkeypatch.setattr(monitor.tg, 'send_photo', send)
    monkeypatch.setattr(sys, 'argv', ['monitor.py', '--chart', 'ZZQ'])
    if fault == 'success':
        assert monitor.main() == 0
    else:
        with pytest.raises(SystemExit) as error:
            monitor.main()
        assert error.value.code == 1
    if fault in {'empty', 'invalid'}:
        assert calls == []


def test_admin_script_dispatch_runs_before_config_or_provider_import(monkeypatch):
    calls = []
    monkeypatch.setenv('GITHUB_ACTIONS', 'false')
    monkeypatch.setattr(sys, 'argv', ['monitor.py', '--add-pos', 'ZZQ', 'invalid', '2'])
    monkeypatch.setitem(sys.modules, 'position_cli', SimpleNamespace(maybe_run=lambda argv: calls.append(argv) or 2))
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name in {'config', 'data_collector', 'telegram_bot', 'visualizer', 'schedule'}:
            pytest.fail('Administrative command imported an operational module')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', guarded)
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(Path(monitor.__file__)), run_name='__main__')
    assert error.value.code == 2
    assert calls == [['--add-pos', 'ZZQ', 'invalid', '2']]


def test_scheduler_keeps_original_intervals_and_continues_after_a_failed_job(local, monkeypatch):
    scheduler = schedule.Scheduler()
    monkeypatch.setattr(monitor.schedule, 'every', scheduler.every)
    calls = []
    def failed():
        calls.append('KR')
        raise RuntimeError('synthetic')
    monkeypatch.setattr(monitor, 'job_kr_daily_report', failed)
    monkeypatch.setattr(monitor, 'job_us_daily_report', lambda: calls.append('US'))
    monkeypatch.setattr(monitor, 'job_stop_check', lambda: calls.append('stop'))
    monkeypatch.setattr(monitor, 'job_trigger_check', lambda: calls.append('trigger'))
    def pending():
        for job in scheduler.jobs:
            job.run()
    monkeypatch.setattr(monitor.schedule, 'run_pending', pending)
    monkeypatch.setattr(monitor.time, 'sleep', lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt()))
    monitor.run_scheduler()
    assert calls == ['KR', 'US', 'stop', 'trigger']
    assert [(job.unit, job.interval) for job in scheduler.jobs] == [('days', 1), ('days', 1), ('minutes', 30), ('minutes', 10)]
    assert all(job.last_run is not None and job.next_run > job.last_run for job in scheduler.jobs)
