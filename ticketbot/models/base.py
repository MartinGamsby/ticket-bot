"""Provider-neutral message/tool types and the `ModelProvider` protocol.

Every concrete provider (`anthropic`, `openai_compat`, `fake`) turns
"system + messages [+ tools]" into "text + tool calls + usage" through this shared
shape. `Msg.native`/`native_provider` is the escape hatch that lets a provider replay
its OWN previously-returned content verbatim (Anthropic thinking blocks must be
echoed back unchanged on the same model) without other providers needing to know
what that content looks like.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable


@dataclass(frozen=True)
class TextBlock:
    text: str


@dataclass(frozen=True)
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False


Block = TextBlock | ToolUseBlock | ToolResultBlock


@dataclass
class Msg:
    role: Literal["user", "assistant"]
    content: str | list[Block]
    # Provider-native content, kept so a provider can replay ITS OWN blocks byte-for-byte
    # (Anthropic thinking blocks MUST be echoed back unchanged on the same model).
    native: Any = None
    native_provider: str | None = None


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    input_schema: dict[str, Any]  # JSON Schema; object with `properties` + `required`


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
        )


@dataclass
class ProviderMessage:
    text: str  # concatenated text blocks, stripped
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"  # end_turn | tool_use | max_tokens | refusal | ...
    usage: Usage = Usage()
    native: Any = None  # native content list (for Msg.native)
    raw: Any = None


class ProviderError(RuntimeError):
    """A model provider call failed. Concrete providers wrap their SDK/HTTP
    exceptions in this so the engine never has to know which SDK is underneath.
    """


class ProviderRefusal(ProviderError):
    """The model declined to answer (Anthropic `stop_reason == "refusal"` and
    equivalents). `category` is the provider's classification when it supplies one.
    """

    def __init__(self, message: str, category: str | None = None) -> None:
        super().__init__(message)
        self.category = category


@runtime_checkable
class ModelProvider(Protocol):
    provider_id: str  # "anthropic" | "openai_compat" | "fake"

    def describe(self) -> str: ...  # "Claude Opus 5 (claude-opus-5)"

    def complete(
        self,
        *,
        system: str,
        messages: list[Msg],
        tools: list[ToolDef] | None = None,
        max_tokens: int | None = None,
    ) -> ProviderMessage: ...


# USD per 1M tokens: (input, output). Keep in sync with the claude-api skill's model table.
PRICING: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
DISPLAY_NAMES: dict[str, str] = {
    "claude-fable-5": "Claude Fable 5",
    "claude-opus-5": "Claude Opus 5",
    "claude-opus-4-8": "Claude Opus 4.8",
    "claude-opus-4-7": "Claude Opus 4.7",
    "claude-opus-4-6": "Claude Opus 4.6",
    "claude-sonnet-5": "Claude Sonnet 5",
    "claude-sonnet-4-6": "Claude Sonnet 4.6",
    "claude-haiku-4-5": "Claude Haiku 4.5",
}

_CACHE_READ_RATE = 0.10  # of the input rate
_CACHE_WRITE_RATE = 1.25  # of the input rate


def estimate_cost(model: str, usage: Usage) -> float:
    """(in/1e6)*rate_in + (out/1e6)*rate_out; 0.0 for an unknown model id.
    Cached reads bill at 10% of the input rate; cache writes at 125%.
    """
    rates = PRICING.get(model)
    if rates is None:
        return 0.0
    rate_in, rate_out = rates
    return (
        (usage.input_tokens / 1_000_000) * rate_in
        + (usage.output_tokens / 1_000_000) * rate_out
        + (usage.cache_read_tokens / 1_000_000) * rate_in * _CACHE_READ_RATE
        + (usage.cache_write_tokens / 1_000_000) * rate_in * _CACHE_WRITE_RATE
    )


def display_name(model: str) -> str:
    """DISPLAY_NAMES.get(model, model)."""
    return DISPLAY_NAMES.get(model, model)


def blocks_to_text(content: str | list[Block]) -> str:
    """Concatenate only the `TextBlock`s in `content`; a plain string is returned
    unchanged (it is already text).
    """
    if isinstance(content, str):
        return content
    return "".join(block.text for block in content if isinstance(block, TextBlock))
