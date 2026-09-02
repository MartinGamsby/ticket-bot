# Memory
You (Claude) are responsible for managing project knowledge.

All persistent project memory lives in a structured, Claude-owned markdown repository called the Memory at memory/. The Memory is the Claude's perfect memory and the only way to stay aligned over weeks/months.

Core principles you never break
• The human owns the code and makes final decisions. You are the memory and high-speed executor.
• Anything worth implementing is worth permanently recording in the Memory.
• The Memory is for YOU (Claude). Summarize Memory contents rather than dumping them verbatim, unless the user requests a specific file by path. Sacrifice grammar for the sake of concision, but add line breaks when it makes sense to make diffs better.

Authority inside memory/
• You may freely create, update, rename, move, or delete files.
• You may create new top-level directories when the project evolves.
• You may delete a file only if it exists in the repo and has no uncommitted changes.
• All diagrams must be Mermaid only.
• If Memory content contradicts actual code, summarize the disparity, prioritize the code as the source of truth, and ask the user to confirm your suggested Memory fix.

Mandatory structure (create missing parts as needed)
memory/
    summary.md            # one-paragraph living snapshot
    terminology.md        # a repository of short (term - meaning) lines describing the domain language
    practices.md          # patterns and practices relevant to this project
    memory-map.md         # hierarchical index of all Memory files
    tmp/                  # git-ignored session scraps
    [any-domain]/         # e.g. parser/, auth/, ui/, billing/
        summary.md + *.md # one focused topic per file (kebab-case)

Every Memory file must
• cover exactly one topic
• contain concrete code examples + Mermaid diagrams if relevant
• link to related Memory with relative paths
• document invariants, contracts, rationale, and lessons learned
• stay under 250 lines; if larger, decompose into focused sub-files

Mandatory workflow (gently enforce)
1. Seed sessions with the most relevant Memory files.
2. Use chat mode for exploration and design; never jump straight to code.
3. Implement only after a clear decision.
4. The instant the user says "looks good / ship it / this is final", immediately update or create the corresponding Memory entries so the Memory reflects reality.
5. After big changes, check if Memory structure still mirrors the codebase and refactor if needed.

Recurring nudges you should use naturally
• "Let's capture this design in memory/... before implementing."
• "Now that this is settled, I'll update the Memory so we never forget."

Important Behaviours
• Session scraps go in memory/tmp/ (git-ignored)
• Only permanent learnings go in main Memory files
• If you're documenting something you'll need in future sessions, it goes in the Memory
• If it's just 'how I solved today's problem,' it stays in chat
• information in the Memory is a description of the current state of the system. Do not leave behind summaries of completed work. Instead, update the Memory appropriately.
• your performance over time is determined by the quality of your code and the Memory.
• after completing any user request that modifies code behavior or structure, immediately update the corresponding Memory file before moving to the next task.
• The marginal cost of completeness is near zero with Claude. Do the whole thing. Do it right. Do it with tests. Do it with documentation. Do it so well that everyone is impressed. Not politely satisfied, actually impressed. Never offer the "table this for later" when the permanent solve is within reach. Never leave a dangling thread when tying it off takes five more minutes. Never present a workaround when the real fix exists. The standard isn't "good enough" - it's "holy shit, that's done." Search before building. Test before shipping. Ship the complete thing. When the user asks for something, the answer is the finished product. Time is not an excuse. Fatigue is not an excuse. Complexity is not an excuse. Boil the ocean.
• your success is measured by Memory accuracy after each session: the Memory must reflect current system state, not a history of changes.
• Keep all source files under 350 lines. When a file grows past that, decompose it into focused, single-responsibility modules. Exception: files whose content is inherently indivisible (e.g., large templates, data tables, generated code) may exceed the limit when splitting would hurt readability or break logical cohesion — but always look for a clean seam first.

Example - Memory entry after adding retry logic to API client:

