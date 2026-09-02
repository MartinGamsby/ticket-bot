"""`Orchestrator` -- the run loop, end to end, against a real `tmp_path` git repo
(`git_repo` fixture), a `FileSource` fed from `--input-text`, a `FakeSink`,
`NoneRuntime` and a `FakeExecutor`. No network, no subprocess coding CLI, no
real model call anywhere in this file.

`Orchestrator._executor` and `Orchestrator._build_sink` are explicitly called
out as test seams in `orchestrator.py`'s own docstrings; these tests replace
them with a `FakeExecutor`/`FakeSink` by plain attribute assignment on the
instance rather than touching the adapter registries.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from ticketbot.config.schema import Profile
from ticketbot.core.run import RunStatus, RunStore, StepStatus
from ticketbot.core.workitem import slugify
from ticketbot.engine.locks import LockHeld, RunLock
from ticketbot.engine.orchestrator import Orchestrator
from ticketbot.executors.base import ExecResult
from ticketbot.models.base import Usage
from tests.fakes import FakeExecutor, FakeSink

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"

HAPPY_TEXT = """---
points: 3
type: Story
acceptance: "Given X, when Y, then Z"
---
# Add a /health endpoint

Add a simple health check endpoint.
"""

# No front matter at all -> acceptance is empty -> the clarify step's `when:`
# holds and it actually runs.
UNCLEAR_TEXT = """# Add a mystery feature

