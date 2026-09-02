"""`GitLocalRepo` -- exercised against real, throwaway git repos created under
pytest's `tmp_path` by the `git_repo` fixture (see `conftest.py`). Never touches
this checkout. Covers the worktree lifecycle, the default-branch guard, the branch
sanitizer's security properties, commit's `-F` file path, `diff()` truncation,
`verify_landed()`, and `cleanup()`.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from ticketbot.adapters.repos.base import RepoError, run_git
from ticketbot.adapters.repos.git_local import GitLocalRepo
from ticketbot.config.schema import AdapterConfig
from ticketbot.core.workitem import WorkItem

COAUTHOR_TRAILER = "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"


def _cfg(**opts) -> AdapterConfig:
    opts.setdefault("path", ".")
    return AdapterConfig(type="git_local", **opts)


def _item(title: str = "Fix the login timeout", key: str = "ENG-1842") -> WorkItem:
    return WorkItem(id=key, title=title, external_id=key)


def _repo(git_repo: Path, **opts) -> GitLocalRepo:
    return GitLocalRepo(_cfg(**opts), base_dir=git_repo)


def _log(ws: Path, fmt: str = "%B", count: str = "-1") -> str:
    return subprocess.run(
        ["git", "-C", str(ws), "log", count, f"--format={fmt}"],
        capture_output=True, text=True, encoding="utf-8",
    ).stdout


# ---- branch_name: sanitization is a security control --------------------------


def test_branch_name_renders_default_template():
    repo = GitLocalRepo(_cfg(), base_dir=Path("."))
    item = _item(title="Login times out on SSO", key="ENG-1842")
    branch = repo.branch_name(item)
    assert branch.startswith("agent/eng-1842-")
    assert "login" in branch


def test_branch_name_sanitizes_spaces_from_ticket_key():
    repo = GitLocalRepo(_cfg(), base_dir=Path("."))
    item = _item(title="t", key="ENG 1842 ticket")
    branch = repo.branch_name(item)
    assert " " not in branch


def test_branch_name_sanitizes_forbidden_git_chars_from_ticket_key():
    repo = GitLocalRepo(_cfg(), base_dir=Path("."))
    item = _item(title="t", key="ENG~1^2:3?4*5[6\\7")
    branch = repo.branch_name(item)
    for ch in "~^:?*[\\":
        assert ch not in branch


def test_branch_name_collapses_dotdot_and_at_brace_sequences():
    repo = GitLocalRepo(_cfg(branch_template="{ticket_key}"), base_dir=Path("."))
    item = _item(title="t", key="a..b@{c")
    branch = repo.branch_name(item)
    assert ".." not in branch
    assert "@{" not in branch


def test_branch_name_caps_length_at_100():
    repo = GitLocalRepo(_cfg(), base_dir=Path("."))
    item = _item(title="t", key="E" * 300)
    branch = repo.branch_name(item)
    assert len(branch) <= 100


def test_branch_name_from_pure_punctuation_title_is_still_usable():
    repo = GitLocalRepo(_cfg(), base_dir=Path("."))
    item = _item(title="!!!@@@###", key="ENG-1")
    branch = repo.branch_name(item)
    assert branch
    assert not branch.startswith("-")
    assert not branch.startswith("/")


def test_branch_name_from_only_forbidden_chars_falls_back_to_usable_branch():
    repo = GitLocalRepo(_cfg(branch_template="{ticket_key}"), base_dir=Path("."))
    item = _item(title="t", key="~^:?*[\\")
    branch = repo.branch_name(item)
    assert branch
    assert not branch.startswith("-")


def test_branch_name_malicious_key_default_template_never_starts_with_dash():
    repo = GitLocalRepo(_cfg(), base_dir=Path("."))
    item = _item(title="evil ticket", key="--upload-pack=evil")
    branch = repo.branch_name(item)
    assert not branch.startswith("-")
    assert branch.startswith("agent/")


def test_branch_name_malicious_key_bare_template_never_starts_with_dash():
    """A ticket titled `--upload-pack=evil` must not become an argument: even with a
    branch_template that embeds the raw ticket_key with no prefix at all, the
    sanitized branch can never be mistaken for a git/gh flag."""
    repo = GitLocalRepo(_cfg(branch_template="{ticket_key}"), base_dir=Path("."))
    item = _item(title="evil ticket", key="--upload-pack=evil")
    branch = repo.branch_name(item)
    assert not branch.startswith("-")


def test_malicious_ticket_key_branch_checks_out_safely(git_repo):
    """End-to-end proof: the sanitized branch, passed as a positional argv element
    to `git worktree add ... -b <branch> ...`, is never read as a flag."""
    repo = _repo(git_repo, branch_template="{ticket_key}")
    item = _item(title="evil", key="--upload-pack=evil")
    branch = repo.branch_name(item)
    ws = repo.checkout(branch)
    assert ws.exists()
    assert ws != git_repo.resolve()


# ---- checkout -------------------------------------------------------------------


def test_checkout_creates_a_worktree_that_is_not_the_repo_path(git_repo):
    repo = _repo(git_repo)
    ws = repo.checkout("agent/eng-1-test")

    assert ws.exists()
    assert ws != git_repo.resolve()
    result = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True,
    )
    assert result.stdout.strip() == "true"


def test_checkout_is_idempotent(git_repo):
    repo = _repo(git_repo)
    ws1 = repo.checkout("agent/eng-2-test")
    ws2 = repo.checkout("agent/eng-2-test")
    assert ws1 == ws2


@pytest.mark.parametrize("branch", ["main", "master", "dev", "develop", "trunk"])
def test_default_branch_guard_raises(git_repo, branch):
    repo = _repo(git_repo)
    with pytest.raises(RepoError):
        repo.checkout(branch)


def test_default_branch_guard_allows_when_configured(git_repo):
    repo = _repo(git_repo, allow_default_branch=True)
    # 'main' is already checked out in the primary repo itself, so `git worktree
    # add` for it would fail for an unrelated git reason (a branch can't be checked
    # out in two worktrees at once) -- use 'master', a DEFAULT_BRANCHES name that
    # isn't the currently-checked-out branch, to isolate what this test checks.
    ws = repo.checkout("master")
    assert ws.exists()


def test_inplace_isolation_returns_repo_path_and_creates_no_worktree(git_repo):
    repo = _repo(git_repo, isolation="inplace", allow_default_branch=True)
    ws = repo.checkout("main")
    assert ws == git_repo.resolve()
    assert not (git_repo.parent / ".ticketbot-worktrees").exists()


def test_inplace_isolation_guards_the_current_branch_not_the_passed_branch(git_repo):
    """isolation: inplace never switches branches -- so the guard must fire based on
    whatever is CURRENTLY checked out ('main'), regardless of the branch argument."""
    repo = _repo(git_repo, isolation="inplace")
    with pytest.raises(RepoError):
        repo.checkout("agent/some-branch")


def test_not_a_git_repo_raises(tmp_path):
    not_a_repo = tmp_path / "plain-dir"
    not_a_repo.mkdir()
    repo = GitLocalRepo(_cfg(), base_dir=not_a_repo)
    with pytest.raises(RepoError):
        repo.checkout("agent/eng-x-test")


def test_workspace_raises_before_checkout(git_repo):
    repo = _repo(git_repo)
    with pytest.raises(RepoError):
        repo.workspace()


# ---- commit ----------------------------------------------------------------------


def test_commit_with_change_returns_sha_and_files(git_repo):
    repo = _repo(git_repo)
    ws = repo.checkout("agent/eng-3-test")
    (ws / "a.txt").write_text("hello\n", encoding="utf-8")

    result = repo.commit("impl: add a.txt")

    assert result.sha is not None
    assert result.files >= 1


def test_second_commit_with_no_change_is_a_noop(git_repo):
    repo = _repo(git_repo)
    ws = repo.checkout("agent/eng-4-test")
    (ws / "b.txt").write_text("hello\n", encoding="utf-8")
    repo.commit("impl: add b.txt")

    result = repo.commit("impl: nothing changed this time")

    assert result.sha is None
    assert result.files == 0


def test_commit_message_roundtrips_multiline_unicode_and_quotes(git_repo):
    repo = _repo(git_repo, coauthor_trailer=False)
    ws = repo.checkout("agent/eng-5-test")
    (ws / "c.txt").write_text("x\n", encoding="utf-8")

    message = 'impl: add "quoted" support -- résumé'
    body = "line one\nline two with 'single' and \"double\" quotes\nünïcödé 日本語"
    repo.commit(message, body)

    log = _log(ws)
    assert message in log
    assert body in log


def test_coauthor_trailer_appears_once_by_default(git_repo):
    repo = _repo(git_repo)
    ws = repo.checkout("agent/eng-6-test")
    (ws / "d.txt").write_text("x\n", encoding="utf-8")
    repo.commit("impl: add d.txt")

    log = _log(ws)
    assert log.count(COAUTHOR_TRAILER) == 1


def test_coauthor_trailer_not_duplicated_across_commits(git_repo):
    repo = _repo(git_repo)
    ws = repo.checkout("agent/eng-7-test")
    (ws / "e.txt").write_text("1\n", encoding="utf-8")
    repo.commit("impl: first")
    (ws / "e.txt").write_text("2\n", encoding="utf-8")
    repo.commit("impl: second")

    log = _log(ws, count="-2")
    assert log.count(COAUTHOR_TRAILER) == 2  # once per commit, never duplicated within one


def test_coauthor_trailer_absent_when_disabled(git_repo):
    repo = _repo(git_repo, coauthor_trailer=False)
    ws = repo.checkout("agent/eng-8-test")
    (ws / "f.txt").write_text("x\n", encoding="utf-8")
    repo.commit("impl: add f.txt")

    log = _log(ws)
    assert "Co-Authored-By" not in log


# ---- diff --------------------------------------------------------------------


def test_diff_contains_changed_file_name(git_repo):
    repo = _repo(git_repo)
    ws = repo.checkout("agent/eng-9-test")
    (ws / "g.txt").write_text("hello\n", encoding="utf-8")
    repo.commit("impl: add g.txt")
    (ws / "g.txt").write_text("hello again\n", encoding="utf-8")

    text = repo.diff()

    assert "g.txt" in text


def test_diff_is_truncated_when_very_large(git_repo):
    repo = _repo(git_repo)
    ws = repo.checkout("agent/eng-10-test")
    big = "line\n" * 200_000
    (ws / "h.txt").write_text(big, encoding="utf-8")
    repo.commit("impl: add a very large file")

    text = repo.diff()

    assert text.endswith("\n…[diff truncated]\n")
    assert len(text) <= 400_000 + len("\n…[diff truncated]\n")


# ---- verify_landed -------------------------------------------------------------


def test_verify_landed_returns_empty_when_files_exist(git_repo):
    repo = _repo(git_repo)
    ws = repo.checkout("agent/eng-11-test")
    (ws / "i.txt").write_text("x\n", encoding="utf-8")

    assert repo.verify_landed(["i.txt"]) == []


def test_verify_landed_names_missing_files(git_repo):
    repo = _repo(git_repo)
    repo.checkout("agent/eng-12-test")

    assert repo.verify_landed(["nope.txt"]) == ["nope.txt"]


def test_verify_landed_flags_absolute_path_outside_workspace(git_repo, tmp_path):
    repo = _repo(git_repo)
    repo.checkout("agent/eng-13-test")

    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x\n", encoding="utf-8")

    missing = repo.verify_landed([outside])

    assert len(missing) == 1
    assert "elsewhere.txt" in missing[0]
    assert "outside workspace" in missing[0]


def test_parent_clone_hint_names_both_paths(git_repo):
    repo = _repo(git_repo)
    ws = repo.checkout("agent/eng-13b-test")
    hint = repo.parent_clone_hint()
    assert str(git_repo.resolve()) in hint
    assert str(ws) in hint


# ---- cleanup ---------------------------------------------------------------------


def test_cleanup_removes_worktree_and_leaves_branch(git_repo):
    repo = _repo(git_repo)
    ws = repo.checkout("agent/eng-14-test")

    repo.cleanup()

    assert not ws.exists()
    branches = run_git(["branch", "--list", "agent/eng-14-test"], cwd=git_repo).stdout
    assert "agent/eng-14-test" in branches


def test_cleanup_keeps_worktree_when_configured(git_repo):
    repo = _repo(git_repo, keep_worktree=True)
    ws = repo.checkout("agent/eng-15-test")

    repo.cleanup()

    assert ws.exists()


# ---- run_git ----------------------------------------------------------------------


def test_run_git_raises_repo_error_with_exit_code_and_stderr(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is not on PATH")
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()

    with pytest.raises(RepoError) as exc_info:
        run_git(["status"], cwd=not_a_repo)

    message = str(exc_info.value)
    assert "128" in message
    assert "not a git repository" in message.lower()


def test_run_git_check_false_does_not_raise(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is not on PATH")
    not_a_repo = tmp_path / "not-a-repo-2"
    not_a_repo.mkdir()

    result = run_git(["status"], cwd=not_a_repo, check=False)

    assert result.returncode != 0
