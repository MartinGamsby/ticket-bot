import re
from datetime import datetime, timezone

import pytest

from ticketbot.config.redact import Redactor
from ticketbot.core.run import Run, RunStatus, RunStore, StepResult, StepStatus
from ticketbot.core.workitem import WorkItem

RUN_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{4}-[a-z0-9-]+-[0-9a-f]{4}$")


def _item(**overrides) -> WorkItem:
    kwargs = dict(id="task-1", title="Add a /health endpoint", external_id="ENG-1842")
    kwargs.update(overrides)
    return WorkItem(**kwargs)


def test_new_id_matches_expected_shape(tmp_path):
    store = RunStore(tmp_path)
    now = datetime(2026, 9, 1, 14, 43, tzinfo=timezone.utc)
    run_id = store.new_id("ENG-1842", now=now)
    assert RUN_ID_RE.match(run_id), run_id
    assert run_id.startswith("2026-09-01-1443-eng-1842-")


def test_new_run_uses_item_key_and_sets_timestamps(tmp_path):
    store = RunStore(tmp_path)
    now = datetime(2026, 9, 1, 14, 43, tzinfo=timezone.utc)
    run = store.new_run(profile_name="jira-claude", item=_item(), now=now)

    assert RUN_ID_RE.match(run.id)
    assert run.work_item_key == "ENG-1842"
    assert run.external_id == "ENG-1842"
    assert run.status == RunStatus.RECEIVED
    assert run.created_at == "2026-09-01T14:43:00Z"
    assert run.updated_at == run.created_at


def test_save_load_round_trip_including_nested_step_results(tmp_path):
    store = RunStore(tmp_path)
    run = store.new_run(profile_name="p", item=_item())
    run.status = RunStatus.IMPLEMENTING
    step = run.step("implement")
    step.role = "coder"
    step.status = StepStatus.OK
    step.started_at = "2026-09-01T14:43:00Z"
    step.ended_at = "2026-09-01T14:50:00Z"
    step.duration_s = 420.5
    step.cost_usd = 0.42
    step.text = "Implemented section 1."
    step.artifacts.append("sections/section-1.md")
    step.commits.append("abc123")
    run.extra["branch"] = "agent/ENG-1842-health"

    store.save(run)
    loaded = store.load(run.id)

    assert loaded.id == run.id
    assert loaded.status == RunStatus.IMPLEMENTING
    assert loaded.extra["branch"] == "agent/ENG-1842-health"
    assert list(loaded.steps.keys()) == ["implement"]
    loaded_step = loaded.steps["implement"]
    assert loaded_step.role == "coder"
    assert loaded_step.status == StepStatus.OK
    assert loaded_step.duration_s == 420.5
    assert loaded_step.cost_usd == 0.42
    assert loaded_step.text == "Implemented section 1."
    assert loaded_step.artifacts == ["sections/section-1.md"]
    assert loaded_step.commits == ["abc123"]


def test_save_leaves_no_tmp_file_behind(tmp_path):
    store = RunStore(tmp_path)
    run = store.new_run(profile_name="p", item=_item())
    store.save(run)

    run_dir = store.dir(run.id)
    assert (run_dir / "run.json").exists()
    assert not (run_dir / "run.json.tmp").exists()


def test_load_missing_run_raises_file_not_found(tmp_path):
    store = RunStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.load("nonexistent-run-id")


def test_list_ids_sorted_chronologically_newest_last(tmp_path):
    store = RunStore(tmp_path)
    early = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    late = datetime(2026, 9, 1, 18, 0, tzinfo=timezone.utc)

    run_late = store.new_run(profile_name="p", item=_item(external_id="ENG-2"), now=late)
    run_early = store.new_run(profile_name="p", item=_item(external_id="ENG-1"), now=early)
    store.save(run_late)
    store.save(run_early)

    ids = store.list_ids()
    assert ids == sorted(ids)
    assert ids[-1] == run_late.id
    assert ids[0] == run_early.id


def test_latest_returns_none_when_no_runs(tmp_path):
    store = RunStore(tmp_path)
    assert store.latest() is None


def test_latest_returns_most_recent_run(tmp_path):
    store = RunStore(tmp_path)
    early = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    late = datetime(2026, 9, 1, 18, 0, tzinfo=timezone.utc)
    run_early = store.new_run(profile_name="p", item=_item(external_id="ENG-1"), now=early)
    run_late = store.new_run(profile_name="p", item=_item(external_id="ENG-2"), now=late)
    store.save(run_early)
    store.save(run_late)

    assert store.latest().id == run_late.id


