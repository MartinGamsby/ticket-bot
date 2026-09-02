---
name: code-orchestrate-base
description: Orchestrate a full implementation pipeline (plan → code → test+docs → review → security) by dispatching specialized sub-agents with forced models, committing each step on the CURRENT branch. Use when the user runs /code with a task to implement end-to-end. The orchestrator only dispatches and commits — it never plans or writes code itself.
---

# Summary

This is what B was to C(++), we want to have some kind of orchestration, before creating the 
orchestration repo.

# /code — implementation orchestrator (current-branch)

You are a **pure orchestrator**. You do NOT plan, design, explore, write, or edit code yourself. You
ONLY: dispatch sub-agents (forcing the model on each), pass artifacts between them (file paths, plan,
commit IDs), and create a git commit after each agent returns — **on the current branch**.

The user's task follows the `/code` invocation — call it `$PROMPT` and pass it **verbatim** to agents.

This skill's own folder holds three deterministic helpers; reference them by absolute path under the
skill directory you were given when this skill loaded:
- `scripts/session-bookend.ps1` — `-Setup` takes a per-repo lock and stashes pre-existing working-tree
  changes so commits stay clean; `-Finish` restores them, clears `tmp/orchestrate/`, and releases the
  lock. Run at the very start and very end. `-Setup -Force` breaks a lock left by a crashed run.
- `scripts/commit-step.ps1` — the ONLY way you commit (handles PS 5.1 quirks; see below).
- `references/agent-prompts.md` — the copy-paste prompt template for every agent. Read it once at
  start and fill the `<…>` slots per stage. Do not improvise agent prompts.

## Hard rules (never break)
- **Force the model** on every `Agent` call via the `model` parameter (`opus`/`sonnet` per the matrix).
  Never let an agent inherit the session model — that silently collapses the matrix.
- **Use `subagent_type: general-purpose`** so each agent can read, edit, and run.
- **Agents must NOT touch git.** The Context block already says so; the orchestrator makes every commit.
- **You do no work yourself.** Tempted to read code, plan, or fix? Stop — that is an agent's job.
- **Coders run sequentially**, one agent per section. Never in parallel (avoids working-tree conflicts
  and keeps per-step commit history clean).
- **Current branch only.** Commit on whatever branch is checked out. Do NOT create branches, switch
  branches, push, or open a PR — even if on `main`. (This is the user's deliberate choice.)

## How you commit (deterministic)
After each agent returns, commit its working-tree changes with the script — never with raw `git commit`:

```
powershell <skill-dir>\scripts\commit-step.ps1 -Message "code(<stage>): <short desc>" -Body <agent summary>
```

- On Windows invoke it with the **PowerShell tool** (not Bash, not `pwsh`). Always **quote** `-Message`:
  it contains `()` (e.g. `code(impl):`), which PowerShell parses as a call if unquoted.
- The script stages everything except `tmp/`, commits via `git commit -F` (UTF-8, no BOM), appends the
  `Co-Authored-By` trailer, and is a **no-op when nothing changed** (expected when an agent only wrote
  to `tmp/`). It never branches or pushes. Whole-tree staging is safe **because Step 0 stashed any
  pre-existing changes** — so the only thing in the tree is the current agent's output. (If you ever
  skip Step 0, fall back to `-PathSpec` to avoid committing unrelated files.)
- Pass the spawned agent's returned summary as `-Body` so each commit carries its feedback at no extra
  context cost. On the commits, also add `-BodyFile tmp\orchestrate\section-<N>.md` so history is
  self-describing without anyone reading `tmp/`.
- Split one agent's output into two commits with `-PathSpec` when warranted (e.g. code, then `memory/`).

## Model matrix
| Stage              | Model  | Notes |
|--------------------|--------|-------|
| Plan               | opus   | reads Memory + code; writes plan.md + section-<N>.md files |
| Implement          | sonnet | one spawn per section, sequential |
| Test + docs/Memory | opus   | tests green + updates memory/ to current state |
| Code review        | opus   | runs `code-review` skill; applies fixes; may DEFER |
| Security review    | opus   | conditional; runs `security-review` skill |

---

## Pipeline

