"""Real OS-lock checks using only guarded workers and synthetic local state.

This trusted parent never imports product modules. It copies an explicit code
allowlist and starts only the copied stdlib-only worker with an empty-of-secrets
environment. The ordinary offline runner and its no-process policy are unchanged.
Like that runner, this prevents accidental I/O, not hostile Python/native code.
Synthetic temporary snapshots remain available for failure diagnosis.
"""
from __future__ import annotations

from collections import Counter
import importlib.util
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading


FILES = (
    "state_lock.py", "state_validation.py", "stop_manager.py", "position_commands.py",
    "market_dates.py", "symbol_market.py",
    "scripts/run_offline_tests.py", "scripts/state_lock_worker.py",
)
PROBES = Counter({"network": 1, "process": 1, "outside-snapshot-read": 1,
                  "outside-snapshot-write": 1, "dotenv-file-access": 1})
ROUNDS = 12
STAGE = "bootstrap"


def require(condition, code):
    if not condition:
        raise RuntimeError(code)


def scratch_directory():
    # Keep inherited sandbox ACLs on Windows, as the ordinary runner does.
    original = os.mkdir
    if os.name == "nt":
        def inherited_mkdir(path, mode=0o777, *, dir_fd=None):
            return original(path, 0o777, dir_fd=dir_fd)
        os.mkdir = inherited_mkdir
    try:
        return Path(tempfile.mkdtemp(prefix="atr-state-lock-")).resolve()
    finally:
        os.mkdir = original


def setup():
    require(sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode,
            "Start this runner with python -I -S -B -X utf8")
    require(len(sys.argv) == 1, "This runner accepts no custom commands or paths")
    source = Path(__file__).resolve().parents[1]
    outer = scratch_directory()
    sandbox, repo = outer / "isolated", outer / "isolated" / "source"
    repo.mkdir(parents=True)
    for relative in FILES:
        path = source / relative
        require(not path.is_symlink() and path.resolve().is_relative_to(source), "Unsafe snapshot source")
        destination = repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    (outer / "synthetic-private.txt").write_text("synthetic only", encoding="utf-8")
    (sandbox / ".env").write_text("SYNTHETIC_ONLY=1", encoding="utf-8")
    (sandbox / "cases").mkdir()

    # Load only the trusted copied guard, never any product module in this parent.
    spec = importlib.util.spec_from_file_location("lock_test_guard", repo / "scripts/run_offline_tests.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class ParentGuard(module.OfflineGuard):
        launch = None

        def deny(self, reason):
            print("PARENT_GUARD_DENIED=" + reason, flush=True)
            super().deny(reason)

        def audit(self, event, args):
            if event == "subprocess.Popen" and self.launch is not None:
                executable, command, cwd, environment = args
                approved_command, approved_env = self.launch
                approved_audit_command = (subprocess.list2cmdline(approved_command)
                                          if os.name == "nt" else approved_command)
                if (executable == sys.executable and command == approved_audit_command
                        and cwd == str(repo) and environment == approved_env):
                    return
            super().audit(event, args)

    runtime_roots = {sys.prefix, sys.base_prefix, *[entry for entry in sys.path if entry]}
    guard = ParentGuard(sandbox, runtime_roots, source, __file__)
    sys.addaudithook(guard.audit)
    env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
    env.update({"HOME": str(sandbox), "USERPROFILE": str(sandbox),
                "TEMP": str(sandbox), "TMP": str(sandbox), "TMPDIR": str(sandbox)})
    os.chdir(repo)
    return source, sandbox, repo, guard, env


class Child:
    def __init__(self, process):
        self.process = process
        self.output = queue.Queue()
        self.errors = []
        self.stderr = []
        self.readers = []
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            thread = threading.Thread(target=self._read, args=(name, stream), daemon=True)
            thread.start()
            self.readers.append(thread)

    def _read(self, name, stream):
        try:
            for count in range(256):
                line = stream.readline(4097)
                if not line:
                    break
                require(len(line) <= 4096 and line.endswith("\n"), "Worker output bound exceeded")
                if name == "stdout":
                    self.output.put(json.loads(line))
                else:
                    self.stderr.append(json.loads(line))
            else:
                raise RuntimeError("Worker output count exceeded")
        except Exception:
            self.errors.append("Invalid worker output")
        finally:
            if name == "stdout":
                self.output.put(None)

    def expect(self, event, timeout=20):
        try:
            value = self.output.get(timeout=timeout)
        except queue.Empty:
            raise RuntimeError("Worker event timed out") from None
        require(isinstance(value, dict) and value.get("event") == event,
                "Worker protocol failure: " + event)
        return value

    def send(self, command):
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()

    def finish(self, *, killed=False):
        self.process.wait(timeout=10)
        for reader in self.readers:
            reader.join(timeout=5)
        require(not self.errors and not any(reader.is_alive() for reader in self.readers), "Worker output failed")
        require((self.process.returncode != 0) if killed else (self.process.returncode == 0), "Worker exit failed")
        require(all(isinstance(item, dict) and item.get("expected") is True for item in self.stderr),
                "Unexpected worker guard violation")
        require(Counter(item.get("guard") for item in self.stderr) == PROBES, "Worker guard probes incomplete")
        require(self.output.get(timeout=1) is None, "Unexpected trailing worker event")

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=10)
        for reader in self.readers:
            reader.join(timeout=5)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()


