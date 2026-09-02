# ticketbot

A config-driven ticket → PR agent runtime. A unit of work — a Jira ticket, a file,
or just a line of text — goes in. A YAML-defined pipeline of AI agent roles (plan,
clarify, code, test, review, security, report) runs over a git repo. A pull
request, a short ticket comment, and screenshots come out.

Every external system — where the ticket comes from, where results are reported,
which model does the thinking, how a step's work actually gets done, where code
runs, which repo host — is an **adapter** selected by one `type:` field in a
profile. Swapping Jira for a text file, or Claude for a local OpenAI-compatible
endpoint, or a coding CLI for ticketbot's own tool loop, is a config edit, never a
code change. The engine itself never imports `anthropic`, `httpx`, or any concrete
adapter — everything is resolved by name through `ticketbot/core/registry.py`.

## The swap points

| Point | Kinds | Change it when… |
|---|---|---|
| `source` | `file`, `jira` | you move from ad-hoc/local tickets to a real Jira project (or back, for a demo). |
| `sink` | `file`, `jira`, `github_pr` | you want results as local files, a Jira comment, a GitHub PR, or several at once (`also:`). |
| `model` | `anthropic`, `openai_compat` | you switch model vendors, or want a cheaper/different model for one role (`model: cheap` on a step). |
| `executor` | `process`, `api` | you want to drive a coding CLI you already trust (`claude -p`, `codex exec`, `aider`) vs. ticketbot's own path-jailed tool loop. |
| `runtime` | `none`, `local_shell`, `solari` | you need code to run and screenshots to come from somewhere other than the machine ticketbot is on. |
| `repo` | `git_local`, `github` | you're iterating locally in a worktree vs. pushing branches and opening PRs on GitHub. |

## Install

```bash
uv venv
uv pip install -e ".[dev]"      # pytest, pytest-cov
uv pip install -e ".[solari]"   # only if runtime.type: solari — pulls the 3 Solari SDKs
```

Secrets are never typed into a profile as literal values — every credential is an
`${ENV_VAR}` reference, expanded only at the moment an adapter actually needs it
(`ticketbot validate`/`config show` never expand them, so a profile validates with
no environment set at all) and never written to `config.resolved.yaml` or a log.

| Env var | Needed by |
|---|---|
| `ANTHROPIC_API_KEY` | `model: {type: anthropic}` — read directly by the Anthropic SDK; set `api_key: ${ANTHROPIC_API_KEY}` explicitly if you want it sourced from a profile-declared ref instead of the SDK's own default lookup. |
| `JIRA_EMAIL`, `JIRA_API_TOKEN` | `source`/`sink: {type: jira}` — basic auth against Jira Cloud REST v3. |
| `GITHUB_TOKEN` | `sink: {type: github_pr}`, `repo: {type: github}` — used by the `gh` CLI when it's on PATH, else REST. |
| `SOLARI_API_KEY` | `runtime: {type: solari}` — one key across sandboxes, browsers, and desktops. |

## Quickstart, fully offline

No API key, no network, no real coding CLI — `profiles/file-text-none.yaml` uses
`source: file`, `sink: file`, `repo: git_local`, `runtime: none`, and its `${ENV}`
refs (for the default Anthropic model) are only touched once a step actually runs.

```bash
ticketbot validate -c profiles/file-text-none.yaml
ticketbot run -c profiles/file-text-none.yaml --input-text "Add a /health endpoint"
```

That produces a `runs/<id>/` directory:

```
runs/2026-09-01-1443-add-a-health-endpoint-a3f9/
  banner.txt   config.resolved.yaml   run.json      workitem.json
  plan.md      sections/section-1.md  patch.diff    test-report.md
  review.md    security.md            pr.md         ticket_comment.md
  steps/<step-id>.md                  screenshots/  logs/<step-id>.log
```

and a `banner.txt` that says what actually ran, not what the config says:

```
Using source=file "Add a /health endpoint" (Task)
pipeline=builtin:pipelines/standard.yaml  (rule: default)
models=ingest:Claude Haiku 4.5 · clarifier:Claude Opus 5 · planner:Claude Opus 5 · coder:Claude Opus 5 · tester:Claude Opus 5 · reviewer:Claude Opus 5 · security:Claude Opus 5 · reporter:Claude Opus 5
executor=api: main
runtime=none
repo=. @ agent/add-a-health-endpoint-add-a-health-endpoint
```

