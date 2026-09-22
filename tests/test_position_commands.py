"""실제 state/서비스 없는 관리 후보·중단·동일 후보 복구 회귀."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import drive_state as ds
import position_commands as pc
import position_cli as cli
import stop_manager as sm

OLD = b'{"positions": {}, "alert_log": {}, "extension": {"keep": true}}'
THIRD = b'{"positions": {}, "alert_log": {}, "extension": {"third": true}}'
SECRET = "SYNTHETIC-PRIVATE-token"


class Files:
    def __init__(self, body=OLD):
        self.body = body
        self.updates = 0
        self.observations = 0
        self.mode = None

    def get_media(self, **kwargs):
        def execute():
            self.observations += 1
            if self.mode == "read" or (self.mode == "readback" and self.updates):
                raise TimeoutError(SECRET)
            return self.body
        return SimpleNamespace(execute=execute)

    def get(self, **kwargs):
        def execute():
            if self.mode == "preflight":
                self.body = THIRD
            return {"md5Checksum": hashlib.md5(self.body).hexdigest()}
        return SimpleNamespace(execute=execute)

    def update(self, **kwargs):
        def execute():
            self.updates += 1
            media = kwargs["media_body"]
            body = media.getbytes(0, media.size())
            if self.mode != "unsent":
                self.body = body
            if self.mode in {"unsent", "lost_ack"}:
                raise TimeoutError(SECRET)
            return {"md5Checksum": hashlib.md5(body).hexdigest()}
        return SimpleNamespace(execute=execute)


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "state.json"
    path.write_bytes(OLD)
    files = Files()
    drive = ds.DriveState(path, "synthetic-target", SimpleNamespace(files=lambda: files))
    return path, files, drive


def add(scope="drive", **kwargs):
    return pc.Command("add", scope, "SYNTH.KS", 100, 120, **kwargs)


def recover(scope="drive", retry=False):
    return pc.Command("recover", scope, retry=retry)


@pytest.mark.parametrize("value", ["", "  ", "123456", "123456.0", "SYN TH", "SYN\tTH", "SYN\nTH", "\x00SYN", "SYN\x7f", "SYN\u0085", "SYN\u202e"])
def test_invalid_symbol_has_zero_io(setup, monkeypatch, value):
    path, files, drive = setup
    def forbidden(*args, **kwargs):
        raise AssertionError("validation must precede state access")
    monkeypatch.setattr(pc, "_read", forbidden)
    with pytest.raises(pc.PositionCommandError, match="symbol_invalid"):
        pc.execute(pc.Command("add", "drive", value, 1, 2), path, drive=drive)
    assert files.observations == files.updates == 0
    assert path.read_bytes() == OLD


@pytest.mark.parametrize("value", [0, -1, "nan", "inf", "-inf", True, "abc", None, 10**999])
def test_invalid_prices_do_not_change_state(setup, value):
    path, files, drive = setup
    with pytest.raises(pc.PositionCommandError, match="price_invalid"):
        pc.execute(pc.Command("add", "drive", "SYNTH.KS", value, 2), path, drive=drive)
    assert path.read_bytes() == OLD
    assert files.observations == files.updates == 0
    assert not pc.journal_path(path).exists()


@pytest.mark.parametrize("symbol", ["syn.th", "0010V0.KS", "02826K.KQ", "SYNTH-USD", "SYNTH-USDT", "^SYNTH", "NA", "NULL"])
def test_normal_symbols_preserved_and_no_stop_below_entry_policy(setup, symbol):
    path, _, _ = setup
    result = pc.execute(pc.Command("add", "local", "  " + symbol + "  ", 1, 2), path)
    record = result["state"]["positions"][symbol.upper()]
    assert record["entry_price"] == 1 and record["current_stop"] == 2


def test_same_client_verified_publish_then_local_promotion(setup):
    path, files, drive = setup
    result = pc.execute(add(), path, drive=drive)
    assert result["code"] == "remote_confirmed"
    assert path.read_bytes() == files.body
    assert result["state"]["extension"] == {"keep": True}
    assert files.updates == 1 and files.observations == 2
    journal = pc.read_journal(path)
    assert journal["phase"] == "completed"
    assert SECRET not in pc.journal_path(path).read_text()
    assert "synthetic-target" not in pc.journal_path(path).read_text()
    assert str(path.parent) not in pc.journal_path(path).read_text()


def test_missing_local_can_use_verified_remote_without_materializing_before_publish(setup):
    path, files, drive = setup
    path.unlink()
    files.mode = "unsent"
    with pytest.raises(pc.PositionCommandError, match="unconfirmed"):
        pc.execute(add(), path, drive=drive)
    assert not path.exists()
    assert pc.read_journal(path)["before"] is None


@pytest.mark.parametrize("mode", ["read", "preflight", "unsent", "lost_ack", "readback"])
def test_publish_boundary_preserves_previous_local_and_same_candidate(setup, mode):
    path, files, drive = setup
    files.mode = mode
    with pytest.raises(pc.PositionCommandError) as error:
        pc.execute(add(), path, drive=drive)
    assert SECRET not in str(error.value)
    assert path.read_bytes() == OLD
    if mode == "read":
        assert pc.read_journal(path) is None
        assert files.updates == 0
        return
    journal = pc.read_journal(path)
    assert journal["phase"] == "unknown"
    frozen = pc.journal_path(path).read_bytes()
    with pytest.raises(pc.PositionCommandError, match="recovery_required"):
        pc.execute(add(), path, drive=drive)
    assert pc.journal_path(path).read_bytes() == frozen
    with pytest.raises(ds.StateSyncError, match="recovery"):
        drive.pull()
    with pytest.raises(ds.StateSyncError, match="recovery"):
        drive.push()
    assert path.read_bytes() == OLD


def test_lost_ack_recovery_observes_candidate_without_second_upload(setup):
    path, files, drive = setup
    files.mode = "lost_ack"
    with pytest.raises(pc.PositionCommandError):
        pc.execute(add(), path, drive=drive)
    identity = pc.read_journal(path)["id"]
    files.mode = None
    assert pc.execute(recover(), path, drive=drive)["code"] == "remote_confirmed"
    assert files.updates == 1 and path.read_bytes() == files.body
    assert pc.read_journal(path)["id"] == identity
    assert pc.execute(recover(), path, drive=drive)["code"] == "no_pending"
    assert files.updates == 1


def test_unsent_requires_explicit_retry_of_same_id_and_bytes(setup):
    path, files, drive = setup
    files.mode = "unsent"
    with pytest.raises(pc.PositionCommandError):
        pc.execute(add(), path, drive=drive)
    journal = pc.read_journal(path)
    files.mode = None
    assert pc.execute(recover(), path, drive=drive)["code"] == "retry_required"
    assert files.updates == 1 and path.read_bytes() == OLD
    assert pc.execute(recover(retry=True), path, drive=drive)["code"] == "remote_confirmed"
    assert files.updates == 2
    after = pc.read_journal(path)
    assert (after["id"], after["candidate"]) == (journal["id"], journal["candidate"])


def test_remote_third_content_and_wrong_target_hold(setup):
    path, files, drive = setup
    files.mode = "unsent"
    with pytest.raises(pc.PositionCommandError):
        pc.execute(add(), path, drive=drive)
    files.mode = None
    files.body = THIRD
    frozen = pc.journal_path(path).read_bytes()
    with pytest.raises(pc.PositionCommandError, match="remote_state_conflict"):
        pc.execute(recover(retry=True), path, drive=drive)
    drive.file_id = "other-synthetic-target"
    before_observations = files.observations
    with pytest.raises(pc.PositionCommandError, match="remote_target_conflict"):
        pc.execute(recover(retry=True), path, drive=drive)
    assert files.observations == before_observations
    assert files.updates == 1 and path.read_bytes() == OLD
    assert pc.journal_path(path).read_bytes() == frozen


def test_existing_local_difference_is_not_adopted_or_overwritten(setup):
    path, files, drive = setup
    path.write_bytes(THIRD)
    with pytest.raises(pc.PositionCommandError, match="local_remote_conflict"):
        pc.execute(add(), path, drive=drive)
    assert files.updates == 0 and path.read_bytes() == THIRD
    assert not pc.journal_path(path).exists()


def test_remote_published_local_replace_failure_recovers_without_reposting(setup, monkeypatch):
    path, files, drive = setup
    original = pc.atomic_write_state_bytes
    def fail(*args):
        raise pc.StateValidationError("synthetic disk failure")
    monkeypatch.setattr(pc, "atomic_write_state_bytes", fail)
    with pytest.raises(pc.PositionCommandError, match="local_promotion_failed"):
        pc.execute(add(), path, drive=drive)
    assert pc.read_journal(path)["phase"] == "confirmed"
    assert path.read_bytes() == OLD and files.updates == 1
    monkeypatch.setattr(pc, "atomic_write_state_bytes", original)
    pc.execute(recover(), path, drive=drive)
    assert path.read_bytes() == files.body and files.updates == 1


@pytest.mark.parametrize("nth", [1, 2, 3, 4])
def test_journal_crashes_never_lose_previous_or_candidate(setup, monkeypatch, nth):
    path, files, drive = setup
    original = pc._save_journal
    count = 0
    def fail_on_call(*args):
        nonlocal count
        count += 1
        if count == nth:
            raise pc.PositionCommandError("journal_write_failed")
        return original(*args)
    monkeypatch.setattr(pc, "_save_journal", fail_on_call)
    with pytest.raises(pc.PositionCommandError, match="journal_write_failed"):
        pc.execute(add(), path, drive=drive)
    assert path.read_bytes() in (OLD, files.body)
    assert files.updates == (0 if nth <= 2 else 1)
    monkeypatch.setattr(pc, "_save_journal", original)
    if nth == 1:
        assert pc.read_journal(path) is None
    else:
        result = pc.execute(recover(), path, drive=drive)
        assert result["code"] == ("retry_required" if nth == 2 else "remote_confirmed")
        assert files.updates == (0 if nth == 2 else 1)


def test_local_changes_survive_following_pull_and_explicit_same_candidate_publish(setup, monkeypatch):
    path, files, drive = setup
    pc.execute(add("local"), path)
    candidate = path.read_bytes()
    monkeypatch.setattr(sm, "DATA_FILE", path)
    assert "SYNTH.KS" in sm.load_all()
    with pytest.raises(ds.StateSyncError, match="recovery"):
        drive.pull()
    assert path.read_bytes() == candidate and files.observations == 0
    assert pc.execute(recover(), path, drive=drive)["code"] == "retry_required"
    assert files.updates == 0
    assert pc.execute(recover(retry=True), path, drive=drive)["code"] == "remote_confirmed"
    assert files.body == candidate
    drive.pull()
    assert path.read_bytes() == candidate


def test_second_local_command_keeps_original_unpublished_baseline(setup):
    path, files, drive = setup
    pc.execute(add("local"), path)
    pc.execute(pc.Command("add", "local", "SYNTH2.KS", 2, 3), path)
    assert pc._unpack(pc.read_journal(path)["baseline"]) == OLD
    pc.execute(recover(retry=True), path, drive=drive)
    assert len(json.loads(files.body)["positions"]) == 2


def test_other_local_change_after_candidate_is_not_silently_published(setup):
    path, files, drive = setup
    pc.execute(add("local"), path)
    path.write_bytes(THIRD)
    with pytest.raises(pc.PositionCommandError, match="local_state_conflict"):
        pc.execute(recover(retry=True), path, drive=drive)
    assert files.updates == files.observations == 0 and path.read_bytes() == THIRD


def test_pending_blocks_monitor_read_and_write(setup, monkeypatch):
    path, files, drive = setup
    files.mode = "unsent"
    with pytest.raises(pc.PositionCommandError):
        pc.execute(add(), path, drive=drive)
    monkeypatch.setattr(sm, "DATA_FILE", path)
    for operation in (sm.load_all, lambda: sm._save_raw({"positions": {}})):
        with pytest.raises(pc.PositionCommandError, match="recovery_required"):
            operation()
    assert path.read_bytes() == OLD


def test_explicit_replace_preserves_extensions_but_resets_only_requested_position(setup):
    path, _, _ = setup
    pc.execute(add("local"), path)
    raw = json.loads(path.read_bytes())
    raw["positions"]["SYNTH.KS"].update(stage=2, future="preserve")
    path.write_text(json.dumps(raw))
    before = path.read_bytes()
    with pytest.raises(pc.PositionCommandError, match="position_exists"):
        pc.execute(add("local"), path)
    assert path.read_bytes() == before
    result = pc.execute(add("local", replace=True), path)
    assert result["state"]["positions"]["SYNTH.KS"]["stage"] == 0
    assert result["state"]["positions"]["SYNTH.KS"]["future"] == "preserve"


@pytest.mark.parametrize("mutation", ["json", "hash", "missing", "unknown", "version"])
def test_corrupt_journal_never_allows_pull_or_recovery(setup, mutation):
    path, files, drive = setup
    pc.execute(add("local"), path)
    raw = pc.journal_path(path).read_bytes()
    data = json.loads(raw)
    if mutation == "json":
        raw = b"{broken"
    else:
        if mutation == "hash": data["candidate"]["sha256"] = "0" * 64
        if mutation == "missing": del data["before"]
        if mutation == "unknown": data["future"] = True
        if mutation == "version": data["version"] = True
        raw = json.dumps(data).encode()
    pc.journal_path(path).write_bytes(raw)
    before = path.read_bytes()
    with pytest.raises(ds.StateSyncError): drive.pull()
    with pytest.raises(pc.PositionCommandError): pc.execute(recover(retry=True), path, drive=drive)
    assert files.observations == files.updates == 0
    assert path.read_bytes() == before


@pytest.mark.parametrize("argv", [
    ["--add-pos", SECRET], ["--remove-pos"], ["--list-pos", "--replace"],
    ["--add-pos", "SYNTH.KS", "1", "2", "--retry"], ["--list-pos", "--bad", SECRET],
    ["--list-pos", "--state-scope", SECRET], ["--list-pos", "--remove-pos", SECRET],
    ["--state-scope", "drive"], ["--replace"], ["--recover-state", "--retry"],
])
def test_cli_invalid_arguments_sanitized_before_factory_or_state(argv, monkeypatch, capsys):
    def forbidden(*args, **kwargs): raise AssertionError("unexpected IO")
    monkeypatch.setattr(cli, "execute", forbidden)
    monkeypatch.setattr(cli, "_drive", forbidden)
    assert cli.maybe_run(argv) == 1
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err


def test_cli_dispatch_help_local_scope_and_strict_drive_configuration(setup, monkeypatch, capsys):
    path, _, _ = setup
    monkeypatch.setattr(cli, "STATE_PATH", path)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv(ds.STATE_FILE_ID_ENV, raising=False)
    monkeypatch.delenv(ds.SA_JSON_ENV, raising=False)
    assert cli.maybe_run(["--once"]) is None
    assert cli.maybe_run(["--add-pos", "--help"]) == 0
    assert cli.maybe_run(["--list-pos", "--state-scope", "drive"]) == 1
    assert "drive_configuration_required" in capsys.readouterr().out
    assert path.read_bytes() == OLD
    assert cli.maybe_run(["--add-pos", "SYNTH.KS", "10", "20"]) == 0
    assert "local_only" in capsys.readouterr().out
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert cli.maybe_run(["--list-pos"]) == 1
    assert "SYNTH.KS" not in capsys.readouterr().out


def test_command_modules_have_no_config_import():
    import ast
    for module in (pc, cli):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name != "config" for alias in node.names)
            if isinstance(node, ast.ImportFrom):
                assert node.module != "config"


@pytest.mark.parametrize("failure", ["replace", "fsync", "short_read"])
def test_real_journal_atomic_failure_preserves_previous_bytes(setup, monkeypatch, failure):
    path, files, drive = setup
    pc.execute(add("local"), path)
    original_journal = pc.journal_path(path).read_bytes()
    original_state = path.read_bytes()
    next_journal = pc.read_journal(path)
    next_journal["id"] = "0" * 32
    if failure == "replace":
        def fail(*args): raise OSError(SECRET)
        monkeypatch.setattr(pc.os, "replace", fail)
    elif failure == "fsync":
        def fail(*args): raise OSError(SECRET)
        monkeypatch.setattr(pc.os, "fsync", fail)
    else:
        read_bytes = Path.read_bytes
        def short_read(self):
            raw = read_bytes(self)
            return raw[:-1] if self.suffix == ".tmp" else raw
        monkeypatch.setattr(Path, "read_bytes", short_read)
    with pytest.raises(pc.PositionCommandError, match="journal_write_failed"):
        pc._save_journal(path, next_journal)
    assert pc.journal_path(path).read_bytes() == original_journal
    assert path.read_bytes() == original_state
    assert files.updates == files.observations == 0
    assert list(pc.journal_path(path).parent.glob("*.tmp")) == []


def test_local_crash_before_materialize_resumes_same_candidate(setup, monkeypatch):
    path, _, _ = setup
    original = pc.atomic_write_state_bytes
    def fail(*args): raise pc.StateValidationError("synthetic failure")
    monkeypatch.setattr(pc, "atomic_write_state_bytes", fail)
    with pytest.raises(pc.PositionCommandError, match="local_promotion_failed"):
        pc.execute(add("local"), path)
    journal = pc.read_journal(path)
    assert journal["phase"] == "prepared" and path.read_bytes() == OLD
    with pytest.raises(pc.PositionCommandError, match="recovery_required"):
        pc.assert_no_pending(path)
    monkeypatch.setattr(pc, "atomic_write_state_bytes", original)
    assert pc.execute(recover("local"), path)["code"] == "local_only"
    assert pc.read_journal(path)["id"] == journal["id"]
    assert path.read_bytes() == pc._unpack(journal["candidate"])


def test_remove_preserves_alert_windows_and_unknown_fields(setup):
    path, files, drive = setup
    raw = json.loads(OLD)
    raw["positions"]["SYNTH.KS"] = {"symbol": "SYNTH.KS", "entry_price": 1, "current_stop": 2, "highest_high": 3}
    raw["alert_log"]["SYNTH.KS"] = {"date": "2026-09-22", "future": {"keep": True}}
    raw["done_windows"] = {"2026-09-22": ["synthetic-window"]}
    body = json.dumps(raw).encode()
    files.body = body
    path.write_bytes(body)
    result = pc.execute(pc.Command("remove", "drive", "SYNTH.KS"), path, drive=drive)
    assert result["state"]["positions"] == {}
    for key in ("alert_log", "done_windows", "extension"):
        assert result["state"][key] == raw[key]
    assert files.updates == 1


@pytest.mark.parametrize("source", ["local", "remote"])
def test_corrupt_baseline_has_no_candidate_or_publish(setup, source):
    path, files, drive = setup
    if source == "local": path.write_bytes(b"{broken")
    else: files.body = b"{broken"
    before = path.read_bytes()
    with pytest.raises(pc.PositionCommandError):
        pc.execute(add(), path, drive=drive)
    assert not pc.journal_path(path).exists()
    assert path.read_bytes() == before and files.updates == 0


def test_duplicate_journal_key_fails_closed(setup):
    path, files, drive = setup
    pc.execute(add("local"), path)
    raw = pc.journal_path(path).read_bytes()
    pc.journal_path(path).write_bytes(b'{"version":1,' + raw[1:])
    with pytest.raises(pc.PositionCommandError, match="journal_invalid"):
        pc.execute(recover(retry=True), path, drive=drive)
    assert files.observations == files.updates == 0


def test_semantically_equal_pretty_remote_preserves_extensions(setup):
    path, files, drive = setup
    files.body = json.dumps(json.loads(OLD), indent=4).encode()
    assert pc.execute(add(), path, drive=drive)["code"] == "remote_confirmed"
    assert files.updates == 1
