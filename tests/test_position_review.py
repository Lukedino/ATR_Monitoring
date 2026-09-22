"""관리 명령의 독립 반례: 설정/네트워크 없이 실제 어댑터를 사용한다."""
import builtins
import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

import pytest

import drive_state
import position_commands as commands
from test_position_commands import Files


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_standalone_help_exits_zero_without_config_state_or_drive(monkeypatch, capsys, flag):
    script = Path(__file__).resolve().parents[1] / "position_cli.py"
    monkeypatch.setattr(sys, "argv", [str(script), flag])
    def forbidden(*args, **kwargs):
        pytest.fail("help must not access state or Drive")
    monkeypatch.setattr(commands, "execute", forbidden)
    original_import = builtins.__import__
    def checked_import(name, *args, **kwargs):
        assert name != "config", "help must not load portfolio configuration"
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", checked_import)
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(str(script), run_name="__main__")
    assert stopped.value.code == 0
    output = capsys.readouterr()
    assert "--add-pos" in output.out and "--state-scope" in output.out


def _body(value):
    return json.dumps({"positions": {}, "alert_log": {}, "extension": {"enabled": value}}).encode()


@pytest.mark.parametrize("phase", ["initial", "recovery"])
def test_boolean_and_numeric_extension_values_are_distinct_states(tmp_path, phase):
    path = tmp_path / "state.json"
    before = _body(True)
    path.write_bytes(before)
    files = Files(before)
    drive = drive_state.DriveState(path, "synthetic-review", SimpleNamespace(files=lambda: files))
    command = commands.Command("add", "drive", "SYM-REVIEW", 10, 20)
    if phase == "recovery":
        files.mode = "unsent"
        with pytest.raises(commands.PositionCommandError, match="remote_publish_unconfirmed"):
            commands.execute(command, path, drive=drive)
        files.mode = None
        pending = commands.journal_path(path).read_bytes()
        command = commands.Command("recover", "drive", retry=True)
    files.body = _body(1)
    previous_updates = files.updates
    with pytest.raises(commands.PositionCommandError, match="local_remote_conflict|remote_state_conflict"):
        commands.execute(command, path, drive=drive)
    assert files.updates == previous_updates
    assert path.read_bytes() == before and files.body == _body(1)
    if phase == "recovery":
        assert commands.journal_path(path).read_bytes() == pending


@pytest.mark.parametrize("phase", ["initial", "recovery"])
def test_format_key_order_and_numeric_equivalence_remain_compatible(tmp_path, phase):
    path = tmp_path / "state.json"
    before = _body(1)
    formatted = b'{\n "extension": {"enabled": 1.0}, "alert_log": {}, "positions": {}\n}'
    path.write_bytes(before)
    files = Files(before)
    drive = drive_state.DriveState(path, "synthetic-review", SimpleNamespace(files=lambda: files))
    command = commands.Command("add", "drive", "SYM-REVIEW", 10, 20)
    frozen = None
    if phase == "recovery":
        files.mode = "unsent"
        with pytest.raises(commands.PositionCommandError, match="remote_publish_unconfirmed"):
            commands.execute(command, path, drive=drive)
        files.mode = None
        frozen = commands.read_journal(path)
        command = commands.Command("recover", "drive", retry=True)
    files.body = formatted
    assert commands.execute(command, path, drive=drive)["code"] == "remote_confirmed"
    assert path.read_bytes() == files.body
    if frozen:
        final = commands.read_journal(path)
        assert (final["id"], final["candidate"]) == (frozen["id"], frozen["candidate"])


@pytest.mark.parametrize("left,right", [(True, 1), (False, 0), ([True], [1]),
                                        ({"nested": [False]}, {"nested": [0]}), ("1", 1)])
def test_nested_json_type_differences_do_not_compare_equal(left, right):
    assert commands._same_state(_body(left), _body(right)) is False
