"""`markdown_to_adf` / `adf_to_text` -- no network, pure conversion. Covers the
section-6 security rail on this module: bounded recursion/node count for
pathological input, so a hostile ticket description or Jira response cannot hang
or crash the process.
"""

from __future__ import annotations

from typing import Any

import pytest

from ticketbot.adapters.sinks.adf import adf_to_text, markdown_to_adf


def _find_empty_text_nodes(node: Any, path: str = "doc") -> list[str]:
    hits: list[str] = []
    if isinstance(node, dict):
        if node.get("type") == "text" and node.get("text") == "":
            hits.append(path)
        for key, value in node.items():
            hits.extend(_find_empty_text_nodes(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            hits.extend(_find_empty_text_nodes(value, f"{path}[{i}]"))
    return hits


def _assert_no_empty_text_nodes(doc: dict) -> None:
    assert _find_empty_text_nodes(doc) == []


def _count_nodes(node: Any) -> int:
    if isinstance(node, dict):
        return 1 + sum(_count_nodes(v) for v in node.values())
    if isinstance(node, list):
        return sum(_count_nodes(v) for v in node)
    return 0


# ---- doc envelope -----------------------------------------------------------


def test_doc_envelope():
    doc = markdown_to_adf("hello")
    assert doc["type"] == "doc"
    assert doc["version"] == 1
    assert isinstance(doc["content"], list)


def test_empty_markdown_is_single_empty_paragraph():
    doc = markdown_to_adf("")
    assert doc["content"] == [{"type": "paragraph"}]
    _assert_no_empty_text_nodes(doc)


# ---- paragraphs ---------------------------------------------------------------


def test_blank_line_separated_paragraphs():
    doc = markdown_to_adf("first paragraph\n\nsecond paragraph")
    paragraphs = [n for n in doc["content"] if n["type"] == "paragraph"]
    assert len(paragraphs) == 2
    assert paragraphs[0]["content"] == [{"type": "text", "text": "first paragraph"}]
    assert paragraphs[1]["content"] == [{"type": "text", "text": "second paragraph"}]


# ---- headings -------------------------------------------------------------------


@pytest.mark.parametrize("level", [1, 2, 3, 4, 5, 6])
def test_each_heading_level(level):
    md = "#" * level + " Heading text"
    doc = markdown_to_adf(md)
    heading = doc["content"][0]
    assert heading["type"] == "heading"
    assert heading["attrs"]["level"] == level
    assert heading["content"] == [{"type": "text", "text": "Heading text"}]


# ---- fenced code ----------------------------------------------------------------


def test_fenced_code_block_with_language():
    doc = markdown_to_adf("```python\nprint(1)\nprint(2)\n```")
    node = doc["content"][0]
    assert node["type"] == "codeBlock"
    assert node["attrs"]["language"] == "python"
    assert node["content"] == [{"type": "text", "text": "print(1)\nprint(2)"}]


def test_fenced_code_block_without_language():
    doc = markdown_to_adf("```\nraw code\n```")
    node = doc["content"][0]
    assert node["type"] == "codeBlock"
    assert node["attrs"]["language"] is None
    assert node["content"] == [{"type": "text", "text": "raw code"}]


# ---- lists ----------------------------------------------------------------------


def test_bullet_list():
    doc = markdown_to_adf("- one\n- two\n- three")
    node = doc["content"][0]
    assert node["type"] == "bulletList"
    texts = [item["content"][0]["content"][0]["text"] for item in node["content"]]
    assert texts == ["one", "two", "three"]


def test_ordered_list_uses_first_number_as_order():
    doc = markdown_to_adf("5. five\n6. six\n7. seven")
    node = doc["content"][0]
    assert node["type"] == "orderedList"
    assert node["attrs"]["order"] == 5
    texts = [item["content"][0]["content"][0]["text"] for item in node["content"]]
    assert texts == ["five", "six", "seven"]


# ---- inline marks -----------------------------------------------------------------


def test_inline_link():
    doc = markdown_to_adf("see [the docs](https://example.com/x)")
    para = doc["content"][0]
    link_node = next(n for n in para["content"] if n["text"] == "the docs")
    assert link_node["marks"] == [{"type": "link", "attrs": {"href": "https://example.com/x"}}]


def test_non_http_link_stays_literal_text():
    doc = markdown_to_adf("see [danger](javascript:alert(1))")
    para = doc["content"][0]
    joined = "".join(n["text"] for n in para["content"])
    assert "[danger](javascript:alert(1))" in joined
    assert not any(n.get("marks") for n in para["content"])


def test_bold_italic_and_inline_code_marks():
    doc = markdown_to_adf("**bold** *italic* _also italic_ `code`")
    para = doc["content"][0]
    marks_by_text = {n["text"]: n.get("marks") for n in para["content"]}
    assert marks_by_text["bold"] == [{"type": "strong"}]
    assert marks_by_text["italic"] == [{"type": "em"}]
    assert marks_by_text["also italic"] == [{"type": "em"}]
    assert marks_by_text["code"] == [{"type": "code"}]


def test_unhandled_inline_syntax_stays_literal():
    doc = markdown_to_adf("weird ~~strikethrough~~ stays as-is")
    para = doc["content"][0]
    joined = "".join(n["text"] for n in para["content"])
    assert "~~strikethrough~~" in joined


# ---- horizontal rule --------------------------------------------------------------


@pytest.mark.parametrize("md", ["---", "***", "-----", "*****"])
def test_horizontal_rule(md):
    doc = markdown_to_adf(md)
    assert doc["content"] == [{"type": "rule"}]


# ---- no empty text nodes, ever -----------------------------------------------------


def test_no_empty_text_nodes_in_a_mixed_document():
    md = (
        "# Heading\n\n"
        "A paragraph with **bold**, *italic*, `code`, and a [link](https://x.test).\n\n"
        "```js\nconsole.log(1)\n```\n\n"
        "- a\n- b\n\n"
        "1. x\n2. y\n\n"
        "---\n"
    )
    doc = markdown_to_adf(md)
    _assert_no_empty_text_nodes(doc)


def test_blank_lines_do_not_create_empty_paragraphs():
    doc = markdown_to_adf("normal text\n\n \n\nmore text")
    paragraphs = [n for n in doc["content"] if n["type"] == "paragraph"]
    assert len(paragraphs) == 2
    _assert_no_empty_text_nodes(doc)


def test_paragraph_with_no_inline_content_is_skipped_entirely():
    import ticketbot.adapters.sinks.adf as adf_module

    # Not reachable through normal markdown (a block's raw text is always
    # non-empty by construction) -- exercised directly to prove the guard works.
    node = adf_module._block_to_node({"type": "paragraph", "text": ""}, [0])
    assert node is None


# ---- pathological input: bounded, not recursive -----------------------------------


def test_pathological_list_input_is_bounded():
    md = "\n".join(f"- item {i}" for i in range(10_000))
    doc = markdown_to_adf(md)  # must return promptly, not hang or blow the stack
    assert _count_nodes(doc) < 20_000
    _assert_no_empty_text_nodes(doc)


def test_pathological_ordered_list_input_is_bounded():
    md = "\n".join(f"{i}. item" for i in range(10_000))
    doc = markdown_to_adf(md)
    assert _count_nodes(doc) < 20_000


# ---- fallback on converter exception ------------------------------------------------


def test_converter_exception_falls_back_to_single_codeblock(monkeypatch):
    import ticketbot.adapters.sinks.adf as adf_module

    def _boom(_md):
        raise RuntimeError("boom")

    monkeypatch.setattr(adf_module, "_convert", _boom)
    doc = markdown_to_adf("anything at all")
    assert doc == {
        "type": "doc",
        "version": 1,
        "content": [{"type": "codeBlock", "content": [{"type": "text", "text": "anything at all"}]}],
    }


def test_fallback_with_empty_markdown_has_no_empty_text_node(monkeypatch):
    import ticketbot.adapters.sinks.adf as adf_module

    def _boom(_md):
        raise RuntimeError("boom")

    monkeypatch.setattr(adf_module, "_convert", _boom)
    doc = markdown_to_adf("")
    _assert_no_empty_text_nodes(doc)
    assert doc == {"type": "doc", "version": 1, "content": [{"type": "codeBlock"}]}


# ---- adf_to_text --------------------------------------------------------------------


def test_adf_to_text_plain_string_passthrough():
    assert adf_to_text("already text") == "already text"


def test_adf_to_text_none_is_empty_string():
    assert adf_to_text(None) == ""


def test_adf_to_text_unknown_node_type_contributes_nested_text():
    doc = {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "mysteryNode", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "hi"}]}]}
        ],
    }
    assert adf_to_text(doc) == "hi"


