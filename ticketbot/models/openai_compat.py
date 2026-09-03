"""`ModelProvider` for any OpenAI-compatible `/chat/completions` endpoint.

Raw `httpx` — deliberately not the `openai` package, so "any base_url that speaks the
OpenAI chat-completions shape" stays literally true, with no extra SDK dependency.
"""

from __future__ import annotations

import json
import warnings
from typing import Any

import httpx

from ..config.loader import expand_env
from ..config.redact import redact, register_secret
from ..config.schema import AdapterConfig
from .base import (
    Msg,
    ProviderError,
    ProviderMessage,
    TextBlock,
    ToolCall,
    ToolDef,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)

DEFAULT_MAX_TOKENS = 16000
DEFAULT_TIMEOUT_S = 900

_FINISH_REASON_MAP = {
    "tool_calls": "tool_use",
    "length": "max_tokens",
}


def _tool_to_api(tool: ToolDef) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
        },
    }


def _msg_to_api_messages(msg: Msg) -> list[dict[str, Any]]:
    """One `Msg` can expand into several OpenAI-shaped messages: an assistant turn
    carrying text and/or `tool_calls`, followed by one `role: tool` message per
    `ToolResultBlock` (OpenAI has no multi-result tool message).
    """
    if isinstance(msg.content, str):
        return [{"role": msg.role, "content": msg.content}]

    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    tool_messages: list[dict[str, Any]] = []

    for block in msg.content:
        if isinstance(block, TextBlock):
            text_parts.append(block.text)
        elif isinstance(block, ToolUseBlock):
            tool_calls.append(
                {
                    "id": block.id,
                    "type": "function",
                    "function": {"name": block.name, "arguments": json.dumps(block.input)},
                }
            )
        elif isinstance(block, ToolResultBlock):
            tool_messages.append(
                {"role": "tool", "tool_call_id": block.tool_use_id, "content": block.content}
            )
        else:
            raise ProviderError(
                f"openai_compat provider: unsupported block type {type(block).__name__!r}"
            )

    messages: list[dict[str, Any]] = []
    if text_parts or tool_calls:
        api_msg: dict[str, Any] = {
            "role": msg.role,
            "content": "".join(text_parts) if text_parts else None,
        }
        if tool_calls:
            api_msg["tool_calls"] = tool_calls
        messages.append(api_msg)
    messages.extend(tool_messages)
    return messages


class OpenAICompatProvider:
    provider_id = "openai_compat"

    def __init__(self, cfg: AdapterConfig, client: httpx.Client | None = None) -> None:
        """`client` is injectable for tests (`httpx.MockTransport`). `base_url` and
        `api_key` are `expand_env()`'d here; the key is `register_secret()`'d.
        """
        self.model: str = str(cfg.opt("model", ""))

        base_url = cfg.opt("base_url")
        self.base_url: str = expand_env(base_url) if base_url else ""

        api_key = cfg.opt("api_key")
        self.api_key: str | None = expand_env(api_key) if api_key else None
        register_secret(self.api_key)

        self.max_tokens: int = int(cfg.opt("max_tokens", DEFAULT_MAX_TOKENS))
        self.timeout_s: float = float(cfg.opt("timeout_s", DEFAULT_TIMEOUT_S))
        self.max_completion_tokens_field: bool = bool(cfg.opt("max_completion_tokens_field", False))
        self.extra_body: dict[str, Any] = dict(cfg.opt("extra_body") or {})

        self._client = client if client is not None else httpx.Client(timeout=self.timeout_s)

    def describe(self) -> str:
        return f"{self.model} (openai_compat)"

    def complete(
        self,
        *,
        system: str,
        messages: list[Msg],
        tools: list[ToolDef] | None = None,
        max_tokens: int | None = None,
    ) -> ProviderMessage:
        api_messages: list[dict[str, Any]] = []
        if system:
            api_messages.append({"role": "system", "content": system})
        for msg in messages:
            api_messages.extend(_msg_to_api_messages(msg))

        body: dict[str, Any] = {"model": self.model, "messages": api_messages}
        token_field = "max_completion_tokens" if self.max_completion_tokens_field else "max_tokens"
        body[token_field] = max_tokens or self.max_tokens
        if tools:
            body["tools"] = [_tool_to_api(t) for t in tools]
        body.update(self.extra_body)

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        url = f"{self.base_url.rstrip('/')}/chat/completions"

        try:
            response = self._client.post(url, headers=headers, json=body)
        except httpx.HTTPError as e:
            # `redact()` like every other adapter's transport-error path: an httpx
            # error message can quote the failing request, and `base_url` may carry
            # a credential in its userinfo or query for some gateways.
            raise ProviderError(
                f"openai_compat: request to {self.model!r} failed: {redact(str(e))}"
            ) from e

        if response.status_code >= 300:
            snippet = redact(response.text[:500])
            raise ProviderError(
                f"openai_compat: HTTP {response.status_code} from {url}: {snippet}"
            )

        data = response.json()
        choice = data["choices"][0]
        api_message = choice.get("message") or {}
        text = api_message.get("content") or ""

        tool_calls: list[ToolCall] = []
        for i, tc in enumerate(api_message.get("tool_calls") or []):
            function = tc.get("function") or {}
            raw_arguments = function.get("arguments") or ""
            try:
                parsed_input = json.loads(raw_arguments) if raw_arguments else {}
            except (json.JSONDecodeError, TypeError):
                warnings.warn(
                    f"openai_compat: malformed tool call arguments for "
                    f"{function.get('name')!r} (id={tc.get('id')!r}); using {{}}",
                    stacklevel=2,
                )
                parsed_input = {}
            tool_calls.append(
                ToolCall(id=tc.get("id", f"toolu_{i}"), name=function.get("name", ""), input=parsed_input)
            )

        stop_reason = _FINISH_REASON_MAP.get(choice.get("finish_reason"), "end_turn")

        usage_data = data.get("usage") or {}
        usage = Usage(
            input_tokens=usage_data.get("prompt_tokens", 0) or 0,
            output_tokens=usage_data.get("completion_tokens", 0) or 0,
            cost_usd=0.0,  # unknown pricing for an arbitrary peer endpoint
        )

        return ProviderMessage(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            usage=usage,
            native=api_message,
            raw=data,
        )
