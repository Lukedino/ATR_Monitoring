"""Drive preflight conflict detection with synthetic bytes and mocked requests."""
import hashlib
import traceback
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import drive_state as ds
import state_validation as sv


OLD = b'{"positions": {}, "alert_log": {}}'
LOCAL = b'{"positions": {}, "alert_log": {}, "done_windows": {"2026-09-22": ["mock-local"]}}'
REMOTE = b'{"positions": {}, "alert_log": {}, "done_windows": {"2026-09-22": ["mock-remote"]}}'
PRIVATE = "SYNTHETIC-PRIVATE https://example.invalid/?token=SYNTHETIC-TOKEN"
DEFAULT = object()


def md5(raw):
    return hashlib.md5(raw).hexdigest()


class Files:
    def __init__(self):
        self.content = OLD
        self.metadata = DEFAULT
        self.failure = None
        self.calls = []

    def get_media(self, **kwargs):
        self.calls.append("download")

        def execute():
            if self.failure == "download":
                raise TimeoutError(PRIVATE)
            return self.content

        return SimpleNamespace(execute=execute)

    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        if self.failure == "get":
            raise RuntimeError(PRIVATE)

        def execute():
            self.calls.append("get.execute")
            if self.failure == "get.execute":
                raise TimeoutError(PRIVATE)
            if self.metadata is not DEFAULT:
                return self.metadata
            return {"md5Checksum": md5(self.content)}

        return SimpleNamespace(execute=execute)

    def update(self, **kwargs):
        self.calls.append("update")
        media = kwargs["media_body"]
        payload = media.getbytes(0, media.size())

        def execute():
            self.calls.append("update.execute")
            self.content = payload
            return {"md5Checksum": md5(payload)}

        return SimpleNamespace(execute=execute)


@pytest.fixture
def client(tmp_path):
    files = Files()
    service = SimpleNamespace(files=lambda: files)
    instance = ds.DriveState(tmp_path / "state.json", "synthetic-file", service)
    return instance, files, service


def changed_after_pull(client):
    instance, files, _ = client
    instance.pull()
    instance.local_path.write_bytes(LOCAL)
    files.calls.clear()
    return instance, files


def assert_no_update(files):
    assert "update" not in files.calls
    assert "update.execute" not in files.calls


def assert_sanitized(error, caplog, capsys):
    captured = capsys.readouterr()
    public = "".join(traceback.format_exception(error)) + caplog.text + captured.out + captured.err
    assert "SYNTHETIC-PRIVATE" not in public
    assert "SYNTHETIC-TOKEN" not in public
    assert "example.invalid" not in public
    assert error.__cause__ is None


def test_existing_valid_local_file_cannot_upload_without_verified_pull(client):
    instance, files, _ = client
    instance.local_path.write_bytes(LOCAL)
    with pytest.raises(ds.StateSyncError, match="requires a successful pull"):
        instance.push()
    assert files.calls == []
    assert instance._pulled_md5 is None


def test_missing_local_file_is_noop_without_pull(client):
    instance, files, _ = client
    assert instance.push() is False
    assert files.calls == []


def test_unchanged_local_file_is_noop_even_if_remote_changed(client):
    instance, files, _ = client
    instance.pull()
    files.content = REMOTE
    files.calls.clear()
    assert instance.push() is False
    assert files.calls == []
    assert files.content == REMOTE
    assert instance.local_path.read_bytes() == OLD


def test_metadata_preflight_uses_downloaded_bytes_then_verifies_one_upload(client):
    instance, files = changed_after_pull(client)
    assert instance.push() is True
    assert files.calls == [
        ("get", {"fileId": "synthetic-file", "fields": "md5Checksum"}),
        "get.execute", "update", "update.execute",
    ]
    assert instance._pulled_md5 == md5(LOCAL)
    assert files.content == LOCAL
    files.calls.clear()
    assert instance.push() is False
    assert files.calls == []
    instance.local_path.write_bytes(REMOTE)
    assert instance.push() is True
    assert instance._pulled_md5 == md5(REMOTE)


def test_remote_change_rejects_update_and_preserves_both_versions(client):
    instance, files = changed_after_pull(client)
    files.content = REMOTE
    with pytest.raises(ds.StateSyncError, match="changed remotely"):
        instance.push()
    assert_no_update(files)
    assert files.calls.count("get.execute") == 1
    assert files.content == REMOTE
    assert instance.local_path.read_bytes() == LOCAL
    assert instance._pulled_md5 == md5(OLD)


