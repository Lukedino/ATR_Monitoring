"""Trusted stdlib-only worker for run_state_lock_tests.py; never run product jobs."""
from __future__ import annotations

from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time


DAY = date(2026, 1, 13)
NOW = datetime(2026, 1, 13, 12, tzinfo=timezone.utc)
ROUNDS = 12


def emit(event, **values):
    print(json.dumps({"event": event, **values}), flush=True)


def bootstrap():
    # -I excludes this script's directory. Only this copied, fixed guard module
    # is imported before the guard; no product module is imported here.
    if len(sys.argv) != 6 or not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise RuntimeError("Invalid worker bootstrap")
    sandbox, source, case = (Path(value).resolve() for value in sys.argv[1:4])
    mode, number = sys.argv[4:]
    repo = Path(__file__).resolve().parents[1]
    if repo.parent != sandbox or case.parent != sandbox / "cases":
        raise RuntimeError("Invalid worker paths")
    if mode not in {"hold", "hold_nested", "blocked", "acquire", "union"} or number not in {"0", "1", "2", "3"}:
        raise RuntimeError("Invalid worker mode")
    sys.path.insert(0, str(repo / "scripts"))
    from run_offline_tests import OfflineGuard

    class WorkerGuard(OfflineGuard):
        probing = False

        def deny(self, reason):
            # A killed worker never reaches finally: publish violations at once.
            print(json.dumps({"guard": reason, "expected": self.probing}), file=sys.stderr, flush=True)
            super().deny(reason)

        def audit(self, event, args):
            if event in {"os.kill", "os.killpg"}:
                self.deny("process-signal")
            super().audit(event, args)

    runtime_roots = {sys.prefix, sys.base_prefix, *[entry for entry in sys.path[1:] if entry]}
    guard = WorkerGuard(sandbox, runtime_roots, source)
    sys.addaudithook(guard.audit)
    os.chdir(repo)
    tempfile.tempdir = str(sandbox)
    Path.home = classmethod(lambda cls: cls(sandbox))

    probes = (
        ("network", lambda: socket.getaddrinfo("synthetic.invalid", 443)),
        ("process", lambda: subprocess.run([sys.executable, "-I", "-S", "-B", "-c", "pass"])),
        ("outside-snapshot-read", lambda: (sandbox.parent / "synthetic-private.txt").read_bytes()),
        ("outside-snapshot-write", lambda: (sandbox.parent / "synthetic-write.txt").write_text("synthetic")),
        ("dotenv-file-access", lambda: (sandbox / ".env").read_bytes()),
    )
    guard.probing = True
    try:
        for expected, action in probes:
            before = len(guard.events)
            try:
                action()
            except PermissionError:
                pass
            else:
                raise RuntimeError("Guard self-check allowed an operation")
            if guard.events[before:] != [expected]:
                raise RuntimeError("Guard self-check boundary mismatch")
    finally:
        guard.probing = False
    sys.path.insert(0, str(repo))
    return guard, case / "state.json", mode, int(number)


def command(expected):
    if sys.stdin.readline().strip() != expected:
        raise RuntimeError("Worker command mismatch")


def run():
    guard, path, mode, number = bootstrap()
    # Every product import and every state access below is now guarded.
    from state_lock import StateLockError, state_transaction
    import state_validation as sv
    import stop_manager as sm

    sm.DATA_FILE = path
    # Only the market-date clock is fixed, so this stdlib-only lock test needs
    # no Windows tzdata package. All lock and state APIs are the actual code.
    sm.market_date = lambda symbol, now_utc=None: DAY
    emit("ready", probes=5)

    if mode in {"hold", "hold_nested"}:
        with state_transaction(path, timeout=5):
            if mode == "hold_nested":
                sm.add_position("NESTED-USD", 100, 90)
                sm.mark_window_done("nested", DAY)
                with state_transaction(path, timeout=0.2):
                    raw = sv.read_state(path)
                    raw["synthetic_replacements"] = 1
                    sv.write_state(path, raw)
                raw["synthetic_replacements"] = 2
                sv.write_state(path, raw)
            else:
                sv.read_state(path)
            emit("acquired", nested=mode == "hold_nested")
            command("release")
        emit("released")
    elif mode == "blocked":
        start = time.monotonic()
        try:
            with state_transaction(path, timeout=0.2):
                raise RuntimeError("Contender entered a held transaction")
        except StateLockError as error:
            if str(error) != "STATE_LOCK_TIMEOUT":
                raise RuntimeError("Contender failed without a lock timeout") from None
            elapsed = time.monotonic() - start
            if not 0.15 <= elapsed < 5:
                raise RuntimeError("Lock timeout did not use its bounded deadline")
            emit("blocked")
    elif mode == "acquire":
        with state_transaction(path, timeout=5):
            raw = sv.read_state(path)
            raw["synthetic_acquired"] = True
            sv.write_state(path, raw)
            emit("acquired")
    else:
        # A common start gate and a delayed read widen actual RMW contention.
        # The earlier held-lock test proves exclusion independently of timing.
        original_read = sm._load_raw

        def delayed_read():
            value = original_read()
            time.sleep(0.002)
            return value

        sm._load_raw = delayed_read
        command("go")
        for index in range(ROUNDS):
            symbol = f"SYNTHETIC{number}{index:02d}-USD"
            sm.add_position(symbol, 100, 90)
            sm.mark_trigger_sent(symbol, ["synthetic"], 100, 90, now_utc=NOW)
            sm.mark_window_done(f"synthetic-{number}-{index}", DAY)
        sv.read_state(path)
        emit("updated", rounds=ROUNDS)
    if len(guard.events) != 5:
        raise RuntimeError("Unexpected guard event")
    emit("done", unexpected_guard_events=0)


if __name__ == "__main__":
    try:
        run()
    except BaseException:
        # Do not print raw exceptions, tracebacks, paths, or state values.
        emit("failed")
        raise SystemExit(1) from None
