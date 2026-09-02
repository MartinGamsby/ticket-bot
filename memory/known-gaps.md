# Known gaps

Things that are deliberately unfinished or need a decision rather than a patch. None of these is a
bug report to be re-discovered — they are the current, accurate state.

## Shipped but not wired: the ticket-comment templates

`ticketbot/builtin/prompts/comments/{clarify,blocked,done}.md` are plain `{placeholder}` templates
(no model involved) meant to shape the ticket comments the ENGINE posts itself. They ship, and they
are shape-tested, but **nothing renders them**. What actually happens today:

- the clarify gate posts the raw `QUESTION:` text the step returned;
- the reporter's own `ticket_comment.md` is posted verbatim (with the PR URL substituted in);
- a failed run posts nothing at all.

Wiring them is a product decision, not a refactor, because it changes what lands on a real ticket.
Three open questions:

1. Should the clarify gate post `clarify.md` (which adds the "I have unassigned myself" line and the
   banner) instead of the raw question block, and does the wording duplicate what
   `prompts/roles/clarifier.md` already asks the model to write?
2. Should `done.md` replace or wrap the reporter's `ticket_comment.md`? The reporter is instructed to
   produce a short comment already; the template would add a second voice.
3. `blocked.md` means commenting on failures the system currently stays SILENT about — that is a new
   outward-facing behaviour on real tickets and needs an explicit yes.

Decide deliberately. Do not assume the templates are live.

## Open engineering items

- **`verify_landed()` is vacuous as a drift detector.** `ProcessExecutor.files_written` comes only
  from `diff_snapshots(req.workspace)`, and so does `ApiLoopExecutor`'s after its workspace-only
  filter — so every path handed to `repo.verify_landed()` is under the workspace by construction and
  the check can never return a miss. Catching real worktree drift (a spawned CLI writing into the
  parent clone) needs a different signal: e.g. an `implement` step whose `commit:` staged nothing AND
  whose workspace snapshot is unchanged, cross-checked against `repo.status()` and the parent clone.
  Whatever replaces it must not fail steps that legitimately change nothing, such as a `review` with
  no findings. See [adapters/repos.md](adapters/repos.md).
- **Credentials for the `process` executor are undecided.** `_build_env` passes `DEFAULT_PASSTHROUGH`
  plus the profile's own `env:`, and neither `jira-claude-solari.yaml` (`claude-cli`) nor
  `github-codex.yaml` (`codex-cli`) declares an `env:` or `env_passthrough:` — so the spawned CLI
  starts with no `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` and must fall back to whatever credential file
  lives under the passed-through `USERPROFILE`/`HOME`. Decide whether the shipped profiles should
  declare `env: {ANTHROPIC_API_KEY: "${ANTHROPIC_API_KEY}"}` explicitly or whether CLI-managed
  credentials are the intended contract, then say so in the README's env table.
- **`resume` ignores the profile's `runs_dir`.** `cli.py: _cmd_resume` hardcodes `Path("runs")` when
  `--runs-dir` is absent, while `run`/`poll` honour `profile.base_dir / profile.runs_dir`. A profile
  with a custom `runs_dir` cannot be resumed without the flag. The fix needs a two-pass load (the run
  must be loaded before the profile name is known, the profile before its runs dir); the clean shape
  is "if `-c` is given, load the profile first and take its runs dir, else fall back to `runs/`",
  plus a CLI test per branch.
- **Run-lock keys can collide.** `RunLock._path()` keys the lock file on `slugify(item.key)`, which
  lowercases, collapses every non-alphanumeric run to a hyphen and truncates at 40 chars — two
  distinct work items with long, similar keys (or keys differing only in case or punctuation) share
  one lock, and `poll()` silently skips one of them. A short hash suffix of the raw key would fix it,
  but it changes the lock-file naming that `--force-lock` users and existing `runs/.locks/` content
  depend on.

## Smaller known limitations

- `tools.from_wire()` maps EVERY `_` back to `.`; correct only because no catalogue name has an
  underscore inside a segment.
- `build_context()["step"]` is a reserved, always-empty namespace: a `when: "step.<anything>"` can
  never be true. Populating it means passing facts through `extra=`.
- `Orchestrator(interactive=...)` has no CLI flag; only `--pause-at` reaches it.
- `for_each` supports only `plan.sections`.
- `engine/orchestrator.py` is 1144 lines, well past the project's own 350-line guidance, and is the
  obvious candidate for decomposition (adapter wiring / the step loop / the per-role hooks).
