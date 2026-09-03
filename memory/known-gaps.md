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

- **The parent-clone drift check is baseline-relative, not authoritative.** `GitLocalRepo.drifted()`
  compares the parent clone's `git status` against a snapshot taken at `checkout()`, so it reports
  only what appeared DURING the run. That is what makes it false-positive-free, but it also means a
  human editing the parent clone while a run is in flight will fail the next `commit:` step. That is
  out of contract (the engine assumes it owns the clone for the duration) and the error names the
  paths, so it is diagnosable — but it is the one way this check can be wrong.
  See [adapters/repos.md](adapters/repos.md).
- **The per-step tool allowlist binds the `api` executor only.** `ProcessExecutor` spawns a coding
  CLI that brings its own tools and never reads `ExecRequest.tools`, so under `jira-claude-solari.yaml`
  and `github-codex.yaml` (both default to a `process` kind) the `clarify` step's
  `tools: [sink.comment, sink.unassign]` and the `review`/`security` steps' absence of `shell.run`
  enforce nothing — every role gets a full CLI on a prompt built from untrusted ticket text.
  Containment there is whatever the spawned CLI does for itself. Closing it means mapping catalogue
  names onto per-CLI permission flags (`--allowedTools`, sandbox modes), which is CLI-specific and a
  real design decision, not a refactor. See [executors/tools.md](executors/tools.md).
- **`DEFAULT_PASSTHROUGH` forwards the D-Bus session to every spawned CLI.**
  `DBUS_SESSION_BUS_ADDRESS` + `XDG_RUNTIME_DIR` are what make a Linux Secret Service keyring
  reachable at all — without them a keyring-authenticated CLI starts unauthenticated, which is why
  they are there. They are also not "non-secret locators" in the sense the rest of that list is: they
  are a capability handle to the user's whole UNLOCKED keyring, so a prompt-injected CLI can read
  every secret in it, not just its own entry. Moving them out of the default into a per-profile
  `env_passthrough:` — the same "visible, per-profile decision" rule already applied to API keys —
  would close it at the cost of Linux keyring auth. Needs an explicit call on whether that path is
  supported; do not flip it silently either way.
- **`env_passthrough:` on the `local_shell` runtime does not register secrets.**
  `ProcessExecutor._build_env` `register_secret()`s a forwarded name that reads like a credential;
  `LocalShellRuntime._build_env` does not. It runs the project's own `shell.run` commands rather than
  a credential-consuming CLI, so nothing ships that forwards a secret to it — but the two `_build_env`
  implementations are now deliberately different, and that is worth knowing before copying either.

## Smaller known limitations

- `tools.from_wire()` maps EVERY `_` back to `.`; correct only because no catalogue name has an
  underscore inside a segment.
- `build_context()["step"]` is a reserved, always-empty namespace: a `when: "step.<anything>"` can
  never be true. Populating it means passing facts through `extra=`.
- `Orchestrator(interactive=...)` has no CLI flag; only `--pause-at` reaches it.
- `for_each` supports only `plan.sections`.
- Two files are past the project's own 350-line guidance: `engine/orchestrator.py` (~1200 lines —
  the obvious candidate for decomposition into adapter wiring / the step loop / the per-role hooks)
  and `adapters/repos/git_local.py` (~400; branch naming + worktree lifecycle + the landing checks
  are the natural seams).
