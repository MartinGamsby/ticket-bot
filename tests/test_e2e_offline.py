"""The offline proof: a copy of `tests/fixtures/toy-repo`, a hand-written profile
equivalent in shape to `profiles/file-text-none.yaml` (file source, file sink,
`git_local` repo, `runtime: none`), and the real "standard" builtin pipeline --
driven through the REAL `EXECUTORS` registry rather than by monkeypatching
`Orchestrator._executor` the way `test_engine_orchestrator.py` does to isolate the
run loop from adapter wiring. This module exists to prove the wiring itself: a
profile's `executor.kinds.<name>.type: fake` resolves through `core/registry.py`
to a working executor, end to end, with no network, no real coding CLI and no API
key anywhere.

`FakeExecutor` (see `tests/fakes.py`) is built with `cfg`-less positional
arguments -- it is designed to be handed to an `Orchestrator` directly via test
seams, not constructed by the registry's `cls(cfg, **kwargs)` call shape. Rather
than change that shared fixture, `_install_fake_executor` below registers a
throwaway adapter class whose `__new__` returns an already-built `FakeExecutor`
unchanged, so the registry's construction call still happens (proving the wiring)
without needing `FakeExecutor` to grow a `cfg` parameter it has no use for.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from ticketbot.cli import main
from ticketbot.config.loader import load_profile
from ticketbot.config.redact import PATTERNS
from ticketbot.core.registry import EXECUTORS
from ticketbot.core.run import RunStatus, StepStatus
from ticketbot.engine.orchestrator import Orchestrator
from ticketbot.engine.pipeline import PipelineDef
from ticketbot.executors.base import ExecRequest, ExecResult, append_log
from tests.fakes import FakeExecutor, FakeSink

TOY_REPO = Path(__file__).resolve().parent / "fixtures" / "toy-repo"

# -- scripted content -------------------------------------------------------- #
# Kept out of the git-tracked workspace diff wherever possible (only `writes=`
# content below lands in the workspace and is reviewed by `_before_review`'s
# `diff.touches_security` scan): none of it contains any of orchestrator.py's
# `_SECURITY_DIFF_KEYWORDS` (auth, login, token, secret, password, crypto,
# subprocess, shell, eval, pickle, sql), so `security` stays SKIPPED as intended.

INTAKE_JSON = (
    '{"summary": "Add a /health endpoint.", "acceptance": "- returns 200", '
    '"ambiguity": "low", "size": "s"}'
)
PLAN_MD = (
    "# Plan\n\n"
    "Goal: add a /health endpoint that returns 200.\n\n"
    "Sections:\n1. Add the health() function\n2. Add a unit test for it\n\n"
    "Security: no\n"
    "No new external surface; purely additive.\n"
)
SECTION_1_MD = "# Add the health() function\n\nImplement `health()` in app.py, returning 200.\n"
SECTION_2_MD = "# Add a unit test\n\nAdd tests/test_health.py asserting health() returns 200.\n"
TEST_REPORT_MD = "# Test Report\n\n2 passed, 0 failed.\n"
REVIEW_MD = "# Review\n\nAPPROVE. No blocking issues found.\n"
PR_MD = (
    "# Pull Request: Add a /health endpoint\n\n"
    "## Context\nThe ticket asked for a simple health check endpoint.\n\n"
    "## Approach\nAdded the route in two independent sections: the function itself, "
    "then a unit test for it.\n\n"
    "## Files touched\n- app.py\n- tests/test_health.py\n\n"
    "## Test plan\nRan the new unit test; it passes.\n\n"
    "## Risks and follow-ups\nNone identified; this is additive.\n"
)
TICKET_COMMENT_MD = "Added the /health endpoint.\nVerified with a new unit test.\n"

_APP_PY_WITH_HEALTH = "def add(a, b):\n    return a + b\n\n\ndef health():\n    return 200\n"
_TEST_HEALTH_PY = (
    "import sys\n"
    "from pathlib import Path\n\n"
    "sys.path.insert(0, str(Path(__file__).resolve().parent.parent))\n\n"
    "from app import health\n\n\n"
    "def test_health():\n"
    "    assert health() == 200\n"
)


# -- toy repo + profile fixtures --------------------------------------------- #


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, shell=False)
    if result.returncode != 0:
        raise RuntimeError(f"git {args} failed: {result.stderr}")
    return result


def _init_toy_repo(dst: Path) -> Path:
    """Copy `tests/fixtures/toy-repo` to `dst` and `git init` + commit it there --
    the fixture itself stays a plain directory in version control, never a repo.
    """
    if shutil.which("git") is None:
        pytest.skip("git is not on PATH")
    shutil.copytree(TOY_REPO, dst)
    _git(["init", "-b", "main"], dst)
    _git(["config", "user.email", "test@example.com"], dst)
    _git(["config", "user.name", "Test User"], dst)
    _git(["add", "-A"], dst)
    _git(["commit", "-m", "init"], dst)
    return dst


def _write_offline_profile(tmp_path: Path, repo_dir: Path) -> Path:
    """A profile that extends nothing, shaped like `profiles/file-text-none.yaml`
    but pointed at `repo_dir` and driven entirely by fake adapters: `source: file`,
    `sink: file`, `repo: git_local`, `runtime: none`, an `executor.kinds.fake`
    resolved through the real registry (see `_install_fake_executor`), and three
    model slots (matching what `builtin/pipelines/standard.yaml` references:
    `default`, `cheap` for `intake`, `peer` for `review`) so the banner's
    `models=` line is fully populated rather than silently dropping entries whose
    slot doesn't exist. `on_pr_ready: auto` is required -- the schema default,
    `human_review`, would leave every run BLOCKED at `publish` forever.

    Includes one harmless, never-expanded `${ENV}` ref (an unused sink option) so
    the "config.resolved.yaml keeps secrets unexpanded" assertion has something
    real to check.
    """
    data = {
        "name": "offline-e2e",
        "version": 1,
        "source": {"type": "file"},
        "sink": {"type": "file", "webhook_token": "${TICKETBOT_TEST_TOKEN}"},
        "repo": {"type": "git_local", "path": str(repo_dir)},
        "model": {
            "default": "default",
            "providers": {
                "default": {"type": "fake", "name": "Fake Default"},
                "cheap": {"type": "fake", "name": "Fake Cheap"},
                "peer": {"type": "fake", "name": "Fake Peer"},
            },
        },
        # `builtin/pipelines/standard.yaml`'s `defaults: {executor: default, ...}`
        # means every step without its own `executor:` resolves the KIND NAME
        # "default" -- so the kind must be named "default" here; its ADAPTER
        # `type:` is "fake", which is what `_install_fake_executor` registers.
        "executor": {"default": "default", "kinds": {"default": {"type": "fake"}}},
        "runtime": {"type": "none"},
        "gates": {"on_pr_ready": "auto"},
    }
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    path = profiles_dir / "offline.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _install_fake_executor(monkeypatch: pytest.MonkeyPatch, executor: FakeExecutor) -> None:
    """Register `executor` as `EXECUTORS['fake']` -- the same registry
    `core/registry.py` uses for every real adapter -- so the orchestrator resolves
    it via `executor.kinds.fake.type: fake` exactly as it would resolve `process`
    or `api`. Every registered adapter is instantiated as `cls(cfg, **kwargs)`;
    `FakeExecutor.__init__` takes no `cfg`, so `_Adapter.__new__` hands back the
    already-built `executor` untouched (Python only calls `__init__` when
    `__new__` returns an instance of the SAME class, and `executor` is not an
    `_Adapter`), rather than trying to reshape `FakeExecutor`'s constructor.
    `monkeypatch` scopes the registration to this one test.
    """

    class _Adapter:
        def __new__(cls, cfg, **kwargs):  # noqa: ANN001, ANN003 - registry call shape
            return executor

    monkeypatch.setitem(EXECUTORS._targets, "fake", _Adapter)


class _OfflineExecutor(FakeExecutor):
    """`FakeExecutor` plus the one behavior a real executor (`process`/`api_loop`)
    has that the rest of the suite doesn't need: writing to `req.log_path`. The
    E2E assertions check that `runs/<id>/logs/` exists, matching what a real run
    produces -- this stays a test-local subclass rather than a change to the
    shared `tests/fakes.py` fixture.
    """

    def run(self, req: ExecRequest) -> ExecResult:
        result = super().run(req)
        if req.log_path is not None:
            append_log(req.log_path, f"[fake executor] step={req.step_id} ok={result.ok}\n")
        return result


def _make_offline_executor(*, review_error: str | None = None) -> FakeExecutor:
    """The scripted executor for the "standard" pipeline: `intake` returns the
    ingest JSON envelope (low ambiguity, non-empty acceptance) so `clarify`'s
    `when:` is false; `plan` writes `plan.md` (with a `Security: no` line) and two
    sections; `implement` writes a real file into the WORKSPACE per section, in
    order, so `verify_landed` passes and a commit lands; `verify`/`review` each
    write both a run-dir artifact (`test-report.md`/`review.md`) and a small
    workspace marker file (so their `commit:` templates have something real to
    commit, exactly like a real tester/reviewer editing a file in passing);
    `security` is left unscripted -- its `when:` is false so it never runs; and
    `publish` writes `pr.md` (long) and `ticket_comment.md` (short) directly,
    so the orchestrator's own `_after_reporter` fallback (which would make both
    files identical) never fires.
    """
    executor = _OfflineExecutor(
        results={
            "intake": ExecResult(text=INTAKE_JSON),
            "plan": ExecResult(text="Plan ready: two sections, security: no. See plan.md."),
            "verify": ExecResult(text="2 passed, 0 failed. See test-report.md."),
            "review": ExecResult(
                text="APPROVE\n0 blockers, 0 should-fix, 0 nits. Nothing deferred.",
                error=review_error,
            ),
        },
        artifact_writes={
            "plan": [
                ("plan.md", PLAN_MD),
                ("sections/section-1.md", SECTION_1_MD),
                ("sections/section-2.md", SECTION_2_MD),
            ],
            "verify": [("test-report.md", TEST_REPORT_MD)],
            "review": [("review.md", REVIEW_MD)],
            "publish": [("pr.md", PR_MD), ("ticket_comment.md", TICKET_COMMENT_MD)],
        },
        writes={
            "verify": [("VERIFIED.txt", "manual verification: tests passed\n")],
            "review": [("REVIEWED.txt", "manual review: approved\n")],
        },
    )

    def _implement_writes(req: ExecRequest) -> list[tuple[str, str]]:
        # `executor.requests` already includes THIS call (FakeExecutor.run()
        # appends before writing), so counting prior "implement" requests gives a
        # stable 1-based section index without needing to parse `req.prompt`.
        n = sum(1 for r in executor.requests if r.step_id == "implement")
        if n == 1:
            return [("app.py", _APP_PY_WITH_HEALTH)]
        return [("tests/test_health.py", _TEST_HEALTH_PY)]

    executor.writes["implement"] = _implement_writes
    return executor


# --------------------------------------------------------------------------- #
# the offline run, asserted against the full runs/<id>/ shape
# --------------------------------------------------------------------------- #


def test_offline_run_produces_full_artifact_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_dir = _init_toy_repo(tmp_path / "repo")
    profile = load_profile(_write_offline_profile(tmp_path, repo_dir))

    executor = _make_offline_executor()
    _install_fake_executor(monkeypatch, executor)

    orch = Orchestrator(profile, runs_dir=tmp_path / "runs")
    run = orch.run_once(input_text="Add a /health endpoint")

    # 1.
    assert run.status == RunStatus.DONE
    run_dir = tmp_path / "runs" / run.id

    # 2. every declared artifact exists
    for rel in (
        "banner.txt", "config.resolved.yaml", "run.json", "workitem.json",
        "plan.md", "sections/section-1.md", "sections/section-2.md",
        "test-report.md", "review.md", "pr.md", "ticket_comment.md",
    ):
        assert (run_dir / rel).is_file(), rel
    assert (run_dir / "logs").is_dir()

    # 3. banner shape
    banner_text = (run_dir / "banner.txt").read_text(encoding="utf-8")
    assert "Using source=" in banner_text
    assert "pipeline=builtin:pipelines/standard.yaml  (rule: default)" in banner_text
    assert "models=" in banner_text
    assert "executor=fake executor" in banner_text
    assert "runtime=none" in banner_text
    assert "repo=" in banner_text

    # 4. no expanded secret in config.resolved.yaml
    resolved_text = (run_dir / "config.resolved.yaml").read_text(encoding="utf-8")
    assert "${TICKETBOT_TEST_TOKEN}" in resolved_text
    for _name, pattern in PATTERNS:
        assert not pattern.search(resolved_text), _name

    # 5. one commit per section (2) plus the test/review commits, on the run's
    # branch. `repo.cleanup()` removes the worktree by the time run_once()
    # returns, so this reads the branch's history from the base repo instead of
    # the (now-gone) workspace directory.
    branch = run.extra["branch"]
    messages = _git(["log", "--format=%s", branch], cwd=repo_dir).stdout.splitlines()
    assert sum(m.startswith("impl:") for m in messages) == 2
    assert sum(m.startswith("test:") for m in messages) == 1
    assert sum(m.startswith("review-fix:") for m in messages) == 1

    # 6. clarify/security SKIPPED; every pipeline step recorded, none absent
    assert run.steps["clarify"].status == StepStatus.SKIPPED
    assert run.steps["security"].status == StepStatus.SKIPPED
    pipeline = PipelineDef.load(run.pipeline_ref, profile.base_dir or Path.cwd())
    assert set(run.steps) == {s.id for s in pipeline.steps}

    # 7. the two-artifact requirement
    pr_text = (run_dir / "pr.md").read_text(encoding="utf-8")
    comment_text = (run_dir / "ticket_comment.md").read_text(encoding="utf-8")
    assert len(comment_text) < len(pr_text)

    # 8. implement ran twice, once per section, in numeric order
    implement_reqs = [r for r in executor.requests if r.step_id == "implement"]
    assert len(implement_reqs) == 2
    indices = []
    for req in implement_reqs:
        m = re.search(r"plan section (\d+) of \d+", req.prompt)
        assert m is not None, req.prompt
        indices.append(int(m.group(1)))
    assert indices == [1, 2]


# --------------------------------------------------------------------------- #
# resume
# --------------------------------------------------------------------------- #


def test_resume_skips_completed_steps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_dir = _init_toy_repo(tmp_path / "repo")
    profile = load_profile(_write_offline_profile(tmp_path, repo_dir))

    executor = _make_offline_executor(review_error="boom: review crashed")
    _install_fake_executor(monkeypatch, executor)

    orch = Orchestrator(profile, runs_dir=tmp_path / "runs")
    run1 = orch.run_once(input_text="Add a /health endpoint")

    assert run1.status == RunStatus.FAILED
    for step_id in ("intake", "plan", "implement", "verify"):
        assert run1.steps[step_id].status == StepStatus.OK, step_id
    assert run1.steps["review"].status == StepStatus.FAILED

    branch = run1.extra["branch"]
    hashes_before = _git(["log", "--format=%H", branch], cwd=repo_dir).stdout.splitlines()
    calls_before_resume = len(executor.requests)

    # "swap in a healthy executor": the review step no longer errors.
    executor.results["review"] = ExecResult(text="APPROVE\n0 blockers, 0 should-fix, 0 nits.")

    run2 = orch.resume(run1.id)

    assert run2.id == run1.id
    assert run2.status == RunStatus.DONE
    assert (tmp_path / "runs" / run2.id) == (tmp_path / "runs" / run1.id)

    new_requests = executor.requests[calls_before_resume:]
    already_ok = {"intake", "plan", "implement", "verify"}
    assert not any(r.step_id in already_ok for r in new_requests)
    assert {r.step_id for r in new_requests} >= {"review"}

    # exactly one new commit (review's REVIEWED.txt marker); nothing earlier was
    # rewritten or duplicated -- the tail of the new history is byte-identical.
    hashes_after = _git(["log", "--format=%H", branch], cwd=repo_dir).stdout.splitlines()
    assert len(hashes_after) == len(hashes_before) + 1
    assert hashes_after[-len(hashes_before):] == hashes_before


# --------------------------------------------------------------------------- #
# dry run
# --------------------------------------------------------------------------- #


def test_dry_run_makes_no_outward_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_dir = _init_toy_repo(tmp_path / "repo")
    profile = load_profile(_write_offline_profile(tmp_path, repo_dir))

    executor = _make_offline_executor()
    _install_fake_executor(monkeypatch, executor)
    fake_sink = FakeSink()

    orch = Orchestrator(profile, runs_dir=tmp_path / "runs", dry_run=True)
    orch._build_sink = lambda run_dir: fake_sink  # type: ignore[method-assign]

    run = orch.run_once(input_text="Add a /health endpoint")

    assert run.status == RunStatus.DONE
    assert fake_sink.comments == []
    assert fake_sink.transitions == []
    assert fake_sink.unassigned == []
    assert fake_sink.links == []

    log_path = tmp_path / "runs" / run.id / "dryrun.log"
    assert log_path.is_file()
    log_text = log_path.read_text(encoding="utf-8")
    assert "sink.comment" in log_text
    assert "sink.transition" in log_text
    assert "repo.open_pr" in log_text


# --------------------------------------------------------------------------- #
# every shipped profile validates offline
# --------------------------------------------------------------------------- #


def test_cli_validate_all_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ticketbot validate` exits 0 for every profile with NO relevant environment
    variable set -- proving `${ENV}` refs are never resolved at validate time.
    """
    for name in (
        "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_BOT_ACCOUNT_ID",
        "GITHUB_TOKEN",
        "SOLARI_API_KEY",
        "ANTHROPIC_API_KEY",
        "PEER_BASE_URL", "PEER_API_KEY", "MODEL_BASE_URL", "MODEL_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    profiles_dir = Path(__file__).resolve().parent.parent / "profiles"
    paths = sorted(profiles_dir.glob("*.yaml"))
    assert paths, f"no profiles found under {profiles_dir}"
    for path in paths:
        assert main(["validate", "-c", str(path)]) == 0, path