def test_write_artifact_rejects_dotdot_relpath(tmp_path):
    store = RunStore(tmp_path)
    run = store.new_run(profile_name="p", item=_item())
    with pytest.raises(ValueError):
        store.write_artifact(run, "../escape.txt", "data")


def test_write_artifact_rejects_absolute_relpath(tmp_path):
    store = RunStore(tmp_path)
    run = store.new_run(profile_name="p", item=_item())
    absolute = str((tmp_path / "elsewhere.txt").resolve())
    with pytest.raises(ValueError):
        store.write_artifact(run, absolute, "data")


def test_write_artifact_creates_parent_dirs_and_round_trips(tmp_path):
    store = RunStore(tmp_path)
    run = store.new_run(profile_name="p", item=_item())
    store.write_artifact(run, "sections/section-1.md", "hello section")
    assert store.read_artifact(run, "sections/section-1.md") == "hello section"


def test_write_artifact_scrubs_secret_strings(tmp_path):
    store = RunStore(tmp_path, redactor=Redactor())
    run = store.new_run(profile_name="p", item=_item())
    path = store.write_artifact(run, "plan.md", "token: sk-ant-abc123def456ghi789")
    text = path.read_text(encoding="utf-8")
    assert "sk-ant-" not in text
    assert "REDACTED" in text


def test_write_artifact_scrubs_a_registered_secret_by_default(tmp_path, monkeypatch):
    """The DEFAULT redactor is the shared, module-level one that
    `register_secret()` populates -- a private `Redactor()` here would see no
    registered secrets and fall back to pattern matching, writing every
    adapter-expanded credential (a Jira token, a `${ENV}` value handed to a
    `process` executor) into `runs/<id>/` verbatim.
    """
    from ticketbot.config import redact as redact_module

    monkeypatch.setattr(redact_module, "_default", redact_module.Redactor())
    literal = "not-a-recognizable-token-shape-at-all"
    redact_module.register_secret(literal)

    store = RunStore(tmp_path)  # no explicit redactor
    run = store.new_run(profile_name="p", item=_item())
    path = store.write_artifact(run, "steps/plan.md", f"the model echoed {literal} back")

    text = path.read_text(encoding="utf-8")
    assert literal not in text
    assert "REDACTED" in text


def test_write_artifact_writes_bytes_verbatim(tmp_path):
    store = RunStore(tmp_path)
    run = store.new_run(profile_name="p", item=_item())
    payload = b"\x89PNG\r\n\x1a\n"
    path = store.write_artifact(run, "screenshots/verify-01.png", payload)
    assert path.read_bytes() == payload


def test_append_log_scrubs_and_appends(tmp_path):
    store = RunStore(tmp_path)
    run = store.new_run(profile_name="p", item=_item())
    store.append_log(run, "implement", "line one with sk-ant-abc123def456ghi789\n")
    store.append_log(run, "implement", "line two\n")

    log_path = store.dir(run.id) / "logs" / "implement.log"
    text = log_path.read_text(encoding="utf-8")
    assert "line two" in text
    assert "sk-ant-" not in text
    assert text.count("\n") == 2


def test_step_get_or_create_is_idempotent():
    run = Run(id="r1", profile_name="p", work_item_key="ENG-1")
    step_a = run.step("implement")
    step_a.status = StepStatus.OK
    step_b = run.step("implement")
    assert step_a is step_b
    assert step_b.status == StepStatus.OK


def test_is_complete_true_only_for_ok_and_skipped():
    run = Run(id="r1", profile_name="p", work_item_key="ENG-1")
    run.steps["a"] = StepResult(id="a", status=StepStatus.OK)
    run.steps["b"] = StepResult(id="b", status=StepStatus.SKIPPED)
    run.steps["c"] = StepResult(id="c", status=StepStatus.PENDING)
    run.steps["d"] = StepResult(id="d", status=StepStatus.FAILED)
    run.steps["e"] = StepResult(id="e", status=StepStatus.BLOCKED)

    assert run.is_complete("a") is True
    assert run.is_complete("b") is True
    assert run.is_complete("c") is False
    assert run.is_complete("d") is False
    assert run.is_complete("e") is False
    assert run.is_complete("nonexistent") is False


def test_step_result_to_dict_from_dict_round_trip():
    step = StepResult(
        id="verify", role="tester", status=StepStatus.FAILED,
        started_at="2026-09-01T14:43:00Z", ended_at="2026-09-01T14:50:00Z",
        duration_s=12.5, cost_usd=0.1, text="failed", artifacts=["a.txt"],
        commits=["c1"], question="what now?", defers=["fix-x"], error="boom",
    )
    restored = StepResult.from_dict(step.to_dict())
    assert restored == step
