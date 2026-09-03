"""`RunLock` -- one work item -> one run, enforced with an atomic
`O_CREAT|O_EXCL` lock file.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

import pytest

from ticketbot.engine.locks import LockHeld, RunLock, lock_filename


def _lock_path(runs_dir: Path, key: str) -> Path:
    """The one place these tests spell the lock-file layout. Built from
    `lock_filename()` rather than hardcoded, because the digest suffix is not
    reproducible by hand -- but every assertion below still pins the exact file,
    not a glob.
    """
    return runs_dir / ".locks" / lock_filename(key)


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

    path = _lock_path(tmp_path, "ENG-1")
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
    _lock_path(tmp_path, "ENG-1").write_text(json.dumps(payload), encoding="utf-8")

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

    path = _lock_path(tmp_path, "ENG-1")
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["run_id"] == "run-1"


def test_context_manager_releases_on_exception(tmp_path: Path) -> None:
    path = _lock_path(tmp_path, "ENG-1")
    with pytest.raises(RuntimeError):
        with RunLock(tmp_path, "ENG-1") as lock:
            lock.acquire("run-1")
            assert path.exists()
            raise RuntimeError("boom")
    assert not path.exists()


def test_context_manager_releases_on_success(tmp_path: Path) -> None:
    path = _lock_path(tmp_path, "ENG-1")
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


# ---- the lock key is the RAW key, not its slug -----------------------------------
#
# `slugify()` is lossy in three separate ways (case, punctuation, a 40-char cut on a
# word boundary). Keying the lock file on the slug alone made every pair below share
# one file: the second `acquire()` raised `LockHeld` and `poll()` skipped that ticket
# with nothing in the log to say why. Each of these fails against the slug-only
# filename and passes against `lock_filename()`.


@pytest.mark.parametrize(
    ("key_a", "key_b", "lossy"),
    [
        ("ENG-1", "eng-1", "case"),
        ("ENG-1", "eng.1", "punctuation"),
        ("ENG-1", "ENG_1", "punctuation"),
        (
            "ENG-1234 implement the new authentication subsystem alpha",
            "ENG-1234 implement the new authentication subsystem beta",
            "40-char truncation",
        ),
    ],
)
def test_keys_that_share_a_slug_still_get_their_own_lock(
    tmp_path: Path, key_a: str, key_b: str, lossy: str
) -> None:
    from ticketbot.core.workitem import slugify

    assert slugify(key_a) == slugify(key_b), f"fixture no longer exercises {lossy}"

    lock_a = RunLock(tmp_path, key_a)
    lock_a.acquire("run-a")

    lock_b = RunLock(tmp_path, key_b)
    lock_b.acquire("run-b")  # must not raise: a DIFFERENT work item

    assert _lock_path(tmp_path, key_a) != _lock_path(tmp_path, key_b)
    assert len(list((tmp_path / ".locks").glob("*.lock"))) == 2

    # ...and each still guards its own key
    with pytest.raises(LockHeld):
        RunLock(tmp_path, key_a).acquire("run-c")
    with pytest.raises(LockHeld):
        RunLock(tmp_path, key_b).acquire("run-d")


def test_lock_filename_is_readable_bounded_and_filesystem_safe() -> None:
    """The digest must not cost the two things the slug was there for: a name a
    human can recognize in `runs/.locks/`, and a name every filesystem accepts.
    """
    name = lock_filename("ENG-1842 Login times out")
    assert name.startswith("eng-1842-login-times-out-")
    assert name.endswith(".lock")
    assert re.fullmatch(r"[a-z0-9-]+\.lock", name), name

    # bounded: slug (<=40) + '-' + 16 hex + '.lock'
    longest = lock_filename("X" * 500 + " and more words after that")
    assert len(longest) <= 40 + 1 + 16 + len(".lock")

    # a key with nothing sluggable still yields a usable, distinct name
    assert lock_filename("...") != lock_filename("!!!")
    assert re.fullmatch(r"task-[0-9a-f]{16}\.lock", lock_filename("..."))


def test_lock_filename_is_stable_across_calls_and_processes() -> None:
    """`release()` and `--force-lock` both re-derive the path from the key, so the
    mapping has to be a pure function of the key -- no salt, no randomness.
    """
    assert lock_filename("ENG-1") == lock_filename("ENG-1")
    expected = hashlib.sha256(b"ENG-1").hexdigest()[:16]
    assert lock_filename("ENG-1") == f"eng-1-{expected}.lock"
