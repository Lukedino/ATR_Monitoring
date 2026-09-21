"""Local state integrity with synthetic symbols, temporary files and mocked Drive."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import json
from pathlib import Path
import time

import pytest

import drive_state as ds
import state_validation as sv
import stop_manager as sm


SYMBOL = "SYM-0001"


def position():
    return {"symbol": SYMBOL, "entry_price": 100.0, "current_stop": 90.0,
            "highest_high": 110.0}


def state():
    return {"positions": {SYMBOL: position()},
            "alert_log": {SYMBOL: {"date": "2026-09-20"}},
            "done_windows": {"2026-09-20": ["mock-window"]}}


def encoded(data):
    return json.dumps(data).encode("utf-8")


@pytest.fixture
def local_state(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_bytes(encoded(state()))
    monkeypatch.setattr(sm, "DATA_FILE", path)
    return path


@pytest.mark.parametrize("section,value", [
    ("positions", []), ("positions", None), ("alert_log", []),
    ("alert_log", "invalid"), ("done_windows", []),
])
def test_invalid_sections_rejected_locally_and_by_drive(section, value):
    data = state()
    data[section] = value
    with pytest.raises(sv.StateValidationError):
        sv.validate_state_bytes(encoded(data))
    with pytest.raises(ds.StateSyncError):
        ds.validate_state_bytes(encoded(data))


@pytest.mark.parametrize("field,value", [
    ("entry_price", "100"), ("current_stop", True), ("highest_high", None),
    ("entry_price", float("nan")), ("current_stop", float("inf")),
    ("highest_high", float("-inf")), ("stage", True), ("stage", -1),
    ("stage", "1"), ("symbol", "SYM-OTHER"), ("last_updated", []),
])
def test_invalid_position_values_reject_entire_state(field, value):
    data = state()
    data["positions"][SYMBOL][field] = value
    with pytest.raises(sv.StateValidationError):
        sv.validate_state_bytes(encoded(data))


@pytest.mark.parametrize("field,value", [
    ("date", "invalid"), ("sent_at", 7), ("triggers", "STOP NEAR"),
    ("triggers", [7]), ("triggers", [""]), ("close", "100"),
    ("close", True), ("stop", float("inf")),
])
def test_invalid_alert_values_are_rejected(field, value):
    data = state()
    data["alert_log"][SYMBOL][field] = value
    with pytest.raises(sv.StateValidationError):
        sv.validate_state_bytes(encoded(data))


@pytest.mark.parametrize("done", [
    {"invalid": []}, {"2026-09-20": "mock-window"}, {"2026-09-20": [None]},
])
def test_invalid_done_windows_are_rejected(done):
    data = state()
    data["done_windows"] = done
    with pytest.raises(sv.StateValidationError):
        sv.validate_state_bytes(encoded(data))


def test_legacy_optional_fields_and_unknown_metadata_are_preserved(local_state):
    data = state()
    data["future_extension"] = {"enabled": True, "revision": 2}
    data["positions"][SYMBOL]["future_label"] = "synthetic"
    local_state.write_bytes(encoded(data))
    assert sm.load_all()[SYMBOL].stage == 0
    sm.mark_window_done("second-window", date(2026, 9, 20))
    sm.update_stop(SYMBOL, 95, 120)
    saved = json.loads(local_state.read_bytes())
    assert saved["future_extension"] == data["future_extension"]
    assert saved["positions"][SYMBOL]["future_label"] == "synthetic"
    assert saved["alert_log"][SYMBOL] == {"date": "2026-09-20"}


@pytest.mark.parametrize("raw", [
    b"", b"{broken", b'{"positions": []}',
    b'{"positions": {}, "positions": {}}',
    b'{"positions": {}, "extension": NaN}',
])
def test_existing_broken_state_is_never_returned_as_empty_or_overwritten(local_state, raw):
    local_state.write_bytes(raw)
    for operation in (
        sm.load_all, lambda: sm.add_position("SYM-0002", 100, 90),
        lambda: sm.mark_window_done("mock-window", date(2026, 9, 20)),
        lambda: sm._save_raw({"positions": {}}),
    ):
        with pytest.raises(sv.StateValidationError):
            operation()
        assert local_state.read_bytes() == raw


def test_missing_local_state_remains_supported_without_creating_a_file(tmp_path, monkeypatch):
    path = tmp_path / "new-folder" / "state.json"
    monkeypatch.setattr(sm, "DATA_FILE", path)
    assert sm.load_all() == {}
    assert not path.parent.exists()
    sm.add_position(SYMBOL, 100, 90)
    assert sm.load_all()[SYMBOL].current_stop == 90


def test_missing_position_fields_are_not_silently_dropped(local_state):
    data = state()
    del data["positions"][SYMBOL]["highest_high"]
    local_state.write_bytes(encoded(data))
    before = local_state.read_bytes()
    with pytest.raises(sv.StateValidationError):
        sm.load_all()
    with pytest.raises(sv.StateValidationError):
        sm.add_position("SYM-0002", 100, 90)
    assert local_state.read_bytes() == before


@pytest.mark.parametrize("failure", ["partial_write", "fsync", "replace", "revalidation"])
def test_atomic_save_failure_preserves_original_and_removes_temp(local_state, monkeypatch, failure):
    before = local_state.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("synthetic disk failure")

    if failure == "partial_write":
        original_factory = sv.tempfile.NamedTemporaryFile

        class PartialWriter:
            def __enter__(self):
                self.handle = original_factory(mode="wb", dir=local_state.parent, delete=False)
                self.name = self.handle.name
                return self

            def write(self, raw):
                self.handle.write(raw[:8])
                raise OSError("synthetic partial write")

            def __exit__(self, *args):
                self.handle.close()

        monkeypatch.setattr(sv.tempfile, "NamedTemporaryFile", lambda **kwargs: PartialWriter())
    elif failure == "revalidation":
        original_read = Path.read_bytes
        monkeypatch.setattr(Path, "read_bytes", lambda path:
                            b'{"positions": []}' if path.suffix == ".tmp" else original_read(path))
    else:
        monkeypatch.setattr(sv.os, failure if failure == "fsync" else "replace", fail)
    with pytest.raises(sv.StateValidationError):
        sm.add_position("SYM-0002", 110, 95)
    assert local_state.read_bytes() == before
    assert list(local_state.parent.iterdir()) == [local_state]


def test_atomic_save_checks_exact_staged_bytes_even_if_json_is_valid(local_state, monkeypatch):
    before = local_state.read_bytes()
    original_read = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda path:
                        b'{"positions": {}}' if path.suffix == ".tmp" else original_read(path))
    with pytest.raises(sv.StateValidationError, match="does not match"):
        sm.add_position("SYM-0002", 110, 95)
    assert local_state.read_bytes() == before


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "100"])
def test_invalid_update_cannot_be_reported_as_held_or_saved(local_state, value):
    before = local_state.read_bytes()
    with pytest.raises(sv.StateValidationError):
        sm.update_stop(SYMBOL, value, 120)
    assert local_state.read_bytes() == before


def test_error_messages_never_include_private_json_values(local_state):
    private = "SYNTHETIC-PRIVATE-SENTINEL"
    raw = encoded({"positions": {private: {"symbol": private, "entry_price": private}}})
    for validator in (sv.validate_state_bytes, ds.validate_state_bytes):
        with pytest.raises(RuntimeError) as error:
            validator(raw)
        assert private not in str(error.value)
    local_state.write_bytes(raw)
    with pytest.raises(sv.StateValidationError) as error:
        sm.load_all()
    assert private not in str(error.value)


def test_concurrent_position_alert_window_updates_do_not_lose_each_other(local_state, monkeypatch):
    original_read = sm._load_raw

    def slow_read():
        data = original_read()
        time.sleep(0.002)
        return data

    monkeypatch.setattr(sm, "_load_raw", slow_read)
    tasks = []
    for number in range(12):
        symbol = f"SYM-{number + 1000:04d}"
        tasks.extend([
            lambda symbol=symbol: sm.add_position(symbol, 100, 90),
            lambda symbol=symbol: sm.mark_trigger_sent(symbol, ["MOCK TRIGGER"], 100, 90),
            lambda number=number: sm.mark_window_done(f"mock-{number}", date(2026, 9, 21)),
        ])
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda task: task(), tasks))
    saved = sv.validate_state_bytes(local_state.read_bytes())
    assert len(saved["positions"]) == 13
    assert len(saved["alert_log"]) == 13
    assert len(saved["done_windows"]["2026-09-21"]) == 12


class MockFiles:
    def __init__(self, raw):
        self.raw = raw
        self.updates = []

    def get_media(self, **kwargs):
        return self

    def execute(self):
        return self.raw

    def update(self, **kwargs):
        self.updates.append(kwargs)
        raise AssertionError("Unexpected mock Drive update")


class MockService:
    def __init__(self, raw):
        self.mock_files = MockFiles(raw)

    def files(self):
        return self.mock_files


@pytest.mark.parametrize("raw", [b'{"positions": []}', b'{"alert_log": {"SYM-0001": {"close": "100"}}}'])
def test_invalid_drive_pull_preserves_existing_local_state(local_state, raw):
    before = local_state.read_bytes()
    client = ds.DriveState(local_state, "mock-state-file", MockService(raw))
    with pytest.raises(ds.StateSyncError):
        client.pull()
    assert local_state.read_bytes() == before
    assert client._pulled_md5 is None


def test_drive_pull_disk_failure_preserves_local_and_checksum(local_state, monkeypatch):
    before = local_state.read_bytes()
    client = ds.DriveState(local_state, "mock-state-file", MockService(b'{"positions": {}}'))

    def fail(*args):
        raise OSError("synthetic fsync failure")

    monkeypatch.setattr(sv.os, "fsync", fail)
    with pytest.raises(ds.StateSyncError):
        client.pull()
    assert local_state.read_bytes() == before
    assert client._pulled_md5 is None


def test_drive_push_rejects_wrong_numeric_types_before_any_update(local_state):
    data = state()
    data["positions"][SYMBOL]["current_stop"] = "invalid"
    local_state.write_bytes(encoded(data))
    service = MockService(b'{"positions": {}}')
    client = ds.DriveState(local_state, "mock-state-file", service)
    with pytest.raises(ds.StateSyncError):
        client.push()
    assert service.mock_files.updates == []
