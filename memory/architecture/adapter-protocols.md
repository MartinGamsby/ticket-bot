# Adapter protocols

Every family is a `@runtime_checkable` `Protocol`. A registered adapter must implement its family's
protocol IN FULL; anything optional is `getattr`-probed by the caller instead of being added to the
protocol. `tests/test_seams.py` asserts both halves of that rule.

## Source — `adapters/sources/base.py`

```python
class Source(Protocol):
    def describe(self) -> str: ...
    def fetch(self, external_id: str | None = None) -> WorkItem: ...
    def poll(self) -> Iterator[WorkItem]: ...
    def claim(self, item: WorkItem) -> bool: ...   # False = someone else got it
    def close(self) -> None: ...
```

Optional: `mark_processed(item)` — retire an item whose run reached a terminal state. `FileSource`
implements it (moves the file into `processed_dir`); `JiraSource` deliberately does not, because
`claim()` already transitioned the issue out of the polled JQL.
Errors: `SourceError`, `WorkItemNotFound`.

## Sink — `adapters/sinks/base.py`

```python
class Sink(Protocol):
    def describe(self) -> str: ...
    def comment(self, item, markdown: str, attachments: Sequence[Attachment] = ()) -> None: ...
    def transition(self, item, state: str) -> None: ...
    def unassign(self, item) -> None: ...
    def link(self, item, url: str, title: str) -> None: ...
    def close(self) -> None: ...
```

Optional: `set_pr_url(url)` — only `GithubPrSink` wants it. `MultiSink` fans it out by `getattr`
probe and never lets a failure there propagate; see [../engine/reporting.md](../engine/reporting.md).
`MultiSink` (primary + `also:`) and `DryRunSink` (records intended calls, never touches its inner
sink) are wrappers, not registered kinds. Error: `SinkError`.

## Repo — `adapters/repos/base.py`

```python
class Repo(Protocol):
    def describe(self) -> str: ...
    def checkout(self, branch: str) -> Path: ...   # absolute workspace path
    def workspace(self) -> Path: ...               # raises before checkout()
    def status(self) -> list[str]: ...
    def diff(self, base: str | None = None) -> str: ...
    def commit(self, message: str, body: str = "") -> CommitResult: ...
    def push(self) -> None: ...                    # no-op for git_local
    def open_pr(self, title: str, body: str) -> str | None: ...  # None for git_local
    def cleanup(self) -> None: ...
    def verify_landed(self, paths: Sequence[Path | str]) -> list[str]: ...
```

Optional: `branch_name(item)`, `parent_clone_hint()`. `CommitResult.sha is None` means nothing was
staged — expected, not an error. **No merge call exists in any repo adapter**, asserted by a
source-level test. Error: `RepoError`. `run_git()` is the single git choke point.

## Runtime — `adapters/runtimes/base.py`

```python
class Runtime(Protocol):
    def describe(self) -> str: ...
    def start(self) -> None: ...                   # idempotent
    def exec(self, argv, *, cwd=None, timeout=None, env=None) -> ExecOut: ...
    def read_file(self, path: str) -> bytes: ...
    def write_file(self, path: str, data: bytes) -> None: ...
    def screenshot(self) -> bytes | None: ...      # PNG bytes, or None
    def preview_url(self, port: int) -> str | None: ...
    def stop(self) -> None: ...                    # idempotent, safe without start()
```

Capability flag `can_exec` (default True on `BaseRuntime`): False means `exec()` raises
`RuntimeUnavailable` and the caller must do the work itself. **A runtime is always a real object —
never gate on `runtime is not None`.** See [../adapters/runtimes.md](../adapters/runtimes.md).
Errors: `RuntimeAdapterError`, `RuntimeUnavailable`.

## ModelProvider — `models/base.py`

```python
class ModelProvider(Protocol):
    provider_id: str                               # "anthropic" | "openai_compat" | "fake"
    def describe(self) -> str: ...                 # "Claude Opus 5 (claude-opus-5) effort=xhigh"
    def complete(self, *, system, messages, tools=None, max_tokens=None) -> ProviderMessage: ...
```

Provider-neutral message types (`Msg`, `TextBlock`, `ToolUseBlock`, `ToolResultBlock`, `ToolDef`,
`ToolCall`, `Usage`) live in the same module; `Msg.native`/`native_provider` is the escape hatch that
lets a provider replay its own blocks verbatim. Errors: `ProviderError`, `ProviderRefusal`.
See [../models/summary.md](../models/summary.md).

## Executor — `executors/base.py`

```python
class Executor(Protocol):
    def describe(self) -> str: ...
    def run(self, req: ExecRequest) -> ExecResult: ...
```

`ExecRequest` carries `system`, `prompt`, `workspace`, `artifacts_dir`, `tools` (the allowlist),
`timeout_s`, `max_cost_usd`, `env`, `step_id`, `log_path`, `model` and `work_item_text`.
`ExecResult` carries `text`, `usage`, `files_written` (workspace only), `question`, `defers`,
`exit_code`, `error`, `timed_out`. Error: `ExecutorError` (a config/environment problem that stops a
step from even being attempted). See [../executors/summary.md](../executors/summary.md).
