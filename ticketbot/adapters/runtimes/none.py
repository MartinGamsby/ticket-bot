"""`NoneRuntime` -- the default. No shell commands, no screenshots, no cloud
session; every step still completes, because `screenshot()`/`preview_url()`
return `None` rather than raising. That is the contract that lets a profile
configure `screenshot_on:` and later swap in `runtime: {type: solari, ...}`
without the pipeline itself changing.
"""

from __future__ import annotations

from ...config.schema import AdapterConfig
from .base import BaseRuntime, ExecOut, RuntimeUnavailable


class NoneRuntime(BaseRuntime):
    def __init__(self, cfg: AdapterConfig | None = None) -> None:
        self.cfg = cfg

    def describe(self) -> str:
        return "none"

    def exec(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecOut:
        raise RuntimeUnavailable("runtime type 'none' cannot execute commands")

    def read_file(self, path: str) -> bytes:
        raise RuntimeUnavailable("runtime type 'none' cannot read files")

    def write_file(self, path: str, data: bytes) -> None:
        raise RuntimeUnavailable("runtime type 'none' cannot write files")

    def screenshot(self) -> bytes | None:
        return None

    def preview_url(self, port: int) -> str | None:
        return None
