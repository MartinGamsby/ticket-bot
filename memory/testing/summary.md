# Testing conventions

`uv run pytest` from the repo root. 875 passed, 0 skipped. `pyproject.toml` sets
`testpaths = ["tests"]`; the dev extra is `pytest` + `pytest-cov`.

## Hard rules

- **No test hits the network, spawns a real coding CLI, or touches a real Jira / GitHub / Solari
  account.** `tests/fakes.py` stands in wherever an adapter would reach outward; HTTP adapters take
  an injected `httpx.Client`, so `httpx.MockTransport` covers the rest.
- **A test that mutates a git repo builds a throwaway one under `tmp_path`** — the `git_repo` fixture
  in `conftest.py`, which `git init -b main`s a directory, sets identity LOCALLY (never `--global`),
  and commits one file. Never this checkout.
- Per-module behaviour belongs in that module's own `test_*.py`. A bug that only appears when two
  modules meet belongs in `tests/test_seams.py`.

## The fakes — `tests/fakes.py`

`FakeModelProvider` (registered as `model: {type: fake}`, so it is also a production-visible kind),
`FakeExecutor`, `FakeRuntime`, `FakeSource`, `FakeSink`, `FakeRepo`, plus `fake_provider()`,
`text_turn()` and `tool_turn()` helpers.

**A fake that satisfies a contract the real adapter does not will hide a broken seam.** Four blockers
shipped green underneath fakes that were more generous than reality:

- `FakeExecutor` wrote run-dir artifacts with `Path.write_text`, never through the path jail — so the
  jail refusing every run-dir write went unnoticed;
- `FakeExecutor` correctly returned workspace-only `files_written` while the real `ApiLoopExecutor`
  did not, and no test compared the two;
- a `RecordingSink` hid the real `FileSink`'s append behaviour, so `ticket_comment.md` shipped
  doubled;
- nothing drove `source.read` through the real tool layer, so it silently returned `""`.

The rule that follows: **drive the REAL shipped profiles and the REAL adapters at the seams**, and
when a fake models a contract, make it model the same one the real adapter must honour — `FakeExecutor`'s
workspace-only `files_written` IS the contract.

## `tests/test_seams.py`

Owns the joins no single module owns, and nothing else:

- `screenshot_on` from profile to runtime to run dir to sink attachment;
- the source-retirement call the poller has to make, and that a second sweep does not reprocess;
- closing what the engine opened (sinks and sources, including on failure);
- the PR URL reaching the comment and the sinks that need it, in the right order;
- the banner reporting live objects and the `--repo` override rather than config;
- every shipped profile defining the model slots and executor kinds its own pipelines name;
- every registry name importing to a class with `describe()` and satisfying its protocol;
- the planner writing run-dir artifacts the fan-out reads back, and the coder reading the section
  file — the path-jail seam;
- a run-dir artifact never being reported as a workspace write;
- the ingest step being handed real ticket text by `source.read`;
- the file sink and the engine not both leaving a ticket comment.

## Other notable modules

- `tests/test_e2e_offline.py` drives the real `standard` pipeline end to end through the real adapter
  registry: every artifact in `runs/<id>/` is produced, `resume` genuinely skips completed steps, and
  `--dry-run` makes no outward call at all.
- `tests/test_profiles.py` loads every shipped profile and asserts on the LOADED object (that is the
  only way to see what `extends:` brought in), including that no GitHub-repo profile inherits a local
  `repo.path`.
- `tests/test_repo_hygiene.py` is deliberately import-light: no secret-shaped literal in any profile,
  and the docs describe ticketbot.
- `tests/test_builtin_pipelines.py` / `test_builtin_prompts.py` shape-check the shipped YAML and
  prompt files.
- `tests/fixtures/toy-repo/` is the tiny repo the manual smoke run uses;
  `tests/fixtures/prompts/roles/` holds stand-in prompts so prompt tests do not depend on the shipped
  wording.

## When fixing a defect

Mutation-check the test: revert the production fix and confirm the new test fails. Every test added
during the build's review pass was checked that way, so none of them passes for the wrong reason.
