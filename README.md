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
code change. The engine never imports `anthropic` or `httpx` — every adapter is
resolved by name at run time through `ticketbot/core/registry.py`, against the
protocol modules alone. (One deliberate exception: the orchestrator imports
`FileSource` directly, so `--input-text`/`--input` can override the profile's
configured source from the command line.)

## The swap points

| Point | Kinds | Change it when… |
|---|---|---|
| `source` | `file`, `jira` | you move from ad-hoc/local tickets to a real Jira project (or back, for a demo). |
| `sink` | `file`, `jira`, `github_pr` | you want results as local files, a Jira comment, a GitHub PR, or several at once (`also:`). |
| `model` | `anthropic`, `openai_compat`, `fake` | you switch model vendors, or want a cheaper/different model for one role (`model: cheap` on a step). `fake` replays a scripted list of turns and exists so the suite can drive the engine with no vendor at all. |
| `executor` | `process`, `api` | you want to drive a coding CLI you already trust (`claude -p`, `codex exec`, `aider`) vs. ticketbot's own path-jailed tool loop. |
| `runtime` | `none`, `local_shell`, `solari` | you need code to run and screenshots to come from somewhere other than the machine ticketbot is on. A runtime with no command surface (`none`, and `solari` in `mode: desktop`/`browser`) leaves `shell.run` running locally rather than failing it. |
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
| `JIRA_BOT_ACCOUNT_ID` | `source: {type: jira}`'s `account_id:` — who `claim()` assigns the issue to. Without it, claiming logs a warning and only transitions. |
| `GITHUB_TOKEN` | `sink: {type: github_pr}`, `repo: {type: github}` — used by the `gh` CLI when it's on PATH, else REST. |
| `SOLARI_API_KEY` | `runtime: {type: solari}` — one key across sandboxes, browsers, and desktops. |
| `MODEL_BASE_URL`, `MODEL_API_KEY` | `model: {type: openai_compat}` in `profiles/github-codex.yaml`. |
| `PEER_BASE_URL`, `PEER_API_KEY` | the `peer` (second-vendor reviewer) slot in `profiles/jira-claude-solari.yaml`. |

The names above are the ones the shipped profiles happen to use; `${ANY_NAME}`
works anywhere in a profile, because the loader never interprets the name — it
just leaves the reference alone until an adapter expands it.

### Where to put them: `.env`

```bash
cp .env.example .env      # then fill in only what your profile uses
```

Every command loads `.env` from the current directory before anything else runs,
so the keys above resolve without exporting them by hand. `.env` is git-ignored;
`.env.example` is the tracked template.

- **A name already set in your real environment always wins.** A stale `.env` in a
  working copy can never shadow what CI or your shell exported.
- `--env-file PATH` loads one from somewhere else; `--no-env-file` ignores it
  entirely. A `--env-file` you name but that doesn't exist is an error — the
  implicit `./.env` is the only optional one.
- Values are literal text: no `$VAR` interpolation inside the file, no command
  substitution. `${ENV}` refs resolve in one place only, when an adapter reads them.
- A loaded name that reads like a credential (`*_KEY`, `*_TOKEN`, `*_SECRET`, …) is
  registered with the redactor as it loads, so its value is masked in `runs/<id>/`
  artifacts and logs even if it matches no known token shape. Matching is on the
  *name*, so a `MODEL_BASE_URL` never becomes a pattern that mangles ordinary output.

Exporting the variables yourself works just as well — `.env` is a convenience, not
a requirement, and nothing reads it in production if you'd rather set real
environment variables.

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
  result.md    attachments/           screenshots/  steps/<step-id>.md
  logs/<step-id>.log
```

and a `banner.txt` that says what actually ran, not what the config says — the
model line names the provider objects the steps really resolved to (including the
effort each was constructed with), and the executor line is the executor object's
own description, not the config's slot name:

```
Using source=file "Add a /health endpoint" (Task)
pipeline=builtin:pipelines/standard.yaml  (rule: default)
models=ingest:Claude Haiku 4.5 (claude-haiku-4-5) effort=low · clarifier:Claude Opus 5 (claude-opus-5) effort=xhigh · planner:Claude Opus 5 (claude-opus-5) effort=xhigh · coder:Claude Opus 5 (claude-opus-5) effort=xhigh · tester:Claude Opus 5 (claude-opus-5) effort=xhigh · reviewer:Claude Opus 5 (claude-opus-5) effort=xhigh · security:Claude Opus 5 (claude-opus-5) effort=xhigh · reporter:Claude Opus 5 (claude-opus-5) effort=xhigh
executor=api: Claude Opus 5 (claude-opus-5) effort=xhigh
runtime=none
repo=. @ agent/add-a-health-endpoint-add-a-health-endpoint
```

`ticketbot config banner <profile>` prints the config-only version of the same
thing — every model slot rather than the ones a pipeline uses, and no ticket line,
because no work item has been fetched.

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
    claude-cli:
      type: process
      cmd: ["claude", "-p"]
      prompt: stdin
      timeout_s: 1800
      env_passthrough: [ANTHROPIC_API_KEY, CLAUDE_CONFIG_DIR]

runtime: {type: solari, mode: desktop, resolution: "1280x720", screenshot_on: [verify, publish]}
```

