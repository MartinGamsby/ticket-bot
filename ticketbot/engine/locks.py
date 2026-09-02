"""`RunLock` -- one work item -> one run, enforced with an atomic `O_CREAT|O_EXCL`
lock file, so two orchestrator processes (two `poll()` loops, or a `run` racing a
`poll`) can never both act on the same work item at once.

The lock file's content is advisory (JSON: pid, host, run id, start time) -- it is
what lets a human diagnose "who's holding this" and what lets `acquire()` recognize
a stale lock left behind by a crashed process, rather than actually enforcing
anything by itself. The enforcement is the atomic file creation.
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path
from typing import Any

from ..core.workitem import slugify

DEFAULT_STALE_AFTER_S = 21600  # 6 hours


class LockHeld(RuntimeError):
    def __init__(self, key: str, info: dict[str, Any]) -> None:
        self.key = key
        self.info = info
        holder = info.get("run_id", "?")
        pid = info.get("pid", "?")
        started_at = info.get("started_at", "?")
        message = (
            f"work item {key!r} is already locked by run {holder!r} "
            f"(pid {pid}, started {started_at})"
        )
        if info.get("_stale"):
            message += " -- the lock looks stale; pass force=True to break it"
        super().__init__(message)


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check; never raises. Windows has no null-signal `kill`,
    so it probes with `OpenProcess` instead.
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            process_query_limited_information = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
                process_query_limited_information, False, pid
            )
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours
    except OSError:
        return False
    return True


class RunLock:
    """One work item -> one run. Lock file:
    `<runs_dir>/.locks/<sanitized key>.lock` containing JSON
    `{pid, host, run_id, started_at, key}`.
    """

    def __init__(self, runs_dir: Path, key: str) -> None:
        self.runs_dir = Path(runs_dir)
        self.key = key
        self._owned_run_id: str | None = None

    def _path(self) -> Path:
        locks_dir = self.runs_dir / ".locks"
        locks_dir.mkdir(parents=True, exist_ok=True)
        return locks_dir / f"{slugify(self.key)}.lock"

    def is_locked(self) -> bool:
        """Best-effort, non-mutating check: does a lock file currently exist for
        this key? Used by `poll()` to skip an item another sweep is already on
        without contending for it.
        """
        return self._path().exists()

    def _read(self, path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def acquire(
        self, run_id: str, *, force: bool = False, stale_after_s: int = DEFAULT_STALE_AFTER_S
    ) -> None:
        """`os.open(path, O_CREAT | O_EXCL | O_WRONLY)` -- atomic on Windows and
        POSIX. On `FileExistsError`: read the holder; if it looks stale (older than
        `stale_after_s`, or its pid is not alive) that fact is folded into the
        raised `LockHeld`'s message -- callers decide whether to retry with
        `force=True`. With `force=True`, an existing lock is unconditionally
        overwritten.
        """
        path = self._path()
        payload = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "run_id": run_id,
            "started_at": time.time(),
            "key": self.key,
        }
        data = json.dumps(payload).encode("utf-8")

        if force:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            info = self._read(path)
            age = time.time() - float(info.get("started_at", 0) or 0)
            info["_stale"] = age > stale_after_s or not _pid_alive(int(info.get("pid", -1) or -1))
            raise LockHeld(self.key, info) from None

        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        self._owned_run_id = run_id

    def release(self) -> None:
        """Only releases the lock if we own it (matching run_id); never raises."""
        if self._owned_run_id is None:
            return
        path = self._path()
        try:
            info = self._read(path)
            if info.get("run_id") == self._owned_run_id:
                path.unlink(missing_ok=True)
        except OSError:
            pass
        finally:
            self._owned_run_id = None

    def __enter__(self) -> "RunLock":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()