def test_adf_to_text_round_trips_a_simple_doc():
    doc = markdown_to_adf("Hello **world**, see [docs](https://example.com).")
    text = adf_to_text(doc)
    assert "Hello world, see [docs](https://example.com)." == text


def test_adf_to_text_renders_code_block_fenced():
    doc = {
        "type": "doc",
        "version": 1,
        "content": [{"type": "codeBlock", "attrs": {"language": "python"}, "content": [{"type": "text", "text": "x = 1"}]}],
    }
    assert adf_to_text(doc) == "```python\nx = 1\n```"


def test_adf_to_text_deeply_nested_input_does_not_recurse_infinitely():
    node: dict = {"type": "text", "text": "leaf"}
    for _ in range(5000):
        node = {"type": "paragraph", "content": [node]}
    doc = {"type": "doc", "version": 1, "content": [node]}
    result = adf_to_text(doc)  # must return, not raise RecursionError
    assert isinstance(result, str)


def test_adf_to_text_malformed_input_never_raises():
    weird_inputs = [
        {"type": "doc", "content": "not a list"},
        {"type": "doc", "content": [1, 2, "three", None, {"no": "type"}]},
        {"type": "doc", "content": [{"type": "paragraph", "content": None}]},
        42,
        [],
        True,
    ]
    for weird in weird_inputs:
        assert isinstance(adf_to_text(weird), str)
