"""Exercise real calculator/configuration failure boundaries without live state."""
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import atr_calculator
import monitor
import stop_manager
from state_validation import StateValidationError


def prices():
    return pd.DataFrame({'Open': 100., 'High': 101., 'Low': 99., 'Close': 100., 'Volume': 1000.},
                        index=pd.date_range('2000-01-01', periods=60))


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    state = tmp_path / 'state.json'
    state.write_text(json.dumps({'positions': {}, 'alert_log': {}}), encoding='utf-8')
    monkeypatch.setattr(stop_manager, 'DATA_FILE', state)
    monkeypatch.setattr(monitor, 'STATE_FILE', state)
    monkeypatch.setattr(monitor, 'calc_chandelier_stop', atr_calculator.calc_chandelier_stop)
    monkeypatch.setattr(monitor, '_problems', [])
    monkeypatch.setattr(monitor, 'load_stops', lambda: {})
    monkeypatch.setattr(monitor, 'check_immediate_triggers', lambda *args: SimpleNamespace(has_trigger=False))
    monkeypatch.setattr(monitor, 'update_stop', lambda *args, **kwargs: pytest.fail('invalid data must not update stops'))
    monkeypatch.setattr(monitor, 'tg', SimpleNamespace(send_message=lambda *a, **k: True))
    return state


@pytest.mark.parametrize('row,value', [(25, np.nan), (-1, np.inf), (0, 0), (30, -2)])
def test_invalid_calculation_never_saves_stops_or_reports_clean_success(isolated, monkeypatch, row, value):
    bars = prices()
    bars.iloc[row, bars.columns.get_loc('High')] = value
    monkeypatch.setattr(monitor, 'fetch_portfolio', lambda syms: {'SYM-BAD': bars})
    before = isolated.read_bytes()
    result = monitor.job_stop_check(['SYM-BAD'])
    assert result.chandelier == []
    assert monitor._problems
    assert isolated.read_bytes() == before


def test_valid_symbol_continues_when_another_cannot_be_calculated(isolated, monkeypatch):
    good, bad = prices(), prices()
    bad.iloc[20, bad.columns.get_loc('Low')] = np.nan
    monkeypatch.setattr(monitor, 'fetch_portfolio', lambda syms: {'SYM-BAD': bad, 'SYM-GOOD': good})
    result = monitor.job_stop_check(['SYM-BAD', 'SYM-GOOD'])
    assert [item.symbol for item in result.chandelier] == ['SYM-GOOD']
    assert monitor._problems


def test_valid_prices_still_update_registered_stop_after_notification(isolated, monkeypatch):
    stop_manager.add_position('SYM-GOOD', 100, 90)
    monkeypatch.setattr(monitor, 'load_stops', stop_manager.load_all)
    monkeypatch.setattr(monitor, 'update_stop', stop_manager.update_stop)
    monkeypatch.setattr(monitor, 'fetch_portfolio', lambda syms: {'SYM-GOOD': prices()})
    stop_at_send = []
    def sent(*args, **kwargs):
        stop_at_send.append(stop_manager.get_position('SYM-GOOD').current_stop)
        return True
    monkeypatch.setattr(monitor, 'tg', SimpleNamespace(send_message=sent, fmt_stop_update=lambda pending: 'synthetic update'))
    monkeypatch.setattr(monitor, '_send_chart_quietly', lambda *args, **kwargs: None)
    result = monitor.job_stop_check(['SYM-GOOD'])
    assert result.updated_symbols == ['SYM-GOOD']
    assert stop_at_send == [90]
    assert stop_manager.get_position('SYM-GOOD').current_stop == result.chandelier[0].stop_level
    assert not monitor._problems


def test_invalid_calculation_makes_github_run_fail(isolated, monkeypatch):
    bars = prices()
    bars.iloc[-1, bars.columns.get_loc('Close')] = np.nan
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    monkeypatch.setenv('GHA_JOB', 'stop_check')
    monkeypatch.setattr(monitor, 'ALL_SYMBOLS', ['SYM-BAD'])
    monkeypatch.setattr(monitor, 'fetch_portfolio', lambda syms: {'SYM-BAD': bars})
    monkeypatch.setattr(monitor.drive_state, 'from_env', lambda path: None)
    with pytest.raises(SystemExit) as error:
        monitor.run_github_actions_mode()
    assert error.value.code == 1


@pytest.mark.parametrize('ci_value', ['true', 'True', 'TRUE'])
def test_configuration_error_stops_before_state_pull_or_collection(isolated, monkeypatch, ci_value):
    monkeypatch.setenv('GITHUB_ACTIONS', ci_value)
    monkeypatch.setenv('GHA_JOB', 'stop_check')
    monkeypatch.setattr(monitor._config, 'PORTFOLIO_ERROR', 'invalid_stock_list', raising=False)
    monkeypatch.setattr(monitor, 'ALL_SYMBOLS', ['SYM-OLD'])
    monkeypatch.setattr(monitor.drive_state, 'from_env', lambda path: pytest.fail('must stop before state pull'))
    monkeypatch.setattr(monitor, 'job_stop_check', lambda: pytest.fail('must not collect'))
    with pytest.raises(SystemExit) as error:
        monitor.run_github_actions_mode()
    assert error.value.code == 1


def test_invalid_latest_price_preserves_registered_position(isolated, monkeypatch):
    stop_manager.add_position('SYM-BAD', 100, 90)
    monkeypatch.setattr(monitor, 'load_stops', stop_manager.load_all)
    bars = prices()
    bars.iloc[-1, bars.columns.get_loc('Close')] = np.nan
    monkeypatch.setattr(monitor, 'fetch_portfolio', lambda syms: {'SYM-BAD': bars})
    before = isolated.read_bytes()
    result = monitor.job_stop_check(['SYM-BAD'])
    assert result.chandelier == []
    assert monitor._problems
    assert isolated.read_bytes() == before


def test_local_explicit_bad_configuration_cannot_start_scheduler(isolated, monkeypatch):
    monkeypatch.setattr(monitor, 'IS_GITHUB_ACTIONS', False)
    monkeypatch.setattr(monitor._config, 'PORTFOLIO_ERROR', 'invalid_stock_list', raising=False)
    monkeypatch.setattr(monitor.sys, 'argv', ['monitor.py'])
    monkeypatch.setattr(monitor, 'run_scheduler', lambda: pytest.fail('must not start scheduler'))
    with pytest.raises(SystemExit) as error:
        monitor.main()
    assert error.value.code == 1


def test_state_validation_failure_is_a_failed_github_run(isolated, monkeypatch):
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    monkeypatch.setenv('GHA_JOB', 'stop_check')
    monkeypatch.setattr(monitor.drive_state, 'from_env', lambda path: None)
    def broken():
        raise StateValidationError('State root must be an object')
    monkeypatch.setattr(monitor, 'job_stop_check', broken)
    with pytest.raises(SystemExit) as error:
        monitor.run_github_actions_mode()
    assert error.value.code == 1
    assert len(monitor._problems) == 1
