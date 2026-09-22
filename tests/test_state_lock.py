"""Synthetic lock API regressions; real OS process tests use the guarded harness."""
from concurrent.futures import ThreadPoolExecutor
import errno
import os
from pathlib import Path
import threading

import pytest

import state_lock as sl


def test_leaf_symlink_is_rejected_before_resolving_or_entering(tmp_path, monkeypatch):
    state = tmp_path / "synthetic-private-alias.json"
    original_is_symlink = Path.is_symlink
    original_resolve = Path.resolve
    def is_symlink(path):
        return True if path == state else original_is_symlink(path)
    def resolve(path, *args, **kwargs):
        if path == state:
            pytest.fail("A leaf symlink must be rejected before following its target")
        return original_resolve(path, *args, **kwargs)
    monkeypatch.setattr(Path, "is_symlink", is_symlink)
    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(sl.StateLockError, match="^STATE_LOCK_PATH_SYMLINK$") as error:
        with sl.state_transaction(state):
            pytest.fail("Entered a leaf-symlink transaction")
    assert "synthetic-private" not in str(error.value)
    assert not state.with_suffix(".json.lock").exists()


def test_real_leaf_symlink_and_original_state_are_preserved(tmp_path):
    target = tmp_path / "target.json"
    alias = tmp_path / "alias.json"
    original = b'{"positions":{}}'
    target.write_bytes(original)
    try:
        alias.symlink_to(target)
    except NotImplementedError:
        pytest.skip("Platform does not implement symlinks")
    except OSError as error:
        if getattr(error, "winerror", None) == 1314 or error.errno in {
            errno.EPERM, errno.ENOSYS, errno.EOPNOTSUPP,
        }:
            pytest.skip("Symlink creation is unsupported or requires unavailable privileges")
        raise  # In particular, never swallow an offline I/O guard rejection.
    with pytest.raises(sl.StateLockError, match="^STATE_LOCK_PATH_SYMLINK$"):
        with sl.state_transaction(alias):
            pytest.fail("Atomic replacement could split the lock identity")
    assert alias.is_symlink()
    assert target.read_bytes() == original
    assert not target.with_suffix(".json.lock").exists()
    assert not alias.with_suffix(".json.lock").exists()


def test_nested_alias_keeps_outer_lock_and_sidecar(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    original = sl._try_lock
    attempts = []

    def observe(handle):
        attempts.append(handle)
        return original(handle)

    monkeypatch.setattr(sl, "_try_lock", observe)
    monkeypatch.chdir(tmp_path)
    with sl.state_transaction(state):
        with sl.state_transaction("state.json", timeout=0):
            state.write_text('{"positions":{}}', encoding="utf-8")
        assert len(attempts) == 1
        assert not attempts[0].closed
    assert attempts[0].closed
    assert state.with_suffix(".json.lock").is_file()


def test_other_thread_times_out_without_entering_body(tmp_path):
    state = tmp_path / "state.json"

    def contender():
        with sl.state_transaction(state, timeout=0.03):
            pytest.fail("Contender entered locked transaction")

    with ThreadPoolExecutor(max_workers=1) as pool:
        with sl.state_transaction(state):
            future = pool.submit(contender)
            with pytest.raises(sl.StateLockError, match="^STATE_LOCK_TIMEOUT$"):
                future.result(timeout=2)


def test_different_path_can_progress_while_first_locked(tmp_path):
    entered = threading.Event()

    def other_path():
        with sl.state_transaction(tmp_path / "b.json", timeout=0.2):
            entered.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with sl.state_transaction(tmp_path / "a.json"):
            pool.submit(other_path).result(timeout=2)
            assert entered.is_set()


def test_exception_releases_and_preserves_state(tmp_path):
    state = tmp_path / "state.json"
    state.write_bytes(b'{"positions":{}}')
    with pytest.raises(ValueError, match="synthetic failure"):
        with sl.state_transaction(state):
            raise ValueError("synthetic failure")
    with sl.state_transaction(state, timeout=0):
        assert state.read_bytes() == b'{"positions":{}}'


@pytest.mark.parametrize("timeout", [-1, True, None, "1", float("inf"), float("nan")])
def test_invalid_timeout_cannot_enter(tmp_path, timeout):
    with pytest.raises(sl.StateLockError, match="^STATE_LOCK_TIMEOUT_INVALID$"):
        with sl.state_transaction(tmp_path / "state.json", timeout=timeout):
            pytest.fail("Entered invalid timeout transaction")


def test_open_failure_is_sanitized_and_retry_is_possible(tmp_path, monkeypatch):
    from pathlib import Path
    original = Path.open
    state = tmp_path / "private-synthetic.json"

    def fail(*args, **kwargs):
        raise OSError("SYNTHETIC_PRIVATE_VALUE")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", fail)
        with pytest.raises(sl.StateLockError, match="^STATE_LOCK_ACQUIRE_FAILED$") as error:
            with sl.state_transaction(state):
                pytest.fail("Entered failed transaction")
        assert error.value.__suppress_context__
    assert Path.open is original
    with sl.state_transaction(state, timeout=0):
        pass


def test_failed_os_acquisition_closes_handle(tmp_path, monkeypatch):
    observed = []

    def fail(handle):
        observed.append(handle)
        raise OSError("SYNTHETIC_PRIVATE_VALUE")

    monkeypatch.setattr(sl, "_try_lock", fail)
    with pytest.raises(sl.StateLockError, match="^STATE_LOCK_ACQUIRE_FAILED$"):
        with sl.state_transaction(tmp_path / "state.json"):
            pytest.fail("Entered failed transaction")
    assert observed[0].closed


def test_forked_process_fails_before_reusing_thread_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(sl, "_process_id", os.getpid() + 1)
    with pytest.raises(sl.StateLockError, match="^STATE_LOCK_PROCESS_CHANGED$"):
        with sl.state_transaction(tmp_path / "state.json"):
            pytest.fail("Inherited process lock was reused")
