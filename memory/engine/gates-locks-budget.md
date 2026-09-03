# Gates, locks and budget

Three small modules that decide when a run pauses, when it may start, and when it must stop.

## Gates — `engine/gates.py`

`GateDecision(action, comment, transition, unassign)` where action is
`continue` | `block` | `fail` | `await_human`.

```python
gates.on_unclear(run, question)          # comment_and_unassign | comment_only | proceed | fail
gates.on_pr_ready(run)                   # human_review -> await_human;  auto -> continue
gates.on_step_gate(step, run, interactive=...)
```

- `on_unclear` increments `run.extra["clarify_rounds"]` FIRST; once it exceeds
  `gates.max_clarify_rounds` (default 2) the decision is `fail` regardless of the configured mode.
  `comment_and_unassign` (the default) blocks, posts the question and unassigns; `comment_only`
  blocks and posts; `proceed` records and continues; `fail` stops.
- `on_step_gate`: `gate: human` always awaits; `gate: optional_human` awaits only when the step is
  interactive, i.e. `--pause-at <step-id>` names it.
- **`on_pr_ready: auto` never means "merge".** It only means "do not pause the run". No adapter has a
  merge call at all; the reporter step opens the PR (a draft for `repo: github` unless
  `draft_pr: false`) and that is the end of the automated path.

An awaited gate writes `question.md`, sets the run BLOCKED and the step BLOCKED, and stops.

## Locks — `engine/locks.py`

One work item, one run. `runs/.locks/<slug>-<16 hex of sha256(raw key)>.lock` (`lock_filename()`),
created with
`os.open(O_CREAT | O_EXCL | O_WRONLY)` — atomic on Windows and POSIX. The enforcement IS the atomic
creation; the JSON content (`pid`, `host`, `run_id`, `started_at`, `key`) is advisory, for diagnosing
who holds it and for recognising staleness.

```python
lock.is_locked()                       # non-mutating; poll() uses it to skip an item
lock.acquire(run_id, force=False, stale_after_s=21600)
lock.release()                         # only if we own it (matching run_id); never raises
```

`acquire()` on an existing lock raises `LockHeld` whose message names the holder and says whether it
looks stale (older than 6h, or its pid is not alive — `_pid_alive` probes `OpenProcess` on Windows,
`os.kill(pid, 0)` elsewhere). Callers decide whether to retry with `--force-lock`.

**The lock key is the RAW item key, never its slug.** `slugify` lowercases, collapses every run of
non-alphanumerics to `-` and truncates at 40 chars on a word boundary, so `ENG-1` / `eng.1` / `ENG_1`
and any two long keys sharing a 40-char prefix all produce the same string. Sharing one lock file
means `poll()` silently skips the second ticket, with nothing in the log. The digest is over the raw
key, so those cases separate; the slug stays in front so `runs/.locks/` is readable and the name is
ASCII `[a-z0-9-]` with a bounded length. It is a truncated hash, not a bijection — two keys collide
only on a 64-bit SHA-256 prefix collision.

## Budget — `engine/budget.py`

```python
budget.start()
budget.charge(usage)                   # accumulates usage.cost_usd
budget.check(where=step.id)            # raises BudgetExceeded naming the cap and the numbers
budget.step_timeout(requested)         # min(requested, remaining wall clock), floor 30s
```

`budget.max_cost_usd` and `budget.max_wall_clock_s` are guard rails, not billing. Cost comes from
`Usage.cost_usd`, which only the `api` executor can report — a spawned coding CLI's token spend is
not observable, so a `process` executor always charges 0.

`Runtime.timeout_ms` (Solari's rolling IDLE window) is explicitly NOT a substitute: it resets on
every call, so the real deadline lives here.

A tripped cap fails the step and the run (`RunStatus.FAILED`) with the cap named in
`StepResult.error`.
