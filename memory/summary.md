# Project summary

`ticketbot` is a config-driven ticket to PR agent runtime, written in Python (>=3.11, developed on
3.12.7), installed as the `ticketbot` console script. One unit of work — a line of text, a markdown
file, or a Jira issue — enters through a **source**, is run through a YAML-defined **pipeline** of AI
agent roles (ingest, clarify, plan, code, test, review, security, report) over a git **repo**, and
leaves as a pull request, a short ticket comment and screenshots reported through a **sink**. Every
external system is an adapter selected by one `type:` string in a profile and resolved by name at run
time through `ticketbot/core/registry.py`, so swapping Jira for a text file, Claude for an
OpenAI-compatible endpoint, or a coding CLI for the built-in tool loop is a config edit, never a code
change. The engine itself never imports `anthropic` or `httpx`. Everything a run produced lands in
`runs/<id>/`, and `run.json` is rewritten atomically after every step so a crash is resumable.
Nothing auto-merges, anywhere.

## Orientation

| I want to... | Read |
|---|---|
| understand the shape of the whole thing | [architecture/summary.md](architecture/summary.md) |
| add or change an adapter | [architecture/registry.md](architecture/registry.md), [architecture/adapter-protocols.md](architecture/adapter-protocols.md) |
| write or debug a profile | [config/summary.md](config/summary.md) |
| change a pipeline or a role prompt | [pipelines/summary.md](pipelines/summary.md), [pipelines/role-prompts.md](pipelines/role-prompts.md) |
| trace what the run loop does | [engine/summary.md](engine/summary.md) |
| touch the path jail or a tool | [executors/path-jail.md](executors/path-jail.md) |
| run the whole thing with no model or credentials | [executors/summary.md](executors/summary.md) |
| avoid a trap this codebase already hit | [practices.md](practices.md) |
| know what is deliberately unfinished | [known-gaps.md](known-gaps.md) |

## State

Suite: `uv run pytest` from the repo root — **1026 passed, 0 skipped**, 46 test modules. No test
touches the network, a real model, a real coding CLI, or a real Jira/GitHub/Solari account. See
[testing/summary.md](testing/summary.md).

A run needs a model only if its executor uses one. Three do different things:
`stub` (calls nothing — `profiles/file-stub-offline.yaml`, the genuinely offline path),
`process` (spawns a coding CLI that authenticates itself — `file-claude-cli.yaml`, no key), and
`api` (ticketbot's own tool loop against a provider — `file-text-none.yaml`, needs a key).
Seven profiles ship. See [config/summary.md](config/summary.md).

Full index: [memory-map.md](memory-map.md). Vocabulary: [terminology.md](terminology.md).
