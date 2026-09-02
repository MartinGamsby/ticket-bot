"""Markdown <-> Atlassian Document Format (ADF).

Jira Cloud comment (and description) bodies are ADF, not markdown -- posting raw
markdown produces a wall of literal asterisks. `markdown_to_adf` converts the
subset that matters (paragraphs, headings, fenced code, bullet/ordered lists,
links, bold/italic/inline-code, horizontal rules) and degrades safely: anything
the inline parser cannot handle stays literal text (never guessed at), and if the
whole conversion raises, the result falls back to a single readable code block
rather than posting broken markup.

`adf_to_text` is the inverse direction, used to read Jira descriptions and
comments back out as plain text. Its input is untrusted (a Jira API response), so
its recursive walk is depth- and shape-bounded and never raises regardless of how
the document is malformed.
"""

from __future__ import annotations

import re
from typing import Any

# Bounds pathological input in both directions: a hostile/deeply-nested ADF
# document being read, or a markdown document with thousands of list markers
# being written.
_MAX_DEPTH = 6
_MAX_NODES = 5000

_ATX_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE = re.compile(r"^```\s*([^\s`]*)\s*$")
_HR = re.compile(r"^(-{3,}|\*{3,})\s*$")
_ORDERED_ITEM = re.compile(r"^(\d+)\.\s+(.*)$")
_BULLET_ITEM = re.compile(r"^[-*]\s+(.*)$")

# Alternatives are tried in order at each position, so `**bold**` is claimed by
# the bold alternative before the single-`*` italic one gets a chance at it. Only
# an http(s) href is recognised as a link at all -- anything else simply doesn't
# match here and falls through as literal text (brackets and all), per "never
# guess".
_INLINE = re.compile(
    r"\[(?P<link_text>[^\]]*)\]\((?P<href>https?://[^)\s]+)\)"
    r"|\*\*(?P<bold>[^*]+)\*\*"
    r"|(?<!\*)\*(?P<italic1>[^*\n]+)\*(?!\*)"
    r"|_(?P<italic2>[^_\n]+)_"
    r"|`(?P<code>[^`\n]+)`"
)


# --------------------------------------------------------------------------- #
# markdown -> ADF
# --------------------------------------------------------------------------- #


def markdown_to_adf(md: str) -> dict:
    """`{"type": "doc", "version": 1, "content": [...]}`.

    Never raises: any exception during conversion is caught here and replaced
    with a single-`codeBlock` fallback document.
    """
    try:
        return _convert(md)
    except Exception:  # noqa: BLE001 - a readable code block beats broken markup
        return _fallback_doc(md)


def _fallback_doc(md: str) -> dict:
    content: list[dict] = [{"type": "codeBlock", "content": [{"type": "text", "text": md}]}] if md else [
        {"type": "codeBlock"}
    ]
    return {"type": "doc", "version": 1, "content": content}


def _convert(md: str) -> dict:
    if md == "":
        return {"type": "doc", "version": 1, "content": [{"type": "paragraph"}]}
    lines = md.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks = _blocks_from_lines(lines)
    nodes = _blocks_to_adf_nodes(blocks)
    if not nodes:
        nodes = [{"type": "paragraph"}]
    return {"type": "doc", "version": 1, "content": nodes}


