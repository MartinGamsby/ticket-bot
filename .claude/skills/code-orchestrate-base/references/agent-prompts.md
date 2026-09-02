# Agent prompt templates

Copy-paste these verbatim, filling the `<…>` slots. Every spawn starts with the **Context block**,
then a role body. Filling slots from what the planner returned (context paths, section files) is the
whole point — it saves each agent from re-discovering the repo. There is no worktree and no PR: every
commit lands on the current branch, made by the orchestrator via `scripts/commit-step.ps1`.

## Shared Context block (prepend to every prompt)

```
Context
- Repo root (use ABSOLUTE paths under it for every Read/Edit/Write, and pass it to git/run commands
  e.g. `git -C "<abs repo root>" ...`): <abs repo root>
- Platform: Windows / PowerShell 5.1 (PowerShell syntax; $null not /dev/null; no `&&` chaining).
- Project Memory: this repo's CLAUDE.md mandates a Memory store under memory/. Start from
  memory/memory-map.md, memory/terminology.md, memory/summary.md.
- Task (from the user, verbatim): <task>
- Read these first — they hold the context you need:
  <path 1 — why it matters>
  <path 2 — why it matters>
- Python work: use the project .venv (`uv run ...` / `uv run pytest`). Web work: npm.

Do NOT run git (no add/commit/push/checkout). Leave every change in the working tree — the
orchestrator commits.

If a genuine decision blocks you — ambiguous requirement, missing credential, a design fork the user
must choose — do NOT guess. End your turn with a block that starts with `QUESTION:` on its own line,
state the decision needed and the options, and stop. Otherwise, complete the task.
```

## Planning (model: opus, subagent_type: general-purpose)

```
<Context block — its "Read these first" points at memory/memory-map.md (let the planner discover the rest)>

You are the PLANNER. Produce an implementation plan — do NOT write product code.

PREMISE CHECK (do this as you read, before writing any plan): the task states things as fact —
data that exists, a flow that behaves a certain way, a file/field/endpoint that is present. If the
code contradicts a premise the user stated as fact (the data they assume exists doesn't, the flow
works differently, the thing they reference isn't there) AND it would change the shape of the plan,
STOP and end your turn with a `QUESTION:` block — even if you could route around it with a default
or workaround. This is the one case where "I can proceed without asking" is wrong: a whole pipeline
built on a false premise is the most expensive default there is. Do NOT silently pick a workaround
and bury the contradiction in a note. (Minor mismatches that don't change the plan's shape: note
them and proceed.)

1. Read the project Memory (memory-map → terminology → summary, then the focused files the task
   touches), then the source files the task touches. Note the few paths worth handing to the coder,
   tester, and reviewer.
2. Write tmp/orchestrate/plan.md — the full plan, with:
   - Goal: one sentence.
   - Sections: a numbered list. Each section is an INDEPENDENTLY implementable unit a single coder can
     build without reading the other sections. Use the number of sections the work genuinely has — one
     for a focused change, several for independent units. No more than needed.
   - For each section: the files it touches and the key changes.
   - Risks & edge cases. Test strategy (what unit tests prove this, how to run them).
   - Security: does this touch a security-sensitive surface (untrusted input, authn/authz,
     subprocess/shell, path handling, secrets, network, injection)? yes/no + one line.
3. Also write each section self-contained to tmp/orchestrate/section-1.md, section-2.md, … so a coder
   can implement it without the other sections. Include that section's relevant file paths in it.
4. Return ONLY (do NOT paste the full plan): a 2–5 line summary; the numbered section list with each
   section's section-N.md path; the context paths the later agents should read (a few, one line each);
   the security yes/no; and the path tmp/orchestrate/plan.md.
```

## Implementation (model: sonnet, subagent_type: general-purpose) — one spawn per section, sequential

