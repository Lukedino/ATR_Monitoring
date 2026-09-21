"""Drive error boundaries: mocked clients, synthetic secrets, no remote calls."""
import builtins
import hashlib
import json
import socket
import traceback
from types import SimpleNamespace

import pytest

import drive_state as ds


PRIVATE_TOKEN = "SYNTHETIC-SECRET-TOKEN"
PRIVATE_FILE = "SYNTHETIC-PRIVATE-FILE"
PRIVATE_URI = f"https://example.invalid/drive/{PRIVATE_FILE}?access_token={PRIVATE_TOKEN}"
PRIVATE_ERROR = f"request failed: {PRIVATE_URI}; credentials={PRIVATE_TOKEN}"
OLD = b'{"positions": {}, "alert_log": {}}'
NEW = b'{"positions": {}, "alert_log": {}, "done_windows": {"2026-09-22": ["mock-window"]}}'


def assert_sanitized(error, caplog, capsys):
    captured = capsys.readouterr()
    rendered = "".join(traceback.format_exception(error))
    public = str(error) + repr(error) + rendered + caplog.text + captured.out + captured.err
    assert PRIVATE_TOKEN not in public
    assert PRIVATE_FILE not in public
    assert PRIVATE_URI not in public
    assert PRIVATE_ERROR not in public
    assert error.__cause__ is None


def failing(error):
    def fail(*args, **kwargs):
        raise error
    return fail


@pytest.mark.parametrize("module", ["google.oauth2", "googleapiclient.discovery"])
def test_build_dependency_failure_is_sanitized(monkeypatch, caplog, capsys, module):
    original_import = builtins.__import__

    def broken_import(name, *args, **kwargs):
        if name == module:
            raise ImportError(PRIVATE_ERROR)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken_import)
    with pytest.raises(ds.StateSyncError, match="dependency initialization") as caught:
        ds.build_service("{}")
    assert_sanitized(caught.value, caplog, capsys)


def test_invalid_credential_json_is_sanitized(caplog, capsys):
    with pytest.raises(ds.StateSyncError, match="authentication configuration") as caught:
        ds.build_service('{"synthetic_private_key": "' + PRIVATE_TOKEN)
    assert_sanitized(caught.value, caplog, capsys)


def test_credential_library_error_is_sanitized(monkeypatch, caplog, capsys):
    from google.oauth2 import service_account
    monkeypatch.setattr(service_account.Credentials, "from_service_account_info",
                        failing(ValueError(PRIVATE_ERROR)))
    with pytest.raises(ds.StateSyncError, match="authentication configuration") as caught:
        ds.build_service(json.dumps({"synthetic_private_key": PRIVATE_TOKEN}))
    assert_sanitized(caught.value, caplog, capsys)


def test_discovery_library_error_is_sanitized(monkeypatch, caplog, capsys):
    from google.oauth2 import service_account
    from googleapiclient import discovery
    monkeypatch.setattr(service_account.Credentials, "from_service_account_info", lambda *a, **k: object())
    monkeypatch.setattr(discovery, "build", failing(TimeoutError(PRIVATE_ERROR)))
    with pytest.raises(ds.StateSyncError, match="service initialization") as caught:
        ds.build_service("{}")
    assert_sanitized(caught.value, caplog, capsys)


def test_successful_build_keeps_existing_scope_and_discovery_contract(monkeypatch):
    from google.oauth2 import service_account
    from googleapiclient import discovery
    credential = object()
    client = object()
    calls = []

    def credentials(info, scopes):
        assert info == {"synthetic": "configuration"}
        calls.append(scopes)
        return credential

    def build(api, version, **kwargs):
        assert (api, version) == ("drive", "v3")
        assert kwargs == {"credentials": credential, "cache_discovery": False}
        return client

    monkeypatch.setattr(service_account.Credentials, "from_service_account_info", credentials)
    monkeypatch.setattr(discovery, "build", build)
    assert ds.build_service('{"synthetic": "configuration"}') is client
    assert calls == [["https://www.googleapis.com/auth/drive"]]


