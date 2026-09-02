# The run directory, `run.json`, resume and the banner

## `runs/<id>/`

Run id = `%Y-%m-%d-%H%M` + `-` + `slugify(item.key)` + `-` + 4 hex chars, e.g.
`2026-09-01-1443-eng-1842-a3f9`. `RunStore.list_ids()` is therefore chronological.

```
runs/2026-09-01-1443-add-a-health-endpoint-a3f9/
  banner.txt   config.resolved.yaml   run.json      workitem.json
  plan.md      sections/section-1.md  patch.diff    test-report.md
  review.md    security.md            pr.md         ticket_comment.md
  question.md  result.md              dryrun.log
  steps/<step-id>.md   logs/<step-id>.log   screenshots/   attachments/
```

Who writes what: the engine writes `banner.txt`, `config.resolved.yaml`, `workitem.json`,
`run.json`, `patch.diff`, `question.md`, `steps/*.md`, `screenshots/<step>-NN.png` and the canonical
`ticket_comment.md`. Role steps write `plan.md`, `sections/`, `test-report.md`, `review.md`,
`security.md`, `pr.md` through the jailed fs tools. `FileSink` adds `result.md` (one line per sink
call) and `attachments/` (screenshot copies, exactly as a Jira sink would have uploaded them).
`--dry-run` adds `dryrun.log`. `screenshots/tool-NN.png` comes from the `runtime.screenshot` tool;
`screenshots/<step>-NN.png` from `runtime.screenshot_on`.

## `RunStore` — `core/run.py`

```python
store.new_run(profile_name=..., item=...)   # pure: no directory, no write
store.dir(run_id)                           # mkdir -p
store.save(run)                             # run.json.tmp -> os.replace (atomic, Windows too)
store.load(run_id) / list_ids() / latest()
store.write_artifact(run, relpath, data)    # str is scrubbed; bytes verbatim
store.read_artifact(run, relpath)
store.append_log(run, step_id, text)
```

`new_run()` being side-effect-free is deliberate: it is called BEFORE the lock is acquired, purely to
obtain the run id the lock file records, while every disk write happens strictly after.
`_jailed_relpath()` rejects absolute paths and any `..` segment. Scrubbing goes through
`default_redactor()` — see [../config/secrets-and-redaction.md](../config/secrets-and-redaction.md).

`run.json` is rewritten atomically after every step (and every fan-out iteration), so a crash never
leaves it half-written.

## `Run` and `StepResult`

`Run`: `id`, `profile_name`, `work_item_key`, `external_id`, `status`, `created_at`, `updated_at`,
`pipeline_ref`, `pipeline_reason`, `banner`, `cost_usd`, `steps` (insertion-ordered by step id),
`extra`. `extra` is the free-form dict everything else keys off: `branch`, `workspace`, `pr_url`,
`section_count`, `plan_security`, `diff_files`, `diff_touches_security`, `clarify_rounds`,
`screenshots`, `error`.

`StepResult`: `id`, `role`, `status`, `started_at`, `ended_at`, `duration_s`, `cost_usd`, `text`,
`artifacts` (run-dir-relative), `commits`, `question`, `defers`, `error`. Both round-trip through
`to_dict()`/`from_dict()`.

## Resume

```bash
ticketbot resume <run-id> [-c profile] [--runs-dir dir] [--force-lock]
```

`Orchestrator.resume()` loads `run.json`, re-fetches the item by `external_id` (or reloads
`workitem.json` when there is none), re-acquires the lock, reloads the pipeline from
`run.pipeline_ref`, and checks out `run.extra["branch"]` — a run with no recorded branch raises
`ConfigError`. `_run_step` then skips every step already OK or SKIPPED and continues from the first
one that is not. `tests/test_e2e_offline.py` proves the skip is real.

## The banner

`core/banner.py` renders exactly this, omitting any line whose fact is empty:

```
Using source=file "Add a /health endpoint" (Task)
pipeline=builtin:pipelines/standard.yaml  (rule: default)
models=ingest:Claude Haiku 4.5 (claude-haiku-4-5) effort=low · planner:Claude Opus 5 ...
executor=api: Claude Opus 5 (claude-opus-5) effort=xhigh
runtime=none
repo=. @ agent/add-a-health-endpoint-add-a-health-endpoint
```

**The banner reports what was USED.** `Orchestrator._banner_facts()` builds every fact from live
objects: one entry per distinct ROLE in the selected pipeline, each naming the `ModelProvider` object
that role actually resolved to (`provider.describe()`, including effort); the executor line is the
executor OBJECT's `describe()`, not the config's slot name; the repo line comes from `_repo_cfg()`,
which honours `--repo`, so an override is what gets reported. Model entries join with ` · ` (U+00B7).
A provider that fails to resolve is skipped, and a failure to describe the executor is logged — the
banner must never crash a run.

`ticketbot config banner <profile>` prints the config-only variant via `facts_from_profile()`: every
model SLOT rather than the ones a pipeline uses, and no ticket line, because no work item exists.

Both are printed and written through `redact()`.
