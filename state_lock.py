"""Cooperative, reentrant state transactions on a local filesystem.

Lock the permanent sidecar, never the JSON inode replaced by atomic writes.
Every participating process must use this API and the same canonical path.
This is not a distributed lock and does not make Drive updates atomic.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import math
import os
from pathlib import Path
import threading
import time

if os.name == "nt":
    import msvcrt
elif os.name == "posix":
    import fcntl


class StateLockError(RuntimeError):
    """A state transaction could not acquire or release its local lock."""


class _Entry:
    def __init__(self):
        self.mutex = threading.RLock()
        self.depth = 0
        self.handle = None


_entries = {}
_registry_mutex = threading.Lock()
_process_id = os.getpid()


def _canonical_path(path):
    try:
        requested = Path(path)
        # os.replace targets the requested directory entry, not a symlink's
        # resolved file. Reject leaf links before choosing the lock identity.
        if requested.is_symlink():
            raise StateLockError("STATE_LOCK_PATH_SYMLINK")
        return Path(os.path.normcase(str(requested.resolve())))
    except StateLockError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise StateLockError("STATE_LOCK_PATH_INVALID") from None


def _try_lock(handle):
    handle.seek(0)
    try:
        if os.name == "nt":
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        elif os.name == "posix":
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            raise StateLockError("STATE_LOCK_PLATFORM_UNSUPPORTED")
    except OSError as error:
        # Only ordinary contention is retryable; permission/I/O failures stop.
        if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            return False
        raise
    return True


def _unlock(handle):
    handle.seek(0)
    if os.name == "nt":
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    elif os.name == "posix":
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _open_locked(path, deadline):
    handle = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        sidecar = path.with_name(path.name + ".lock")
        if sidecar.is_symlink():
            raise StateLockError("STATE_LOCK_SIDECAR_INVALID")
        handle = sidecar.open("a+b")
        # Windows locks byte zero. Never truncate or remove this file.
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        while not _try_lock(handle):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StateLockError("STATE_LOCK_TIMEOUT")
            time.sleep(min(0.025, remaining))
        return handle
    except BaseException as error:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        if isinstance(error, (StateLockError, KeyboardInterrupt, SystemExit)):
            raise
        raise StateLockError("STATE_LOCK_ACQUIRE_FAILED") from None


@contextmanager
def state_transaction(path, *, timeout=30.0):
    """Hold one path's thread and OS locks across a complete transaction.

    Nested calls by the same thread retain the outer OS lock. Other threads
    and processes wait up to ``timeout`` seconds, then fail without entering
    the body. Different state paths have independent locks. Start fresh Python
    workers rather than forking a process which has imported this module.
    """
    if os.getpid() != _process_id:
        raise StateLockError("STATE_LOCK_PROCESS_CHANGED")
    if (type(timeout) not in (int, float) or timeout < 0
            or timeout > threading.TIMEOUT_MAX or not math.isfinite(timeout)):
        raise StateLockError("STATE_LOCK_TIMEOUT_INVALID")
    canonical = _canonical_path(path)
    deadline = time.monotonic() + timeout
    with _registry_mutex:
        entry = _entries.setdefault(canonical, _Entry())
    if not entry.mutex.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise StateLockError("STATE_LOCK_TIMEOUT")
    entered = False
    try:
        if entry.depth == 0:
            entry.handle = _open_locked(canonical, deadline)
        entry.depth += 1
        entered = True
        yield
    finally:
        try:
            if entered:
                entry.depth -= 1
                if entry.depth == 0:
                    handle, entry.handle = entry.handle, None
                    try:
                        # A forked child must never unlock its parent's lock.
                        if os.getpid() == _process_id:
                            _unlock(handle)
                    except Exception:
                        raise StateLockError("STATE_LOCK_RELEASE_FAILED") from None
                    finally:
                        try:
                            handle.close()
                        except OSError:
                            raise StateLockError("STATE_LOCK_RELEASE_FAILED") from None
        finally:
            entry.mutex.release()