def _blocks_from_lines(lines: list[str]) -> list[dict]:
    """First pass: group raw lines into block-shaped dicts (paragraph/heading text
    not yet inline-parsed). Purely line-oriented and iterative -- no recursion, so
    thousands of consecutive list markers cannot blow the stack.
    """
    blocks: list[dict] = []
    i = 0
    n = len(lines)

    def _is_block_start(stripped: str) -> bool:
        return bool(
            _FENCE.match(stripped)
            or _HR.match(stripped)
            or _ATX_HEADING.match(stripped)
            or _ORDERED_ITEM.match(stripped)
            or _BULLET_ITEM.match(stripped)
        )

    while i < n:
        stripped = lines[i].strip()

        if stripped == "":
            i += 1
            continue

        fence_m = _FENCE.match(stripped)
        if fence_m:
            lang = fence_m.group(1) or None
            code_lines: list[str] = []
            i += 1
            while i < n and lines[i].strip() != "```":
                code_lines.append(lines[i])
                i += 1
            if i < n:
                i += 1  # consume the closing fence
            blocks.append({"type": "codeBlock", "language": lang, "code": "\n".join(code_lines)})
            continue

        if _HR.match(stripped):
            blocks.append({"type": "rule"})
            i += 1
            continue

        heading_m = _ATX_HEADING.match(stripped)
        if heading_m:
            blocks.append({"type": "heading", "level": len(heading_m.group(1)), "text": heading_m.group(2).strip()})
            i += 1
            continue

        ordered_m = _ORDERED_ITEM.match(stripped)
        if ordered_m:
            order = int(ordered_m.group(1))
            items: list[str] = []
            while i < n:
                m = _ORDERED_ITEM.match(lines[i].strip())
                if not m:
                    break
                items.append(m.group(2).strip())
                i += 1
            blocks.append({"type": "orderedList", "order": order, "items": items})
            continue

        bullet_m = _BULLET_ITEM.match(stripped)
        if bullet_m:
            items = []
            while i < n:
                m = _BULLET_ITEM.match(lines[i].strip())
                if not m:
                    break
                items.append(m.group(1).strip())
                i += 1
            blocks.append({"type": "bulletList", "items": items})
            continue

        # paragraph: accumulate until a blank line or the start of another block
        para_lines = [stripped]
        i += 1
        while i < n:
            nxt = lines[i].strip()
            if nxt == "" or _is_block_start(nxt):
                break
            para_lines.append(nxt)
            i += 1
        blocks.append({"type": "paragraph", "text": " ".join(para_lines)})

    return blocks


def _blocks_to_adf_nodes(blocks: list[dict]) -> list[dict]:
    nodes: list[dict] = []
    budget = [0]
    for block in blocks:
        if budget[0] >= _MAX_NODES:
            break
        node = _block_to_node(block, budget)
        if node is not None:
            nodes.append(node)
    return nodes


def _block_to_node(block: dict, budget: list[int]) -> dict | None:
    btype = block["type"]

    if btype == "paragraph":
        content = _inline_nodes(block["text"], budget)
        if not content:
            return None  # a paragraph with no inline content is skipped entirely
        budget[0] += 1
        return {"type": "paragraph", "content": content}

    if btype == "heading":
        content = _inline_nodes(block["text"], budget)
        node: dict[str, Any] = {"type": "heading", "attrs": {"level": block["level"]}}
        if content:
            node["content"] = content
        budget[0] += 1
        return node

    if btype == "codeBlock":
        node = {"type": "codeBlock", "attrs": {"language": block["language"]}}
        if block["code"]:
            node["content"] = [{"type": "text", "text": block["code"]}]
        budget[0] += 1
        return node

    if btype == "rule":
        budget[0] += 1
        return {"type": "rule"}

    if btype in ("bulletList", "orderedList"):
        items: list[dict] = []
        for item_text in block["items"]:
            if budget[0] >= _MAX_NODES:
                break
            inline = _inline_nodes(item_text, budget)
            para: dict[str, Any] = {"type": "paragraph"}
            if inline:
                para["content"] = inline
            items.append({"type": "listItem", "content": [para]})
            budget[0] += 2
        if not items:
            return None
        node = {"type": btype, "content": items}
        if btype == "orderedList":
            node["attrs"] = {"order": block["order"]}
        return node

    return None  # unreachable: every block type produced by _blocks_from_lines is handled above


def _inline_nodes(text: str, budget: list[int]) -> list[dict]:
    """Parse `text` into ADF inline text/mark nodes. Never emits an empty text node."""
    nodes: list[dict] = []
    pos = 0
    for m in _INLINE.finditer(text):
        if budget[0] >= _MAX_NODES:
            break
        start, end = m.span()
        if start > pos:
            literal = text[pos:start]
            if literal:
                nodes.append({"type": "text", "text": literal})
                budget[0] += 1
        node = _inline_match_to_node(m)
        if node is not None:
            nodes.append(node)
            budget[0] += 1
        pos = end
    tail = text[pos:]
    if tail:
        nodes.append({"type": "text", "text": tail})
        budget[0] += 1
    return nodes


