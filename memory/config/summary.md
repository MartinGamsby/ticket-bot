# Config: the profile model

A profile is one YAML file, loaded by `ticketbot/config/loader.py` and validated into
`ticketbot/config/schema.py: Profile` (pydantic v2, `extra="forbid"` at the top level).

## The two ways to reach a model

A profile drives a model through ONE of two executors, and this is the swap the
project exists to demonstrate:

| Profile | `executor` | Credentials |
|---|---|---|
| `file-claude-cli.yaml` | `process`, `cmd: ["claude", "-p"]` | none - the CLI authenticates itself from its own store |
| `file-text-none.yaml` | `api` (ticketbot's own tool loop) | `ANTHROPIC_API_KEY` |

They are otherwise identical. With a `process` executor the `model:` slots are
never constructed at all -- only the `api` executor's tool loop resolves them --
so the inherited Anthropic providers in `_base.yaml` cost nothing and are kept so a
single step can be flipped back to `executor: inline` without redeclaring one.

`env_passthrough:` (not `env:`) is how a profile forwards a key to a spawned CLI: an
unset name in `env_passthrough` is skipped, whereas an `env:` `${ENV}` ref is
expanded strictly and would fail the run on every machine that authenticates by
OAuth rather than by key.

## Shape

```yaml
name: file-text-none          # required
version: 1
extends: _base.yaml           # optional; dropped after merging
source: {type: file, ...}
sink:   {type: file, also: [{type: github_pr}, ...]}
repo:   {type: git_local, path: "."}
model:
  default: main               # must be a key of providers
  providers:
    main:  {type: anthropic, model: claude-opus-5, effort: xhigh}
executor:
  default: inline             # must be a key of kinds
  kinds:
    inline: {type: api, model: main, max_iterations: 40}
runtime: {type: none, screenshot_on: [verify, publish]}
pipeline_selector: {rules: [...], default: builtin:pipelines/standard.yaml}
gates:  {on_unclear: comment_and_unassign, on_pr_ready: human_review, max_clarify_rounds: 2}
budget: {max_cost_usd: 25, max_wall_clock_s: 5400}
runs_dir: runs
```

## Why the schema stays small

Every adapter block is an `AdapterConfig`: `{type: str}` plus `extra="allow"`. Options are read with
`cfg.opt("name", default)` and validated by the ADAPTER at construction time. That is what makes
"adding an adapter never touches the schema" true. Two subclasses add one typed field each:
`SinkConfig.also: list[AdapterConfig]` and `RuntimeConfig.screenshot_on: list[str]` (step ids).

Validated by the schema itself, at load time: `model.default` must name a provider slot;
`executor.default` must name an executor kind; `gates` values are `Literal`s.

`Profile.base_dir` is set by the loader (the directory of the OUTERMOST profile) and excluded from
serialization. It is what makes relative `path:`/`glob:`/`extends:` resolution profile-relative
rather than cwd-relative.

## Loading

```python
load_profile(path) -> Profile          # merge extends chain, validate, set base_dir
load_profile_dict(path) -> (dict, base_dir)
resolved_yaml(profile) -> str          # ${ENV} still unexpanded; caller must redact()
```

`${ENV}` references are NEVER expanded by the loader — `ticketbot validate` and `config show` succeed
with no environment set at all. See [secrets-and-redaction.md](secrets-and-redaction.md).
`extends:` and `builtin:` are covered in [profile-inheritance.md](profile-inheritance.md).

## Shipped profiles (`profiles/`)

| Profile | Vertical it proves |
|---|---|
| `_base.yaml` | the shared parent: default models (`main`/`cheap`/`peer`), `inline` api executor, `runtime: none`, selector rules, gates, budget, `source`/`sink: file`, `repo: {type: git_local}` with NO `path` |
| `file-text-none.yaml` | fully offline default: text or a file in, `runs/<id>/` out |
| `file-solari-desktop.yaml` | the screenshot path, with a real Solari desktop runtime |
| `github-codex.yaml` | the "any AI" proof: zero Anthropic references, `openai_compat` models plus a `codex exec` process executor, GitHub repo + PR sink |
| `jira-claude-solari.yaml` | the full vertical: Jira source and sink, GitHub PR as `also:`, GitHub repo, Claude + a gpt-5 peer reviewer, Solari desktop runtime |

Every one of them validates with no environment variables set — that is a test
(`tests/test_profiles.py`), not an aspiration.

## Config-only CLI surface

`ticketbot validate`, `config list`, `config show`, `config banner`, `config init` never touch the
engine. See [../cli/summary.md](../cli/summary.md).
