# Pipelines: the step YAML

A pipeline is a YAML file loaded into `engine/pipeline.py: PipelineDef`. Everything structural is
validated at LOAD time — duplicate step ids, an unknown key, a missing `id`/`role`, an empty `steps`
list, an unsupported `for_each`, an unknown `gate`, an unparseable `when:` — and raised as
`ConfigError` naming the file and the step. Nothing surfaces as a `KeyError` mid-run.

```yaml
name: standard
defaults: {timeout_s: 1800}
steps:
  - {id: intake, role: ingest, model: cheap, tools: [source.read], produces: [workitem.json]}
  - id: clarify
    role: clarifier
    when: "workitem.acceptance is empty or workitem.ambiguity >= medium"
    tools: [sink.comment, sink.unassign]
    on_block: blocked
  - {id: plan, role: planner, tools: [fs.read, fs.write, fs.list], produces: [plan.md, sections/], gate: optional_human}
  - id: implement
    role: coder
    for_each: plan.sections
    tools: [fs.read, fs.write, fs.edit, fs.list, shell.run]
    isolation: worktree
    commit: "impl: {section.title}"
on_question: pause_and_relay     # pause_and_relay | fail | ignore
on_defer: spawn_fixer            # spawn_fixer | ignore
```

## `StepDef` keys

| Key | Meaning |
|---|---|
| `id` | unique within the pipeline; names `steps/<id>.md`, `logs/<id>.log`, the resume skip, `--pause-at` |
| `role` | selects `builtin:prompts/roles/<role>.md` unless `prompt:` overrides it |
| `model` | a model SLOT name (`profile.model.providers` key) |
| `executor` | an executor KIND name (`profile.executor.kinds` key) |
| `tools` | the per-step allowlist, enforced by the executor's `dispatch()` |
| `when` | string expression or mapping; see [predicates.md](predicates.md) |
| `for_each` | only `plan.sections` is supported |
| `produces` | artifact names; a missing one logs a warning, it does not fail the step |
| `gate` | `human` (always pause) or `optional_human` (pause only with `--pause-at <id>`) |
| `isolation` | recorded but advisory — the repo adapter decides how the workspace is isolated |
| `commit` | commit message template, rendered with the step's prompt values plus `{section.*}` |
| `on_block` | sink state to transition to when the step blocks |
| `timeout_s`, `prompt`, `optional`, `max_rounds` | per-step overrides; `optional: true` means a failure marks the step failed without failing the run |

## `defaults:` — the trap that broke every shipped profile

A pipeline's `defaults:` block holds only real fallback VALUES (a real slot name, a real kind name, a
timeout). It must **never** hold the literal string `"default"` for `executor:` or `model:`.

`"default"` is not a sentinel anywhere in the code — it is just another name to look up, and no
shipped profile names a slot or kind `"default"`, so every run died with
`unknown executor kind 'default'`. The actual "fall back to the profile's own default" behaviour is
triggered by **OMITTING the key**, so `step.model`/`step.executor` end up `None` and
`Orchestrator._provider(None)` / `_executor(None)` apply `profile.model.default` /
`profile.executor.default` themselves.

Resolution order, per step:

```
step.executor  ->  pipeline.defaults["executor"]  ->  profile.executor.default
step.model     ->  pipeline.defaults["model"]     ->  profile.model.default
step.timeout_s ->  pipeline.defaults["timeout_s"] ->  1800
```

All three built-in pipelines carry a comment on `defaults:` saying this. Read it before
reintroducing a key there.

## Fan-out: `for_each: plan.sections`

The planner writes `sections/section-1.md`, `section-2.md`, ... into the run dir. `_list_sections()`
globs them and sorts **numerically** (`section-10.md` after `section-9.md`, not before). The engine
then runs one execution of the step per section, each with `{section_file}`, `{section_title}`,
`{section_index}`, `{section_count}` bound, and commits each one separately. No sections at all is a
FAILED run (`"the planner produced no sections"`) — which is exactly how the path-jail defect
surfaced; see [../executors/path-jail.md](../executors/path-jail.md).

## The three built-ins (`ticketbot/builtin/pipelines/`)

| Pipeline | Shape |
|---|---|
| `standard.yaml` | eight steps: intake, clarify (conditional), plan (`optional_human`), implement (fan-out), verify, review (`model: peer`), security (conditional), publish |
| `small-bug.yaml` | drops `clarify` and `security`; `verify` absorbs review's job (it already reads the plan and the diff and is granted `fs.edit`) |
| `large-with-clarification.yaml` | `clarify` runs unconditionally, an extra read-only `research` step reads the codebase before planning, the plan gate is mandatory (`gate: human`), `review` and `security` always run |

Which one runs is decided per work item: [selection.md](selection.md).