def _inline_match_to_node(m: re.Match) -> dict | None:
    if m.group("href") is not None:
        text = m.group("link_text") or m.group("href")
        if not text:
            return None
        return {"type": "text", "text": text, "marks": [{"type": "link", "attrs": {"href": m.group("href")}}]}
    if m.group("bold") is not None:
        text = m.group("bold")
        return {"type": "text", "text": text, "marks": [{"type": "strong"}]} if text else None
    if m.group("italic1") is not None:
        text = m.group("italic1")
        return {"type": "text", "text": text, "marks": [{"type": "em"}]} if text else None
    if m.group("italic2") is not None:
        text = m.group("italic2")
        return {"type": "text", "text": text, "marks": [{"type": "em"}]} if text else None
    text = m.group("code")
    return {"type": "text", "text": text, "marks": [{"type": "code"}]} if text else None


# --------------------------------------------------------------------------- #
# ADF -> plain text
# --------------------------------------------------------------------------- #


def adf_to_text(doc: Any) -> str:
    """ADF (a dict), a plain string, or `None` -> readable plain text.

    Walks `content` recursively, joining text nodes and emitting a blank line
    between block nodes; renders `codeBlock` fenced and `link` marks as
    `[text](href)`. An unknown node type contributes its nested text rather than
    being dropped or raising. Recursion is depth-bounded so a hostile/deeply
    nested document cannot blow the stack -- this is the one direction that reads
    untrusted network input.
    """
    if doc is None:
        return ""
    if isinstance(doc, str):
        return doc
    if not isinstance(doc, dict):
        return ""
    blocks = _render_children_as_blocks(doc.get("content"), depth=0)
    return "\n\n".join(b for b in blocks if b)


def _render_children_as_blocks(content: Any, *, depth: int) -> list[str]:
    if depth > _MAX_DEPTH or not isinstance(content, list):
        return []
    blocks: list[str] = []
    for node in content:
        rendered = _render_block(node, depth=depth + 1)
        if rendered:
            blocks.append(rendered)
    return blocks


def _render_block(node: Any, *, depth: int) -> str:
    if depth > _MAX_DEPTH or not isinstance(node, dict):
        return ""
    node_type = node.get("type")

    if node_type == "codeBlock":
        code = _render_inline(node.get("content"), depth=depth + 1)
        lang = (node.get("attrs") or {}).get("language") or ""
        return f"```{lang}\n{code}\n```"

    if node_type == "rule":
        return "---"

    if node_type in ("bulletList", "orderedList"):
        lines: list[str] = []
        for i, item in enumerate(node.get("content") or [], start=1):
            text = _render_list_item(item, depth=depth + 1)
            if not text:
                continue
            prefix = f"{i}. " if node_type == "orderedList" else "- "
            lines.append(prefix + text)
        return "\n".join(lines)

    if node_type in ("paragraph", "heading"):
        return _render_inline(node.get("content"), depth=depth + 1)

    # blockquote, doc-within-doc, and any unrecognised block-ish node: walk its
    # content so nested text is never silently lost.
    if isinstance(node.get("content"), list):
        return "\n\n".join(_render_children_as_blocks(node.get("content"), depth=depth))
    return ""


def _render_list_item(node: Any, *, depth: int) -> str:
    if depth > _MAX_DEPTH or not isinstance(node, dict):
        return ""
    inner_blocks = _render_children_as_blocks(node.get("content"), depth=depth)
    return " ".join(inner_blocks)


def _render_inline(content: Any, *, depth: int) -> str:
    if depth > _MAX_DEPTH or not isinstance(content, list):
        return ""
    parts: list[str] = []
    for node in content:
        if not isinstance(node, dict):
            continue
        node_type = node.get("type")
        if node_type == "text":
            text = node.get("text")
            if not isinstance(text, str):
                continue
            href = None
            for mark in node.get("marks") or []:
                if isinstance(mark, dict) and mark.get("type") == "link":
                    href = (mark.get("attrs") or {}).get("href")
            parts.append(f"[{text}]({href})" if href else text)
        elif node_type == "hardBreak":
            parts.append("\n")
        elif isinstance(node.get("content"), list):
            # unknown inline-ish node: recurse into its content rather than drop it
            parts.append(_render_inline(node.get("content"), depth=depth + 1))
    return "".join(parts)
