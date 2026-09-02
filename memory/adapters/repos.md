# Repos

The git host. `git_local` works in a worktree of a local clone; `github` adds clone, push and PR.

## `run_git` — the single git choke point

`adapters/repos/base.py: run_git(args, *, cwd, timeout=120, check=True)` is
`subprocess.run(["git", *args], shell=False)` — never a composed command string, because branch
names, commit messages and PR bodies all trace back to untrusted ticket text or model output. On a
non-zero exit with `check=True` it raises `RepoError` naming the argv and the exit code, with
`redact(stderr)`, and never the child environment.

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
- `verify_landed(paths)` returns the subset that does NOT exist under the workspace, naming anything
  that resolves outside it. A spawned coding CLI does not inherit our working directory and may
  write to the parent clone instead of the worktree — a clean agent summary is not proof the edit
  landed in the right tree. `parent_clone_hint()` produces the "check git status in the parent
  clone" message the engine appends to that failure. What this check can actually catch today is a
  recorded open question: [../known-gaps.md](../known-gaps.md).

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
