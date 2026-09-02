from pathlib import Path

from ticketbot.cli import main

FIXTURES = Path(__file__).parent / "fixtures" / "profiles"


def test_validate_valid_profile_returns_0_and_prints_ok(capsys):
    code = main(["validate", "-c", str(FIXTURES / "minimal.yaml")])
    captured = capsys.readouterr()

    assert code == 0
    assert "OK" in captured.out
    assert "minimal" in captured.out


def test_validate_broken_profile_returns_2_and_prints_no_traceback(tmp_path, capsys):
    broken = tmp_path / "broken.yaml"
    # missing required top-level keys (sink, repo, model, executor)
    broken.write_text("name: broken\nsource: {type: file}\n", encoding="utf-8")

    code = main(["validate", "-c", str(broken)])
    captured = capsys.readouterr()

    assert code == 2
    assert "Traceback" not in captured.err
    assert captured.err.strip() != ""


def test_validate_unparseable_yaml_returns_2_and_prints_no_traceback(tmp_path, capsys):
    broken = tmp_path / "evil.yaml"
    broken.write_text(
        "cmd: !!python/object/apply:os.system ['echo pwned']\n", encoding="utf-8"
    )

    code = main(["validate", "-c", str(broken)])
    captured = capsys.readouterr()

    assert code == 2
    assert "Traceback" not in captured.err


def test_config_show_output_contains_unexpanded_env_ref(tmp_path, capsys, monkeypatch):
    profile_path = tmp_path / "secret.yaml"
    profile_path.write_text(
        """
name: secret-profile
source: {type: jira, token: "${JIRA_API_TOKEN}"}
sink: {type: file}
repo: {type: git_local, path: "."}
model:
  default: main
  providers:
    main: {type: anthropic, model: claude-opus-5}
executor:
  default: inline
  kinds:
    inline: {type: api, model: main}
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)

    code = main(["config", "show", str(profile_path)])
    captured = capsys.readouterr()

    assert code == 0
    assert "${" in captured.out
    assert "JIRA_API_TOKEN" in captured.out


def test_config_init_writes_valid_profile_and_refuses_overwrite(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)

    code = main(["config", "init", "demo", "--dir", "profiles"])
    capsys.readouterr()
    assert code == 0

    profile_path = tmp_path / "profiles" / "demo.yaml"
    assert profile_path.exists()

    code = main(["validate", "-c", str(profile_path)])
    captured = capsys.readouterr()
    assert code == 0
    assert "OK" in captured.out

    # second init without --force refuses to overwrite
    code = main(["config", "init", "demo", "--dir", "profiles"])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.err.strip() != ""

    # --force allows it
    code = main(["config", "init", "demo", "--dir", "profiles", "--force"])
    capsys.readouterr()
    assert code == 0


def test_config_list_skips_underscore_prefixed_files(tmp_path, capsys):
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "_base.yaml").write_text("name: base\n", encoding="utf-8")
    (profiles_dir / "visible.yaml").write_text("name: visible\n", encoding="utf-8")

    code = main(["config", "list", "--dir", str(profiles_dir)])
    captured = capsys.readouterr()

    assert code == 0
    assert "visible" in captured.out
    assert "_base" not in captured.out
    assert str(profiles_dir / "_base.yaml") not in captured.out
