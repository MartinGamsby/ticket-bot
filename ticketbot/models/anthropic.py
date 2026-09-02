"""`ModelProvider` backed by the Anthropic API.

The `anthropic` package is imported LAZILY (inside functions, never at module
scope) so that `import ticketbot.models.anthropic` succeeds even when the `anthropic`
package is not installed — the registry only resolves this module when a profile
actually selects `type: anthropic`.

Request shape is copied verbatim from the `claude-api` skill and is NOT negotiable:
always stream (`.messages.stream(...)` + `.get_final_message()` — a large `max_tokens`
risks an HTTP timeout on a non-streaming call), `thinking={"type": "adaptive"}`,
`effort` inside `output_config` (never top-level), and never a legacy fixed thinking
token budget, `temperature`, `top_p`, `top_k`, or an assistant prefill.
"""

from __future__ import annotations

from typing import Any

from ..config.loader import expand_env
from ..config.redact import register_secret
from ..config.schema import AdapterConfig
from .base import (
    Block,
    Msg,
    ProviderError,
    ProviderMessage,
    ProviderRefusal,
    TextBlock,
    ToolCall,
    ToolDef,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    display_name,
    estimate_cost,
)

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_EFFORT = "high"
DEFAULT_MAX_TOKENS = 32000
DEFAULT_TIMEOUT_S = 900
DEFAULT_MAX_RETRIES = 2


def _import_anthropic() -> Any:
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover - exercised only without the SDK installed
        raise ProviderError(
            "the 'anthropic' package is required for model provider type=anthropic "
            "(pip install anthropic)"
        ) from e
    return anthropic


def _block_to_api(block: Block) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if isinstance(block, ToolResultBlock):
        api_block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": block.content,
        }
        if block.is_error:
            api_block["is_error"] = True
        return api_block
    raise ProviderError(f"anthropic provider: unsupported block type {type(block).__name__!r}")


def _msg_to_api(msg: Msg) -> dict[str, Any]:
    if msg.native_provider == "anthropic" and msg.native is not None:
        return {"role": msg.role, "content": msg.native}
    if isinstance(msg.content, str):
        return {"role": msg.role, "content": msg.content}
    return {"role": msg.role, "content": [_block_to_api(b) for b in msg.content]}


def _tool_to_api(tool: ToolDef) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_schema,
    }


class AnthropicProvider:
    provider_id = "anthropic"

    def __init__(self, cfg: AdapterConfig, *, client: Any | None = None) -> None:
        self.model: str = str(cfg.opt("model", DEFAULT_MODEL))
        self.effort: str = str(cfg.opt("effort", DEFAULT_EFFORT))
        self.max_tokens: int = int(cfg.opt("max_tokens", DEFAULT_MAX_TOKENS))
        self.display: str = str(cfg.opt("display", "omitted"))

        api_key = cfg.opt("api_key")
        self.api_key: str | None = expand_env(api_key) if api_key else None
        register_secret(self.api_key)

        base_url = cfg.opt("base_url")
        self.base_url: str | None = expand_env(base_url) if base_url else None

        self.timeout_s: float = float(cfg.opt("timeout_s", DEFAULT_TIMEOUT_S))
        self.max_retries: int = int(cfg.opt("max_retries", DEFAULT_MAX_RETRIES))

        # Constructed lazily on first complete() unless injected (tests pass a stub).
        self._client: Any = client

    def describe(self) -> str:
        return f"{display_name(self.model)} ({self.model}) effort={self.effort}"

    def _get_client(self) -> Any:
        if self._client is None:
            anthropic = _import_anthropic()
            self._client = anthropic.Anthropic(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout_s,
                max_retries=self.max_retries,
            )
        return self._client

    def complete(
        self,
        *,
        system: str,
        messages: list[Msg],
        tools: list[ToolDef] | None = None,
        max_tokens: int | None = None,
    ) -> ProviderMessage:
        anthropic = _import_anthropic()
        client = self._get_client()

        thinking: dict[str, Any] = {"type": "adaptive"}
        if self.display and self.display != "omitted":
            thinking["display"] = self.display

        api_tools = [_tool_to_api(t) for t in tools] if tools else None

        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=max_tokens or self.max_tokens,
            system=system or anthropic.NOT_GIVEN,
            messages=[_msg_to_api(m) for m in messages],
            thinking=thinking,
            output_config={"effort": self.effort},
            tools=api_tools or anthropic.NOT_GIVEN,
        )

        try:
            with client.messages.stream(**kwargs) as stream:
                response = stream.get_final_message()
        except anthropic.AuthenticationError as e:
            raise ProviderError(f"anthropic: authentication failed: {e}") from e
        except anthropic.PermissionDeniedError as e:
            raise ProviderError(f"anthropic: permission denied: {e}") from e
        except anthropic.NotFoundError as e:
            raise ProviderError(f"anthropic: model or endpoint not found: {e}") from e
        except anthropic.RateLimitError as e:
            retry_after = _retry_after(e)
            suffix = f" (retry-after={retry_after})" if retry_after else ""
            raise ProviderError(f"anthropic: rate limited{suffix}: {e}") from e
        except anthropic.APIStatusError as e:
            raise ProviderError(f"anthropic: API error (status={e.status_code}): {e}") from e
        except anthropic.APITimeoutError as e:
            raise ProviderError(f"anthropic: request timed out: {e}") from e
        except anthropic.APIConnectionError as e:
            raise ProviderError(f"anthropic: connection error: {e}") from e

        if response.stop_reason == "refusal":
            category = None
            if response.stop_details is not None:
                category = getattr(response.stop_details, "category", None)
            raise ProviderRefusal("anthropic: the model refused the request", category=category)

        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        ).strip()
        tool_calls = [
            ToolCall(id=block.id, name=block.name, input=block.input)
            for block in response.content
            if getattr(block, "type", None) == "tool_use"
        ]

        raw_usage = response.usage
        usage_before_cost = Usage(
            input_tokens=getattr(raw_usage, "input_tokens", 0) or 0,
            output_tokens=getattr(raw_usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(raw_usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(raw_usage, "cache_creation_input_tokens", 0) or 0,
        )
        usage = Usage(
            input_tokens=usage_before_cost.input_tokens,
            output_tokens=usage_before_cost.output_tokens,
            cache_read_tokens=usage_before_cost.cache_read_tokens,
            cache_write_tokens=usage_before_cost.cache_write_tokens,
            cost_usd=estimate_cost(self.model, usage_before_cost),
        )

        return ProviderMessage(
            text=text,
            tool_calls=tool_calls,
            stop_reason=response.stop_reason,
            usage=usage,
            native=response.content,
            raw=response,
        )


def _retry_after(error: Any) -> str | None:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    if headers is None:
        return None
    try:
        return headers.get("retry-after")
    except AttributeError:
        return None
