"""Scripted `ModelProvider` — the workhorse of every later section's tests.

No network, no subprocess, fully deterministic: given a script of turns, it plays
them back in order and keeps repeating the last one once the script is exhausted, so
an executor's tool loop terminates instead of hanging on `StopIteration`.
"""

from __future__ import annotations

from typing import Any

from ..config.schema import AdapterConfig
from .base import Msg, ProviderMessage, ToolCall, ToolDef, Usage

_FAKE_USAGE = Usage(input_tokens=10, output_tokens=10)


def _normalize_entry(entry: "ProviderMessage | str | dict[str, Any]") -> ProviderMessage:
    if isinstance(entry, ProviderMessage):
        return entry
    if isinstance(entry, str):
        return ProviderMessage(text=entry)
    if isinstance(entry, dict):
        tool_calls = [
            ToolCall(id=str(tc.get("id", "")), name=tc["name"], input=dict(tc.get("input", {})))
            for tc in entry.get("tool_calls", [])
        ]
        stop_reason = entry.get("stop_reason")
        if stop_reason is None:
            stop_reason = "tool_use" if tool_calls else "end_turn"
        return ProviderMessage(text=entry.get("text", ""), tool_calls=tool_calls, stop_reason=stop_reason)
    raise TypeError(f"fake model script entry has unsupported type: {type(entry)!r}")


class FakeModelProvider:
    provider_id = "fake"

    def __init__(
        self,
        cfg: AdapterConfig | None = None,
        *,
        script: list[ProviderMessage | str] | None = None,
        name: str = "fake-model",
    ) -> None:
        """`script` entries are returned in order; a plain str becomes
        `ProviderMessage(text=str)`. When the script runs out, the LAST entry repeats
        (so an unbounded loop still terminates on an end_turn message).

        Also constructible from an `AdapterConfig` with `{type: fake, script: [...]}`
        where each entry is a str or `{text, tool_calls: [{name, input}], stop_reason}`.
        An explicit `script=` kwarg always wins over `cfg.opt("script")`.
        """
        if script is not None:
            raw_script: list[Any] = list(script)
        elif cfg is not None:
            raw_script = list(cfg.opt("script") or [])
        else:
            raw_script = []

        normalized = [_normalize_entry(e) for e in raw_script]
        self._script: list[ProviderMessage] = normalized or [ProviderMessage(text="")]

        if cfg is not None:
            self.name: str = str(cfg.opt("name", name))
        else:
            self.name = name

        self.calls: list[dict[str, Any]] = []
        self._call_index = 0
        self._next_tool_id = 0

    def describe(self) -> str:
        return f"{self.name} (fake)"

    def complete(
        self,
        *,
        system: str,
        messages: list[Msg],
        tools: list[ToolDef] | None = None,
        max_tokens: int | None = None,
    ) -> ProviderMessage:
        self.calls.append(
            {"system": system, "messages": messages, "tools": tools, "max_tokens": max_tokens}
        )

        index = min(self._call_index, len(self._script) - 1)
        self._call_index += 1
        template = self._script[index]

        tool_calls: list[ToolCall] = []
        for call in template.tool_calls:
            if not call.id:
                call = ToolCall(id=f"toolu_{self._next_tool_id}", name=call.name, input=call.input)
                self._next_tool_id += 1
            tool_calls.append(call)

        return ProviderMessage(
            text=template.text,
            tool_calls=tool_calls,
            stop_reason=template.stop_reason,
            usage=_FAKE_USAGE,
            native=template.native,
            raw=template.raw,
        )
