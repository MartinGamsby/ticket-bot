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


# ---- `resume` and the profile's own runs_dir -------------------------------------
#
# `run`/`poll` build an `Orchestrator`, which resolves `base_dir / runs_dir`.
# `resume` has to resolve the SAME directory itself, before it can build one: the
# run must be loaded to learn which profile it belongs to, but the profile owns the
# directory the run lives in. `resume` used to hardcode `Path("runs")`, so a profile
# with a custom `runs_dir` could not be resumed at all without `--runs-dir`.

_PROFILE_YAML = """\
name: custom-runs
version: 1
runs_dir: {runs_dir}
source: {{type: file}}
sink: {{type: file}}
repo: {{type: git_local, path: "."}}
model:
  default: main
  providers:
    main: {{type: fake, name: test-model}}
executor:
  default: inline
  kinds:
    inline: {{type: api, model: main}}
runtime: {{type: none}}
"""


class _RecordingOrchestrator:
    """Stands in for the real `Orchestrator` so these tests exercise the runs-dir
    resolution in `_cmd_resume` and nothing else -- no pipeline, no executor, no
    lock. Records the `runs_dir` the CLI decided on."""

    last: "_RecordingOrchestrator | None" = None

    def __init__(self, profile, *, runs_dir=None, **kw) -> None:  # noqa: ANN001
        self.profile = profile
        self.runs_dir = Path(runs_dir) if runs_dir is not None else Path("runs")
        self.store = RunStore(self.runs_dir)
        _RecordingOrchestrator.last = self

    def resume(self, run_id: str, *, force_lock: bool = False) -> Run:
        self.resumed = (run_id, force_lock)
        return self.store.load(run_id)


def _seed(runs_dir: Path, profile_name: str = "custom-runs") -> Run:
    store = RunStore(runs_dir)
    run = store.new_run(profile_name=profile_name, item=WorkItem(id="x", title="a title"))
    store.save(run)
    return run


def _write_profile(tmp_path: Path, runs_dir_value: str) -> Path:
    profiles = tmp_path / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    path = profiles / "custom-runs.yaml"
    path.write_text(_PROFILE_YAML.format(runs_dir=runs_dir_value), encoding="utf-8")
    return path


def test_cmd_resume_uses_the_profiles_runs_dir_when_only_config_is_given(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("ticketbot.cli.Orchestrator", _RecordingOrchestrator)
    profile_path = _write_profile(tmp_path, "my-runs")
    # `runs_dir` resolves against the PROFILE's directory, exactly as `repo.path` does
    expected = (tmp_path / "profiles" / "my-runs").resolve()
    run = _seed(expected)

    exit_code = main(["resume", run.id, "-c", str(profile_path)])

    assert exit_code == 0
    assert _RecordingOrchestrator.last.runs_dir == expected
    assert _RecordingOrchestrator.last.resumed == (run.id, False)


def test_cmd_resume_runs_dir_flag_still_wins_over_the_profile(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("ticketbot.cli.Orchestrator", _RecordingOrchestrator)
    profile_path = _write_profile(tmp_path, "my-runs")
    override = tmp_path / "elsewhere"
    run = _seed(override)

    exit_code = main(
        ["resume", run.id, "-c", str(profile_path), "--runs-dir", str(override), "--force-lock"]
    )

    assert exit_code == 0
    assert _RecordingOrchestrator.last.runs_dir == override
    assert _RecordingOrchestrator.last.resumed == (run.id, True)


def test_cmd_resume_falls_back_to_runs_when_neither_is_given(
    tmp_path: Path, monkeypatch
) -> None:
    """With no `-c` and no `--runs-dir` there is nothing to resolve from -- the run
    id alone cannot name a profile -- so `runs/` under the cwd is the only answer.
    """
    monkeypatch.setattr("ticketbot.cli.Orchestrator", _RecordingOrchestrator)
    monkeypatch.chdir(tmp_path)
    _write_profile(tmp_path, "my-runs")
    run = _seed(tmp_path / "runs")

    exit_code = main(["resume", run.id])

    assert exit_code == 0
    assert _RecordingOrchestrator.last.runs_dir == Path("runs")


def test_cmd_resume_reports_an_invalid_config_before_touching_the_store(
    tmp_path: Path, monkeypatch
) -> None:
    """The `-c` branch loads the profile FIRST, so a bad profile must exit 2 with the
    config error rather than a confusing "no such run under runs/"."""
    monkeypatch.setattr("ticketbot.cli.Orchestrator", _RecordingOrchestrator)
    _RecordingOrchestrator.last = None
    bad = tmp_path / "broken.yaml"
    bad.write_text("name: broken\nsource: {type: file}\n", encoding="utf-8")

    exit_code = main(["resume", "some-run-id", "-c", str(bad)])

    assert exit_code == 2
    assert _RecordingOrchestrator.last is None
