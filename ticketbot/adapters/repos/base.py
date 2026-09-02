"""`CommitResult`/`Repo` -- the shared shape every repo kind (`git_local`, `github`)
is driven through -- plus `run_git`, the single choke point every git invocation in
this package goes through.

**Security hot spot.** `run_git` is `subprocess.run(["git", *args], ..., shell=False)`
-- never a composed command string -- because branch names, commit messages and PR
bodies all trace back to untrusted ticket text or model output. On failure it raises
`RepoError` with the argv and the exit code, but never the child environment (which
may carry an expanded token), and it redacts stderr before it reaches the message.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ...config.redact import redact


@dataclass
class CommitResult:
    sha: str | None  # None when nothing was staged (a no-op commit)
    message: str
    files: int = 0


@runtime_checkable
class Repo(Protocol):
    def describe(self) -> str: ...  # 'acme/app @ agent/ENG-1842-login-timeout'

    def checkout(self, branch: str) -> Path: ...  # returns the ABSOLUTE workspace path

    def workspace(self) -> Path: ...  # the path checkout() returned; raises before checkout

    def status(self) -> list[str]: ...  # porcelain lines, e.g. ['M  src/a.py']

    def diff(self, base: str | None = None) -> str: ...

    def commit(self, message: str, body: str = "") -> CommitResult: ...

    def push(self) -> None: ...  # no-op for git_local

    def open_pr(self, title: str, body: str) -> str | None: ...  # None for git_local

    def cleanup(self) -> None: ...

    def verify_landed(self, paths: Sequence[Path | str]) -> list[str]: ...  # declared writes missing from the workspace

    def drifted(self) -> list[str]: ...  # changes that appeared OUTSIDE the workspace since checkout


class RepoError(RuntimeError):
    """A repo adapter failed: not a git repository, the default-branch guard, a
    failing `git`/`gh` invocation, an unparseable clone URL, ...
    """


DEFAULT_BRANCHES = {"main", "master", "dev", "develop", "trunk"}


def run_git(
    args: list[str], *, cwd: Path, timeout: float = 120, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run `git <args>` as an argv list, `cwd`-scoped, `shell=False` always -- never
    a composed command string, so a hostile branch name or commit message can never
    be reinterpreted as a shell fragment.

    On a non-zero exit with `check=True`, raises `RepoError` naming the argv (NOT the
    environment) and the exit code, with `redact(stderr)`. `check=False` callers get
    the raw `CompletedProcess` back to inspect (e.g. a probe that expects to fail).
    """
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )
    except OSError as exc:
        raise RepoError(f"failed to start git {args!r}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RepoError(f"git {args!r} timed out after {timeout:g}s") from exc

    if check and result.returncode != 0:
        raise RepoError(
            f"git {args!r} failed (exit {result.returncode}): {redact(result.stderr.strip())}"
        )
    return result