## Configuration

A profile is one YAML file validated against `ticketbot/config/schema.py`'s
`Profile` model. Every adapter block (`source:`, `sink:`, `model.providers.*`,
`executor.kinds.*`, `runtime:`, `repo:`) is a permissive `{type: <name>, ...}` —
each adapter validates its own options at construction time, so adding a new kind
never touches the schema.

`extends:` deep-merges a parent profile underneath the child's own keys (scalars
and **lists** are replaced wholesale, not concatenated) — `profiles/_base.yaml` is
the shared parent every shipped example extends, holding the default models,
executor, gates, budget, and pipeline-selector rules so each example profile only
states what makes it different. A `builtin:` reference (`builtin:pipelines/
standard.yaml`) resolves against the installed package regardless of where the
profile file lives; any other reference resolves relative to the profile's own
directory.

`profiles/jira-claude-solari.yaml` is the annotated full-vertical example — Jira
source and sink, a GitHub PR as a secondary sink, a GitHub repo, Claude Opus 5 (+
Haiku for intake, + gpt-5 as a peer reviewer), and a Solari desktop runtime:

```yaml
extends: _base.yaml
name: jira-claude-solari

source:
  type: jira
  base_url: https://acme.atlassian.net
  email: ${JIRA_EMAIL}
  token: ${JIRA_API_TOKEN}
  jql: 'assignee = currentUser() AND status = "To Do" AND labels = agent'
  points_field: customfield_10016    # the story-point field id differs per instance

sink:
  type: jira
  also: [{type: github_pr, token: "${GITHUB_TOKEN}"}, {type: file}]

repo:
  type: github
  clone: git@github.com:acme/app.git
  branch_template: "agent/{ticket_key}-{slug}"
  token: ${GITHUB_TOKEN}

model:
  default: claude-opus
  providers:
    claude-opus: {type: anthropic, model: claude-opus-5, effort: xhigh}
    cheap:       {type: anthropic, model: claude-haiku-4-5, effort: low}
    peer:        {type: openai_compat, base_url: "${PEER_BASE_URL}", model: gpt-5}

executor:
  default: claude-cli
  kinds:
    claude-cli: {type: process, cmd: ["claude", "-p"], prompt: stdin, timeout_s: 1800}

runtime: {type: solari, mode: desktop, resolution: "1280x720", screenshot_on: [verify, publish]}
```

Every secret above is an `${ENV}` reference — `ticketbot validate` on this exact
file succeeds with none of those variables set, because nothing is expanded until
an adapter is actually constructed at run time.

## Pipelines

A pipeline is a YAML list of role steps, loaded and validated (duplicate ids,
unknown keys, an unparseable `when:`, an unsupported `for_each`) at load time —
never mid-run:

```yaml
name: standard
defaults: {timeout_s: 1800}
steps:
  - {id: intake,  role: ingest,   model: cheap, tools: [source.read], produces: [workitem.json]}
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
    commit: "impl: {section.title}"
  - {id: verify,  role: tester,   tools: [fs.read, fs.write, fs.edit, shell.run, runtime.screenshot], produces: [test-report.md]}
  - {id: review,  role: reviewer, model: peer, tools: [fs.read, fs.edit], produces: [review.md]}
  - {id: security, role: security, when: "plan.security == yes or diff.touches_security", produces: [security.md]}
  - {id: publish, role: reporter, tools: [fs.read, fs.write, runtime.screenshot], produces: [pr.md, ticket_comment.md]}
on_question: pause_and_relay
on_defer: spawn_fixer
```

Three built-ins ship in `ticketbot/builtin/pipelines/`:

| Pipeline | Shape |
|---|---|
| `standard.yaml` | the eight steps above — the default. |
| `small-bug.yaml` | drops `clarify` and `security`; `verify` absorbs `review`'s job. |
| `large-with-clarification.yaml` | `clarify` runs unconditionally, an extra `research` step reads the codebase before planning, the plan gate is mandatory (`gate: human`), and `review`/`security` always run. |

