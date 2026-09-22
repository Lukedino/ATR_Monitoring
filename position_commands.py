"""설정/시세 import 없는 관리 명령과 비공개 동일 후보 복구.

원격 MD5 사전 검사는 CAS가 아니다. 다른 호스트의 writer를 배제하는
운영 절차가 필요하며, 이 모듈은 응답 유실 뒤 자동 재전송하지 않는다.
"""
from __future__ import annotations

import base64
import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import unicodedata
import uuid

from state_lock import state_transaction
from state_validation import StateValidationError, atomic_write_state_bytes, validate_state_bytes
from symbol_market import is_ambiguous_numeric_symbol

MAX_BYTES = 4 * 1024 * 1024
MAX_JOURNAL_BYTES = 4 * MAX_BYTES + 4096
PHASES = {"prepared", "local_only", "unknown", "confirmed", "completed"}


class PositionCommandError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Command:
    action: str
    scope: str = "local"
    symbol: str | None = None
    entry: float | None = None
    stop: float | None = None
    replace: bool = False
    retry: bool = False


def validate_command(command):
    if not isinstance(command, Command) or command.action not in {"add", "remove", "list", "recover"}:
        raise PositionCommandError("command_invalid")
    if command.scope not in {"local", "drive"} or type(command.replace) is not bool or type(command.retry) is not bool:
        raise PositionCommandError("command_invalid")
    if (command.replace and command.action != "add") or (command.retry and command.action != "recover"):
        raise PositionCommandError("command_invalid")
    if command.retry and command.scope != "drive":
        raise PositionCommandError("command_invalid")
    symbol = command.symbol
    if command.action in {"add", "remove"}:
        if not isinstance(symbol, str) or any(unicodedata.category(c).startswith("C") for c in symbol):
            raise PositionCommandError("symbol_invalid")
        symbol = symbol.strip().upper()
        if not symbol or any(c.isspace() for c in symbol) or is_ambiguous_numeric_symbol(symbol):
            raise PositionCommandError("symbol_invalid")
    elif symbol is not None:
        raise PositionCommandError("command_invalid")
    entry, stop = command.entry, command.stop
    if command.action == "add":
        numbers = []
        for value in (entry, stop):
            try:
                if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                    raise ValueError
                number = float(value)
                if not math.isfinite(number) or number <= 0:
                    raise ValueError
            except (ValueError, TypeError, OverflowError):
                raise PositionCommandError("price_invalid") from None
            numbers.append(number)
        entry, stop = numbers
    elif entry is not None or stop is not None:
        raise PositionCommandError("command_invalid")
    return Command(command.action, command.scope, symbol, entry, stop, command.replace, command.retry)


def journal_path(path):
    path = Path(path)
    return path.with_name(path.name + ".commands") / "pending.json"


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _read(path, *, missing=False, limit=MAX_BYTES):
    try:
        with Path(path).open("rb") as handle:
            raw = handle.read(limit + 1)
    except FileNotFoundError:
        if missing:
            return None
        raise PositionCommandError("state_missing") from None
    except OSError:
        raise PositionCommandError("state_read_failed") from None
    if len(raw) > limit:
        raise PositionCommandError("state_too_large")
    return raw


def _state(raw):
    try:
        return validate_state_bytes(raw)
    except (StateValidationError, RecursionError):
        raise PositionCommandError("state_invalid") from None


def _same_state(left, right):
    """서식은 무시하되 JSON 불리언을 숫자와 같은 상태로 보지 않는다."""
    def equal(a, b):
        # JSON 숫자의 기존 1/1.0 동등성은 유지한다(bool은 별도 타입).
        if type(a) in (int, float) and type(b) in (int, float):
            return a == b
        if type(a) is not type(b):
            return False
        if isinstance(a, dict):
            return a.keys() == b.keys() and all(equal(a[key], b[key]) for key in a)
        if isinstance(a, list):
            return len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b))
        return a == b
    return equal(_state(left), _state(right))


def _pack(raw):
    if raw is None:
        return None
    _state(raw)
    if len(raw) > MAX_BYTES:
        raise PositionCommandError("state_too_large")
    return {"sha256": _digest(raw), "body": base64.b64encode(raw).decode("ascii")}


