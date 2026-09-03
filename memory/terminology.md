# Terminology

Domain language of `ticketbot`. One line each: term - meaning.

## The five nouns of a run

- **Work item** (`WorkItem`) - the provider-neutral unit of work every source produces and everything
  downstream consumes. `core/workitem.py`.
- **Run** (`Run`) - one attempt at one work item; owns `runs/<id>/` and `run.json`. `core/run.py`.
- **Profile** - one YAML file validated into `config/schema.py: Profile`; names every adapter.
- **Pipeline** - a YAML list of role steps (`PipelineDef`/`StepDef`), selected per work item.
- **Step** - one role executed once (or once per section under `for_each`), optionally committed.

## Swap points (adapter families)

- **Source** - where a work item comes from: `file`, `jira`.
- **Sink** - where results are reported: `file`, `jira`, `github_pr` (plus `also:` fan-out).
- **Model** / **ModelProvider** - raw completion vendor: `anthropic`, `openai_compat`, `fake`.
- **Executor** - HOW a step's work gets done: `process` (spawn a coding CLI) or `api` (own tool loop).
- **Runtime** - WHERE shell commands run and screenshots come from: `none`, `local_shell`, `solari`.
- **Repo** - the git host: `git_local`, `github`.

## Config vocabulary

- **`type:`** - the registry key that selects an adapter class. The only thing the schema requires.
- **Slot** - a named entry in `model.providers` (`main`, `cheap`, `peer`); a step's `model:` is a slot.
- **Kind** - a named entry in `executor.kinds` (`inline`, `claude-cli`); a step's `executor:` is a kind.
- **`extends:`** - parent profile reference; DEEP-merged under the child. Lists are replaced, not joined.
- **`builtin:`** - reference scheme resolving against the installed package's `ticketbot/builtin/`.
- **`${ENV}` ref** - the only legal way to name a secret; expanded by the adapter at use time.
- **Effective config** - what a run actually used, e.g. `_repo_cfg()` after `--repo` override.

## Engine vocabulary

- **Gate** - a human-in-the-loop decision point: `gates.on_unclear`, `gates.on_pr_ready`, `step.gate`.
- **`QUESTION:`** - marker line that makes a step blocked on a human decision. `engine/protocol.py`.
- **`DEFER:`** - marker line for non-blocking follow-up work; can spawn a `fixer` step.
- **Fan-out** (`for_each: plan.sections`) - one coder execution per `sections/section-N.md`.
- **Budget** - cost and wall-clock caps that stop a run; not billing.
- **Run lock** - `runs/.locks/<slug>-<digest>.lock`, keyed on the RAW item key; one work item to one run.
- **Banner** - the "what was USED" summary printed and written to `runs/<id>/banner.txt`.
- **Retire / `mark_processed`** - telling a source an item's run is terminal so polling moves on.
- **Claim** - a source assigning the item to the bot and transitioning it; `False` means lost race.

## Filesystem vocabulary

- **Workspace** - the repo checkout a step edits (a git worktree, for `git_local`).
- **Run dir / artifacts dir** - `runs/<id>/`; the second permitted root of the path jail.
- **Jail** - `executors/tools.py: jail()`, the single containment check for model-supplied paths.
- **`files_written`** - an `ExecResult` field holding WORKSPACE writes only; feeds `verify_landed()`.
- **`verify_landed()`** - repo check that a step's DECLARED writes exist under the workspace.
- **`drifted()`** - repo check that nothing appeared in the PARENT clone since checkout; the half
  `verify_landed()` cannot see, since a stray write never enters `files_written`.
- **Landing check** - the two together, run before every `commit:` step (`_landing_error`).

## Roles

`ingest`, `clarifier`, `planner`, `coder`, `tester`, `reviewer`, `security`, `reporter`, `fixer` —
each a prompt file under `ticketbot/builtin/prompts/roles/` ending in a `Return ONLY:` contract.
See [pipelines/role-prompts.md](pipelines/role-prompts.md).

## Non-terms

- **Solari is not a model.** It is exactly one `runtime` adapter (cloud browsers/sandboxes/desktops).
- **`"default"` is not a sentinel.** In a pipeline `defaults:` block it is just another slot/kind name
  to look up. Omitting the key is what triggers the profile-level fallback. See [practices.md](practices.md).