`when:` is evaluated by `core/predicate.py` — a hand-written tokenizer and
recursive-descent parser, **never `eval`**, bounded on expression length, token
count, and nesting depth so a hostile string fails fast instead of doing unbounded
work. It supports `field op value` over `workitem.*` and prior step outputs
(`plan.security`, `diff.touches_security`, …), `and`/`or`/`not`, and the operators
`eq`/`ne`/`lt`/`lte`/`gt`/`gte`/`in`/`contains`/`is empty`/`is not empty` (`==`,
`!=`, `<`, `<=`, `>`, `>=` also work as symbols). The mapping form
(`{story_points: {lte: 2}}`, used by `pipeline_selector.rules`) uses the same
operator set. `ambiguity`/`size`/`severity` compare by their declared order (`low
< medium < high`, …), not alphabetically.

`for_each: plan.sections` reproduces "one coder per section, sequentially": the
planner writes `sections/section-1.md`, `section-2.md`, …, sorted **numerically**
(`section-10.md` after `section-9.md`), and the engine spawns one `implement`
execution per section, committing each one separately.

## Roles

Nine role prompts live in `ticketbot/builtin/prompts/roles/`, each ending in an
explicit `Return ONLY:` contract — that text is what lands in the step's commit
body and its `runs/<id>/steps/<id>.md` artifact.

| Role | Returns |
|---|---|
| `ingest` | a JSON envelope: summary, acceptance criteria, ambiguity, size. |
| `clarifier` | a `QUESTION:` block for the ticket, or nothing if it never triggers. |
| `planner` | `plan.md` + one `sections/section-N.md` per unit of work, plus a premise check that can raise a `QUESTION:` before any code is touched. |
| `coder` | one section implemented; a one-paragraph summary of what changed. |
| `tester` | tests added, the suite's result, `test-report.md`. |
| `reviewer` | `review.md`: verdict (APPROVE / CHANGES-NEEDED), findings by severity, fixes applied, anything deferred. |
| `security` | `security.md`: findings on untrusted input, authn/authz, subprocess/shell, path handling, secrets, injection. |
| `reporter` | `pr.md` (full) and `ticket_comment.md` (short) — two DIFFERENT write-ups, never the same text. |
| `fixer` | spawned per `DEFER:` line (capped per step) to implement what a reviewer/security step chose not to force. |

## Pipeline selection

`pipeline_selector.rules` are evaluated in order against the work item; the first
whose `when:` mapping holds wins, and its human-readable reason lands in the
banner (`pipeline=...  (rule: story_points <= 5)`). No match falls through to
`pipeline_selector.default`. The shipped default rules key off story points and
issue type:

```yaml
pipeline_selector:
  rules:
    - when: {story_points: {lte: 2}, issue_type: Bug}
      use: builtin:pipelines/small-bug.yaml
    - when: {story_points: {lte: 5}}
      use: builtin:pipelines/standard.yaml
    - when: {story_points: {gte: 8}}
      use: builtin:pipelines/large-with-clarification.yaml
  default: builtin:pipelines/standard.yaml
```

## The run directory