def run():
    global STAGE
    source, sandbox, repo, guard, env = setup()
    children = []

    def spawn(case, mode, number=0):
        command = [sys.executable, "-I", "-S", "-B", "-u", "-X", "utf8",
                   str(repo / "scripts/state_lock_worker.py"), str(sandbox), str(source),
                   str(case), mode, str(number)]
        guard.launch = (command, env)
        try:
            process = subprocess.Popen(command, executable=sys.executable, cwd=str(repo), env=env,
                                       shell=False, close_fds=True,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, encoding="utf-8")
        finally:
            guard.launch = None
        child = Child(process)
        children.append(child)
        require(child.expect("ready").get("probes") == 5, "Worker guard was not ready")
        return child

    def case(name):
        directory = sandbox / "cases" / name
        directory.mkdir()
        (directory / "state.json").write_text('{"positions":{},"alert_log":{},"done_windows":{}}', encoding="utf-8")
        return directory

    def done(child):
        require(child.expect("done").get("unexpected_guard_events") == 0, "Worker guard was not clean")
        child.finish()

    try:
        STAGE = "contention-and-nesting"
        normal = case("normal")
        holder = spawn(normal, "hold_nested")
        require(holder.expect("acquired").get("nested") is True, "Nested holder failed")
        contender = spawn(normal, "blocked")
        contender.expect("blocked")
        done(contender)
        raw = json.loads((normal / "state.json").read_text(encoding="utf-8"))
        require(raw.get("synthetic_replacements") == 2 and "NESTED-USD" in raw["positions"],
                "Nested product operations failed")
        print("PASS=contention_timeout,nested_api_and_replace_hold", flush=True)
        STAGE = "normal-release"
        holder.send("release")
        holder.expect("released")
        done(holder)
        require((normal / "state.json.lock").is_file(), "Lock sidecar was removed")
        after_release = spawn(normal, "acquire")
        after_release.expect("acquired")
        done(after_release)
        print("PASS=normal_release", flush=True)

        STAGE = "kill-release"
        abrupt = case("kill")
        before = (abrupt / "state.json").read_bytes()
        killed = spawn(abrupt, "hold")
        killed.expect("acquired")
        killed.process.kill()
        killed.finish(killed=True)
        require((abrupt / "state.json").read_bytes() == before, "Killed reader changed the state")
        require((abrupt / "state.json.lock").is_file(), "Killed holder lost permanent sidecar")
        after_kill = spawn(abrupt, "acquire")
        after_kill.expect("acquired")
        done(after_kill)
        print("PASS=kill_release", flush=True)

        STAGE = "concurrent-rmw"
        concurrent = case("union")
        writers = [spawn(concurrent, "union", number) for number in range(4)]
        for child in writers:
            child.send("go")
        for child in writers:
            require(child.expect("updated", timeout=45).get("rounds") == ROUNDS, "Worker updates incomplete")
            done(child)
        saved = json.loads((concurrent / "state.json").read_text(encoding="utf-8"))
        symbols = {f"SYNTHETIC{number}{index:02d}-USD" for number in range(4) for index in range(ROUNDS)}
        windows = {f"synthetic-{number}-{index}" for number in range(4) for index in range(ROUNDS)}
        require(set(saved["positions"]) == symbols and set(saved["alert_log"]) == symbols,
                "Concurrent position or alert update was lost")
        require(set(saved["done_windows"]["2026-01-13"]) == windows, "Concurrent window update was lost")
        require(len(saved["done_windows"]["2026-01-13"]) == len(windows), "Window was duplicated")
        require((concurrent / "state.json.lock").is_file(), "Concurrent sidecar was removed")
        require(not guard.events, "Unexpected parent guard event")
        print("PASS=four_process_union_rmw", flush=True)
        print(f"REAL_PROCESS_TESTS=5 passed; OS={os.name}; Python={sys.version.split()[0]}; "
              f"backend={'msvcrt' if os.name == 'nt' else 'fcntl'}; workers={len(children)}", flush=True)
        print(f"EXPECTED_GUARD_PROBES={5 * len(children)}; UNEXPECTED_GUARD_EVENTS=0", flush=True)
        return 0
    finally:
        cleanup_failed = False
        for child in children:
            try:
                child.close()
            except Exception:
                cleanup_failed = True
        require(not cleanup_failed, "Worker cleanup failed")


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except Exception as error:
        # Avoid raw exception strings, paths, environment or worker output in CI.
        print(f"REAL_PROCESS_TESTS=FAILED; stage={STAGE}; error_type={type(error).__name__}", flush=True)
        raise SystemExit(1) from None
