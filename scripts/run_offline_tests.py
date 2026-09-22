"""Run synthetic tests in a code-only snapshot with accidental I/O guards.

This protects local development and secret-free CI from accidental product I/O;
it is not a security sandbox for hostile Python/native extensions.
"""
from __future__ import annotations

from collections import Counter
import json
import os
from pathlib import Path
import platform
import shutil
import site
import sys
import sysconfig
import tempfile
from zoneinfo import TZPATH


def within(path, root):
    try:
        Path(os.fsdecode(path)).resolve().relative_to(root)
        return True
    except (ValueError, TypeError, OSError):
        return False


class OfflineGuard:
    def __init__(self, sandbox, read_roots, source=None, runner=None):
        self.sandbox = Path(sandbox).resolve()
        self.read_roots = tuple(Path(root).resolve() for root in read_roots)
        self.source = Path(source).resolve() if source else None
        self.runner = Path(runner).resolve() if runner else None
        self.events = []

    def deny(self, reason):
        self.events.append(reason)
        raise PermissionError("Offline test guard: " + reason)

    def path_check(self, path, writing=False):
        if not isinstance(path, (str, bytes, os.PathLike)):
            return
        decoded = os.fsdecode(path)
        if decoded.upper() in {"NUL", "\\\\.\\NUL", "\\\\?\\NUL"} or decoded == os.devnull:
            return
        if Path(decoded).name == ".env":
            self.deny("dotenv-file-access")
        if within(path, self.sandbox):
            return
        if self.source is not None and within(path, self.source):
            if not writing and self.runner == Path(decoded).resolve():
                return  # Traceback source for this trusted bootstrap only.
            if not writing and any(root != self.source and within(root, self.source) and within(path, root)
                                   for root in self.read_roots):
                return  # A repository-local .venv is still a dependency root.
            self.deny("original-checkout-access")
        if not writing and any(within(path, root) for root in self.read_roots):
            return
        self.deny("outside-snapshot-write" if writing else "outside-snapshot-read")

    def audit(self, event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "socket.bind", "socket.gethostbyname",
                     "socket.gethostbyaddr", "socket.getnameinfo", "socket.sendto", "socket.sendmsg"}:
            self.deny("network")
        if event in {"subprocess.Popen", "os.system", "os.fork", "os.forkpty", "os.posix_spawn",
                     "os.startfile", "os.startfile/2"} or event.startswith(("os.exec", "os.spawn")):
            self.deny("process")
        if event == "open":
            path, mode, flags = args
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            self.path_check(path, writing=bool(writing))
        elif event in {"os.listdir", "os.scandir"}:
            self.path_check(args[0])
        elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.utime"}:
            self.path_check(args[0], writing=True)
        elif event in {"os.rename", "os.link", "os.symlink"}:
            for path in args[:2]:
                self.path_check(path, writing=True)


def snapshot(source, target):
    files = list(source.glob("*.py")) + [source / "dispatcher_schedules.json"]
    for directory in ("tests", "scripts", ".github"):
        files.extend(path for path in (source / directory).rglob("*")
                     if path.is_file() and path.suffix in {".py", ".txt", ".yaml", ".yml"}
                     and "__pycache__" not in path.parts)
    files.extend(path for path in (source / "requirements.txt", source / "requirements-dev.txt",
                                   source / "constraints.txt") if path.exists())
    for path in files:
        if path.is_symlink() or not within(path, source):
            raise RuntimeError("Code snapshot cannot include linked external files")
        destination = target / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)


def self_check(guard, outer, sandbox):
    import socket
    import subprocess
    checks = (
        ("network", lambda: socket.getaddrinfo("synthetic.invalid", 443)),
        ("process", lambda: subprocess.run([sys.executable, "-c", "pass"])),
        ("outside-snapshot-read", lambda: (outer / "synthetic-private.txt").read_bytes()),
        ("outside-snapshot-write", lambda: (outer / "synthetic-write.txt").write_text("synthetic")),
        ("dotenv-file-access", lambda: (sandbox / ".env").read_bytes()),
    )
    for expected, operation in checks:
        before = len(guard.events)
        try:
            operation()
        except PermissionError:
            pass
        else:
            raise RuntimeError("Offline guard self-check did not block an operation")
        if guard.events[before:] != [expected]:
            raise RuntimeError("Offline guard self-check failed its expected boundary")
    print("GUARD_SELF_CHECK=5 passed (synthetic probes; no external effects)")
    return 0


