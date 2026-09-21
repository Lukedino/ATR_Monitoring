"""Runtime failures reach the owner without copying private exception values."""
import logging
from types import SimpleNamespace

import pytest

import drive_state
import monitor


PRIVATE = 'SYNTHETIC-PRIVATE https://example.invalid/?credential=SYNTHETIC-TOKEN'


@pytest.fixture
def runtime(monkeypatch):
    messages, called = [], []
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setenv('GHA_JOB', 'stop_check')
    monkeypatch.setattr(monitor, '_portfolio_problem', lambda: None)
    monkeypatch.setattr(monitor, '_problems', [])
    monkeypatch.setattr(monitor, 'tg', SimpleNamespace(send_message=lambda text, **kwargs: messages.append(text) or True))
    monkeypatch.setattr(monitor, 'job_stop_check', lambda: called.append('job'))
    return messages, called


def test_drive_transport_failure_is_sanitized_before_runtime_notification(runtime, monkeypatch, tmp_path, caplog):
    messages, called = runtime
    def fail(**kwargs):
        raise TimeoutError(PRIVATE)
    client = drive_state.DriveState(tmp_path / 'state.json', 'synthetic-id',
                                    SimpleNamespace(files=lambda: SimpleNamespace(get_media=fail)))
    monkeypatch.setattr(monitor.drive_state, 'from_env', lambda path: client)
    with pytest.raises(SystemExit) as stopped:
        monitor.run_github_actions_mode()
    assert stopped.value.code == 1
    assert not called
    assert any('Drive state download failed' in message for message in messages)
    assert PRIVATE not in caplog.text + str(messages)
    assert 'SYNTHETIC-TOKEN' not in caplog.text + str(messages)


def test_unexpected_job_failure_still_pushes_prior_progress_without_raw_error(runtime, monkeypatch, caplog):
    messages, called = runtime
    def fail():
        raise ValueError(PRIVATE)
    monkeypatch.setattr(monitor, 'job_stop_check', fail)
    client = SimpleNamespace(pull=lambda: {'positions': {}}, push=lambda: called.append('push'))
    monkeypatch.setattr(monitor.drive_state, 'from_env', lambda path: client)
    with pytest.raises(SystemExit) as stopped:
        monitor.run_github_actions_mode()
    assert stopped.value.code == 1
    assert called == ['push']
    assert any('ValueError' in message for message in messages)
    assert 'SYNTHETIC-PRIVATE' not in caplog.text + str(messages)
    assert 'SYNTHETIC-TOKEN' not in caplog.text + str(messages)


def test_push_failure_is_failed_even_after_successful_job(runtime, monkeypatch):
    messages, called = runtime
    def fail():
        called.append('push')
        raise drive_state.StateSyncError('Drive state upload failed; remote result is unconfirmed')
    monkeypatch.setattr(monitor.drive_state, 'from_env', lambda path: SimpleNamespace(pull=lambda: {}, push=fail))
    with pytest.raises(SystemExit) as stopped:
        monitor.run_github_actions_mode()
    assert stopped.value.code == 1
    assert called == ['job', 'push']
    assert len(messages) == 1
    assert 'remote result is unconfirmed' in messages[0]


def test_state_symbol_masks_are_registered_before_job(runtime, monkeypatch):
    _, called = runtime
    loaded = {'positions': {'SYM-SYNTHETIC-OLD': {}}, 'alert_log': {}}
    monkeypatch.setattr(monitor.drive_state, 'from_env', lambda path: SimpleNamespace(pull=lambda: loaded, push=lambda: None))
    def register(state, names):
        assert state is loaded
        called.append('mask')
    monkeypatch.setattr(monitor.log_masking, 'register_state_symbols_for_github_actions', register)
    monitor.run_github_actions_mode()
    assert called == ['mask', 'job']
