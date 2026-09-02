# The tool catalogue and its allowlist

`executors/tools.py` implements the tools the `api` executor exposes. `build_tools(names, ctx)`
returns `(tool_defs, dispatch)`.

## Catalogue

| Name | Handler | Notes |
|---|---|---|
| `fs.read` | `_fs_read` | optional `offset`/`limit` produce numbered lines; capped at 1 MB with a `…[truncated]` marker |
| `fs.list` | `_fs_list` | one entry per line, `name/` for a directory, max 500 entries |
| `fs.write` | `_fs_write` | creates parents; 5 MB cap; records the path in `ctx.files_written` |
| `fs.edit` | `_fs_edit` | exact substring replace; fails if `old` is absent or (without `replace_all`) not unique |
| `shell.run` | `_shell_run` | argv list only; see below |
| `runtime.screenshot` | `_runtime_screenshot` | writes `screenshots/tool-NN.png` under the artifacts dir; returns "no runtime configured" when there is no image |
| `source.read` | `_source_read` | returns `ctx.work_item_text` |

`sink.comment` / `sink.unassign` are known names the ORCHESTRATOR implements from the step's returned
text; `build_tools` skips them silently (no log line). An unknown name is skipped with a log line,
not an error.

Wire names: the Anthropic API only accepts `^[a-zA-Z0-9_-]{1,128}$`, so `fs.read` is advertised as
`fs_read` and mapped back by `from_wire()`. That mapping turns EVERY `_` into `.` — correct only
because no catalogue name has an underscore inside a segment. Keep it that way, or make the mapping
explicit.

## Enforcement lives in `dispatch()`

```python
def dispatch(wire, args):
    name = from_wire(wire)
    try:
        if name not in ctx.allow:          # the step's tools: list
            raise ToolError(...)
        ...
        return redact(handler(ctx, args)), False
    except Exception as exc:
        return redact(str(exc)), True      # NEVER propagates out of the loop
```

Two invariants: a name outside `ctx.allow` never reaches its handler, whether or not it was ever
advertised; and no exception escapes — a bad tool call becomes a `tool_result` with `is_error=True`
that the model can recover from. Both the result and the error text are redacted.

The allowlist is why the clarifier gets no filesystem tools and the reviewer/security roles never get
`shell.run` — **under the `api` executor, and only there.**

### Scope: the allowlist does not bind `process`

`ProcessExecutor` spawns a whole coding CLI, which brings its OWN tools and never reads
`ExecRequest.tools`. Under a profile whose steps run on a `process` kind — `jira-claude-solari.yaml`
and `github-codex.yaml` both default to one — every role, the clarifier included, gets a full CLI
driven by a prompt built from untrusted ticket text, and containment is whatever that CLI enforces
for itself. Do not read "the clarifier gets no filesystem tools" as a property of the system; it is a
property of `executors/tools.py`. Closing the gap means translating catalogue names into per-CLI
permission flags (`--allowedTools`, sandbox modes), which is CLI-specific — see
[../known-gaps.md](../known-gaps.md).

## `shell.run` and the runtime

```python
if ctx.runtime is not None and getattr(ctx.runtime, "can_exec", True):
    out = ctx.runtime.exec(list(argv), cwd=str(cwd), timeout=timeout_s)
else:
    subprocess.run(list(argv), cwd=str(cwd), timeout=timeout_s, capture_output=True, shell=False)
```

- `argv` must be a non-empty list of strings. Never a shell string, `shell=False` always.
- `cwd` is jailed to the workspace; the requested `timeout` is clamped to `ctx.shell_timeout_s`.
- Output is `exit=<code>\n<stdout>\n<stderr>`, truncated at 20 000 chars.
- **The capability check is `can_exec`, not `is not None`.** A `Runtime` is a real object even when
  it does nothing; see [../adapters/runtimes.md](../adapters/runtimes.md). `can_exec` is read
  duck-typed so this module knows nothing about runtime classes.

## `ToolContext`

`workspace`, `artifacts_dir`, `runtime`, `allow`, `max_read_bytes`, `max_write_bytes`,
`shell_timeout_s`, `files_written`, `log`, `work_item_text`.

`work_item_text` is filled by the engine from `ExecRequest.work_item_text`
(`Orchestrator._work_item_text` renders key, title, type, points, labels, url, description,
acceptance and comments as plain text). Executors never see a `WorkItem`, so without that field
`source.read` — the ONLY tool `intake` is granted in all three built-in pipelines — hands the ingest
role an empty string, which is exactly the defect that shipped once.

Path handling: [path-jail.md](path-jail.md).