Every run gets `runs/<id>/` (id = timestamp + item slug + 4 random hex chars),
holding `banner.txt`, `config.resolved.yaml` (secrets still `${ENV}` refs, scrubbed
of anything secret-shaped besides), `workitem.json`, `run.json`, every artifact a
step declared via `produces:`, `steps/<id>.md` for each step's raw returned text,
`screenshots/` (only populated when `runtime.screenshot_on` names a step and the
runtime isn't `none`), and `logs/<id>.log` per step.

`run.json` is rewritten atomically (temp file + `os.replace`) after **every**
step, recording status, timing, cost, commits and artifacts for each one — so a
crash mid-run never leaves it half-written, and

```bash
ticketbot resume <run-id>
```

reloads it, skips every step already `ok`/`skipped`, and continues from the first
one that isn't.

## CLI reference

| Command | Flags |
|---|---|
| `ticketbot validate -c <profile>` | load and validate a profile; exit 0/2. |
| `ticketbot config list [--dir profiles]` | list profiles in a directory. |
| `ticketbot config show <profile>` | print the resolved (still-unexpanded) profile as YAML. |
| `ticketbot config banner <profile>` | print the config-only "what would be used" banner. |
| `ticketbot config init <name> [--dir profiles] [--force]` | scaffold a minimal offline profile. |
| `ticketbot run -c <profile>` | `--once <id>` \| `--input <path>` \| `--input-text <text>` \| `--repo <path>` \| `--dry-run` \| `--pause-at <step-id>` \| `--force-lock` \| `--runs-dir <dir>`. |
| `ticketbot poll -c <profile>` | `--once` \| `--max-items <n>` \| `--dry-run` \| `--runs-dir <dir>`. |
| `ticketbot resume <run-id>` | `-c <profile>` (default: `profiles/<run's profile>.yaml`) \| `--runs-dir <dir>` \| `--force-lock`. |

Exit codes are shared by `run`/`poll`/`resume`: `0` done, `2` config/usage error,
`3` blocked (needs a human), `4` failed.

## What Solari is

[Solari](https://getsolari.com) is cloud browsers, sandboxes, and desktops behind
one `SOLARI_API_KEY`. **It is not a model** — here it is exactly one `runtime`
adapter, providing where a step's shell commands run and where screenshots come
from, while the coding itself is still whatever `model`/`executor` the profile
configures. It is off (`runtime: {type: none}`) by default.

## Safety rails

- Per-step **tool allowlists**, enforced by the executor — the clarifier gets no
  filesystem tools, the reviewer/security roles never get `shell.run`.
- Every filesystem tool is **path-jailed** (`executors/tools.py`) via
  `Path.resolve()` + `is_relative_to()` against the workspace root — `../`,
  absolute paths, and symlinks pointing outside it are all rejected.
- Subprocesses are always `subprocess.run(argv, shell=False)` — never a composed
  shell string — with an explicit env allowlist.
- `when:` is a restricted parser, **never `eval`**; profile/pipeline YAML is
  always loaded with `yaml.safe_load`.
- **One work item, one run** — a file lock (`runs/.locks/<item>.lock`) prevents
  two runs racing the same ticket; `--force-lock` breaks a stale one deliberately.
- **Cost and wall-clock budgets** (`budget.max_cost_usd`/`max_wall_clock_s`) stop
  a run before it can spend unboundedly; treat them as guard rails, not billing.
- **Secrets are `${ENV}` references only** — expanded at use time, registered with
  the redactor immediately, and scrubbed (pattern-based, plus every literal secret
  value seen) from `config.resolved.yaml`, every run artifact, and every log line.
- **No auto-merge, anywhere.** `gates.on_pr_ready: human_review` opens the PR and
  stops the run for a human; `auto` still never means "merge", only "don't pause".
- `GitLocalRepo.verify_landed()` checks that a coder's declared file writes
  actually exist under the workspace before committing — a clean agent summary is
  not proof the edit landed in the right tree.

## Development

```bash
uv run pytest
```

Tests are plain `pytest`; nothing in the suite hits the network, spawns a real
coding CLI, or touches a real Jira/GitHub/Solari account — `tests/fakes.py`
(`FakeModelProvider`, `FakeExecutor`, `FakeRuntime`, `FakeSource`, `FakeSink`,
`FakeRepo`) stands in everywhere an adapter would otherwise reach outward, and
`tests/test_e2e_offline.py` drives the real `standard` pipeline end to end through
the real adapter registry to prove the whole thing produces every artifact in
`runs/<id>/`, that `resume` genuinely skips completed steps, and that
`--dry-run` makes no outward call at all.

Manual smoke sequence, in order:

```bash
# every shipped profile validates with no environment set
for f in profiles/*.yaml; do ticketbot validate -c "$f"; done

# the offline profile against the toy fixture repo
ticketbot run -c profiles/file-text-none.yaml \
  --input-text "Add a /health endpoint" \
  --repo tests/fixtures/toy-repo
```

Inspect `runs/<id>/banner.txt` and `pr.md`. Then flip `executor.default` from
`inline` to a `process` kind pointed at a real coding CLI (or back) and re-run the
same command — same artifacts, different engine, is the proof the swap works.
