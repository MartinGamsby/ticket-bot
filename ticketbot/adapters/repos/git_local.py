"""`GitLocalRepo` -- an isolated `git worktree` per branch, one commit per pipeline
step via `git commit -F <utf8 file>` (never an inline multiline message), `diff()`
for the reviewer, and the pair `verify_landed()` + `drifted()` -- the "a clean agent
summary is not proof the edit is in the right tree" lesson, made executable.
`verify_landed()` audits the paths a step DECLARED; `drifted()` watches the parent
clone, which is where a stray write actually goes and where nothing the step
declares would ever mention it.

`branch_name()` is a security control, not cosmetics: it is the only place an
untrusted ticket title/key becomes a git ref and, eventually, an argv element passed
to `git`/`gh`. It sanitizes the FULL rendered string (not just the pieces) so a
ticket titled e.g. `--upload-pack=evil` can never surface as something that looks
like a flag.
"""

from __future__ import annotations

import logging
import re
import tempfile
from collections.abc import Sequence
from pathlib import Path
from secrets import token_hex

from ...config.schema import AdapterConfig
from ...core.templating import render
from ...core.workitem import WorkItem
from .base import DEFAULT_BRANCHES, CommitResult, RepoError, run_git

logger = logging.getLogger(__name__)

COAUTHOR_TRAILER = "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"

_FORBIDDEN_BRANCH_CHARS = "~^:?*[\\"
_BRANCH_MAX_LEN = 100
_MAX_DIFF_CHARS = 400_000


def _sanitize_branch(rendered: str) -> str:
    """lowercase, spaces -> '-', strip `~^:?*[\\`, collapse '..' and '@{' sequences,
    collapse repeated '/', strip leading/trailing '/' or '-', cap at 100 chars, and
    fall back to 'task' if nothing usable survives. Never returns a string that
    starts with '-' -- that is what stops a hostile title from being read as a flag
    by `git`/`gh`.
    """
    s = rendered.lower()
    s = re.sub(r"\s+", "-", s)
    for ch in _FORBIDDEN_BRANCH_CHARS:
        s = s.replace(ch, "")
    while ".." in s:
        s = s.replace("..", "-")
    while "@{" in s:
        s = s.replace("@{", "-")
    s = re.sub(r"/+", "/", s)
    s = s.strip("/-")
    if len(s) > _BRANCH_MAX_LEN:
        s = s[:_BRANCH_MAX_LEN].strip("/-")
    if not s:
        s = "task"
    if s.startswith("-"):  # unreachable given the strip above; kept as a hard stop
        raise RepoError(f"sanitized branch name still starts with '-': {s!r}")
    return s


def _truncate_diff(text: str) -> str:
    if len(text) <= _MAX_DIFF_CHARS:
        return text
    return text[:_MAX_DIFF_CHARS] + "\n…[diff truncated]\n"


