"""Recovering from an interrupted run, and branch names that don't stutter.

Both come from actually running the offline quickstart and hitting Ctrl+C: the
branch was `agent/add-a-health-endpoint-add-a-health-endpoint`, and the next run
died with `fatal: '...' is already checked out at ...`.
"""

from __future__ import annotations

import shutil

import pytest

from ticketbot.adapters.repos.base import run_git
from ticketbot.adapters.repos.git_local import GitLocalRepo, _parse_worktree_list
from ticketbot.config.schema import AdapterConfig
from ticketbot.core.workitem import WorkItem


def _repo(git_repo, **opts) -> GitLocalRepo:
    return GitLocalRepo(AdapterConfig(type="git_local", path=str(git_repo), **opts))


def _file_item(title: str) -> WorkItem:
    """A file/text item: no external_id, so `key` falls back to `id` -- the slug."""
    from ticketbot.core.workitem import slugify

    return WorkItem(id=slugify(title), title=title)


def _jira_item(key: str, title: str) -> WorkItem:
    return WorkItem(id=key.lower(), title=title, external_id=key)


# --------------------------------------------------------------- branch naming


def test_a_file_item_does_not_repeat_its_slug_in_the_branch(git_repo):
    # A file/text item has no ticket key -- key IS the slug -- so the default
    # `agent/{ticket_key}-{slug}` template used to render the words twice.
    repo = _repo(git_repo)
    item = _file_item("Add a /health endpoint")

    assert repo.branch_name(item) == "agent/add-a-health-endpoint"


def test_a_jira_item_still_gets_key_and_slug(git_repo):
    repo = _repo(git_repo)
    item = _jira_item("ENG-1842", "Login times out on SSO")

    assert repo.branch_name(item) == "agent/eng-1842-login-times-out-on-sso"


def test_an_empty_template_slot_leaves_no_stray_separator(git_repo):
    repo = _repo(git_repo, branch_template="agent/{ticket_key}-{slug}")
    item = _file_item("same")

    name = repo.branch_name(item)

    assert name == "agent/same"
    assert "--" not in name and "/-" not in name


# ------------------------------------------------------- worktree list parsing


def test_parse_worktree_list_reads_the_porcelain_records():
    stdout = (
        "worktree C:/repo\nHEAD abc\nbranch refs/heads/main\n\n"
        "worktree C:/repo/.ticketbot-worktrees/agent-x-1a2b\nHEAD def\n"
        "branch refs/heads/agent/x\n\n"
    )

    parsed = _parse_worktree_list(stdout)

    assert parsed["refs/heads/main"].as_posix() == "C:/repo"
    assert parsed["refs/heads/agent/x"].name == "agent-x-1a2b"


def test_parse_worktree_list_skips_a_detached_worktree():
    parsed = _parse_worktree_list("worktree C:/repo/wt\nHEAD abc\ndetached\n\n")

    assert parsed == {}


def test_parse_worktree_list_handles_a_path_containing_spaces():
    parsed = _parse_worktree_list(
        "worktree C:/My Repos/thing\nHEAD abc\nbranch refs/heads/main\n\n"
    )

    assert parsed["refs/heads/main"].as_posix() == "C:/My Repos/thing"


# ------------------------------------------------------------------- recovery


def test_a_second_checkout_of_the_same_branch_reuses_the_worktree(git_repo):
    # The Ctrl+C case: the first run took the worktree and never released it.
    repo = _repo(git_repo)
    item = _file_item("Add a /health endpoint")
    branch = repo.branch_name(item)

    first = repo.checkout(branch)

    fresh = _repo(git_repo)  # a new process would build a new adapter
    second = fresh.checkout(branch)

    assert second == first
    assert second.is_dir()


def test_a_worktree_deleted_by_hand_is_pruned_and_recreated(git_repo):
    repo = _repo(git_repo)
    branch = "agent/gone"
    workspace = repo.checkout(branch)

    shutil.rmtree(workspace)  # directory removed without `git worktree remove`

    fresh = _repo(git_repo)
    recreated = fresh.checkout(branch)

    assert recreated.is_dir()
    assert recreated != workspace


def test_reuse_keeps_the_branch_that_was_already_checked_out(git_repo):
    repo = _repo(git_repo)
    branch = "agent/keeps-branch"
    repo.checkout(branch)

    fresh = _repo(git_repo)
    workspace = fresh.checkout(branch)

    current = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=workspace).stdout.strip()
    assert current == branch


def test_the_main_clone_is_never_mistaken_for_a_reusable_worktree(git_repo):
    # `main` is checked out in the clone itself; that must not be reused as this
    # run's workspace, and the default-branch guard must still fire.
    repo = _repo(git_repo)

    with pytest.raises(Exception):
        repo.checkout("main")
