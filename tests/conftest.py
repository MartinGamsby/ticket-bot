"""Shared pytest fixtures. `git_repo` builds a real throwaway git repository under
`tmp_path` for the repo-adapter tests -- see `test_repo_git_local.py` and
`test_repo_github.py`. Every git identity here is set LOCALLY (never `--global`),
so these tests can never touch the developer's real git config, and the repo lives
entirely under pytest's `tmp_path` -- never this checkout.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def _git(args: list[str], cwd: Path) -> None:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, shell=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {args} failed: {result.stderr}")


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git is not on PATH")

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test User"], repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-m", "init"], repo)
    return repo