def main(argv=None):
    selection = list(sys.argv[1:] if argv is None else argv)
    platform.uname()  # Cache OS metadata before process access is denied.
    if os.name == "nt":
        # Retain sandbox ACL inheritance for Windows temporary directories.
        original_mkdir = os.mkdir
        def inherited_mkdir(path, mode=0o777, *, dir_fd=None):
            return original_mkdir(path, 0o777, dir_fd=dir_fd)
        os.mkdir = inherited_mkdir
    source = Path(__file__).resolve().parents[1]
    outer = Path(tempfile.mkdtemp(prefix="atr-offline-"))
    sandbox = outer / "isolated"
    repo = sandbox / "source"
    repo.mkdir(parents=True)
    snapshot(source, repo)
    (outer / "synthetic-private.txt").write_text("synthetic only", encoding="utf-8")
    (sandbox / ".env").write_text("SYNTHETIC_ONLY=1", encoding="utf-8")

    preserved = {key: value for key, value in os.environ.items() if key.upper() in {
        "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    }}
    os.environ.clear()
    os.environ.update(preserved)
    os.environ.update({
        "USERPROFILE": str(sandbox), "TEMP": str(sandbox), "TMP": str(sandbox),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "MPLCONFIGDIR": str(sandbox / "mpl"), "XDG_CACHE_HOME": str(sandbox / "cache"),
        "XDG_DATA_HOME": str(sandbox / "share"),
        "NUMBA_DISABLE_INTEL_SVML": "1", "NUMBA_CACHE_DIR": str(sandbox / "numba-cache"),
    })
    tempfile.tempdir = str(sandbox)
    Path.home = classmethod(lambda cls: cls(sandbox))
    sys.dont_write_bytecode = True
    os.chdir(repo)
    # Remove original checkout/script entries so collection imports only copied code.
    dependency_roots = {sys.prefix, sys.base_prefix, *sysconfig.get_paths().values(), site.getusersitepackages()}
    sys.path[:] = [str(repo)] + [entry for entry in sys.path if entry and (
        not within(entry, source) or any(within(entry, Path(root).resolve()) for root in dependency_roots))]
    read_roots = dependency_roots | {str(Path(__file__).resolve())}
    # Linux uses OS zoneinfo, while Windows normally imports the tzdata wheel.
    read_roots.update(TZPATH)
    if os.name != "nt":
        read_roots.update({"/etc/localtime", "/etc/timezone"})
    read_roots.update({"/usr/share/fonts", "/usr/local/share/fonts", "/etc/fonts",
                       "/usr/X11R6/lib/X11/fonts/TTF", "/usr/X11/lib/X11/fonts",
                       "/usr/lib/openoffice/share/fonts/truetype"} if os.name != "nt" else {
        str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts")})
    guard = OfflineGuard(sandbox, read_roots, source, __file__)
    sys.addaudithook(guard.audit)
    if selection == ["--self-check"]:
        return self_check(guard, outer, sandbox)

    import dotenv
    dotenv.load_dotenv = lambda *args, **kwargs: False
    dotenv.main.load_dotenv = dotenv.load_dotenv
    import subprocess
    original_check_output = subprocess.check_output
    def fontconfig_stub(command, *args, **kwargs):
        # Matplotlib's optional Linux font discovery needs no child process here.
        if isinstance(command, (list, tuple)) and command and command[0] == "fc-list":
            return b""
        return original_check_output(command, *args, **kwargs)
    subprocess.check_output = fontconfig_stub

    def offline_http(*args, **kwargs):
        guard.deny("external-http")
    import socket
    # urllib3 probes IPv6 by binding a socket during import. A synthetic test
    # runner does not need that capability probe; do not create even a listener.
    ipv6_available = socket.has_ipv6
    socket.has_ipv6 = False
    try:
        import requests
    finally:
        socket.has_ipv6 = ipv6_available
    import urllib.request
    requests.sessions.Session.request = offline_http
    urllib.request.urlopen = offline_http
    try:
        import curl_cffi.requests
        curl_cffi.requests.Session.request = offline_http
    except ImportError:
        pass
    import pytest
    print(f"ISOLATED_REPO=atr; Python={sys.version.split()[0]}; code-only snapshot")
    code = pytest.main((selection or ["tests"]) + [
        "-q", "-p", "no:cacheprovider", "--basetemp", str(sandbox / "pytest"),
    ])
    print("GUARD_EVENTS=" + json.dumps(dict(Counter(guard.events))))
    final_code = code or (1 if guard.events else 0)
    print("EXIT_CODE=" + str(final_code))
    return final_code


if __name__ == "__main__":
    raise SystemExit(main())
