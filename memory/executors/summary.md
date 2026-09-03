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

## `stub` - the genuinely offline executor

`executors/stub.py`, registered as `stub`, used by `profiles/file-stub-offline.yaml`.
Calls nothing: no model, no network, no subprocess. Each step reports that it did
nothing, echoes the prompt it received, and writes placeholder content for every
entry in `ExecRequest.produces`.

`produces` is on `ExecRequest` FOR this executor -- `process` and `api` ignore it,
because their agent is told what to write in the role prompt. The stub needs it to
satisfy the engine's contracts from config rather than by hardcoding step ids.

Three traps, each found by running the pipeline rather than reasoning about it, each
pinned by a test in `tests/test_executor_stub.py`:

- **Never build the result with `finish_result()`.** It parses `QUESTION:`/`DEFER:`
  out of the text, and the echoed prompt CONTAINS both markers - every role prompt
  carries the "end your turn with QUESTION:" protocol. Doing so made the stub raise
  a question it never asked and block the run. It returns `question=None, defers=[]`.
- **`files_written` must stay empty.** It means WORKSPACE files, and the landing
  check fails a step whose declared paths sit outside the workspace; the stub writes
  only run-dir artifacts. Reporting them made `verify` kill its own run - the same
  failure a captured screenshot once caused.
- **The intake JSON fence must precede the prompt echo.** `_extract_json_block()`
  takes the FIRST fenced block, and role prompts contain fenced examples of their own.

It also writes `Security: no` into `plan.md` (the gate fails closed, so silence
would RUN the security step) and fills `acceptance` in the intake JSON with
`ambiguity: low`, so `clarify` is skipped and a stub run takes the full happy path.

A successful stub run ends `blocked` / exit 3 at `gates.on_pr_ready: human_review`.
That is the designed terminal state, not a failure.

## `ProcessExecutor` — `executors/process.py`

Config: `cmd` (a non-empty LIST of strings — never a shell string, never `shlex.split`), `prompt`
(`stdin` default | `arg` | `file`), `timeout_s`, `cwd` (`workspace` default | `artifacts`), `env`,
`env_passthrough`, `args_template`, `prompt_file_name`, `encoding`.

Non-negotiable and tested:

- `shell=False` always; the executable is resolved with `shutil.which` so a relative name cannot be
  hijacked by cwd and a bare `claude` finds `claude.cmd` on Windows;
- the child environment is an explicit allowlist (`DEFAULT_PASSTHROUGH` + the profile's
  `env_passthrough` + expanded `env:` values) — never `os.environ` wholesale; see
  **the credential contract** below;
- the prompt goes via stdin by default (Windows caps a command line near 32 KB);
- a timeout kills the whole process TREE: on Windows `taskkill /F /T /PID` runs FIRST, while the
  parent is still alive, because once the parent is dead Windows no longer relates the children to
  it and killing first orphans the subtree.

`ExecutorError` is raised (not returned) when the step cannot even be attempted: a bad `cmd`, an
executable not on PATH, a missing cwd.

### The credential contract: the spawned CLI authenticates itself

`claude -p` and `codex exec` sign in from their OWN credential store (an OAuth profile under the
user's home, or the OS keyring) — not from an API key we hand them. `DEFAULT_PASSTHROUGH` therefore
carries the non-secret LOCATORS that store needs to be findable, and no credential of its own:

| Platform | Names | Why |
|---|---|---|
| Windows | `USERPROFILE`, `APPDATA`, `LOCALAPPDATA` | `%USERPROFILE%\.claude`, roaming config, DPAPI-backed credential files |
| POSIX | `HOME`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME` | `~/.claude`, `~/.codex`, relocated roots |
| Linux | `XDG_RUNTIME_DIR` + `DBUS_SESSION_BUS_ADDRESS` | without BOTH, a Secret Service keyring cannot be reached at all |
| both | `PATH`, `PATHEXT`, `COMSPEC`, `SYSTEMROOT`, `WINDIR`, `PROGRAMDATA`, `TEMP`/`TMP`/`TMPDIR`, `LANG`, `LC_ALL` | start at all, write scratch files, decode output |

**No API key is ever in that list.** A profile that needs one (headless, CI, no interactive login)
names it in that executor kind's own `env_passthrough:` — `jira-claude-solari.yaml` forwards
`ANTHROPIC_API_KEY` + `CLAUDE_CONFIG_DIR`, `github-codex.yaml` forwards `OPENAI_API_KEY` +
`CODEX_HOME`. That keeps "this credential goes into this subprocess" a visible, per-profile decision.

Two rules that make `env_passthrough:` the right spelling, not `env:`:

- an `env_passthrough` name that is **not set** is skipped, never an error — which is what lets ONE
  profile serve both an OAuth developer machine and a keyed CI runner. An `env:` `${ENV}` ref is
  expanded strictly, so `env: {ANTHROPIC_API_KEY: "${ANTHROPIC_API_KEY}"}` would FAIL the run on
  every machine that signs in by OAuth.
- a forwarded name matching `_SECRET_NAME_RE` (`*_KEY`, `*_TOKEN`, `*_SECRET`, `*_PASSWORD`,
  `*_CREDENTIAL(S)`) is `register_secret()`'d, so the child's own stdout/stderr cannot echo it into
  `runs/<id>/logs/`. Matched on the NAME, not the value — a path forwarded as `CLAUDE_CONFIG_DIR`
  must not become a redaction pattern applied to every log line in the process.

`adapters/runtimes/local_shell.py` keeps its own, shorter `DEFAULT_PASSTHROUGH`: it runs the
project's own `shell.run` commands, not a CLI that has to find credentials.

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
with "declared files were not found under the workspace". Because that list is derived from a
snapshot of the workspace, it can never name a write that went elsewhere — `repo.drifted()` is
the check that covers that. See [../adapters/repos.md](../adapters/repos.md).

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