@pytest.mark.parametrize("failure", ["raw", "already_sanitized"])
def test_from_env_normalizes_initialization_errors(monkeypatch, tmp_path, caplog, capsys, failure):
    monkeypatch.setenv(ds.STATE_FILE_ID_ENV, PRIVATE_FILE)
    monkeypatch.setenv(ds.SA_JSON_ENV, PRIVATE_TOKEN)
    error = RuntimeError(PRIVATE_ERROR) if failure == "raw" else ds.StateSyncError("Drive authentication configuration failed")
    monkeypatch.setattr(ds, "build_service", failing(error))
    with pytest.raises(ds.StateSyncError) as caught:
        ds.from_env(tmp_path / "state.json")
    assert_sanitized(caught.value, caplog, capsys)
    assert not (tmp_path / "state.json").exists()


def external_error(kind):
    if kind == "authentication":
        from google.auth.exceptions import RefreshError
        return RefreshError(PRIVATE_ERROR)
    if kind == "http":
        from googleapiclient.errors import HttpError
        from httplib2 import Response
        return HttpError(Response({"status": "403"}),
                         json.dumps({"error": {"message": PRIVATE_ERROR}}).encode(), uri=PRIVATE_URI)
    if kind == "transport":
        return socket.timeout(PRIVATE_ERROR)
    return RuntimeError(PRIVATE_ERROR)


class MockFiles:
    def __init__(self):
        self.raw = OLD
        self.phase = None
        self.error = None
        self.calls = {"get_media": 0, "update": 0, "execute": 0}
        self.uploaded = None
        self.response = None

    def get_media(self, **kwargs):
        self.calls["get_media"] += 1
        if self.phase == "request":
            raise self.error
        return SimpleNamespace(execute=self.download)

    def download(self):
        self.calls["execute"] += 1
        if self.phase == "execute":
            raise self.error
        return self.raw

    def update(self, *, fileId, media_body, fields):
        self.calls["update"] += 1
        if self.phase == "request":
            raise self.error
        self.uploaded = media_body.getbytes(0, media_body.size())
        return SimpleNamespace(execute=self.upload)

    def upload(self):
        self.calls["execute"] += 1
        if self.phase == "execute":
            raise self.error
        return self.response


@pytest.fixture
def client(tmp_path):
    path = tmp_path / "state.json"
    path.write_bytes(OLD)
    files = MockFiles()
    service = SimpleNamespace(files=lambda: files)
    instance = ds.DriveState(path, PRIVATE_FILE, service)
    instance._pulled_md5 = hashlib.md5(OLD).hexdigest()
    return instance, files, service


@pytest.mark.parametrize("phase", ["files", "request", "execute"])
@pytest.mark.parametrize("kind", ["authentication", "http", "transport", "unexpected"])
def test_pull_errors_preserve_local_state_and_checksum(client, monkeypatch, caplog, capsys, phase, kind):
    instance, files, service = client
    previous_md5 = instance._pulled_md5
    files.phase, files.error = phase, external_error(kind)
    if phase == "files":
        monkeypatch.setattr(service, "files", failing(files.error))
    with pytest.raises(ds.StateSyncError, match="download failed") as caught:
        instance.pull()
    assert instance.local_path.read_bytes() == OLD
    assert instance._pulled_md5 == previous_md5
    assert files.calls["get_media"] <= 1
    assert files.calls["execute"] <= 1
    assert_sanitized(caught.value, caplog, capsys)


