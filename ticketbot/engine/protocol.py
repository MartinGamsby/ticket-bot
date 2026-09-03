"""The `QUESTION:`/`DEFER:` escalation protocol.

A step's `ExecResult.text` is the free-form "Return ONLY:" payload a role prompt
produces. Two markers, each recognized only on a line whose STRIPPED form starts
with the marker, carry structured signal inside that free text:

- `QUESTION:` — the step is blocked on a decision only a human (or the orchestrator,
  per `gates.on_unclear`) can make. Everything from that line to the end of the text
  is the question block; section 8 relays it and pauses the run.
- `DEFER:` — a reviewer/security step's non-blocking follow-up, one per line. These
  are kept in the report; only the `QUESTION:` block is ever stripped out.

A marker is only recognized OUTSIDE a fenced code block (``` ... ```), so a step
that quotes example output containing the literal string "QUESTION:" does not
accidentally trip escalation.
"""

from __future__ import annotations

QUESTION_MARKER = "QUESTION:"
DEFER_MARKER = "DEFER:"

_FENCE = "```"


def _non_fenced_lines(text: str) -> list[tuple[int, str]]:
    """(index, line) pairs for every line of `text` that is NOT inside a fenced code
    block. Fence delimiter lines themselves (the ``` lines) are excluded too — they
    are markup, never marker content.
    """
    lines = text.split("\n")
    in_fence = False
    result: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        if line.strip().startswith(_FENCE):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        result.append((i, line))
    return result


def parse_question(text: str) -> str | None:
    """A QUESTION: block is a line whose stripped form STARTS WITH 'QUESTION:' plus
    everything after it to the end of the text. Returns that whole block (including
    the marker line), stripped, or None. Only the FIRST such line starts the block.
    A 'QUESTION:' appearing inside a fenced code block (``` ... ```) is ignored.
    """
    lines = text.split("\n")
    for i, line in _non_fenced_lines(text):
        if line.strip().startswith(QUESTION_MARKER):
            return "\n".join(lines[i:]).strip()
    return None


def parse_defers(text: str) -> list[str]:
    """Every line whose stripped form starts with 'DEFER:'; returns the text AFTER
    the marker, stripped, one entry per line, in order. Lines inside fenced code
    blocks are ignored. Empty payloads are dropped.
    """
    defers: list[str] = []
    for _, line in _non_fenced_lines(text):
        stripped = line.strip()
        if stripped.startswith(DEFER_MARKER):
            payload = stripped[len(DEFER_MARKER):].strip()
            if payload:
                defers.append(payload)
    return defers


def strip_protocol(text: str) -> str:
    """`text` with the QUESTION: block removed (DEFER: lines are kept — they are
    part of the reviewer's report).
    """
    lines = text.split("\n")
    for i, line in _non_fenced_lines(text):
        if line.strip().startswith(QUESTION_MARKER):
            return "\n".join(lines[:i]).rstrip()
    return text


def has_question(text: str) -> bool:
    return parse_question(text) is not None