def _unpack(value):
    if value is None:
        return None
    try:
        if not isinstance(value, dict) or set(value) != {"sha256", "body"}:
            raise ValueError
        raw = base64.b64decode(value["body"], validate=True)
        if len(raw) > MAX_BYTES or value["sha256"] != _digest(raw):
            raise ValueError
        _state(raw)
        return raw
    except (ValueError, TypeError, KeyError, PositionCommandError):
        raise PositionCommandError("journal_invalid") from None


def _unique(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError
        data[key] = value
    return data


def _journal(raw):
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
        if not isinstance(data, dict) or set(data) != {"version", "id", "scope", "phase", "target", "before", "baseline", "candidate"}:
            raise ValueError
        if type(data["version"]) is not int or data["version"] != 1:
            raise ValueError
        if not isinstance(data["id"], str) or uuid.UUID(data["id"]).hex != data["id"]:
            raise ValueError
        if data["scope"] not in {"local", "drive"} or data["phase"] not in PHASES:
            raise ValueError
        if data["scope"] == "local":
            if data["target"] is not None or data["phase"] not in {"prepared", "local_only"}:
                raise ValueError
        elif data["phase"] == "local_only" or not isinstance(data["target"], str) or len(data["target"]) != 64 or any(c not in "0123456789abcdef" for c in data["target"]):
            raise ValueError
        _unpack(data["before"])
        if _unpack(data["baseline"]) is None or _unpack(data["candidate"]) is None:
            raise ValueError
        return data
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise PositionCommandError("journal_invalid") from None


def read_journal(path):
    raw = _read(journal_path(path), missing=True, limit=MAX_JOURNAL_BYTES)
    return None if raw is None else _journal(raw)


def assert_no_pending(path, *, remote=False):
    """로컬 전용 게시 표식도 일반 원격 pull이 지우지 못하게 한다."""
    journal = read_journal(path)
    if journal and journal["phase"] != "completed":
        if remote or journal["phase"] != "local_only":
            raise PositionCommandError("state_recovery_required")


def _save_journal(path, journal):
    raw = json.dumps(journal, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(raw) > MAX_JOURNAL_BYTES:
        raise PositionCommandError("journal_too_large")
    _journal(raw)
    target = journal_path(path)
    temporary = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="wb", dir=target.parent, prefix=".candidate-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        if temporary.read_bytes() != raw:
            raise PositionCommandError("journal_write_failed")
        _journal(temporary.read_bytes())
        os.replace(temporary, target)
    except OSError:
        raise PositionCommandError("journal_write_failed") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def mutate(raw, command, *, timestamp=None):
    """positions만 변경한다. 알림/창/알 수 없는 필드는 그대로 보존한다."""
    command = validate_command(command)
    data = copy.deepcopy(_state(raw))
    positions = data.setdefault("positions", {})
    if command.action == "add":
        if command.symbol in positions and not command.replace:
            raise PositionCommandError("position_exists")
        if command.replace and command.symbol not in positions:
            raise PositionCommandError("position_missing")
        # 명시 replace만 기존 Stop/stage 초기화를 허용하고 확장 필드는 보존한다.
        record = dict(positions.get(command.symbol, {}))
        record.update(symbol=command.symbol, entry_price=command.entry, current_stop=command.stop,
                      highest_high=command.entry, stage=0,
                      last_updated=timestamp or datetime.now(timezone.utc).isoformat(timespec="seconds"))
        positions[command.symbol] = record
    elif command.action == "remove":
        if command.symbol not in positions:
            raise PositionCommandError("position_missing")
        del positions[command.symbol]
    else:
        raise PositionCommandError("command_invalid")
    encoded = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    _pack(encoded)
    return encoded


def _target(drive):
    # 실제 파일 ID는 journal/로그에 기록하지 않는다.
    return _digest(drive.file_id.encode("utf-8"))


def _observe(drive):
    try:
        raw = drive.observe()
        _pack(raw)
        return raw
    except Exception:
        raise PositionCommandError("remote_observation_failed") from None


def _promote(path, journal, *, local=False):
    before = _unpack(journal["before"])
    candidate = _unpack(journal["candidate"])
    current = _read(path, missing=True)
    if current not in (before, candidate):
        raise PositionCommandError("local_state_conflict")
    if current != candidate:
        try:
            atomic_write_state_bytes(path, candidate)
        except StateValidationError:
            raise PositionCommandError("local_promotion_failed") from None
    journal["phase"] = "local_only" if local else "completed"
    _save_journal(path, journal)
    return {"code": "local_only" if local else "remote_confirmed", "state": _state(candidate)}


def _publish(path, journal, drive):
    # 쓰기 직전 저널 실패면 update 0. 확인 불가 상태로 먼저 내구 저장한다.
    journal["phase"] = "unknown"
    _save_journal(path, journal)
    candidate = _unpack(journal["candidate"])
    try:
        drive.publish(candidate)
    except Exception:
        raise PositionCommandError("remote_publish_unconfirmed") from None
    if _observe(drive) != candidate:
        raise PositionCommandError("remote_publish_unconfirmed")
    journal["phase"] = "confirmed"
    _save_journal(path, journal)
    return _promote(path, journal)


def _recover(path, command, journal, drive):
    if journal is None or journal["phase"] == "completed":
        return {"code": "no_pending"}
    if command.scope == "local":
        if journal["scope"] != "local":
            raise PositionCommandError("recovery_scope_mismatch")
        return _promote(path, journal, local=True)
    if drive is None:
        raise PositionCommandError("drive_configuration_required")
    if journal["scope"] == "drive" and journal["target"] != _target(drive):
        raise PositionCommandError("remote_target_conflict")
    candidate, baseline = _unpack(journal["candidate"]), _unpack(journal["baseline"])
    current = _read(path, missing=True)
    if current not in (_unpack(journal["before"]), candidate):
        raise PositionCommandError("local_state_conflict")
    remote = _observe(drive)
    if remote == candidate:
        journal.update(scope="drive", target=_target(drive), phase="confirmed")
        _save_journal(path, journal)
        return _promote(path, journal)
    if not _same_state(remote, baseline):
        raise PositionCommandError("remote_state_conflict")
    if not command.retry:
        return {"code": "retry_required"}
    # 로컬 전용 후보의 최초 명시 게시도 동일 후보를 유지한다.
    journal.update(scope="drive", target=_target(drive), baseline=_pack(remote), phase="prepared")
    _save_journal(path, journal)
    return _publish(path, journal, drive)


def execute(command, path, *, drive=None):
    """가짜 Drive client로 전체 실패 경계를 검사할 수 있는 관리 API."""
    command = validate_command(command)  # 어떤 파일/원격 접근보다 먼저
    path = Path(path)
    with state_transaction(path):
        journal = read_journal(path)
        if command.action == "recover":
            return _recover(path, command, journal, drive)
        if journal and journal["phase"] not in {"completed", "local_only"}:
            raise PositionCommandError("state_recovery_required")
        if command.scope == "drive" and journal and journal["phase"] == "local_only":
            raise PositionCommandError("state_recovery_required")
        before = _read(path, missing=True)
        if before is not None:
            _state(before)
        if command.scope == "drive":
            if drive is None:
                raise PositionCommandError("drive_configuration_required")
            baseline = _observe(drive)
            if before is not None and not _same_state(before, baseline):
                raise PositionCommandError("local_remote_conflict")
        else:
            baseline = before if before is not None else b'{"positions": {}}'
        if command.action == "list":
            return {"code": "local_only" if command.scope == "local" else "remote_observed", "state": _state(baseline)}
        candidate = mutate(baseline, command)
        if command.scope == "local" and journal and journal["phase"] == "local_only":
            # 추가 명령이어도 미게시 이전의 기준본을 잃지 않는다.
            baseline = _unpack(journal["baseline"])
        journal = {"version": 1, "id": uuid.uuid4().hex, "scope": command.scope,
                   "phase": "prepared", "target": _target(drive) if command.scope == "drive" else None,
                   "before": _pack(before), "baseline": _pack(baseline), "candidate": _pack(candidate)}
        _save_journal(path, journal)
        if command.scope == "local":
            return _promote(path, journal, local=True)
        return _publish(path, journal, drive)
