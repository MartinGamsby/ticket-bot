"""`PipelineDef`/`StepDef` loading: valid shapes, and every structural problem
caught eagerly at LOAD time (never mid-run) as `ConfigError`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ticketbot.config.loader import ConfigError
from ticketbot.engine.pipeline import PipelineDef

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_tiny_fixture_with_right_step_count_and_defaults() -> None:
    pipeline = PipelineDef.load("pipelines/tiny.yaml", FIXTURE_DIR)
    assert pipeline.name == "tiny"
    assert [s.id for s in pipeline.steps] == [
        "intake", "clarify", "plan", "implement", "verify", "review",
        "security", "publish", "fixer-template",
    ]
    assert pipeline.defaults == {"executor": "default", "model": "default", "timeout_s": 60}
    assert pipeline.on_question == "pause_and_relay"
    assert pipeline.on_defer == "spawn_fixer"
    assert pipeline.ref == "pipelines/tiny.yaml"
    assert pipeline.source_path == (FIXTURE_DIR / "pipelines" / "tiny.yaml").resolve()


def test_step_and_index_of_lookup() -> None:
    pipeline = PipelineDef.load("pipelines/tiny.yaml", FIXTURE_DIR)
    assert pipeline.step("plan").role == "planner"
    assert pipeline.step("nope") is None
    assert pipeline.index_of("plan") == 2
    assert pipeline.index_of("nope") == -1


def test_block_and_inline_flow_mapping_forms_both_parse(tmp_path: Path) -> None:
    text = """
name: mixed
steps:
  - {id: a, role: ingest}
  - id: b
    role: coder
    tools: [fs.read]
"""
    path = _write(tmp_path, "mixed.yaml", text)
    pipeline = PipelineDef.load(str(path), tmp_path)
    assert [s.id for s in pipeline.steps] == ["a", "b"]
    assert pipeline.steps[0].role == "ingest"
    assert pipeline.steps[1].tools == ["fs.read"]


def test_duplicate_step_ids_raise_naming_the_id(tmp_path: Path) -> None:
    text = """
name: dup
steps:
  - {id: a, role: ingest}
  - {id: a, role: coder}
"""
    path = _write(tmp_path, "dup.yaml", text)
    with pytest.raises(ConfigError, match="a"):
        PipelineDef.load(str(path), tmp_path)


def test_unknown_step_key_raises(tmp_path: Path) -> None:
    text = """
name: bad
steps:
  - {id: a, role: ingest, bogus_key: 1}
"""
    path = _write(tmp_path, "bad.yaml", text)
    with pytest.raises(ConfigError, match="bogus_key"):
        PipelineDef.load(str(path), tmp_path)


def test_empty_steps_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, "empty.yaml", "name: empty\nsteps: []\n")
    with pytest.raises(ConfigError, match="no steps"):
        PipelineDef.load(str(path), tmp_path)


def test_missing_steps_key_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, "nosteps.yaml", "name: nosteps\n")
    with pytest.raises(ConfigError):
        PipelineDef.load(str(path), tmp_path)


def test_unsupported_for_each_raises(tmp_path: Path) -> None:
    text = """
name: badforeach
steps:
  - {id: a, role: coder, for_each: plan.foo}
"""
    path = _write(tmp_path, "badforeach.yaml", text)
    with pytest.raises(ConfigError, match="for_each"):
        PipelineDef.load(str(path), tmp_path)


def test_syntactically_broken_when_raises_at_load_time(tmp_path: Path) -> None:
    text = """
name: badwhen
steps:
  - {id: a, role: coder, when: "workitem.acceptance >>> nonsense((("}
"""
    path = _write(tmp_path, "badwhen.yaml", text)
    with pytest.raises(ConfigError):
        PipelineDef.load(str(path), tmp_path)


def test_missing_id_raises(tmp_path: Path) -> None:
    text = """
name: noid
steps:
  - {role: coder}
"""
    path = _write(tmp_path, "noid.yaml", text)
    with pytest.raises(ConfigError, match="id"):
        PipelineDef.load(str(path), tmp_path)


def test_missing_role_raises(tmp_path: Path) -> None:
    text = """
name: norole
steps:
  - {id: a}
"""
    path = _write(tmp_path, "norole.yaml", text)
    with pytest.raises(ConfigError, match="role"):
        PipelineDef.load(str(path), tmp_path)


def test_unknown_gate_raises(tmp_path: Path) -> None:
    text = """
name: badgate
steps:
  - {id: a, role: coder, gate: yolo}
"""
    path = _write(tmp_path, "badgate.yaml", text)
    with pytest.raises(ConfigError, match="gate"):
        PipelineDef.load(str(path), tmp_path)


# --------------------------------------------------------------------------- #
# Security: `when:` is validated through the safe predicate parser, never
# `eval`; the underlying YAML load is `yaml.safe_load` only.
# --------------------------------------------------------------------------- #


def test_when_never_reaches_eval_hostile_expression_rejected_not_executed(tmp_path: Path) -> None:
    text = """
name: hostile
steps:
  - {id: a, role: coder, when: "__import__('os').system('echo pwned')"}
"""
    path = _write(tmp_path, "hostile.yaml", text)
    # If this were ever eval()'d, `__import__` would be a callable identifier and
    # the string would execute; the safe parser only understands `path [op value]`
    # shapes and must reject this as a malformed expression instead.
    with pytest.raises(ConfigError):
        PipelineDef.load(str(path), tmp_path)


def test_pipeline_yaml_uses_safe_load_python_tag_rejected(tmp_path: Path) -> None:
    text = """
name: yamlattack
steps:
  - id: a
    role: coder
    commit: !!python/object/apply:os.system ["echo pwned"]
"""
    path = _write(tmp_path, "yamlattack.yaml", text)
    with pytest.raises(ConfigError):
        PipelineDef.load(str(path), tmp_path)