Every secret above is an `${ENV}` reference — `ticketbot validate` on this exact
file succeeds with none of those variables set, because nothing is expanded until
an adapter is actually constructed at run time.

**A spawned coding CLI authenticates itself.** `executor: {type: process}` builds
the child's environment from an allowlist, never `os.environ` wholesale, and the
default list carries only non-secret *locators* — `USERPROFILE`/`APPDATA`/
`LOCALAPPDATA` on Windows, `HOME`/`XDG_*` on POSIX, plus `XDG_RUNTIME_DIR` and
`DBUS_SESSION_BUS_ADDRESS` so a Secret Service keyring is reachable at all — so
`claude -p` and `codex exec` find the OAuth profile or keyring entry they
already signed in with. No API key is ever forwarded by default. A profile that
needs one (headless, CI, no interactive login) names it in that executor kind's
own `env_passthrough:`, as above: the name is forwarded only when it is actually
set in the parent environment, and a forwarded name that reads like a credential
(`*_KEY`, `*_TOKEN`, `*_SECRET`, …) is registered with the redactor so the
child's own output can never echo it into `runs/<id>/logs/`. Use
`env_passthrough:`, not `env: {ANTHROPIC_API_KEY: "${ANTHROPIC_API_KEY}"}` —
`${ENV}` refs are expanded strictly, so that spelling fails the run on every
machine that authenticates by OAuth rather than by key.

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
    isolation: worktree
    commit: "impl: {section.title}"
  - {id: verify,  role: tester,   tools: [fs.read, fs.write, fs.edit, shell.run, runtime.screenshot], produces: [test-report.md], commit: "test: {ticket_key}"}
  - {id: review,  role: reviewer, model: peer, tools: [fs.read, fs.edit], produces: [review.md], commit: "review-fix: {ticket_key}"}
  - {id: security, role: security, when: "plan.security == yes or diff.touches_security", tools: [fs.read, fs.edit], produces: [security.md], commit: "security-fix: {ticket_key}"}
  - {id: publish, role: reporter, tools: [fs.read, fs.write, runtime.screenshot], produces: [pr.md, ticket_comment.md]}