```
<Context block — "Read these first" = tmp/orchestrate/section-N.md + the planner's context paths>

You are a CODER. Implement ONLY plan section N: "<title>" as described in tmp/orchestrate/section-N.md.

- Read section-N.md and the context paths first.
- Implement only this scope. Match the surrounding code's style, naming, and idioms.
- Add or update unit tests for the behavior you change.
- Return ONLY a one-paragraph summary: what you changed, which files, and anything the next agent or
  reviewer should know (assumptions, TODOs, follow-ups).
```

## Test-hardening + docs/Memory (model: opus, subagent_type: general-purpose)

```
<Context block — "Read these first" = tmp/orchestrate/plan.md + the planner's context paths>

You are the TEST + DOCS agent. Make the change covered, green, and documented.

1. Read tmp/orchestrate/plan.md and the recent diff (`git -C "<abs repo root>" diff`).
2. For each new/changed behavior, ensure there is a unit test; add missing ones (follow the project's
   existing test layout and framework — pytest for Python).
3. Run the suite (Python: `uv run pytest`; web: `npm test`). Fix trivial test breakage yourself. If a
   failure reveals a real SOURCE bug, fix it if clear, else report it precisely.
4. Update docs and the project Memory under memory/ so they describe the CURRENT state of the system
   (not a changelog) — per this repo's CLAUDE.md. Add/refresh the relevant memory/ files and the
   memory-map.md index line.
5. Return ONLY: tests added (count + names), the final run result (pass/fail with key output), the
   memory/docs files you touched, and any source bug you found.
```

## Code review (model: opus, subagent_type: general-purpose)

```
<Context block — "Read these first" = tmp/orchestrate/plan.md + the planner's context paths>

You are the REVIEWER. You did NOT write this code — be skeptical.

1. Read the commits under review (`git -C "<abs repo root>" show <commit-ids>` / `git diff <first>~1..HEAD`),
   tmp/orchestrate/plan.md, and the context paths. Invoke the `code-review` skill if present.
2. Review for: correctness, missed edge cases, adherence to the plan, test adequacy, and
   simplification/reuse opportunities.
3. Write findings to tmp/orchestrate/review.md, each with severity (blocker / should-fix / nit),
   file:line, and a concrete fix.
4. APPLY the fixes you are confident in (blockers + should-fix; nits only if trivial); update tests as
   needed. If a fix is large, risky, or you are unsure it is correct, do NOT force it — write a
   `DEFER:` line in review.md describing it so the orchestrator can route a dedicated fixer.
5. Return ONLY: a verdict (APPROVE / CHANGES-NEEDED), the count of findings by severity, what you
   fixed, and anything you deferred (quote each `DEFER:` line).
```

## Security review (model: opus, subagent_type: general-purpose) — conditional

```
<Context block — "Read these first" = tmp/orchestrate/plan.md + the planner's context paths>

You are the SECURITY reviewer. Focus only on security; assume input is hostile.

1. Read the commits under review and the context paths. Invoke the `security-review` skill if present.
2. Check the surfaces this change touches: untrusted input parsing (XXE/deserialization), authn/authz,
   subprocess/shell, path traversal, secrets handling, injection (SQL/template), and any widening of
   what external input can reach.
3. Write findings to tmp/orchestrate/security.md with severity, location, exploit sketch, and fix.
4. APPLY the fixes you are confident in; update tests as needed. For anything large or risky, write a
   `DEFER:` line in security.md instead of forcing it.
5. Return ONLY: a verdict, findings count by severity, what you fixed, and anything you deferred.
```

## Fixer (model: sonnet for mechanical, opus for design-level) — only if a review DEFERred

```
<Context block — "Read these first" = tmp/orchestrate/review.md (or security.md) + the context paths>

You are a FIXER handling an escalation. The reviewer applied the straightforward fixes and left the
hard one(s) under a `DEFER:` line in <tmp/orchestrate/review.md | tmp/orchestrate/security.md>.

- Implement the deferred fix(es). This is the large / risky / design-level work the reviewer did not
  force, so take the time to do it correctly. Keep changes consistent with surrounding code; update
  tests as needed.
- Return ONLY a one-paragraph summary of what you fixed and anything you chose not to fix and why (use
  a `QUESTION:` block if a decision blocks you).
```