class GitLocalRepo:
    def __init__(
        self, cfg: AdapterConfig, *, base_dir: Path | None = None, run_dir: Path | None = None
    ) -> None:
        """`path` resolves against `base_dir` (the profile's directory). `run_dir`
        is where artifacts like `patch.diff` go; it is NOT the workspace.
        """
        base = Path(base_dir) if base_dir is not None else Path(".")
        raw_path = Path(str(cfg.opt("path", ".")))
        self.path: Path = (raw_path if raw_path.is_absolute() else (base / raw_path)).resolve()
        self.run_dir: Path | None = Path(run_dir) if run_dir is not None else None

        self.base_branch: str | None = cfg.opt("base_branch")
        self.branch_template: str = str(cfg.opt("branch_template", "agent/{ticket_key}-{slug}"))
        self.isolation: str = str(cfg.opt("isolation", "worktree"))

        worktrees_dir_cfg = cfg.opt("worktrees_dir")
        self.worktrees_dir: Path = (
            Path(str(worktrees_dir_cfg)).resolve()
            if worktrees_dir_cfg
            else (self.path.parent / ".ticketbot-worktrees").resolve()
        )
        self.keep_worktree: bool = bool(cfg.opt("keep_worktree", False))
        self.git_user_name: str = str(cfg.opt("git_user_name", "ticketbot"))
        self.git_user_email: str = str(cfg.opt("git_user_email", "ticketbot@localhost"))
        self.allow_default_branch: bool = bool(cfg.opt("allow_default_branch", False))
        self.coauthor_trailer: bool = bool(cfg.opt("coauthor_trailer", True))

        self._workspace: Path | None = None
        self._branch: str | None = None
        self._start_sha: str | None = None
        # Porcelain status of the PARENT clone as `checkout()` left it. `None`
        # means "drift is not observable here" -- before checkout, and under
        # `isolation: inplace`, where the workspace IS the parent clone. See
        # `drifted()`.
        self._parent_baseline: set[str] | None = None

    # ------------------------------------------------------------------ #
    # branch naming
    # ------------------------------------------------------------------ #

    def branch_name(self, item: WorkItem) -> str:
        rendered = render(
            self.branch_template,
            {"ticket_key": item.key, "slug": item.slug(), "issue_type": item.issue_type.lower()},
        )
        return _sanitize_branch(rendered)

    # ------------------------------------------------------------------ #
    # describe / workspace
    # ------------------------------------------------------------------ #

    def describe(self) -> str:
        branch = self._branch or "(no checkout)"
        return f"{self.path.name} @ {branch}"

    def workspace(self) -> Path:
        if self._workspace is None:
            raise RepoError("checkout() has not been called yet")
        return self._workspace

    # ------------------------------------------------------------------ #
    # checkout
    # ------------------------------------------------------------------ #

    def _guard_default_branch(self, branch: str) -> None:
        if branch in DEFAULT_BRANCHES and not self.allow_default_branch:
            raise RepoError(
                f"refusing to work directly on the default branch '{branch}'; set a branch_template"
            )

    def _is_git_repo(self, path: Path) -> bool:
        result = run_git(["rev-parse", "--git-dir"], cwd=path, check=False)
        return result.returncode == 0

    def _current_branch(self, path: Path) -> str:
        return run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path).stdout.strip()

    def _rev_parse(self, path: Path, rev: str) -> str:
        return run_git(["rev-parse", rev], cwd=path).stdout.strip()

    def _branch_exists(self, branch: str) -> bool:
        result = run_git(
            ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=self.path, check=False
        )
        return result.returncode == 0

    def checkout(self, branch: str) -> Path:
        if self._workspace is not None and self._branch == branch:
            return self._workspace  # idempotent

        self._guard_default_branch(branch)

        if not self._is_git_repo(self.path):
            raise RepoError(f"not a git repository: {self.path}")

        if self.isolation == "inplace":
            current = self._current_branch(self.path)
            self._guard_default_branch(current)
            self._workspace = self.path.resolve()
            self._branch = branch
            self._start_sha = self._rev_parse(self._workspace, "HEAD")
            self._parent_baseline = None  # workspace IS the parent clone
            return self._workspace

        if self.isolation != "worktree":
            raise RepoError(f"unknown isolation mode: {self.isolation!r}")

        base = self.base_branch or self._rev_parse(self.path, "HEAD")
        wt_name = f"{branch.replace('/', '-')}-{token_hex(2)}"
        wt = self.worktrees_dir / wt_name
        wt.parent.mkdir(parents=True, exist_ok=True)

        if self._branch_exists(branch):
            run_git(["worktree", "add", str(wt), branch], cwd=self.path)
        else:
            run_git(["worktree", "add", str(wt), "-b", branch, base], cwd=self.path)

        # Set identity LOCALLY in the worktree so commits never depend on the
        # developer's / CI runner's global git config.
        run_git(["config", "user.name", self.git_user_name], cwd=wt)
        run_git(["config", "user.email", self.git_user_email], cwd=wt)

        self._workspace = wt.resolve()
        self._branch = branch
        self._start_sha = self._rev_parse(self._workspace, "HEAD")
        # Taken AFTER `worktree add`, so whatever that wrote into the parent clone
        # is part of the baseline and can never be reported as drift.
        self._parent_baseline = self._parent_porcelain()
        return self._workspace

    # ------------------------------------------------------------------ #
    # status / diff
    # ------------------------------------------------------------------ #

    def status(self) -> list[str]:
        result = run_git(["status", "--porcelain"], cwd=self.workspace())
        return [line for line in result.stdout.splitlines() if line]

    def _runs_pathspec(self) -> list[str]:
        """Pathspec args that keep ticketbot's OWN run directory out of the parent
        clone's porcelain output.

        `runs/` at the project root is a natural place to put `runs_dir`, and then
        every step writes artifacts into a tree that is also the repo being worked
        on. Those are the engine's files, not an agent's stray edit, so they must
        never read as drift. The whole runs dir is excluded (not just this run's
        subdirectory) because git collapses an untracked tree to a single `?? runs/`
        line, which a narrower exclusion could not suppress.
        """
        if self.run_dir is None:
            return []
        try:
            rel = self.run_dir.resolve().parent.relative_to(self.path)
        except ValueError:
            return []  # the runs dir is outside the clone: nothing to exclude
        return ["--", ".", f":(exclude){rel.as_posix()}"]

    def _parent_porcelain(self) -> set[str]:
        """`git status --porcelain` in the PARENT clone, as a set of non-empty
        lines, minus the runs dir.

        Unlike `status()` this never raises: it backs `drifted()`, and a drift
        probe that could throw would turn a diagnostic into a new way to fail a
        run. An unreadable or missing tree reports "no changes", which is the
        direction that cannot invent a failure.
        """
        try:
            result = run_git(
                ["status", "--porcelain", *self._runs_pathspec()], cwd=self.path, check=False
            )
        except RepoError:
            return set()
        if result.returncode != 0:
            return set()
        return {line for line in result.stdout.splitlines() if line.strip()}

    def _diff_base(self) -> str:
        if self.base_branch:
            result = run_git(
                ["merge-base", self.base_branch, "HEAD"], cwd=self.workspace(), check=False
            )
            merge_base = result.stdout.strip()
            if result.returncode == 0 and merge_base:
                return merge_base
        if self._start_sha:
            return self._start_sha
        return "HEAD"

    def diff(self, base: str | None = None) -> str:
        ws = self.workspace()
        base_rev = base if base is not None else self._diff_base()
        range_diff = run_git(["diff", f"{base_rev}...HEAD"], cwd=ws).stdout
        uncommitted_diff = run_git(["diff"], cwd=ws).stdout
        return _truncate_diff(range_diff + uncommitted_diff)

    # ------------------------------------------------------------------ #
    # commit
    # ------------------------------------------------------------------ #

    def _compose_message(self, message: str, body: str) -> str:
        text = f"{message}\n\n{body}\n" if body else f"{message}\n"
        if self.coauthor_trailer and COAUTHOR_TRAILER not in text:
            text = text.rstrip("\n") + "\n\n" + COAUTHOR_TRAILER + "\n"
        return text

    def commit(self, message: str, body: str = "") -> CommitResult:
        ws = self.workspace()
        run_git(["add", "-A"], cwd=ws)

        staged = run_git(["diff", "--cached", "--quiet"], cwd=ws, check=False)
        if staged.returncode == 0:
            return CommitResult(sha=None, message=message, files=0)  # nothing staged is expected, not an error

        full_message = self._compose_message(message, body)

        # Write the message to a temp file, UTF-8 without BOM, LF newlines --
        # `-F <file>`, never an inline multiline message on the command line.
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="\n", suffix=".txt", delete=False
        ) as f:
            f.write(full_message)
            msg_file = f.name
        try:
            run_git(["commit", "-F", msg_file], cwd=ws)
        finally:
            Path(msg_file).unlink(missing_ok=True)

        sha = self._rev_parse(ws, "HEAD")
        stat_result = run_git(["show", "--stat", "--name-only", "--format=", "HEAD"], cwd=ws)
        files = len([line for line in stat_result.stdout.splitlines() if line.strip()])

        return CommitResult(sha=sha, message=message, files=files)

    # ------------------------------------------------------------------ #
    # push / PR -- no-ops for a purely local repo
    # ------------------------------------------------------------------ #

    def push(self) -> None:
        pass

    def open_pr(self, title: str, body: str) -> None:
        return None

    # ------------------------------------------------------------------ #
    # verify_landed / drifted -- the worktree-drift lesson, made executable
    # ------------------------------------------------------------------ #

    def verify_landed(self, paths: Sequence[Path | str]) -> list[str]:
        """Return the subset of `paths` that do NOT exist under the workspace.

        A containment-and-existence check on paths someone else declared, and only
        that. It is the SECOND half of the drift story, not the first: today every
        caller feeds it `ExecResult.files_written`, which both executors derive
        from a snapshot of the workspace itself, so those paths are inside the
        workspace by construction and this returns `[]` every time. It still earns
        its place -- it is what stops a future executor that trusts an agent's own
        "files I edited" list from committing a path that is absent, or that points
        clean outside the tree -- but it CANNOT see a write that went to the parent
        clone, because such a write never enters `files_written` at all.

        `drifted()` is the half that sees that. Call both before committing.

        Accepts absolute or workspace-relative paths; an absolute path outside the
        workspace counts as missing and is named as such.
        """
        ws = self.workspace().resolve()
        missing: list[str] = []
        for raw in paths:
            label = str(raw)
            candidate = Path(raw)
            abs_path = candidate if candidate.is_absolute() else (ws / candidate)
            resolved = abs_path.resolve(strict=False)
            if resolved != ws and not resolved.is_relative_to(ws):
                missing.append(f"{label} (outside workspace {ws})")
                continue
            if not resolved.exists():
                missing.append(label)
        return missing

    def drifted(self) -> list[str]:
        """Porcelain lines that are dirty in the PARENT clone now but were not when
        `checkout()` finished -- i.e. the spawned agent edited `self.path` instead
        of the worktree it was pointed at, and then returned a clean summary.

        This is the signal `verify_landed()` structurally cannot produce: a write
        outside the workspace leaves NO trace in `files_written`, so the only
        evidence is in the tree that received it.

        It cannot false-positive on a step that legitimately changes nothing. The
        answer is a set DIFFERENCE against a baseline taken at checkout, so:
          - a reviewer that finds no problems, a `when:`-skipped step, a `for_each`
            section that writes nothing -> parent clone unchanged -> `[]`;
          - a parent clone that was already dirty before the run (a developer's own
            work in progress) is in the baseline -> `[]`;
          - `worktree add`'s own effects are in the baseline -> `[]`.

        Returns `[]` -- "not observable", never "clean" -- before `checkout()` and
        under `isolation: inplace`, where the workspace IS the parent clone and no
        tree exists that a stray write could land in *instead*.
        """
        if self._parent_baseline is None:
            return []
        return sorted(self._parent_porcelain() - self._parent_baseline)

    def parent_clone_hint(self) -> str:
        ws = self._workspace if self._workspace is not None else self.path
        return (
            f'files may have landed in {self.path} instead of {ws}; '
            f'check `git -C "{self.path}" status`'
        )

    # ------------------------------------------------------------------ #
    # cleanup
    # ------------------------------------------------------------------ #

    def cleanup(self) -> None:
        if self.isolation != "worktree":
            return
        if self._workspace is None or self._workspace == self.path:
            return
        if self.keep_worktree:
            return
        try:
            run_git(["worktree", "remove", str(self._workspace), "--force"], cwd=self.path)
        except RepoError as exc:  # cleanup must not mask a real error
            logger.warning("git_local: failed to remove worktree %s: %s", self._workspace, exc)
        try:
            run_git(["worktree", "prune"], cwd=self.path)
        except RepoError as exc:
            logger.warning("git_local: failed to prune worktrees: %s", exc)