on_question: pause_and_relay
on_defer: spawn_fixer
```

A step's `commit:` is rendered with the same `{placeholder}` values its prompt
gets (plus `{section.*}` inside a `for_each` fan-out) and committed only after the
landing check passes: `verify_landed()` confirms the step's declared writes are
really under the workspace, and `drifted()` confirms nothing appeared in the
parent clone. A step's `model:`/`executor:` are the slot and kind NAMES to look up;
omitting them (as `defaults:` above does) is what selects `model.default` /
`executor.default` — the literal string `"default"` is not a sentinel, it is just
another name to resolve. `isolation: worktree` is recorded but advisory: the repo
adapter decides how the workspace is isolated, and `git_local` already gives the
whole run its own worktree.

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
operators, spelling the two emptiness tests `empty` and `not_empty`.
`ambiguity`/`size`/`severity` compare by their declared order (`low < medium <
high`, …), not alphabetically.

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

## Polling for work

`ticketbot run` handles one item; `ticketbot poll` keeps taking them from the
source (`--once` does a single sweep instead of looping every
`source.poll_seconds`). Each item is claimed, run to a terminal state, and then
**retired** so the next sweep moves on rather than picking it up again:

| Source | A sweep yields | Retiring an item |
|---|---|---|
| `file` | files matching `glob:` (default `inbox/*.md`), oldest first, skipping anything already under `processed_dir:` (default `inbox/processed`) | the file is moved into `processed_dir` |
| `jira` | the issues matching `jql:`, paged | nothing to do — `claim()` already assigned the issue and transitioned it out of the polled query |

An item currently locked by another run is skipped rather than waited for, so two
pollers can share a source without racing.

## The run directory

Every run gets `runs/<id>/` (id = timestamp + item slug + 4 random hex chars),
holding `banner.txt`, `config.resolved.yaml` (secrets still `${ENV}` refs, scrubbed
of anything secret-shaped besides), `workitem.json`, `run.json`, every artifact a
step declared via `produces:`, `patch.diff` (the diff as the reviewer saw it),
`steps/<id>.md` for each step's raw returned text,
`screenshots/` (only populated when `runtime.screenshot_on` names a step and the
runtime actually returns an image), and `logs/<id>.log` per step. `sink: {type:
file}` adds `result.md` (a one-line log of every sink call it received) and
`attachments/` — where the screenshots the reporter sent are copied, exactly as a
Jira sink would have uploaded them. That sink also appends each comment it is given
to `ticket_comment.md` (separated by a `---` rule), but the engine writes that file
last, after the sink call, so the artifact always holds exactly the one comment
that was posted rather than a copy of it per sink. `--dry-run` adds `dryrun.log`,
the list of outward calls that were suppressed.

The reporter writes `ticket_comment.md` before the pull request exists, so its
`PR:` line is blank at that moment; once `repo.open_pr()` returns a URL the engine
substitutes it into both the posted comment and the file, and hands it to any sink
that reports onto the PR itself (`github_pr`, which drops comments until it knows
which PR to post to).

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

- Per-step **tool allowlists** — the clarifier gets no filesystem tools, the
  reviewer/security roles never get `shell.run`. Enforced by the `api` executor,
  which owns the tool catalogue. A `process` executor spawns a whole coding CLI
  that brings its own tools and cannot be constrained this way, so under a
  profile that defaults to one (`jira-claude-solari.yaml`, `github-codex.yaml`)
  containment is whatever that CLI enforces for itself.
- Every filesystem tool is **path-jailed** (`executors/tools.py`) via
  `Path.resolve()` + `is_relative_to()` against two orchestrator-owned roots and
  nothing else: the workspace (tried first, so a relative path always means "in
  the repo") and the run directory (so a role prompt's `{plan_file}`,
  `{section_file}` and `{run_dir}/pr.md` are writable). `../`, absolute paths
  outside both, and symlinks pointing outside them are all rejected. Admitting
  the run directory does not admit the run *record*: `run.json`,
  `config.resolved.yaml`, `workitem.json`, `banner.txt` and `logs/` are written
  by the engine and refused to a step, so a run cannot erase its own audit trail.
- **The security-review gate fails closed.** `plan.security` is scraped out of a
  file the planner model wrote, so an absent `Security:` line (or an absent
  `plan.md`) counts as "unknown" and runs the `security` step; only an explicit
  `Security: no` skips it.
- Subprocesses are always `subprocess.run(argv, shell=False)` — never a composed
  shell string — with an explicit env allowlist.
- `when:` is a restricted parser, **never `eval`**; profile/pipeline YAML is
  always loaded with `yaml.safe_load`.
- **One work item, one run** — a file lock (`runs/.locks/<slug>-<digest>.lock`,
  the digest taken from the raw item key so two tickets whose slugs collide never
  share a lock) prevents two runs racing the same ticket; `--force-lock` breaks a
  stale one deliberately.
- **Cost and wall-clock budgets** (`budget.max_cost_usd`/`max_wall_clock_s`) stop
  a run before it can spend unboundedly; treat them as guard rails, not billing.
- **Secrets are `${ENV}` references only** — expanded at use time, registered with
  the redactor immediately, and scrubbed (pattern-based, plus every literal secret
  value seen) from `config.resolved.yaml`, every run artifact, and every log line.
  Also from everything that leaves the machine: the Jira comment and remote link,
  the GitHub PR comment, the PR title and body, and the git commit message. A
  ticket is readable by whoever filed it, so the remote copy must not be the
  leakier one.
- **No auto-merge, anywhere.** `gates.on_pr_ready: human_review` opens the PR and
  stops the run for a human; `auto` still never means "merge", only "don't pause".
- **A clean agent summary is not proof the edit landed in the right tree**, so
  every `commit:` step is checked twice before it commits.
  `GitLocalRepo.verify_landed()` audits the paths the step *declared*: they must
  exist and be inside the workspace. It is given a step's workspace writes only —
  run-directory artifacts (a screenshot, `plan.md`) are excluded, so producing one
  is never mistaken for a stray edit. `GitLocalRepo.drifted()` covers what that
  audit structurally cannot see: a spawned CLI that ignored its cwd and edited the
  **parent clone** leaves nothing in the declared list, because that list comes
  from a snapshot of the workspace. `drifted()` diffs the parent clone's
  `git status` against a baseline taken at checkout, so a pre-existing dirty tree,
  the run directory, and a step that legitimately changes nothing (a reviewer with
  no findings) are all silent — only a *new* change outside the workspace fails the
  step, naming the paths and the clone they landed in.

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

Most test modules cover one module against its own contract.
`tests/test_seams.py` covers the joins instead — the paths no single module owns:
`screenshot_on` from profile to run dir to sink attachment, the source-retirement
call the poller has to make, closing what the engine opened, the pull request URL
reaching the comment and the sinks that need it, the banner reporting live objects
rather than config, every shipped profile defining the model slots its own
pipelines name, and every name in the adapter registry actually importing to a
class with a `describe()`.

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