@pytest.mark.parametrize("metadata", [
    None, [], "invalid", {}, {"md5Checksum": None}, {"md5Checksum": True},
    {"md5Checksum": 123}, {"md5Checksum": b"a" * 32}, {"md5Checksum": "a" * 31},
    {"md5Checksum": "a" * 33}, {"md5Checksum": "g" * 32}, {"md5Checksum": "ａ" * 32},
    {"md5Checksum": " " + "a" * 32}, {"md5Checksum": "a" * 32 + "\n"},
    {"md5Checksum": PRIVATE},
])
def test_invalid_remote_checksum_fails_closed(client, metadata, caplog, capsys):
    instance, files = changed_after_pull(client)
    files.metadata = metadata
    with pytest.raises(ds.StateSyncError, match="preflight checksum is invalid") as caught:
        instance.push()
    assert_no_update(files)
    assert files.calls.count("get.execute") == 1
    assert instance._pulled_md5 == md5(OLD)
    assert_sanitized(caught.value, caplog, capsys)


def test_checksum_hex_case_is_not_a_content_conflict(client):
    instance, files = changed_after_pull(client)
    files.metadata = {"md5Checksum": md5(OLD).upper()}
    assert instance.push() is True


@pytest.mark.parametrize("phase", ["files", "get", "get.execute"])
def test_preflight_transport_failure_never_attempts_update(client, monkeypatch, phase, caplog, capsys):
    instance, files = changed_after_pull(client)
    files.failure = phase
    if phase == "files":
        def fail():
            raise RuntimeError(PRIVATE)
        monkeypatch.setattr(client[2], "files", fail)
    with pytest.raises(ds.StateSyncError, match="preflight check failed") as caught:
        instance.push()
    assert_no_update(files)
    assert files.calls.count("get.execute") <= 1
    assert instance._pulled_md5 == md5(OLD)
    assert instance.local_path.read_bytes() == LOCAL
    assert_sanitized(caught.value, caplog, capsys)


@pytest.mark.parametrize("failure", ["download", "invalid", "disk"])
def test_failed_refresh_revokes_old_baseline_until_successful_pull(client, monkeypatch, failure):
    instance, files = changed_after_pull(client)
    if failure == "download":
        files.failure = "download"
    elif failure == "invalid":
        files.content = b"{broken"
    else:
        def fail(*args):
            raise ds.StateValidationError("State file could not be saved")
        monkeypatch.setattr(ds, "atomic_write_state_bytes", fail)
    with pytest.raises(ds.StateSyncError):
        instance.pull()
    assert instance.local_path.read_bytes() == LOCAL
    assert instance._pulled_md5 is None
    files.calls.clear()
    with pytest.raises(ds.StateSyncError, match="requires a successful pull"):
        instance.push()
    assert files.calls == []
    monkeypatch.undo()
    files.failure = None
    files.content = OLD
    instance.pull()
    instance.local_path.write_bytes(LOCAL)
    assert instance.push() is True


@pytest.mark.parametrize("operation", ["pull", "push"])
def test_lock_failure_is_sanitized_before_any_remote_call(client, monkeypatch, operation, caplog, capsys):
    instance, files = changed_after_pull(client)

    @contextmanager
    def unavailable(path):
        assert path == instance.local_path
        raise ds.StateLockError(PRIVATE)
        yield

    monkeypatch.setattr(sv, "state_transaction", unavailable)
    with pytest.raises(ds.StateSyncError, match="local state lock") as caught:
        getattr(instance, operation)()
    assert files.calls == []
    assert instance._pulled_md5 is None
    assert_sanitized(caught.value, caplog, capsys)


@pytest.mark.parametrize("operation", ["exists", "read_bytes"])
def test_local_file_failure_is_sanitized_before_preflight(client, monkeypatch, operation, caplog, capsys):
    instance, files = changed_after_pull(client)
    original = getattr(Path, operation)

    def fail_local(path, *args, **kwargs):
        if path == instance.local_path:
            raise PermissionError(PRIVATE)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, operation, fail_local)
    with pytest.raises(ds.StateSyncError, match="Local state file could not be read") as caught:
        instance.push()
    assert files.calls == []
    assert instance._pulled_md5 == md5(OLD)
    assert_sanitized(caught.value, caplog, capsys)


@pytest.mark.parametrize("failure", ["before_commit", "after_commit", "response"])
def test_uncertain_upload_requires_new_pull_before_retry(client, monkeypatch, failure):
    instance, files = changed_after_pull(client)

    def update(**kwargs):
        files.calls.append("update")

        def execute():
            files.calls.append("update.execute")
            if failure != "before_commit":
                files.content = LOCAL
            if failure == "response":
                return {}
            raise TimeoutError(PRIVATE)

        return SimpleNamespace(execute=execute)

    monkeypatch.setattr(files, "update", update)
    with pytest.raises(ds.StateSyncError, match="remote result is unconfirmed"):
        instance.push()
    assert files.calls.count("update") == files.calls.count("update.execute") == 1
    assert instance._pulled_md5 is None
    files.calls.clear()
    with pytest.raises(ds.StateSyncError, match="requires a successful pull"):
        instance.push()
    assert files.calls == []
