"""`FileSource` -- no network. Front matter is untrusted input: `yaml.safe_load`
only, malformed YAML raises `SourceError` naming the file (never crashes the
poller), and unknown keys land in `raw`, never on an attribute.
"""

from __future__ import annotations

import time

import pytest

from ticketbot.adapters.sources.base import SourceError, WorkItemNotFound
from ticketbot.adapters.sources.file import FileSource
from ticketbot.config.schema import AdapterConfig


def _cfg(**opts) -> AdapterConfig:
    return AdapterConfig(type="file", **opts)


# ---- front matter -----------------------------------------------------------


def test_front_matter_parsed_into_the_right_fields(tmp_path):
    (tmp_path / "ticket.md").write_text(
        "---\n"
        "key: ENG-1842\n"
        "title: Login times out on SSO\n"
        "type: Bug\n"
        "points: 5\n"
        "labels: [agent, sso]\n"
        "acceptance: |\n"
        "  - SSO login completes in under 5s\n"
        "  - A failure shows a retryable error\n"
        "---\n"
        "Users on the acme SSO tenant see a spinner.\n",
        encoding="utf-8",
    )
    source = FileSource(_cfg(path="ticket.md"), base_dir=tmp_path)
    item = source.fetch()

    assert item.external_id == "ENG-1842"
    assert item.title == "Login times out on SSO"
    assert item.issue_type == "Bug"
    assert item.story_points == 5.0
    assert item.labels == ["agent", "sso"]
    assert "SSO login completes in under 5s" in item.acceptance
    assert "Users on the acme SSO tenant see a spinner." in item.description
    assert item.id == "ENG-1842"


def test_points_non_numeric_ignored_with_rest_intact(tmp_path, caplog):
    (tmp_path / "t.md").write_text("---\ntitle: T\npoints: abc\nlabels: x\n---\nbody\n", encoding="utf-8")
    source = FileSource(_cfg(path="t.md"), base_dir=tmp_path)
    item = source.fetch()

    assert item.story_points is None
    assert item.title == "T"
    assert item.labels == ["x"]


def test_malformed_yaml_raises_source_error_naming_the_file(tmp_path):
    bad = tmp_path / "bad.md"
    bad.write_text("---\nkey: [unclosed\n---\nbody\n", encoding="utf-8")
    source = FileSource(_cfg(path="bad.md"), base_dir=tmp_path)

    with pytest.raises(SourceError) as exc_info:
        source.fetch()
    assert str(bad.resolve()) in str(exc_info.value) or "bad.md" in str(exc_info.value)


def test_non_mapping_front_matter_raises_source_error(tmp_path):
    bad = tmp_path / "bad2.md"
    bad.write_text("---\n- just\n- a\n- list\n---\nbody\n", encoding="utf-8")
    source = FileSource(_cfg(path="bad2.md"), base_dir=tmp_path)

    with pytest.raises(SourceError):
        source.fetch()


def test_unclosed_front_matter_delimiter_treats_whole_file_as_body(tmp_path):
    (tmp_path / "t.md").write_text("---\nkey: ENG-1\nno closing delimiter here\n", encoding="utf-8")
    source = FileSource(_cfg(path="t.md"), base_dir=tmp_path)
    item = source.fetch()
    assert item.external_id is None
    assert "no closing delimiter here" in item.description


def test_unknown_front_matter_keys_land_in_raw_not_on_attributes(tmp_path):
    (tmp_path / "t.md").write_text(
        "---\ntitle: T\ncustom_field: surprise\nanother: 123\n---\nbody\n", encoding="utf-8"
    )
    source = FileSource(_cfg(path="t.md"), base_dir=tmp_path)
    item = source.fetch()

    assert item.raw == {"custom_field": "surprise", "another": 123}
    assert not hasattr(item, "custom_field")


def test_yaml_front_matter_never_executes_arbitrary_tags(tmp_path):
    # A YAML tag that `yaml.load` (unsafe) would try to execute; `safe_load` must
    # reject it rather than instantiate anything.
    (tmp_path / "evil.md").write_text(
        "---\ntitle: !!python/object/apply:os.system ['echo pwned']\n---\nbody\n", encoding="utf-8"
    )
    source = FileSource(_cfg(path="evil.md"), base_dir=tmp_path)
    with pytest.raises(SourceError):
        source.fetch()


# ---- title fallback chain -----------------------------------------------------


def test_title_falls_back_to_heading_then_first_line_then_stem(tmp_path):
    (tmp_path / "heading-file.md").write_text("intro line\n\n# The Real Title\n\nmore text\n", encoding="utf-8")
    item = FileSource(_cfg(path="heading-file.md"), base_dir=tmp_path).fetch()
    assert item.title == "The Real Title"


