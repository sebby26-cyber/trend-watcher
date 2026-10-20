"""One process-lifetime flock shared by every mutating CLI entry point.

All mutating stages are globally serialized: WAL allows concurrent readers but
writers (tick, run, ack, backup, migrations, config toggles) take this lock.
Re-entrant within one process so nested helpers do not deadlock themselves.
"""
import contextlib
import fcntl
import os
import time

LOCK_NAME = "trend-watch.lock"
_held = {}


class LockBusy(Exception):
    pass


@contextlib.contextmanager
def state_lock(state_dir, wait_s=0.0, sleep=None, clock=None):
    sleep, clock = sleep or time.sleep, clock or time.monotonic
    state_dir.mkdir(parents=True, exist_ok=True)
    path = str((state_dir / LOCK_NAME).resolve())
    if path in _held:
        _held[path][1] += 1
        try:
            yield path
        finally:
            _held[path][1] -= 1
        return
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = clock() + max(0.0, wait_s)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, PermissionError):
                if clock() >= deadline:
                    raise LockBusy("another trend-watch process holds " + path)
                sleep(0.2)
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode("ascii"))
        _held[path] = [fd, 1]
        try:
            yield path
        finally:
            del _held[path]
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