@pytest.mark.parametrize("phase", ["files", "request", "execute"])
@pytest.mark.parametrize("kind", ["authentication", "http", "transport", "unexpected"])
def test_push_errors_are_not_retried_or_acknowledged(client, monkeypatch, caplog, capsys, phase, kind):
    instance, files, service = client
    instance.local_path.write_bytes(NEW)
    previous_md5 = instance._pulled_md5
    files.phase, files.error = phase, external_error(kind)
    if phase == "files":
        monkeypatch.setattr(service, "files", failing(files.error))
    with pytest.raises(ds.StateSyncError, match="remote result is unconfirmed") as caught:
        instance.push()
    assert instance.local_path.read_bytes() == NEW
    assert instance._pulled_md5 == previous_md5
    assert files.calls["update"] <= 1
    assert files.calls["execute"] <= 1
    assert_sanitized(caught.value, caplog, capsys)


@pytest.mark.parametrize("failure", ["import", "media"])
def test_push_preparation_errors_are_sanitized_before_any_remote_update(client, monkeypatch, caplog, capsys, failure):
    instance, files, _ = client
    instance.local_path.write_bytes(NEW)
    previous_md5 = instance._pulled_md5
    if failure == "import":
        original_import = builtins.__import__

        def broken_import(name, *args, **kwargs):
            if name == "googleapiclient.http":
                raise ImportError(PRIVATE_ERROR)
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", broken_import)
    else:
        from googleapiclient import http
        monkeypatch.setattr(http, "MediaIoBaseUpload", failing(RuntimeError(PRIVATE_ERROR)))
    with pytest.raises(ds.StateSyncError, match="upload preparation failed") as caught:
        instance.push()
    assert instance.local_path.read_bytes() == NEW
    assert instance._pulled_md5 == previous_md5
    assert files.calls["update"] == 0
    assert_sanitized(caught.value, caplog, capsys)


@pytest.mark.parametrize("response", [None, [], {}, {"md5Checksum": PRIVATE_TOKEN}])
def test_unverifiable_upload_does_not_advance_checksum(client, caplog, capsys, response):
    instance, files, _ = client
    instance.local_path.write_bytes(NEW)
    previous_md5 = instance._pulled_md5
    files.response = response
    with pytest.raises(ds.StateSyncError, match="verification failed") as caught:
        instance.push()
    assert instance.local_path.read_bytes() == NEW
    assert instance._pulled_md5 == previous_md5
    assert files.calls["update"] == files.calls["execute"] == 1
    assert_sanitized(caught.value, caplog, capsys)


def test_sdk_transport_timeout_uses_one_attempt_even_if_server_may_have_committed(client, monkeypatch, caplog, capsys):
    """Exercise the real SDK's default execute retry policy with a fake transport."""
    from googleapiclient.http import HttpRequest
    instance, files, _ = client
    instance.local_path.write_bytes(NEW)
    previous_md5 = instance._pulled_md5
    requests = []

    def request(*args, **kwargs):
        requests.append(True)
        raise socket.timeout(PRIVATE_ERROR)

    sdk_request = HttpRequest(SimpleNamespace(request=request), lambda r, c: {},
                              uri=PRIVATE_URI, method="PATCH", body=b"{}")
    monkeypatch.setattr(files, "update", lambda **kwargs: sdk_request)
    with pytest.raises(ds.StateSyncError) as caught:
        instance.push()
    assert requests == [True]
    assert instance.local_path.read_bytes() == NEW
    assert instance._pulled_md5 == previous_md5
    assert_sanitized(caught.value, caplog, capsys)


def test_successful_pull_and_push_advance_checksum_only_after_validation(client):
    instance, files, _ = client
    files.raw = NEW
    assert instance.pull()["done_windows"] == {"2026-09-22": ["mock-window"]}
    assert instance.local_path.read_bytes() == NEW
    assert instance._pulled_md5 == hashlib.md5(NEW).hexdigest()
    instance.local_path.write_bytes(OLD)
    files.response = {"md5Checksum": hashlib.md5(OLD).hexdigest()}
    assert instance.push() is True
    assert files.uploaded == OLD
    assert instance._pulled_md5 == hashlib.md5(OLD).hexdigest()
