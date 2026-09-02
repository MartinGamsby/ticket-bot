"""CLI additions for this section: `run`, `poll`, `resume`.

The engine's own behavior (the run loop, gating, budgets, locks) is covered
end-to-end in `test_engine_orchestrator.py` against the real `Orchestrator`;
these tests cover the thin CLI layer on top of it -- argument parsing, exit
code mapping, and the config/usage error paths -- without re-driving a full
pipeline through a subprocess-spawning executor (which would make the test
depend on what coding CLIs happen to be on the machine's PATH).
"""

from __future__ import annotations

from pathlib import Path

from ticketbot.cli import _run_exit_code, build_parser, main
from ticketbot.core.run import Run, RunStatus, RunStore
from ticketbot.core.workitem import WorkItem


def test_run_exit_code_mapping() -> None:
    def _run(status: RunStatus) -> Run:
        return Run(id="r", profile_name="p", work_item_key="k", status=status)

    assert _run_exit_code(_run(RunStatus.DONE)) == 0
    assert _run_exit_code(_run(RunStatus.BLOCKED)) == 3
    assert _run_exit_code(_run(RunStatus.FAILED)) == 4
    # a non-terminal status should never be reachable at exit time, but the
    # mapping must still degrade to something rather than raising
    assert _run_exit_code(_run(RunStatus.PLANNING)) == 0


def test_build_parser_parses_run_flags() -> None:
    parser = build_parser()
    args = parser.parse_args([
        "run", "-c", "profiles/p.yaml",
        "--once", "ENG-1",
        "--input", "in.md",
        "--input-text", "some text",
        "--repo", "C:/repo",
        "--dry-run",
        "--pause-at", "plan",
        "--force-lock",
        "--runs-dir", "myruns",
    ])
    assert args.command == "run"
    assert args.config == "profiles/p.yaml"
    assert args.once == "ENG-1"
    assert args.input == "in.md"
    assert args.input_text == "some text"
    assert args.repo == "C:/repo"
    assert args.dry_run is True
    assert args.pause_at == "plan"
    assert args.force_lock is True
    assert args.runs_dir == "myruns"


def test_build_parser_run_defaults() -> None:
    parser = build_parser()
    args = parser.parse_args(["run", "-c", "profiles/p.yaml"])
    assert args.once is None
    assert args.input is None
    assert args.input_text is None
    assert args.repo is None
    assert args.dry_run is False
    assert args.pause_at is None
    assert args.force_lock is False
    assert args.runs_dir is None


def test_build_parser_parses_poll_flags() -> None:
    parser = build_parser()
    args = parser.parse_args([
        "poll", "-c", "profiles/p.yaml", "--once", "--max-items", "3", "--dry-run", "--runs-dir", "r",
    ])
    assert args.command == "poll"
    assert args.once is True
    assert args.max_items == 3
    assert args.dry_run is True
    assert args.runs_dir == "r"


def test_build_parser_parses_resume_flags() -> None:
    parser = build_parser()
    args = parser.parse_args([
        "resume", "2026-01-01-0000-eng-1-abcd", "-c", "profiles/p.yaml", "--runs-dir", "r", "--force-lock",
    ])
    assert args.command == "resume"
    assert args.run_id == "2026-01-01-0000-eng-1-abcd"
    assert args.config == "profiles/p.yaml"
    assert args.runs_dir == "r"
    assert args.force_lock is True


def test_build_parser_resume_config_optional() -> None:
    parser = build_parser()
    args = parser.parse_args(["resume", "some-run-id"])
    assert args.config is None
    assert args.runs_dir is None
    assert args.force_lock is False


def test_cmd_run_invalid_config_path_returns_2(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "does-not-exist.yaml"
    exit_code = main(["run", "-c", str(missing), "--input-text", "hello", "--runs-dir", str(tmp_path / "runs")])
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "error" in captured.err.lower()


def test_cmd_poll_invalid_config_path_returns_2(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.yaml"
    exit_code = main(["poll", "-c", str(missing), "--once", "--runs-dir", str(tmp_path / "runs")])
    assert exit_code == 2


def test_cmd_resume_unknown_run_id_returns_2(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    exit_code = main(["resume", "no-such-run", "--runs-dir", str(runs_dir)])
    assert exit_code == 2


def test_cmd_resume_defaults_to_profiles_dir_when_config_omitted(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    runs_dir = tmp_path / "runs"
    store = RunStore(runs_dir)
    item = WorkItem(id="x", title="a title")
    run = store.new_run(profile_name="ghost-profile", item=item)
    store.save(run)

    # cwd has no profiles/ghost-profile.yaml -- a config error, not a crash.
    exit_code = main(["resume", run.id, "--runs-dir", str(runs_dir)])
    assert exit_code == 2
