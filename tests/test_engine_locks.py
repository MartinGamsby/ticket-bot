"""`RunLock` -- one work item -> one run, enforced with an atomic
`O_CREAT|O_EXCL` lock file.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ticketbot.engine.locks import LockHeld, RunLock


def test_acquire_then_second_acquire_raises_lock_held_naming_run_id(tmp_path: Path) -> None:
    lock1 = RunLock(tmp_path, "ENG-1")
    lock1.acquire("run-1")

    lock2 = RunLock(tmp_path, "ENG-1")
    with pytest.raises(LockHeld) as excinfo:
        lock2.acquire("run-2")

    assert "run-1" in str(excinfo.value)
    assert excinfo.value.info["run_id"] == "run-1"


def test_release_then_reacquire_succeeds(tmp_path: Path) -> None:
    lock1 = RunLock(tmp_path, "ENG-1")
    lock1.acquire("run-1")
    lock1.release()

    lock2 = RunLock(tmp_path, "ENG-1")
    lock2.acquire("run-2")  # must not raise
    lock2.release()


def test_force_breaks_an_existing_lock(tmp_path: Path) -> None:
    lock1 = RunLock(tmp_path, "ENG-1")
    lock1.acquire("run-1")

    lock2 = RunLock(tmp_path, "ENG-1")
    lock2.acquire("run-2", force=True)  # must not raise

    path = tmp_path / ".locks" / "eng-1.lock"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["run_id"] == "run-2"


def test_stale_lock_message_says_so(tmp_path: Path) -> None:
    locks_dir = tmp_path / ".locks"
    locks_dir.mkdir(parents=True)
    payload = {
        "pid": 999999,  # astronomically unlikely to be a live pid
        "host": "somehost",
        "run_id": "old-run",
        "started_at": time.time() - 999999,  # ancient
        "key": "ENG-1",
    }
    (locks_dir / "eng-1.lock").write_text(json.dumps(payload), encoding="utf-8")

    lock = RunLock(tmp_path, "ENG-1")
    with pytest.raises(LockHeld) as excinfo:
        lock.acquire("run-2")
    assert "stale" in str(excinfo.value).lower()


def test_fresh_lock_from_live_process_is_not_reported_stale(tmp_path: Path) -> None:
    lock1 = RunLock(tmp_path, "ENG-1")
    lock1.acquire("run-1")  # pid = our own, definitely alive; started_at = now

    lock2 = RunLock(tmp_path, "ENG-1")
    with pytest.raises(LockHeld) as excinfo:
        lock2.acquire("run-2")
    assert "stale" not in str(excinfo.value).lower()


def test_release_of_a_lock_we_do_not_own_is_a_noop(tmp_path: Path) -> None:
    lock1 = RunLock(tmp_path, "ENG-1")
    lock1.acquire("run-1")

    # lock2 never successfully acquired (never set _owned_run_id) -- releasing
    # it must not touch lock1's file.
    lock2 = RunLock(tmp_path, "ENG-1")
    lock2.release()

    path = tmp_path / ".locks" / "eng-1.lock"
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["run_id"] == "run-1"


def test_context_manager_releases_on_exception(tmp_path: Path) -> None:
    path = tmp_path / ".locks" / "eng-1.lock"
    with pytest.raises(RuntimeError):
        with RunLock(tmp_path, "ENG-1") as lock:
            lock.acquire("run-1")
            assert path.exists()
            raise RuntimeError("boom")
    assert not path.exists()


def test_context_manager_releases_on_success(tmp_path: Path) -> None:
    path = tmp_path / ".locks" / "eng-1.lock"
    with RunLock(tmp_path, "ENG-1") as lock:
        lock.acquire("run-1")
        assert path.exists()
    assert not path.exists()


def test_is_locked_reflects_lock_file_presence(tmp_path: Path) -> None:
    lock = RunLock(tmp_path, "ENG-1")
    assert lock.is_locked() is False
    lock.acquire("run-1")
    assert lock.is_locked() is True
    lock.release()
    assert lock.is_locked() is False


def test_different_keys_do_not_contend(tmp_path: Path) -> None:
    lock1 = RunLock(tmp_path, "ENG-1")
    lock1.acquire("run-1")
    lock2 = RunLock(tmp_path, "ENG-2")
    lock2.acquire("run-2")  # must not raise -- different work item
    lock1.release()
    lock2.release()
