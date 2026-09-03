# Memory map

Index of every Memory file. Read this, [terminology.md](terminology.md) and [summary.md](summary.md)
at session start.

```
memory/
  summary.md          living snapshot of what ticketbot is + where to start
  terminology.md      domain vocabulary (term - meaning)
  practices.md        invariants, conventions, and the traps this codebase already hit
  known-gaps.md       what is deliberately unfinished or needs a decision
  memory-map.md       this index
  tmp/                git-ignored session scraps
  architecture/       the swap-point runtime and its contracts
  config/             profiles, inheritance, secrets
  pipelines/          step YAML, selection, predicates, role prompts
  engine/             the run loop, run dir, gates/locks/budget, reporting
  executors/          process vs api, the path jail, the tool catalogue
  models/             the ModelProvider layer
  adapters/           sources, sinks, repos, runtimes
  testing/            test conventions and the fakes
  cli/                commands, flags, exit codes
```

## Top level

| File | Topic |
|---|---|
| [summary.md](summary.md) | what the system is, in one paragraph, plus an orientation table |
| [terminology.md](terminology.md) | every domain term, including the two "non-terms" that mislead |
| [practices.md](practices.md) | the 12 invariants, the conventions, and a table of real traps |
| [known-gaps.md](known-gaps.md) | unwired comment templates, two open engineering items, smaller limits |

## architecture/

| File | Topic |
|---|---|
| [architecture/summary.md](architecture/summary.md) | swap points, package layout, dependency direction |
| [architecture/registry.md](architecture/registry.md) | the `type:`-string registry, kwarg filtering, how to add an adapter |
| [architecture/adapter-protocols.md](architecture/adapter-protocols.md) | all six protocols and their optional methods |

## config/

| File | Topic |
|---|---|
| [config/summary.md](config/summary.md) | the `Profile` schema, why it stays small, the shipped profiles |
| [config/profile-inheritance.md](config/profile-inheritance.md) | `extends:` deep-merge and the leak it caused twice; `builtin:` refs |
| [config/secrets-and-redaction.md](config/secrets-and-redaction.md) | `${ENV}`-only secrets, `.env` loading, the redactor, who scrubs what |

## pipelines/

| File | Topic |
|---|---|
| [pipelines/summary.md](pipelines/summary.md) | step YAML, `StepDef` keys, the `defaults:` trap, fan-out, the three built-ins |
| [pipelines/selection.md](pipelines/selection.md) | `pipeline_selector` rules and what they can see |
| [pipelines/predicates.md](pipelines/predicates.md) | the safe `when:` language and the evaluation context |
| [pipelines/role-prompts.md](pipelines/role-prompts.md) | prompt files, templating values, `Return ONLY:`, `QUESTION:`/`DEFER:` |

## engine/

| File | Topic |
|---|---|
| [engine/summary.md](engine/summary.md) | entry points, the step lifecycle, per-role hooks, escalation |
| [engine/run-store.md](engine/run-store.md) | `runs/<id>/`, `run.json`, resume, the banner |
| [engine/gates-locks-budget.md](engine/gates-locks-budget.md) | human gates, the run lock, cost and wall-clock caps |
| [engine/reporting.md](engine/reporting.md) | the PR-URL hand-off and who owns `ticket_comment.md` |

## executors/

| File | Topic |
|---|---|
| [executors/summary.md](executors/summary.md) | `process` vs `api`, and the `files_written` contract |
| [executors/path-jail.md](executors/path-jail.md) | `jail()`, the two permitted roots, and why the run dir is one |
| [executors/tools.md](executors/tools.md) | the tool catalogue, the allowlist, `shell.run` and the runtime |

## models/, adapters/, testing/, cli/

| File | Topic |
|---|---|
| [models/summary.md](models/summary.md) | neutral message types, the three providers, pricing |
| [adapters/summary.md](adapters/summary.md) | the four adapter directories and their shared rules |
| [adapters/sources.md](adapters/sources.md) | `file` and `jira` sources, claiming and retiring |
| [adapters/sinks.md](adapters/sinks.md) | `file`, `jira`, `github_pr`, `MultiSink`/`DryRunSink`, ADF |
| [adapters/repos.md](adapters/repos.md) | `git_local` worktrees, `github` clone/push/PR, never merging |
| [adapters/runtimes.md](adapters/runtimes.md) | `can_exec`, `none`, `local_shell`, the Solari lifecycle rules |
| [testing/summary.md](testing/summary.md) | the hard rules, the fakes, what the seams file owns |
| [cli/summary.md](cli/summary.md) | commands, flag semantics, exit codes, the smoke sequence |

## Keeping this accurate

- The Memory describes the CURRENT state. No changelogs, no dates, no "previously".
- Code is the source of truth. If the Memory contradicts it, summarize the disparity and propose the
  fix rather than trusting the Memory.
- After a change that alters behaviour or structure, update the matching file here before moving on.
  A new adapter touches [architecture/registry.md](architecture/registry.md), its family's file under
  `adapters/`, the README's swap-point and env tables, and this index.
