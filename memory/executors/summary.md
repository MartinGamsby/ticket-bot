# Executors: `process` vs `api`

An executor is HOW a step's work gets done. Both kinds are driven through the same
`ExecRequest` -> `ExecResult` shape (`executors/base.py`), so a profile swaps between them with one
config edit and the engine notices nothing.

| | `process` | `api` |
|---|---|---|
| What it does | spawns a coding CLI (`claude -p`, `codex exec`, `aider`) | runs our own tool loop over a `ModelProvider` |
| Model | whatever the CLI brings | `provider=` resolved by the orchestrator from the step's slot |
| Tools | the CLI's own | `executors/tools.py`, path-jailed, per-step allowlist |
| Usage/cost | not observable — always `Usage()` | real token counts and `estimate_cost` |
| `files_written` | `diff_snapshots(workspace)` | `diff_snapshots(workspace)` plus workspace-only tool writes |
| `describe()` | `process: claude -p` | `api: Claude Opus 5 (claude-opus-5) effort=xhigh` |

## `ProcessExecutor` — `executors/process.py`

Config: `cmd` (a non-empty LIST of strings — never a shell string, never `shlex.split`), `prompt`
(`stdin` default | `arg` | `file`), `timeout_s`, `cwd` (`workspace` default | `artifacts`), `env`,
`env_passthrough`, `args_template`, `prompt_file_name`, `encoding`.

Non-negotiable and tested:

- `shell=False` always; the executable is resolved with `shutil.which` so a relative name cannot be
  hijacked by cwd and a bare `claude` finds `claude.cmd` on Windows;
- the child environment is an explicit allowlist (`DEFAULT_PASSTHROUGH` + the profile's
  `env_passthrough` + expanded `env:` values, each `register_secret()`'d) — never `os.environ`
  wholesale;
- the prompt goes via stdin by default (Windows caps a command line near 32 KB);
- a timeout kills the whole process TREE: on Windows `taskkill /F /T /PID` runs FIRST, while the
  parent is still alive, because once the parent is dead Windows no longer relates the children to
  it and killing first orphans the subtree.

`ExecutorError` is raised (not returned) when the step cannot even be attempted: a bad `cmd`, an
executable not on PATH, a missing cwd.

## `ApiLoopExecutor` — `executors/api_loop.py`

Config: `model` (slot name), `max_iterations` (40), `max_tokens` (32000), `shell_timeout_s` (600).

The loop is intentionally dumb: send `system` + the running `messages` to the provider; if it asked
for tools, dispatch every call and feed ALL of that turn's results back in ONE `user` message, as the
Anthropic tool-use protocol and its OpenAI equivalent expect. It stops on the first turn with no tool
calls, a wall-clock deadline, a cost cap, a `ProviderError`, or `max_iterations` — whichever comes
first. Each of those returns an `ExecResult` with `error` set rather than raising.

Assistant turns are appended with `native=pm.native` and `native_provider=self.provider.provider_id`
so a provider can replay its own blocks verbatim (Anthropic thinking blocks must be echoed back
unchanged on the same model).

## `files_written` — a contract, not a convenience

`ExecResult.files_written` is **WORKSPACE writes only**. The orchestrator hands the list straight to
`repo.verify_landed()`, which reports anything outside the workspace as MISSING and fails the run
with "declared files were not found under the workspace".

`ToolContext.files_written` legitimately collects run-dir paths too — `runtime.screenshot` drops a
PNG under the artifacts dir, and the fs tools can write `{plan_file}` / `{run_dir}/pr.md` there — so
`ApiLoopExecutor._finish` filters to workspace-relative entries before building the result. Without
that filter, a `verify` step that takes one screenshot kills its own run, because `verify` has both
`runtime.screenshot` and a `commit:`.

`tests/fakes.py: FakeExecutor` draws the same line (it returns workspace writes and deliberately
excludes `artifact_writes`) — keep it that way; that fake IS the contract.

## Shared helpers — `executors/base.py`

`snapshot_tree(root)` / `diff_snapshots(before, after)` detect changed files by (mtime_ns, size),
skipping `.git`, `.venv`, `node_modules`, `__pycache__`, `runs`, `dist`, `build` and the tool caches,
never following symlinked directories, and stopping after 20 000 files with a warning.
`finish_result(text, **kw)` fills `question`/`defers` from `engine/protocol.py`. `append_log(path,
text)` never scrubs — callers redact first.

See also: [tools.md](tools.md), [path-jail.md](path-jail.md).
