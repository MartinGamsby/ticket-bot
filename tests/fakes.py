"""Shared test fakes. Created in section 3 (`FakeModelProvider` helpers); later
sections append `FakeExecutor`, `FakeRuntime`, `FakeSource`, `FakeSink` here.
"""

from __future__ import annotations

from ticketbot.models.base import ProviderMessage, ToolCall
from ticketbot.models.fake import FakeModelProvider


def fake_provider(*texts: str) -> FakeModelProvider:
    """A `FakeModelProvider` whose script is exactly these texts, in order (each
    becomes an end_turn `ProviderMessage`)."""
    return FakeModelProvider(script=list(texts))


def tool_turn(name: str, input: dict, *, id: str = "toolu_1") -> ProviderMessage:
    return ProviderMessage(text="", tool_calls=[ToolCall(id, name, input)], stop_reason="tool_use")


def text_turn(text: str) -> ProviderMessage:
    return ProviderMessage(text=text, stop_reason="end_turn")