def test_title_falls_back_to_first_non_empty_line_when_no_heading(tmp_path):
    (tmp_path / "no-heading.md").write_text("\n\nJust a plain first line here\nsecond line\n", encoding="utf-8")
    item = FileSource(_cfg(path="no-heading.md"), base_dir=tmp_path).fetch()
    assert item.title == "Just a plain first line here"


def test_title_falls_back_to_file_stem_when_body_is_empty(tmp_path):
    (tmp_path / "my-task-name.md").write_text("", encoding="utf-8")
    item = FileSource(_cfg(path="my-task-name.md"), base_dir=tmp_path).fetch()
    assert item.title == "my-task-name"


def test_title_falls_back_to_untitled_task_when_nothing_else_is_available():
    source = FileSource(_cfg())
    item = source._from_text("   \n   \n")  # whitespace-only body, no front matter
    assert item.title == "untitled task"


def test_no_front_matter_uses_whole_file_as_description(tmp_path):
    (tmp_path / "plain.md").write_text("Just prose, no front matter at all.\n", encoding="utf-8")
    item = FileSource(_cfg(path="plain.md"), base_dir=tmp_path).fetch()
    assert item.external_id is None
    assert "Just prose, no front matter at all." in item.description


# ---- --input-text style construction -------------------------------------------


def test_input_text_construction_uses_cfg_text():
    source = FileSource(_cfg(text="# Add a health endpoint\n\nDescribe the work here."))
    item = source.fetch()
    assert item.title == "Add a health endpoint"
    assert item.source_ref == "--input-text"
    assert item.external_id is None


def test_fetch_with_no_path_no_text_raises_work_item_not_found():
    source = FileSource(_cfg())
    with pytest.raises(WorkItemNotFound):
        source.fetch()


def test_fetch_external_id_treated_as_path_when_it_exists(tmp_path):
    (tmp_path / "ad-hoc.md").write_text("# Ad hoc ticket\n\nbody text\n", encoding="utf-8")
    source = FileSource(_cfg(text="fallback text"), base_dir=tmp_path)
    item = source.fetch(external_id="ad-hoc.md")
    assert item.title == "Ad hoc ticket"


def test_fetch_external_id_falls_through_to_configured_path_when_not_a_file(tmp_path):
    (tmp_path / "configured.md").write_text("# Configured ticket\n", encoding="utf-8")
    source = FileSource(_cfg(path="configured.md"), base_dir=tmp_path)
    item = source.fetch(external_id="does-not-exist.md")
    assert item.title == "Configured ticket"


# ---- poll() ---------------------------------------------------------------------


def test_poll_skips_files_under_processed_dir_and_yields_in_mtime_order(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    processed = inbox / "processed"
    processed.mkdir()

    (processed / "old.md").write_text("# Already processed\n", encoding="utf-8")

    first = inbox / "a.md"
    first.write_text("# First\n", encoding="utf-8")
    time.sleep(0.02)
    second = inbox / "b.md"
    second.write_text("# Second\n", encoding="utf-8")

    source = FileSource(_cfg(glob="inbox/*.md", processed_dir="inbox/processed"), base_dir=tmp_path)
    items = list(source.poll())

    assert [i.title for i in items] == ["First", "Second"]


def test_poll_with_no_matches_yields_nothing(tmp_path):
    source = FileSource(_cfg(glob="inbox/*.md"), base_dir=tmp_path)
    assert list(source.poll()) == []


def test_mark_processed_moves_the_file(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    f = inbox / "task.md"
    f.write_text("# A task\n", encoding="utf-8")

    source = FileSource(_cfg(glob="inbox/*.md", processed_dir="inbox/processed"), base_dir=tmp_path)
    item = next(source.poll())
    source.mark_processed(item)

    assert not f.exists()
    assert (inbox / "processed" / "task.md").exists()


def test_mark_processed_is_a_no_op_for_input_text():
    source = FileSource(_cfg())
    item = source._from_text("# whatever\n")
    source.mark_processed(item)  # must not raise


# ---- claim() --------------------------------------------------------------------


def test_claim_always_true(tmp_path):
    source = FileSource(_cfg(text="anything"))
    item = source.fetch()
    assert source.claim(item) is True


# ---- describe() -------------------------------------------------------------------


def test_describe_plain_when_no_path():
    assert FileSource(_cfg(text="x")).describe() == "file"


def test_describe_includes_path_name(tmp_path):
    (tmp_path / "ticket.md").write_text("x", encoding="utf-8")
    source = FileSource(_cfg(path="ticket.md"), base_dir=tmp_path)
    assert source.describe() == "file (ticket.md)"
