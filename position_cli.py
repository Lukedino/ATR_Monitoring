"""포트폴리오 config import 전에 호출하는 명시적 상태 관리 CLI."""
import argparse
import os
from pathlib import Path

from position_commands import Command, PositionCommandError, execute, validate_command

STATE_PATH = Path(__file__).parent / "data" / "stop_levels.json"
MANAGEMENT_FLAGS = {"--add-pos", "--remove-pos", "--list-pos", "--recover-state", "--state-scope", "--replace", "--retry"}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse 기본 오류는 잘못된 입력의 원문을 stderr에 포함한다.
        raise PositionCommandError("arguments_invalid")


def _parse(argv):
    parser = _Parser(description="명시적 로컬/Drive 상태 관리 (시세·알림 없음)", allow_abbrev=False)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--add-pos", nargs=3, metavar=("SYMBOL", "ENTRY", "STOP"))
    group.add_argument("--remove-pos", metavar="SYMBOL")
    group.add_argument("--list-pos", action="store_true")
    group.add_argument("--recover-state", action="store_true")
    parser.add_argument("--state-scope", choices=("local", "drive"), default="local")
    parser.add_argument("--replace", action="store_true", help="기존 포지션 Stop/stage의 명시적 재등록")
    parser.add_argument("--retry", action="store_true", help="이전 원격 기준이 같은 미게시 후보만 명시 재시도")
    if "--help" in argv or "-h" in argv:
        parser.print_help()
        raise SystemExit(0)
    args = parser.parse_args(argv)
    if args.add_pos:
        action, symbol, entry, stop = "add", *args.add_pos
    else:
        action = "remove" if args.remove_pos is not None else "list" if args.list_pos else "recover"
        symbol, entry, stop = args.remove_pos, None, None
    return validate_command(Command(action, args.state_scope, symbol, entry, stop, args.replace, args.retry))


def _drive(path):
    import drive_state
    # 명시 Drive 모드는 기존 from_env의 로컬 fallback을 사용하지 않는다.
    if not os.environ.get(drive_state.STATE_FILE_ID_ENV, "").strip() or not os.environ.get(drive_state.SA_JSON_ENV, "").strip():
        raise PositionCommandError("drive_configuration_required")
    result = drive_state.from_env(path)
    if result is None:
        raise PositionCommandError("drive_configuration_required")
    return result


def maybe_run(argv):
    """관리 플래그가 없으면 None. 있으면 정제된 종료코드만 반환한다."""
    argv = list(argv)
    if not any(arg.split("=", 1)[0] in MANAGEMENT_FLAGS for arg in argv):
        return None
    try:
        command = _parse(argv)
        if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
            raise PositionCommandError("management_requires_local_invocation")
        drive = _drive(STATE_PATH) if command.scope == "drive" else None
        result = execute(command, STATE_PATH, drive=drive)
        if command.action == "list":
            for symbol, record in sorted(result["state"].get("positions", {}).items()):
                print(f"{symbol}\tentry={record['entry_price']}\tstop={record['current_stop']}\tstage={record.get('stage', 0)}")
        print("position_command: " + result["code"])
        return 1 if result["code"] == "retry_required" else 0
    except PositionCommandError as error:
        print("position_command: " + error.code)
        return 1
    except SystemExit as error:
        return 0 if error.code == 0 else 1
    except Exception:
        print("position_command: command_failed")
        return 1


if __name__ == "__main__":
    import sys
    if sys.argv[1:] in (["--help"], ["-h"]):
        _parse(sys.argv[1:])
    result = maybe_run(sys.argv[1:])
    raise SystemExit(1 if result is None else result)
