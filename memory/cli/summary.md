# The CLI

`ticketbot/cli.py`, argparse, entry point `ticketbot = ticketbot.cli:main`. Every subcommand function
returns an int exit code; nothing calls `sys.exit()` from deep inside. Every printed error goes
through `redact()`.

## Commands

| Command | Flags |
|---|---|
| `ticketbot validate -c <profile>` | load and validate; exit 0 or 2 |
| `ticketbot config list [--dir profiles]` | list profiles in a directory (skips `_`-prefixed files) |
| `ticketbot config show <profile>` | the resolved profile as YAML, `${ENV}` still unexpanded |
| `ticketbot config banner <profile>` | the config-only "what would be used" banner |
| `ticketbot config init <name> [--dir] [--force]` | scaffold a minimal offline profile |
| `ticketbot run -c <profile>` | `--once <id>`, `--input <path>`, `--input-text <text>`, `--repo <path>`, `--dry-run`, `--pause-at <step-id>`, `--force-lock`, `--runs-dir <dir>` |
| `ticketbot poll -c <profile>` | `--once`, `--max-items <n>`, `--dry-run`, `--runs-dir <dir>` |
| `ticketbot resume <run-id>` | `-c <profile>` (default `profiles/<run's profile_name>.yaml`), `--runs-dir <dir>`, `--force-lock` |

`validate` and the `config` group never touch the engine — they only load and print. That is what
makes "a profile validates with no environment set" cheap to guarantee.

## Exit codes

Shared by `run`, `poll` and `resume`:

| Code | Meaning |
|---|---|
| 0 | done |
| 2 | config or usage error (also a held lock) |
| 3 | blocked — needs a human |
| 4 | failed |

`poll` returns the WORST code across the runs in the sweep, and prints `poll: no runs` with exit 0
when a sweep produced nothing.

## Flag semantics worth remembering

- `--input-text` / `--input` force a `FileSource` regardless of the profile's configured source, so
  any profile can be driven from the command line. `--once <id>` instead fetches that external id
  from the CONFIGURED source.
- `--repo <path>` overrides `repo.path` for the run and is what the banner reports (`_repo_cfg()`).
- `--dry-run` still does real local git work; only outward-facing calls (sink methods, `push`,
  `open_pr`) are recorded to `dryrun.log` instead of being made.
- `--pause-at <step-id>` turns exactly that step's `optional_human` gate interactive, without
  pausing every such gate in the pipeline.
- `--force-lock` breaks an existing (possibly stale) run lock deliberately.
- `--runs-dir` overrides the profile's `runs_dir`. Without it the rule is
  `profile.base_dir / profile.runs_dir`, resolved by `engine.orchestrator.resolve_runs_dir()` —
  the single copy of that rule, shared by `Orchestrator.__init__` and `cli._cmd_resume`.

  `resume` is the awkward one: the run must be loaded to learn its profile name, but the PROFILE
  owns the directory the run lives in. So it resolves in three branches — `--runs-dir` wins
  outright; failing that an explicit `-c` is loaded FIRST and `resolve_runs_dir(profile)` used
  (that profile is then reused, not re-loaded, after the store opens); with neither, `runs/` under
  the cwd is the only thing knowable from a run id alone. Duplicating the rule instead of sharing
  it is exactly how `resume` came to ignore a custom `runs_dir`.

`Orchestrator(interactive=True)` exists but has no CLI flag; only `--pause-at` reaches that behaviour.

## Manual smoke sequence

```bash
for f in profiles/*.yaml; do ticketbot validate -c "$f"; done

ticketbot run -c profiles/file-text-none.yaml \
  --input-text "Add a /health endpoint" \
  --repo tests/fixtures/toy-repo
```

Then inspect `runs/<id>/banner.txt` and `pr.md`, flip `executor.default` between `inline` and a
`process` kind, and re-run: same artifacts, different engine, is the proof the swap works.
