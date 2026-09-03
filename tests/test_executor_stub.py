"""`StubExecutor`, and the genuinely-offline end-to-end run it makes possible.

Three of these pin bugs found by actually running the pipeline rather than by
reasoning about it, and each is a trap the next stub-like executor would fall into:
the echoed prompt being re-parsed as protocol, run-dir artifacts leaking into
`files_written`, and the JSON fence losing a race with the prompt's own fences.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ticketbot.cli import main
from ticketbot.config.schema import AdapterConfig
from ticketbot.executors.base import ExecRequest
from ticketbot.executors.stub import StubExecutor

PROFILE = "profiles/file-stub-offline.yaml"


def _stub(**opts) -> StubExecutor:
    return StubExecutor(AdapterConfig(type="stub", **opts))


def _req(tmp_path: Path, *, produces=(), prompt="hello", step_id="plan", **kw) -> ExecRequest:
    ws = tmp_path / "ws"
    ar = tmp_path / "run"
    ws.mkdir(exist_ok=True)
    ar.mkdir(exist_ok=True)
    return ExecRequest(
        system="",
        prompt=prompt,
        workspace=ws,
        artifacts_dir=ar,
        produces=list(produces),
        step_id=step_id,
        **kw,
    )


# ------------------------------------------------------------------ basics


def test_describe_says_it_reaches_nothing():
    assert _stub().describe() == "stub: no model, no network"


def test_it_says_it_did_nothing(tmp_path):
    result = _stub().run(_req(tmp_path))

    assert "did nothing" in result.text.lower()
    assert result.exit_code == 0


def test_the_note_is_configurable(tmp_path):
    result = _stub(note="custom note here").run(_req(tmp_path))

    assert "custom note here" in result.text


# ------------------------------------------------------- produces contracts


def test_it_writes_the_artifacts_the_step_declares(tmp_path):
    req = _req(tmp_path, produces=["test-report.md", "pr.md"])

    _stub().run(req)

    assert (req.artifacts_dir / "test-report.md").is_file()
    assert (req.artifacts_dir / "pr.md").is_file()


def test_a_sections_directory_becomes_real_section_files(tmp_path):
    # `implement` fans out over `section-*.md` and the run FAILS with "the planner
    # produced no sections" if the directory is empty -- an mkdir is not enough.
    req = _req(tmp_path, produces=["sections/"])

    _stub(sections=3).run(req)

    files = sorted(p.name for p in (req.artifacts_dir / "sections").glob("section-*.md"))
    assert files == ["section-1.md", "section-2.md", "section-3.md"]


def test_the_plan_declares_security_no_explicitly(tmp_path):
    # The security gate fails CLOSED: an absent `Security:` line means "unknown",
    # which RUNS the step. A stub plan has to say no out loud.
    req = _req(tmp_path, produces=["plan.md"])

    _stub().run(req)

    assert "Security: no" in (req.artifacts_dir / "plan.md").read_text(encoding="utf-8")


def test_a_json_artifact_contains_json_not_markdown(tmp_path):
    req = _req(tmp_path, produces=["workitem.json"], step_id="intake")

    _stub().run(req)

    data = json.loads((req.artifacts_dir / "workitem.json").read_text(encoding="utf-8"))
    assert data["stub"] is True


def test_it_never_clobbers_an_artifact_a_real_step_produced(tmp_path):
    req = _req(tmp_path, produces=["plan.md"])
    (req.artifacts_dir / "plan.md").write_text("REAL PLAN", encoding="utf-8")

    _stub().run(req)

    assert (req.artifacts_dir / "plan.md").read_text(encoding="utf-8") == "REAL PLAN"


# --------------------------------------------------------- the three traps


@pytest.mark.parametrize("marker", ["QUESTION:", "DEFER:"])
def test_an_echoed_prompt_is_never_parsed_as_protocol(tmp_path, marker):
    # Every role prompt carries the "end your turn with QUESTION:" protocol, so
    # echoing the prompt through `finish_result()` made the stub raise a question
    # it never asked and BLOCK the run.
    prompt = f"Do the thing.\n{marker}\nWhich option do you want?"

    result = _stub().run(_req(tmp_path, prompt=prompt))

    assert result.question is None
    assert result.defers == []
    assert marker in result.text  # still echoed, just not interpreted


def test_run_dir_artifacts_do_not_leak_into_files_written(tmp_path):
    # `files_written` is WORKSPACE files. The engine's landing check fails a step
    # whose declared paths sit outside the workspace, so reporting run-dir
    # artifacts here made `verify` kill its own run.
    req = _req(tmp_path, produces=["test-report.md", "sections/"])

    result = _stub().run(req)

    assert result.files_written == []


def test_the_json_fence_comes_before_the_prompt_echo(tmp_path):
    # `_extract_json_block` takes the FIRST fenced block in the text, and role
    # prompts contain fenced examples of their own.
    prompt = "Here is an example:\n```json\n{\"not\": \"ours\"}\n```\n"
    req = _req(tmp_path, produces=["workitem.json"], prompt=prompt, step_id="intake")

    text = _stub().run(req).text

    first_fence = text.index("```")
    assert text.index("--- prompt it received ---") > first_fence
    block = text[first_fence:].split("```")[1].lstrip("json\n")
    assert json.loads(block)["ambiguity"] == "low"


# ------------------------------------------------------------------- echo


def test_the_prompt_echo_is_truncated(tmp_path):
    result = _stub(echo_limit=20).run(_req(tmp_path, prompt="x" * 500))

    assert "truncated, 500 chars" in result.text
    assert "x" * 500 not in result.text


def test_the_echo_can_be_turned_off(tmp_path):
    result = _stub(echo_prompt=False).run(_req(tmp_path, prompt="SECRET PROMPT"))

    assert "SECRET PROMPT" not in result.text


# --------------------------------------------------------------- end to end


def _scratch_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "scratch"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
    (repo / "app.py").write_text("print(1)\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
    )
    return repo


def test_the_whole_pipeline_runs_with_no_credentials_at_all(tmp_path, monkeypatch):
    """The offline test this executor exists for: every step, no network, no key.

    Ends `blocked` (exit 3) because `gates.on_pr_ready: human_review` holds the run
    at the PR-ready gate -- that is the designed terminal state for a successful
    run, not a failure.
    """
    for name in ("ANTHROPIC_API_KEY", "JIRA_API_TOKEN", "GITHUB_TOKEN", "SOLARI_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    repo = _scratch_repo(tmp_path)
    runs = tmp_path / "runs"

    code = main(
        [
            "run", "-c", PROFILE,
            "--input-text", "Add a /health endpoint",
            "--repo", str(repo),
            "--runs-dir", str(runs),
        ]
    )

    assert code == 3  # blocked at the human-review gate

    run_dir = sorted(runs.glob("*"))[-1]
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["status"] == "blocked"

    steps = state["steps"]
    assert steps["intake"]["status"] == "ok"
    assert steps["plan"]["status"] == "ok"
    assert steps["implement"]["status"] == "ok"
    assert steps["verify"]["status"] == "ok"
    assert steps["review"]["status"] == "ok"
    assert steps["publish"]["status"] == "ok"
    # `clarify` is skipped because the stub's intake JSON fills `acceptance`;
    # `security` because its plan says `Security: no`.
    assert steps["clarify"]["status"] == "skipped"
    assert steps["security"]["status"] == "skipped"
    assert not [s for s in steps.values() if s["status"] == "failed"]

    for name in (
        "banner.txt", "config.resolved.yaml", "run.json", "workitem.json",
        "plan.md", "sections/section-1.md", "sections/section-2.md",
        "patch.diff", "test-report.md", "review.md", "pr.md", "ticket_comment.md",
    ):
        assert (run_dir / name).is_file(), f"missing {name}"


def test_the_offline_run_reports_the_stub_executor_in_its_banner(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    repo = _scratch_repo(tmp_path)
    runs = tmp_path / "runs"

    main(
        ["run", "-c", PROFILE, "--input-text", "Add a thing",
         "--repo", str(repo), "--runs-dir", str(runs)]
    )

    banner = (sorted(runs.glob("*"))[-1] / "banner.txt").read_text(encoding="utf-8")
    assert "stub: no model, no network" in banner
