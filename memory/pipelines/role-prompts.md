# Role prompts and the escalation protocol

## Where they live

`ticketbot/builtin/prompts/roles/<role>.md`, resolved as `builtin:prompts/roles/<role>.md` unless a
step sets `prompt:`. Nine roles ship: `ingest`, `clarifier`, `planner`, `coder`, `tester`,
`reviewer`, `security`, `reporter`, `fixer`.

Each file may open with `---`-delimited front matter whose `system:` key becomes the system prompt;
without it (the shipped state) the default is used:

> You are a careful, precise software engineering agent operating as one step of an automated
> ticket-to-PR pipeline.

Malformed front matter falls back to the default rather than failing the step.

## Rendering

`core/templating.py: render(template, values)` — brace-safe, deliberately not `str.format` (prompts
carry literal braces in JSON bodies and code fences):

- `{name}` and `{a.b.c}` are looked up by dotted path through dicts and objects;
- `{{` -> `{`, `}}` -> `}`;
- an UNKNOWN placeholder is left EXACTLY as written — never blanked, never an exception;
- `None` renders as `""`, a list as `", ".join(...)`, a `Path` as `str(path)`;
- substituted text is never rescanned.

Values come from `engine/context.py: prompt_values()`:

| Group | Keys |
|---|---|
| location | `workspace`, `repo_root`, `run_dir`, `platform`, `python_note` |
| ticket | `task`, `ticket_key`, `ticket_title`, `ticket_type`, `ticket_points`, `ticket_url`, `ticket_description`, `ticket_acceptance`, `ticket_labels`, `ticket_comments` |
| step | `step_id`, `role`, `banner`, `context_paths` |
| artifacts (absolute run-dir paths) | `plan_file`, `sections_dir`, `section_file`, `section_title`, `section_index`, `section_count`, `diff`, `test_report`, `review_file`, `security_file` |
| result | `pr_url`, `screenshots` |
| fixed text | `question_protocol`, `git_prohibition` |

`question_protocol` and `git_prohibition` are single constants in `engine/context.py`, embedded
verbatim by every role prompt so the wording cannot drift between roles. `git_prohibition` is what
keeps agents from committing: **the orchestrator commits, the agent leaves changes in the working
tree.**

The artifact placeholders render as ABSOLUTE run-dir paths. That is why the path jail must admit the
run dir — see [../executors/path-jail.md](../executors/path-jail.md).

## The `Return ONLY:` contract

Every role prompt ends with one. That returned text is what lands in `runs/<id>/steps/<id>.md` and in
the step's commit body (after `strip_protocol()`).

| Role | Returns |
|---|---|
| `ingest` | a JSON object: `summary`, `acceptance`, `ambiguity`, `size`, `missing` — parsed by `_after_ingest` (a fenced block is unwrapped first) to fill `item.acceptance` and `item.ambiguity` |
| `clarifier` | a `QUESTION:` block for the ticket |
| `planner` | `plan.md` + one `sections/section-N.md` per unit of work, a premise check that can raise a `QUESTION:` before any code is touched, and a `Security: yes|no` line in the shape `_PLAN_SECURITY_RE` expects |
| `coder` | one section implemented; a one-paragraph summary |
| `tester` | tests added, the suite result, `test-report.md`, docs updated to describe the CURRENT state |
| `reviewer` | `review.md`: verdict APPROVE / CHANGES-NEEDED, findings by severity, fixes applied, `DEFER:` lines for what it would not force |
| `security` | `security.md`: untrusted input, authn/authz, subprocess/shell, path handling, secrets, injection |
| `reporter` | `pr.md` (full) and `ticket_comment.md` (short) — two DIFFERENT write-ups, plus a `PR: {pr_url}` line it cannot fill yet |
| `fixer` | one `DEFER:` line implemented; spawned per defer, capped at 2 per step |

## `QUESTION:` / `DEFER:` — `engine/protocol.py`

A marker is recognised only on a line whose STRIPPED form starts with it, and only OUTSIDE a fenced
code block, so a step quoting example output does not trip escalation.

```python
parse_question(text)  # the first QUESTION: line and everything after it, or None
parse_defers(text)    # the payload of every DEFER: line, in order, empties dropped
strip_protocol(text)  # text with the QUESTION: block removed; DEFER: lines KEPT
has_question(text)
```

`finish_result()` in `executors/base.py` fills `ExecResult.question`/`.defers` from these, so both
executors get the behaviour identically. What the engine then does with them is
`pipeline.on_question` / `pipeline.on_defer`; see [../engine/summary.md](../engine/summary.md).

## Ticket-comment templates

`ticketbot/builtin/prompts/comments/{clarify,blocked,done}.md` are plain `{placeholder}` templates
with no model involved. They ship and are shape-tested, but **nothing renders them yet** — see
[../known-gaps.md](../known-gaps.md) before assuming they are live.
