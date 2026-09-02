"""`ApiLoopExecutor` — our own path-jailed tool loop over a `ModelProvider`, used
when a step's executor kind is `api` instead of spawning a coding CLI.

The loop is intentionally dumb: send `system` + the running `messages` list to the
provider, and if it asked for tools, dispatch every call through
`executors.tools.build_tools()` (which enforces the step's tool allowlist) and feed
ALL of that turn's results back in a single `user` message, exactly as the Anthropic
tool-use protocol (and every OpenAI-compatible equivalent) expects. It stops on the
first turn with no tool calls, a wall-clock deadline, a cost cap, or
`max_iterations` — whichever comes first.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..config.redact import redact
from ..config.schema import AdapterConfig
from ..models.base import (
    Block,
    Msg,
    ModelProvider,
    ProviderError,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from .base import ExecRequest, ExecResult, append_log, diff_snapshots, finish_result, snapshot_tree
from .tools import ToolContext, build_tools

DEFAULT_MAX_ITERATIONS = 40
DEFAULT_MAX_TOKENS = 32000
DEFAULT_SHELL_TIMEOUT_S = 600
MAX_LOGGED_TOOL_RESULT_CHARS = 2000


class ApiLoopExecutor:
    def __init__(
        self, cfg: AdapterConfig, *, provider: ModelProvider, runtime: Any | None = None
    ) -> None:
        """`cfg` carries `{type: api, model: <slot name>, max_iterations: 40,
        max_tokens: 32000, shell_timeout_s: 600}`. The caller (section 8) is the one
        that resolves `cfg.opt('model')` to a concrete `ModelProvider` and passes it
        in here -- this class never touches the model registry or the profile.
        """
        self.provider = provider
        self.runtime = runtime
        self.model_slot: str | None = cfg.opt("model")
        self.max_iterations: int = int(cfg.opt("max_iterations", DEFAULT_MAX_ITERATIONS))
        self.max_tokens: int = int(cfg.opt("max_tokens", DEFAULT_MAX_TOKENS))
        self.shell_timeout_s: int = int(cfg.opt("shell_timeout_s", DEFAULT_SHELL_TIMEOUT_S))

    def describe(self) -> str:
        return f"api: {self.provider.describe()}"

    def run(self, req: ExecRequest) -> ExecResult:
        log = _log_fn(req.log_path)
        ctx = ToolContext(
            workspace=Path(req.workspace),
            artifacts_dir=Path(req.artifacts_dir),
            runtime=self.runtime,
            allow=set(req.tools),
            shell_timeout_s=self.shell_timeout_s,
            log=log,
        )
        tool_defs, dispatch = build_tools(req.tools, ctx)

        messages: list[Msg] = [Msg(role="user", content=req.prompt)]
        usage = Usage()
        before = snapshot_tree(req.workspace)
        deadline = time.monotonic() + req.timeout_s
        text = ""

        for _ in range(self.max_iterations):
            if time.monotonic() > deadline:
                return self._finish(
                    text, usage, before, ctx,
                    error=f"timed out after {req.timeout_s}s", timed_out=True, exit_code=-1,
                )

            try:
                pm = self.provider.complete(
                    system=req.system,
                    messages=messages,
                    tools=tool_defs or None,
                    max_tokens=self.max_tokens,
                )
            except ProviderError as exc:
                return self._finish(text, usage, before, ctx, error=str(exc))

            usage = usage + pm.usage
            text = pm.text

            if req.max_cost_usd is not None and usage.cost_usd > req.max_cost_usd:
                return self._finish(
                    text, usage, before, ctx,
                    error=(
                        f"cost cap exceeded: ${usage.cost_usd:.4f} > "
                        f"max_cost_usd=${req.max_cost_usd}"
                    ),
                )

            if not pm.tool_calls:
                break  # final answer

            assistant_content: list[Block] = [TextBlock(pm.text)] + [
                ToolUseBlock(call.id, call.name, call.input) for call in pm.tool_calls
            ]
            messages.append(
                Msg(
                    role="assistant",
                    content=assistant_content,
                    native=pm.native,
                    native_provider=self.provider.provider_id,
                )
            )

            results: list[Block] = []
            for call in pm.tool_calls:
                out, is_err = dispatch(call.name, call.input)
                results.append(ToolResultBlock(tool_use_id=call.id, content=out, is_error=is_err))
                if log is not None:
                    log(f"tool_call {call.name} {call.input!r} -> {_truncate_for_log(out)}")
            messages.append(Msg(role="user", content=results))
        else:
            return self._finish(
                text, usage, before, ctx,
                error=f"max_iterations ({self.max_iterations}) reached",
            )

        return self._finish(text, usage, before, ctx)

    def _finish(self, text: str, usage: Usage, before: dict, ctx: ToolContext, **kw: Any) -> ExecResult:
        after = snapshot_tree(ctx.workspace)
        files_written = sorted(set(diff_snapshots(before, after)) | set(ctx.files_written))
        return finish_result(text, usage=usage, files_written=files_written, **kw)


def _truncate_for_log(text: str) -> str:
    if len(text) <= MAX_LOGGED_TOOL_RESULT_CHARS:
        return text
    return text[:MAX_LOGGED_TOOL_RESULT_CHARS] + "…[truncated]"


def _log_fn(log_path: Path | None):
    if log_path is None:
        return None

    def _write(message: str) -> None:
        append_log(log_path, redact(message) + "\n")

    return _write
