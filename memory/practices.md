# Practices and invariants

Project-wide rules. Each links to the file that holds the detail. The traps at the bottom are real
defects this codebase hit; they are the reason this Memory exists.

## Invariants that must not be broken

1. **Adapters are selected by a `type:` string, never by an `if`.** Adding one means a new module,
   one `register()` line, and its own option validation — never a change to `config/schema.py`.
   Every adapter also implements `describe()`. See [architecture/registry.md](architecture/registry.md).
2. **The engine never imports a vendor SDK.** `anthropic` is imported only inside
   `ticketbot/models/anthropic.py` (lazily, inside functions); `httpx` only inside adapters and
   `models/openai_compat.py`. The one deliberate direct import in the orchestrator is `FileSource`,
   so `--input-text`/`--input` can override the configured source.
3. **Secrets are `${ENV}` references only.** Expanded at use time by the adapter that needs them,
   `register_secret()`'d immediately, never written to `config.resolved.yaml` or a log — and never
   posted outward either: the Jira comment, the GitHub PR comment, the PR title/body and the commit
   message are all `redact()`ed, because a ticket is readable by whoever filed it.
   See [config/secrets-and-redaction.md](config/secrets-and-redaction.md).
4. **`shell=False`, always, with an argv list and an env allowlist.** Never a composed shell string,
   never `os.environ` passed through wholesale. Applies to `ProcessExecutor`, `LocalShellRuntime`,
   `run_git`, `gh`, and `shell.run`.
5. **Never `eval`.** `when:` is parsed by `core/predicate.py`; profile and pipeline YAML is always
   `yaml.safe_load`. See [pipelines/predicates.md](pipelines/predicates.md).
6. **Every filesystem tool goes through `_jailed()` / `jail()`.** Two orchestrator-owned roots and
   nothing else. See [executors/path-jail.md](executors/path-jail.md).
7. **Nothing auto-merges.** No adapter has a merge call. `gates.on_pr_ready: auto` means "do not
   pause", never "merge". See [engine/gates-locks-budget.md](engine/gates-locks-budget.md).
8. **The banner reports what was USED, not what was configured.** Build its facts from live adapter
   objects and the effective config (`_repo_cfg()`, which honours `--repo`), never from
   `profile.<block>`. See [engine/run-store.md](engine/run-store.md).
9. **The engine closes what it opens.** `_run_pipeline` closes the sink it built (the real one, not
   the `--dry-run` wrapper, which deliberately never touches its inner sink); `run_once`/`resume`/
   `poll` each close the source they opened; `poll` also calls `mark_processed(item)`.
10. **Model-written text never reaches a command line inline.** Commit messages go through
    `git commit -F <file>`; PR bodies and PR comments through `gh --body-file`.
11. **`str.format` is banned for prompts.** Prompts carry literal braces (JSON, code fences);
    `core/templating.py: render()` is brace-safe and leaves unknown placeholders untouched.
12. **Keep source files under 350 lines** and decompose past that, unless the content is genuinely
    indivisible. `engine/orchestrator.py` (1144 lines) is the standing exception and the obvious
    candidate for a split.

## Conventions

- Optional capabilities are `getattr`-probed, never added to a protocol every adapter must implement
  in full: `set_pr_url`, `mark_processed`, `close`, `parent_clone_hint`, `can_exec`, `branch_name`.
- `MultiSink` calls the primary first and lets it raise; secondaries are caught, logged and never
  allowed to lose the primary's result.
- Docs describe the CURRENT state, never a changelog — `README.md` and this Memory alike.
- Tests are pytest, run with `uv run pytest` from the repo root.

## Traps that already bit this codebase

| Trap | Rule | Detail |
|---|---|---|
| A pipeline `defaults:` block holding the literal string `"default"` for `executor:`/`model:` broke every shipped profile | OMIT the key; `None` is what triggers the profile-level fallback | [pipelines/summary.md](pipelines/summary.md) |
| `extends:` deep-merges, so a key in `_base.yaml` leaks into children that meant to opt out — hit twice, on the `peer` model slot and on `repo.path` | A parent holds only what EVERY child wants; a child that means to replace a block must SAY so; assert on the LOADED profile, never the file text | [config/profile-inheritance.md](config/profile-inheritance.md) |
| A workspace-only path jail refused every artifact the role prompts write, leaving `plan.sections` empty and failing the run | The jail admits the workspace AND the run dir; `files_written` still reports workspace writes only | [executors/path-jail.md](executors/path-jail.md) |
| `NoneRuntime` is an object, so `runtime is not None` was never False and `shell.run` failed on every call under three of four shipped profiles | Ask `getattr(runtime, "can_exec", True)` and do the work locally when it is False | [adapters/runtimes.md](adapters/runtimes.md) |
| A fake that satisfied a contract the real adapter did not hid four broken seams; the suite was green at the time | Drive the REAL shipped profiles and the real adapters at the seams; keep fakes honest to the real contract | [testing/summary.md](testing/summary.md) |
| `RunStore` scrubbed with a private, empty `Redactor`, so registered secrets reached run artifacts | Anything that scrubs without calling `redact()` must take `default_redactor()` | [config/secrets-and-redaction.md](config/secrets-and-redaction.md) |
| The orchestrator and `FileSink` both owned `ticket_comment.md`, so the headline artifact held the comment twice | The engine writes the canonical artifact AFTER the sink call, in a `finally` | [engine/reporting.md](engine/reporting.md) |
| `source.read` returned `""` because nothing filled `ToolContext.work_item_text` | The engine fills `ExecRequest.work_item_text`; executors never see a `WorkItem` | [executors/tools.md](executors/tools.md) |
| Every LOCAL write was scrubbed while the identical text went to Jira/GitHub/git history in the clear, making the world-readable copy the leakiest one | Redaction is a property of the BOUNDARY, not of the filesystem: scrub at every point text leaves the machine | [config/secrets-and-redaction.md](config/secrets-and-redaction.md) |
| A security gate read from model output defaulted to "off" when the marker was absent, so *omitting* a line disabled the security step more cheaply than lying in one | A gate fed by model output fails CLOSED: absent means unknown, and unknown runs the check | [pipelines/predicates.md](pipelines/predicates.md) |