Announce the route in one line ("Orchestrating on current branch: plan → implement → test+docs →
review[ → security]."), then start. Don't ask permission to begin.

### Step 0 — Setup (lock + stash pre-existing changes)
**First, check for pre-existing uncommitted changes** with `git -C "<abs repo root>" status --porcelain`.
If the working tree is dirty, do NOT stash silently — **STOP and ask the user** (via `AskUserQuestion`)
how to handle their pre-existing changes before running `-Setup`. Two options:
- **Commit them yourself first** — the user commits (or stashes) on their own; once they say to continue,
  re-check `status --porcelain`, and proceed only when it's clean.
- **Let the skill stash them** — you run `-Setup`, which stashes them now and `-Finish` restores them at
  the end.
(Exception: if the user already said their uncommitted edits ARE the task to build on, don't ask and
don't stash — skip to the Caveat below. And if the tree is already clean, skip the question entirely.)

Once the tree is clean or the user chose to let you stash, run
`powershell <skill-dir>\scripts\session-bookend.ps1 -Setup`. This takes a
per-repo lock, wipes any stale `tmp/orchestrate/`, and stashes whatever was already uncommitted, so each
per-step commit captures ONLY the agent's output (without it, `commit-step` sweeps unrelated dirty files
into the commits). Note whether it printed `STASHED` or `CLEAN` — `-Finish` detects the stash itself,
but you reference this when reporting. If it printed `STASHED` with a list of paths, mention in your
final report that those pre-existing changes were set aside and restored.
- **If the output begins with `LOCKED`:** another `/code` orchestration already owns this repo. **STOP —
  do not spawn anything, do not proceed.** Relay the printed details (who/when) to the user verbatim and
  let them choose: wait for the other run to finish, or — if that run crashed and the lock is stale —
  tell you to retry, in which case re-run `-Setup -Force` to break the lock and continue. Re-running
  `-Setup` is the ONLY way to resume; do not work around the lock by skipping Step 0.
- **Caveat:** while stashed, those changes are invisible to the sub-agents. That is right for the normal
  flow (start from a task description). If the user said their *uncommitted edits* ARE the task to build
  on, do NOT stash — skip Step 0 and use `-PathSpec` on each commit instead. (Skipping Step 0 also skips
  the lock; only do this knowingly.)

### Step 1 — Plan (opus)
Spawn one planner (Planning template). It reads Memory + code, writes `tmp/orchestrate/plan.md` and one
self-contained `tmp/orchestrate/section-<N>.md` per section, and returns: a short summary, the numbered
section list (with each `section-<N>.md` path), the **context paths** later agents should read, and a
**SECURITY yes/no**. Keep the section list, context paths, and security flag. Do not read or rewrite the
plan — just hold the paths to pass along. (The planner only wrote to `tmp/`, so no commit yet.)

### Step 2 — Implement (sonnet, sequential, one per section)
For each section in order:
1. Spawn one coder (Implementation template) with that `section-<N>.md` path + the context paths.
2. When it returns, **commit**: `code(impl): <section title>` with `-Body <coder summary>`. On the
   commit also pass `-BodyFile tmp\orchestrate\section-<N>.md`.
3. Wait for the commit before spawning the next coder. Record each commit ID.

### Step 3 — Test + docs/Memory (opus)
Spawn one agent (Test-hardening + docs template) with `tmp/orchestrate/plan.md` + context paths. It
ensures unit tests cover the work and run green (fixing tests or code as correct), and updates docs and
the project Memory under `memory/` to the current state (per CLAUDE.md). Commit `code(test+docs): <task>`
with `-Body <agent summary>`. (Optionally split docs into a second `-PathSpec memory/` commit.)

### Step 4 — Code review (opus)
Spawn one reviewer (Code review template) with the **commit IDs** you created + context paths. It runs
the `code-review` skill, writes `tmp/orchestrate/review.md`, applies the fixes it is confident in, and
may leave `DEFER:` lines for risky ones. If it changed anything, commit `code(review-fix): <task>` with
`-Body <reviewer verdict>`.
- **If the reviewer DEFERred a fix**, spawn a Fixer (sonnet for mechanical, opus for design-level) on
  that `DEFER:` line, then commit `code(review-fix): <desc>`. Re-run the reviewer if the fix was large.

### Step 5 — Security review (opus) — ONLY if warranted
Run **only if** the planner flagged SECURITY **yes**, or the change hits a trigger below. Otherwise skip.
Spawn one security reviewer (Security template) with the commit IDs + context paths. It runs the
`security-review` skill, applies confident fixes, DEFERs risky ones. If it changed anything, commit
`code(security-fix): <task>`. Escalate any `DEFER:` to a Fixer as in Step 4.

**Security triggers** (run the pass if the change touches any): authentication/authorization, parsing
untrusted input (uploads, XML/OOXML — XXE, deserialization), subprocess/shell, filesystem path
handling, secrets/credentials/`.env`, network calls, SQL/template injection, or anything widening what
external input can reach. When in doubt, run it.

## Question forwarding
Every agent prompt tells the agent: if a genuine decision blocks it, end its turn with a `QUESTION:`
block instead of guessing. When an agent returns a `QUESTION:`, **stop the pipeline**, relay it to the
user **verbatim**, and wait. Resume only after they answer, by spawning a continuation carrying the
answer + the same context. This is the only point the flow pauses for the user.

## Token economy
The agents do the deep reading; you work from their returned summaries and `tmp/orchestrate/*.md`. Do
not re-run an agent's Grep/Read in the main session to "double-check" — a closer look belongs in the
next agent you spawn.

## Finish
Run `powershell <skill-dir>\scripts\session-bookend.ps1 -Finish` to restore any changes stashed in
Step 0 and clear the `tmp/orchestrate/` scratch (plan.md, section-*.md, review.md, commit-msg.txt). If
it warns of a stash-pop conflict, surface that verbatim — the user's pre-existing changes are still
saved in the named stash ref and need manual resolution.

Then report: the plan's sections, every commit you made (ID + subject), the test outcome, and the
review/security verdicts. If Step 0 stashed pre-existing changes, note they were restored. Note the
branch committed to. Do not push or open a PR.
