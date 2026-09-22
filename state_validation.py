"""Private state validation and atomic storage within path-scoped transactions.

Unknown JSON fields are preserved for forward compatibility. Existing position
records require the fields StopRecord consumes; older optional stage/timestamp
and alert detail fields may remain absent. Error messages never include values,
record keys, file paths or JSON excerpts from private portfolio state.
"""
from __future__ import annotations

from datetime import date, datetime
from functools import wraps
import json
import math
import os
from pathlib import Path
import tempfile

from state_lock import state_transaction


class StateValidationError(RuntimeError):
    """Invalid or unreadable state; callers must stop rather than use empty state."""


def state_locked(path_resolver):
    """Lock the call's current state path for its complete operation.

    Resolve at invocation so instance paths and redirected test paths share the
    same reentrant transaction as nested readers, writers and complete jobs.
    """
    def decorate(function):
        @wraps(function)
        def locked(*args, **kwargs):
            with state_transaction(path_resolver(*args, **kwargs)):
                return function(*args, **kwargs)
        return locked
    return decorate


def validate_number(value):
    if type(value) not in (int, float):
        raise StateValidationError("State numeric field has an invalid type")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise StateValidationError("State numeric field must be finite")


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise StateValidationError("State text field must be nonempty")


def _day(value):
    _text(value)
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError:
        raise StateValidationError("State date field is invalid") from None


def _timestamp(value):
    _text(value)
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise StateValidationError("State timestamp field is invalid") from None


def validate_string_list(value):
    if not isinstance(value, list):
        raise StateValidationError("State list field has an invalid type")
    for item in value:
        _text(item)


def _json_value(value):
    """Reject nonfinite values even inside unknown, otherwise preserved fields."""
    if value is None or type(value) in (str, bool):
        return
    if type(value) in (int, float):
        validate_number(value)
    elif isinstance(value, list):
        for item in value:
            _json_value(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise StateValidationError("State object keys must be text")
            _json_value(item)
    else:
        raise StateValidationError("State contains a non-JSON value")


def validate_state(data: dict) -> dict:
    if not isinstance(data, dict) or not any(key in data for key in ("positions", "alert_log")):
        raise StateValidationError("State requires a positions or alert_log object")
    _json_value(data)
    for section in ("positions", "alert_log", "done_windows"):
        if section in data and not isinstance(data[section], dict):
            raise StateValidationError("State section must be an object")
    for symbol, record in data.get("positions", {}).items():
        _text(symbol)
        if not isinstance(record, dict):
            raise StateValidationError("Position record must be an object")
        if record.get("symbol") != symbol:
            raise StateValidationError("Position record symbol does not match its key")
        for field in ("entry_price", "current_stop", "highest_high"):
            if field not in record:
                raise StateValidationError("Position record is missing a required numeric field")
            validate_number(record[field])
        if "stage" in record and (type(record["stage"]) is not int or record["stage"] not in (0, 1, 2)):
            raise StateValidationError("Position stage is invalid")
        if "last_updated" in record:
            _timestamp(record["last_updated"])
    for symbol, record in data.get("alert_log", {}).items():
        _text(symbol)
        if not isinstance(record, dict):
            raise StateValidationError("Alert record must be an object")
        if "date" not in record:
            raise StateValidationError("Alert record is missing its date")
        _day(record["date"])
        if "sent_at" in record:
            _timestamp(record["sent_at"])
        if "triggers" in record:
            validate_string_list(record["triggers"])
        if "close" in record:
            validate_number(record["close"])
        if record.get("stop") is not None:
            validate_number(record["stop"])
    for day, names in data.get("done_windows", {}).items():
        _day(day)
        validate_string_list(names)
    return data


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise StateValidationError("State contains duplicate object keys")
        result[key] = value
    return result


def _invalid_constant(value):
    raise StateValidationError("State numeric field must be finite")


def validate_state_bytes(raw: bytes) -> dict:
    if not isinstance(raw, bytes) or not raw.strip():
        raise StateValidationError("State file is empty or is not bytes")
    try:
        data = json.loads(raw.decode("utf-8"), parse_constant=_invalid_constant,
                          object_pairs_hook=_unique_object)
        return validate_state(data)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise StateValidationError("State JSON or schema is invalid") from None


@state_locked(lambda path, **kwargs: path)
def read_state(path: Path, *, missing_ok=False) -> dict:
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        if missing_ok:
            return {"positions": {}}
        raise StateValidationError("State file is missing") from None
    except OSError:
        raise StateValidationError("State file could not be read") from None
    return validate_state_bytes(raw)


@state_locked(lambda path, raw: path)
def atomic_write_state_bytes(path: Path, raw: bytes) -> dict:
    """Stage, fsync and revalidate before atomic replacement of the local file."""
    data = validate_state_bytes(raw)
    path = Path(path)
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent,
                                         prefix=f".{path.name}-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        staged = temporary.read_bytes()
        validate_state_bytes(staged)
        if staged != raw:
            raise StateValidationError("Staged state does not match the requested state")
        os.replace(temporary, path)
    except OSError:
        raise StateValidationError("State write failed; existing file was preserved") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return data


@state_locked(lambda path, data: path)
def write_state(path: Path, data: dict) -> None:
    validate_state(data)
    try:
        raw = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise StateValidationError("State could not be encoded") from None
    atomic_write_state_bytes(path, raw)
