# Repos

The git host. `git_local` works in a worktree of a local clone; `github` adds clone, push and PR.

## Interrupted runs recover themselves

`checkout()` runs `git worktree prune` first, then looks for an existing worktree
holding the branch (`_worktree_for_branch()`, parsing `git worktree list --porcelain`
-- parsed, not eyeballed, because the human format aligns columns with spaces that a
Windows path contains). If one is there and its directory still exists, it is REUSED;
if the record is stale the prune already removed it and a fresh worktree is created.

This is what makes a Ctrl+C'd run recoverable: the branch stays checked out in an
abandoned worktree, and without the prune-and-reuse the next run dies with
`fatal: '<branch>' is already checked out at ...`. The main clone is explicitly
excluded from reuse, so the default-branch guard still fires.

`branch_name()` blanks `{ticket_key}` when the slugified key equals the slug, which
is always true for a file/text item (its `key` falls back to `id`, which IS the
slug). Without it the default `agent/{ticket_key}-{slug}` template renders the same
words twice. `_sanitize_branch()` then collapses the separator run the empty slot
leaves behind.


## `run_git` — the single git choke point

`adapters/repos/base.py: run_git(args, *, cwd, timeout=120, check=True)` is
`subprocess.run(["git", *args], shell=False)` — never a composed command string, because branch
names, commit messages and PR bodies all trace back to untrusted ticket text or model output. On a
non-zero exit with `check=True` it raises `RepoError` naming the argv and the exit code, with
`redact(stderr)`, and never the child environment.

Outbound text is scrubbed as well as errors: `GitLocalRepo._compose_message` redacts the whole commit
message (`push()` publishes it, and branch history cannot be un-published) and `GithubRepo.open_pr`
redacts the PR title and body. See
[../config/secrets-and-redaction.md](../config/secrets-and-redaction.md).

## `GitLocalRepo` — `adapters/repos/git_local.py`

Options: `path` (default `"."`, resolved against `base_dir`), `base_branch`, `branch_template`
(default `agent/{ticket_key}-{slug}`), `isolation` (`worktree` default, or `inplace`),
`worktrees_dir` (default a `.ticketbot-worktrees` directory beside the clone), `keep_worktree`,
`git_user_name` / `git_user_email`, `allow_default_branch`, `coauthor_trailer`.

- `branch_name(item)` is a **security control, not cosmetics**: it is the only place an untrusted
  ticket title or key becomes a git ref and eventually an argv element. `_sanitize_branch`
  lowercases, turns whitespace into a hyphen, strips the git-forbidden characters, collapses
  double-dot and at-brace sequences, collapses repeated slashes, strips leading and trailing slash
  or hyphen, caps at 100 chars, falls back to `task`, and hard-stops if the result could still start
  with a hyphen — so a title that looks like a command-line flag can never become one.
- `checkout()` refuses `main` / `master` / `dev` / `develop` / `trunk` unless `allow_default_branch`,
  adds a worktree named `<branch>-<4 hex>` under `worktrees_dir`, and sets `user.name` /
  `user.email` LOCALLY in that worktree so commits never depend on the developer's global git
  config. It is idempotent for the same branch and records the start sha for `diff()`.
- `diff()` is `<base>...HEAD` plus uncommitted changes, truncated at 400 000 chars. The base is the
  merge-base with `base_branch` when set, else the recorded start sha.
- `commit()` stages `-A`; nothing staged returns `CommitResult(sha=None)` (expected, not an error);
  otherwise the message is written to a UTF-8, LF-newline temp file and committed with `-F <file>`,
  never inline, with a co-author trailer appended when `coauthor_trailer` is on.
- `cleanup()` removes and prunes the worktree unless `keep_worktree` or `isolation: inplace`;
  failures are logged, never raised over a real error.
### The landing check — two methods, because neither can answer the other

A clean agent summary is not proof the edit landed in the right tree. Every `commit:` step runs both
of these (`engine/orchestrator._landing_error`) before it commits; either one non-empty fails the
step, with `parent_clone_hint()`'s "check `git status` in the parent clone" appended.

- `verify_landed(paths)` — the subset of the DECLARED paths that do not exist under the workspace,
  naming anything that resolves outside it. Today every caller feeds it `ExecResult.files_written`,
  which both executors derive from a snapshot of the workspace, so those paths are inside by
  construction and it returns `[]` every time. It is a guard on a future executor that trusts an
  agent's own "files I edited" list — it can never see a write that went somewhere else, because
  such a write never enters `files_written` at all.
- `drifted()` — porcelain lines dirty in the PARENT clone now but not when `checkout()` finished.
  This is the one that catches a spawned CLI ignoring its cwd. It is a set difference against a
  baseline taken after `worktree add`, so it cannot false-positive: a pre-existing dirty parent
  clone, `worktree add`'s own effects, and a step that legitimately writes nothing (a reviewer with
  no findings, a `when:`-skipped step) all report `[]`. The runs dir is excluded by pathspec when it
  sits inside the clone (`runs/` at the project root is normal), so the engine's own artifacts are
  never read as drift. Returns `[]` — "not observable" — before `checkout()` and under
  `isolation: inplace`, where the workspace IS the parent clone.

```python
# git_local.py
def drifted(self) -> list[str]:
    if self._parent_baseline is None:      # inplace, or before checkout
        return []
    return sorted(self._parent_porcelain() - self._parent_baseline)
```

`_parent_porcelain()` never raises (unlike `status()`): a drift probe that could throw would turn a
diagnostic into a new way to fail a run.

## `GithubRepo` — `adapters/repos/github.py`

Subclasses `GitLocalRepo`. Extra options: `clone` (required; an SSH or https URL), `remote`
(`origin`), `token`, `api_url`, `prefer_gh` (true), `draft_pr` (true).

- With no explicit `path`, it installs a per-repo clone cache at `<base_dir>/<owner>-<repo>`.
  **This is why `_base.yaml` must not carry `repo.path`** — an inherited `"."` makes it fetch and
  `git worktree add` inside the profile's own directory. See
  [../config/profile-inheritance.md](../config/profile-inheritance.md).
- `ensure_clone()` clones when the directory is empty, else fetches with `--prune`. **Never
  `git pull`** — no merge surprises in a repo whose state we do not own.
- `push()` is `git push -u <remote> <branch>`; an auth-looking failure is re-raised with a hint that
  the token is never embedded in the remote URL (use a credential helper or an SSH agent).
- `open_pr()` uses `gh pr create --body-file ...` (plus `--draft`) when `gh` is on PATH, else REST;
  an "already exists" failure looks the existing PR up instead of erroring. Returns the PR URL.
- **`open_pr` never merges.** No merge subcommand, no PUT to a merge endpoint, no auto-merge flag —
  verified by a source-level test.
- `cleanup()` also closes the `httpx.Client` it may have created.