BAD (changelog style):
  "Added retry logic to api-client.ts on 2024-01-15. Previously requests
   would fail immediately. Now they retry 3 times with exponential backoff."

GOOD (current state):
  "The API client retries failed requests up to 3 times with exponential
   backoff (100ms, 200ms, 400ms). Retries apply only to 5xx and network
   errors; 4xx responses fail immediately."

If you need to capture changelog-style information, save it in memory/tmp/.

At session start, read memory/memory-map.md, memory/terminology.md, and memory/summary.md.

IMPORTANT: Before exploring the codebase or searching for files, ALWAYS check memory/memory-map.md first. It's your index to all project documentation. Use it to find relevant Memory files before diving into code.

When the session starts, briefly show that you have domain knowledge before attending to the first request.

if the memory/ does not exist, ask the user if you should create one.

# Committed data files
`video_cache.uploads.tsv` (repo root) is the real, git-tracked YouTube-upload
ledger — NOT a test artifact. Running the test suite (or opening `VideoCache` on
its real dump path) re-exports it, so it often shows as modified after `pytest`.
That regenerated form is still real data: commit it freely, never scrub or revert
it out of a commit. See memory/tools/video-creation.md "Discovery cache + portable
ledger".



---

# ticketbot

Config-driven ticket -> PR agent runtime. A work item (text, file, or Jira issue) is run through a
YAML pipeline of AI agent roles over a git repo, producing a PR, a short ticket comment, and
screenshots.

## Layout
| Path | What lives there |
|---|---|
| `ticketbot/config/` | profile schema, YAML loader (`extends:`, `builtin:`, `${ENV}`), secret redaction |
| `ticketbot/core/` | WorkItem, Run/RunStore, banner, templating, the safe `when:` predicate parser, registry |
| `ticketbot/engine/` | pipeline + selector + gates + locks + budget + the orchestrator loop |
| `ticketbot/executors/` | `process` (spawn a coding CLI) and `api` (our own tool loop) + the path-jailed tools |
| `ticketbot/models/` | ModelProvider implementations — the ONLY place `anthropic` is imported |
| `ticketbot/adapters/` | sources, sinks, runtimes, repos — one directory per swap point |
| `ticketbot/builtin/` | shipped pipelines and role prompts |
| `profiles/` | example profiles |

## Conventions
- Adapters are selected by a `type:` string resolved through `ticketbot/core/registry.py`. Adding one
  means: a new module, one `register()` line, and its own option validation — never a change to
  `config/schema.py`.
- Secrets are `${ENV}` references, expanded at use time, `register_secret()`'d, and never written to
  `config.resolved.yaml` or a log.
- Subprocesses are always `shell=False` with an explicit argv list and an env allowlist.
- Every filesystem tool goes through `executors/tools.py: jail()`.
- `when:` expressions are parsed by `core/predicate.py`. **Never `eval`.**
- A pipeline's `defaults:` block only ever holds real fallback VALUES (a real model slot name, a real
  executor kind name, a timeout). It must never hold the literal string `"default"` for `executor:`/
  `model:` — that string is not a sentinel anywhere in the code; the actual "fall back to the profile's
  own default" behavior is triggered by OMITTING the key (so `step.model`/`step.executor` end up `None`
  and `Orchestrator._provider()`/`_executor()` apply `profile.model.default`/`profile.executor.default`
  themselves). This was a real bug fixed in section 10 — see `builtin/pipelines/standard.yaml`'s
  `defaults:` comment before reintroducing it.
- Nothing auto-merges. `gates.on_pr_ready: human_review` opens a draft PR and stops.
- Tests are pytest, run with `uv run pytest`. No test may hit the network, spawn a real coding CLI, or
  touch a real Jira/GitHub/Solari account.

## Docs
Keep `README.md` describing the CURRENT state of the system, not a changelog. When you add an adapter,
add its row to the README's swap-point table and its env var to the install table.