Do the thing, somehow.
"""


def _profile_dict(repo_path: Path, *, gates: dict | None = None, budget: dict | None = None) -> dict:
    return {
        "name": "test-profile",
        "source": {"type": "file"},
        "sink": {"type": "file"},
        "repo": {"type": "git_local", "path": str(repo_path)},
        "model": {
            "default": "default",
            "providers": {"default": {"type": "fake", "name": "test-model"}},
        },
        "executor": {
            "default": "default",
            "kinds": {"default": {"type": "api", "model": "default"}},
        },
        "runtime": {"type": "none", "screenshot_on": ["verify"]},
        "pipeline_selector": {
            "rules": [{"when": {"story_points": {"lte": 5}}, "use": "pipelines/tiny.yaml"}],
            "default": "pipelines/tiny.yaml",
        },
        "gates": gates
        or {"on_unclear": "comment_and_unassign", "on_pr_ready": "auto", "max_clarify_rounds": 2},
        "budget": budget or {},
    }


def _make_profile(repo_path: Path, **kw) -> Profile:
    profile = Profile.model_validate(_profile_dict(repo_path, **kw))
    profile.base_dir = FIXTURE_DIR
    return profile


def _make_orchestrator(
    profile: Profile, runs_dir: Path, executor: FakeExecutor, sink: FakeSink, **kw
) -> Orchestrator:
    orch = Orchestrator(profile, runs_dir=runs_dir, **kw)
    orch._executor = lambda kind=None, step=None: executor  # type: ignore[method-assign]
    orch._build_sink = lambda run_dir: sink  # type: ignore[method-assign]
    return orch


def _plan_artifact_writes(n_sections: int = 1, security: str = "no") -> dict:
    entries = [("plan.md", f"# Plan\n\nSecurity: {security}\n")]
    for i in range(1, n_sections + 1):
        entries.append((f"sections/section-{i}.md", f"# Section {i} title\n\nDo thing {i}.\n"))
    return {"plan": entries}


def _basic_executor(n_sections: int = 1, **kw) -> FakeExecutor:
    return FakeExecutor(
        artifact_writes=_plan_artifact_writes(n_sections),
        writes={"implement": [("src/impl.py", "print('hello')\n")]},
        **kw,
    )


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #


def test_happy_run_produces_artifacts_and_ends_done(tmp_path: Path, git_repo: Path, caplog) -> None:
    profile = _make_profile(git_repo)
    runs_dir = tmp_path / "runs"
    fake_sink = FakeSink()
    executor = _basic_executor()
    orch = _make_orchestrator(profile, runs_dir, executor, fake_sink)

    caplog.set_level(logging.WARNING, logger="ticketbot.engine.orchestrator")
    run = orch.run_once(input_text=HAPPY_TEXT)

    assert run.status == RunStatus.DONE
    run_dir = runs_dir / run.id

    # top-level artifacts
    assert (run_dir / "banner.txt").is_file()
    assert (run_dir / "config.resolved.yaml").is_file()
    assert (run_dir / "run.json").is_file()
    assert (run_dir / "workitem.json").is_file()

    # per-step artifacts for every step that actually ran
    for step_id in ("intake", "plan", "implement", "verify", "review", "publish"):
        assert (run_dir / "steps" / f"{step_id}.md").is_file(), step_id

    # `when:` false marks a step SKIPPED and the run still completes
    assert run.steps["clarify"].status == StepStatus.SKIPPED
    assert run.steps["clarify"].text.startswith("skipped: when")
    assert run.steps["security"].status == StepStatus.SKIPPED

    # a step only ever gets the tools its pipeline entry allowlists, unchanged
    by_step = {r.step_id: r for r in executor.requests}
    assert by_step["implement"].tools == ["fs.read", "fs.write", "fs.edit", "shell.run"]
    assert by_step["review"].tools == ["fs.read", "fs.edit"]
    assert by_step["intake"].tools == ["source.read"]

    # screenshot_on: [verify] with NoneRuntime writes no PNG and does not fail
    assert run.extra.get("screenshots", []) == []
    assert not (run_dir / "screenshots").exists()

    # the lock is released on the success path
    lock_path = runs_dir / ".locks" / f"{slugify(run.work_item_key)}.lock"
    assert not lock_path.exists()

    # a declared artifact the step did not create is logged as a warning
    assert "test-report.md" in caplog.text
    assert "review.md" in caplog.text

    # banner contains source, pipeline (+ rule reason), models, executor, runtime, repo
    banner_text = (run_dir / "banner.txt").read_text(encoding="utf-8")
    assert "Using source=file" in banner_text
    assert "Add a /health endpoint" in banner_text
    assert "3 points, Story" in banner_text
    assert "pipeline=pipelines/tiny.yaml  (rule: story_points <= 5)" in banner_text
    assert "models=" in banner_text and "ingest:" in banner_text
    assert "executor=fake executor" in banner_text
    assert "runtime=none" in banner_text
    assert str(git_repo) in banner_text and " @ " in banner_text

    # the reporter step (publish) reported through the sink, never merges
    assert len(fake_sink.comments) == 1
    assert fake_sink.transitions == [(run.work_item_key, "In Review")]
    assert fake_sink.links == []  # git_local never returns a PR url


def test_run_json_rewritten_after_every_step_via_induced_mid_run_failure(
    tmp_path: Path, git_repo: Path
) -> None:
    profile = _make_profile(git_repo)
    runs_dir = tmp_path / "runs"
    fake_sink = FakeSink()
    executor = _basic_executor(results={"verify": ExecResult(text="ok", error="boom: tests failed")})
    orch = _make_orchestrator(profile, runs_dir, executor, fake_sink)

    run = orch.run_once(input_text=HAPPY_TEXT)

    assert run.status == RunStatus.FAILED
    assert run.steps["verify"].status == StepStatus.FAILED
    assert run.steps["verify"].error == "boom: tests failed"

    # steps before the failure were persisted incrementally as they completed,
    # not only bundled into one write at the very end of the run.
    reloaded = RunStore(runs_dir).load(run.id)
    assert reloaded.steps["intake"].status == StepStatus.OK
    assert reloaded.steps["plan"].status == StepStatus.OK
    assert reloaded.steps["implement"].status == StepStatus.OK
    assert reloaded.steps["verify"].status == StepStatus.FAILED
    assert "review" not in reloaded.steps
    assert "publish" not in reloaded.steps

    executed_ids = {r.step_id for r in executor.requests}
    assert "review" not in executed_ids
    assert "publish" not in executed_ids


# --------------------------------------------------------------------------- #
# for_each fan-out
# --------------------------------------------------------------------------- #


def test_for_each_plan_sections_spawns_one_execution_per_section_in_numeric_order(
    tmp_path: Path, git_repo: Path
) -> None:
    profile = _make_profile(git_repo)
    runs_dir = tmp_path / "runs"
    fake_sink = FakeSink()
    executor = _basic_executor(n_sections=11)
    orch = _make_orchestrator(profile, runs_dir, executor, fake_sink)

    run = orch.run_once(input_text=HAPPY_TEXT)

    assert run.status == RunStatus.DONE
    implement_reqs = [r for r in executor.requests if r.step_id == "implement"]
    assert len(implement_reqs) == 11

    indices = []
    for req in implement_reqs:
        m = re.search(r"Implement section (\d+)/11", req.prompt)
        assert m is not None, req.prompt
        indices.append(int(m.group(1)))
    # section-10/section-11 must sort after section-2..9 -- numeric, not lexicographic
    assert indices == list(range(1, 12))


def test_implement_files_written_outside_workspace_fails_step_and_does_not_commit(
    tmp_path: Path, git_repo: Path
) -> None:
    profile = _make_profile(git_repo)
    runs_dir = tmp_path / "runs"
    fake_sink = FakeSink()
    outside = tmp_path / "outside" / "evil.py"
    executor = FakeExecutor(
        artifact_writes=_plan_artifact_writes(1),
        results={"implement": ExecResult(text="ok", files_written=[outside])},
    )
    orch = _make_orchestrator(profile, runs_dir, executor, fake_sink)

    run = orch.run_once(input_text=HAPPY_TEXT)

    assert run.status == RunStatus.FAILED
    step = run.steps["implement"]
    assert step.status == StepStatus.FAILED
    assert "outside workspace" in step.error
    assert "evil.py" in step.error
    assert step.commits == []

    executed_ids = {r.step_id for r in executor.requests}
    assert "verify" not in executed_ids
    assert "publish" not in executed_ids


# --------------------------------------------------------------------------- #
# QUESTION / DEFER protocol
# --------------------------------------------------------------------------- #


def test_question_blocks_run_writes_question_md_and_notifies_sink(
    tmp_path: Path, git_repo: Path
) -> None:
    profile = _make_profile(git_repo)
    runs_dir = tmp_path / "runs"
    fake_sink = FakeSink()
    executor = FakeExecutor(
        results={"clarify": ExecResult(text="QUESTION: what auth mechanism should be used?")}
    )
    orch = _make_orchestrator(profile, runs_dir, executor, fake_sink)

    run = orch.run_once(input_text=UNCLEAR_TEXT)

    assert run.status == RunStatus.BLOCKED
    assert (runs_dir / run.id / "question.md").is_file()

    assert len(fake_sink.comments) == 1
    assert fake_sink.comments[0][0] == run.work_item_key
    assert fake_sink.comments[0][1] == "QUESTION: what auth mechanism should be used?"
    assert fake_sink.unassigned == [run.work_item_key]
    assert fake_sink.transitions == [(run.work_item_key, "blocked")]

    # later steps never execute
    executed_ids = [r.step_id for r in executor.requests]
    assert executed_ids == ["intake", "clarify"]


def test_defer_line_spawns_exactly_one_fixer_execution(tmp_path: Path, git_repo: Path) -> None:
    profile = _make_profile(git_repo)
    runs_dir = tmp_path / "runs"
    fake_sink = FakeSink()
    executor = _basic_executor(
        results={"review": ExecResult(text="Looks fine overall.\nDEFER: add more integration tests")}
    )
    orch = _make_orchestrator(profile, runs_dir, executor, fake_sink)

    run = orch.run_once(input_text=HAPPY_TEXT)

    assert run.status == RunStatus.DONE
    fixer_reqs = [r for r in executor.requests if r.step_id == "review-fixer"]
    assert len(fixer_reqs) == 1
    assert "add more integration tests" in fixer_reqs[0].prompt
    assert run.steps["review"].defers == ["add more integration tests"]


# --------------------------------------------------------------------------- #
# dry-run
# --------------------------------------------------------------------------- #


def test_dry_run_makes_no_sink_calls_and_writes_dryrun_log(tmp_path: Path, git_repo: Path) -> None:
    profile = _make_profile(git_repo)
    runs_dir = tmp_path / "runs"
    fake_sink = FakeSink()
    executor = _basic_executor()
    orch = _make_orchestrator(profile, runs_dir, executor, fake_sink, dry_run=True)

    run = orch.run_once(input_text=HAPPY_TEXT)

    assert run.status == RunStatus.DONE
    assert fake_sink.comments == []
    assert fake_sink.transitions == []
    assert fake_sink.unassigned == []
    assert fake_sink.links == []

    log_path = runs_dir / run.id / "dryrun.log"
    assert log_path.is_file()
    log_text = log_path.read_text(encoding="utf-8")
    assert "sink.comment" in log_text
    assert "sink.transition" in log_text
    assert "repo.push" in log_text
    assert "repo.open_pr" in log_text


# --------------------------------------------------------------------------- #
# resume
# --------------------------------------------------------------------------- #


def test_resume_reruns_only_incomplete_steps(tmp_path: Path, git_repo: Path) -> None:
    profile = _make_profile(git_repo)
    runs_dir = tmp_path / "runs"
    fake_sink = FakeSink()
    executor = _basic_executor(results={"verify": ExecResult(text="ok", error="boom: flaky")})
    orch = _make_orchestrator(profile, runs_dir, executor, fake_sink)

    run1 = orch.run_once(input_text=HAPPY_TEXT)
    assert run1.status == RunStatus.FAILED

    calls_before_resume = len(executor.requests)
    executor.results["verify"] = ExecResult(text="ok now")  # "fix" the flaky step

    run2 = orch.resume(run1.id)

    assert run2.id == run1.id
    assert run2.status == RunStatus.DONE

    new_requests = executor.requests[calls_before_resume:]
    already_complete = {"intake", "plan", "implement"}
    assert not any(r.step_id in already_complete for r in new_requests)
    assert {r.step_id for r in new_requests} >= {"verify"}


# --------------------------------------------------------------------------- #
# security rails
# --------------------------------------------------------------------------- #


def test_run_lock_prevents_two_runs_on_one_work_item(tmp_path: Path, git_repo: Path) -> None:
    profile = _make_profile(git_repo)
    runs_dir = tmp_path / "runs"
    fake_sink = FakeSink()
    executor = _basic_executor()
    orch = _make_orchestrator(profile, runs_dir, executor, fake_sink)

    # HAPPY_TEXT's item has no external_id, so its key is slugify(title); the
    # title comes from the body's H1 heading (no `title:` in front matter).
    item_key = slugify("Add a /health endpoint")
    other = RunLock(runs_dir, item_key)
    other.acquire("someone-elses-run")
    try:
        with pytest.raises(LockHeld):
            orch.run_once(input_text=HAPPY_TEXT)
    finally:
        other.release()


def test_lock_released_on_the_exception_path(tmp_path: Path, git_repo: Path) -> None:
    profile = _make_profile(git_repo)
    runs_dir = tmp_path / "runs"
    fake_sink = FakeSink()

    class _BoomExecutor:
        def describe(self) -> str:
            return "boom"

        def run(self, req):  # noqa: ANN001
            raise RuntimeError("executor exploded")

    orch = Orchestrator(profile, runs_dir=runs_dir)
    orch._executor = lambda kind=None, step=None: _BoomExecutor()  # type: ignore[method-assign]
    orch._build_sink = lambda run_dir: fake_sink  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="executor exploded"):
        orch.run_once(input_text=HAPPY_TEXT)

    locks_dir = runs_dir / ".locks"
    remaining = list(locks_dir.glob("*.lock")) if locks_dir.exists() else []
    assert remaining == []


def test_runtime_start_failure_still_stops_runtime_and_releases_lock(
    tmp_path: Path, git_repo: Path
) -> None:
    profile = _make_profile(git_repo)
    runs_dir = tmp_path / "runs"
    fake_sink = FakeSink()

    class _BoomRuntime:
        def __init__(self) -> None:
            self.stopped = False

        def describe(self) -> str:
            return "boom-runtime"

        def start(self) -> None:
            raise RuntimeError("runtime failed to start")

        def stop(self) -> None:
            self.stopped = True

        def screenshot(self):  # noqa: ANN201
            return None

    boom_runtime = _BoomRuntime()
    orch = Orchestrator(profile, runs_dir=runs_dir)
    orch._executor = lambda kind=None, step=None: FakeExecutor()  # type: ignore[method-assign]
    orch._build_sink = lambda run_dir: fake_sink  # type: ignore[method-assign]
    orch._runtime_obj = boom_runtime  # pre-seed the adapter cache

    with pytest.raises(RuntimeError, match="runtime failed to start"):
        orch.run_once(input_text=HAPPY_TEXT)

    assert boom_runtime.stopped is True  # stop() is called even though start() raised
    locks_dir = runs_dir / ".locks"
    remaining = list(locks_dir.glob("*.lock")) if locks_dir.exists() else []
    assert remaining == []


def test_budget_cost_cap_stops_run_before_later_steps(tmp_path: Path, git_repo: Path) -> None:
    profile = _make_profile(git_repo, budget={"max_cost_usd": 0.01})
    runs_dir = tmp_path / "runs"
    fake_sink = FakeSink()
    executor = FakeExecutor(results={"intake": ExecResult(text="ok", usage=Usage(cost_usd=100.0))})
    orch = _make_orchestrator(profile, runs_dir, executor, fake_sink)

    run = orch.run_once(input_text=HAPPY_TEXT)

    assert run.status == RunStatus.FAILED
    assert run.steps["intake"].status == StepStatus.FAILED
    assert "cost cap" in (run.steps["intake"].error or "")
    assert {r.step_id for r in executor.requests} == {"intake"}


def test_budget_wall_clock_cap_of_zero_stops_run_before_any_step(
    tmp_path: Path, git_repo: Path
) -> None:
    profile = _make_profile(git_repo, budget={"max_wall_clock_s": 0})
    runs_dir = tmp_path / "runs"
    fake_sink = FakeSink()
    executor = FakeExecutor()
    orch = _make_orchestrator(profile, runs_dir, executor, fake_sink)

    run = orch.run_once(input_text=HAPPY_TEXT)

    assert run.status == RunStatus.FAILED
    assert executor.requests == []


def test_config_resolved_yaml_keeps_env_refs_unexpanded(tmp_path: Path, git_repo: Path) -> None:
    data = _profile_dict(git_repo)
    data["sink"]["webhook_token"] = "${TICKETBOT_TEST_TOKEN}"
    profile = Profile.model_validate(data)
    profile.base_dir = FIXTURE_DIR

    runs_dir = tmp_path / "runs"
    fake_sink = FakeSink()
    executor = _basic_executor()
    orch = _make_orchestrator(profile, runs_dir, executor, fake_sink)

    run = orch.run_once(input_text=HAPPY_TEXT)

    resolved_text = (runs_dir / run.id / "config.resolved.yaml").read_text(encoding="utf-8")
    assert "${TICKETBOT_TEST_TOKEN}" in resolved_text


def test_step_artifact_text_is_redacted(tmp_path: Path, git_repo: Path) -> None:
    profile = _make_profile(git_repo)
    runs_dir = tmp_path / "runs"
    fake_sink = FakeSink()
    secret = "sk-ant-abcdefghijklmnopqrstuvwxyz0123"
    executor = _basic_executor(results={"intake": ExecResult(text=f"noted token {secret} for later")})
    orch = _make_orchestrator(profile, runs_dir, executor, fake_sink)

    run = orch.run_once(input_text=HAPPY_TEXT)

    step_text = (runs_dir / run.id / "steps" / "intake.md").read_text(encoding="utf-8")
    assert secret not in step_text
    assert "REDACTED" in step_text
