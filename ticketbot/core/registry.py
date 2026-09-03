"""Name -> adapter class lookup, so config `type:` strings resolve to a class
without importing every adapter module up front (most of which don't exist until
later sections, and some of which depend on optional extras like `solari`).
"""

from __future__ import annotations

import importlib
from typing import Any

from ..config.schema import AdapterConfig


class RegistryError(KeyError):
    """Raised for an unknown adapter type name, or a target that fails to import."""


class Registry:
    """One family (e.g. 'source'); maps a type name to a 'module:ClassName' target
    resolved with importlib at first use.
    """

    def __init__(self, family: str) -> None:
        self.family = family
        self._targets: dict[str, str | type] = {}

    def register(self, name: str, target: str | type) -> None:
        self._targets[name] = target

    def names(self) -> list[str]:
        return sorted(self._targets)

    def get(self, name: str) -> type:
        """Import and return the class. Unknown name -> RegistryError listing the
        registered names. An ImportError for an optional dependency is re-raised as
        RegistryError naming the missing extra (the registry key doubles as the
        `pip install ticketbot[<extra>]` name for every built-in target below).
        """
        if name not in self._targets:
            available = ", ".join(self.names()) or "(none)"
            raise RegistryError(f"unknown {self.family} type {name!r} (registered: {available})")

        target = self._targets[name]
        if isinstance(target, type):
            return target

        module_name, sep, class_name = str(target).partition(":")
        if not sep or not module_name or not class_name:
            raise RegistryError(
                f"{self.family} type {name!r} has an invalid target {target!r} "
                f"(expected 'module:ClassName')"
            )
        try:
            module = importlib.import_module(module_name)
        except ImportError as e:
            raise RegistryError(
                f'{self.family} type "{name}" needs: pip install ticketbot[{name}] ({e})'
            ) from e
        try:
            return getattr(module, class_name)
        except AttributeError as e:
            raise RegistryError(
                f"{self.family} type {name!r}: {module_name!r} has no attribute {class_name!r}"
            ) from e

    def create(self, cfg: AdapterConfig, **kwargs: Any) -> Any:
        """get(cfg.type)(cfg, **kwargs)."""
        return self.get(cfg.type)(cfg, **kwargs)


SOURCES = Registry("source")
SINKS = Registry("sink")
RUNTIMES = Registry("runtime")
REPOS = Registry("repo")
MODELS = Registry("model")
EXECUTORS = Registry("executor")

SOURCES.register("file", "ticketbot.adapters.sources.file:FileSource")
SOURCES.register("jira", "ticketbot.adapters.sources.jira:JiraSource")
SINKS.register("file", "ticketbot.adapters.sinks.file:FileSink")
SINKS.register("jira", "ticketbot.adapters.sinks.jira:JiraSink")
SINKS.register("github_pr", "ticketbot.adapters.sinks.github_pr:GithubPrSink")
RUNTIMES.register("none", "ticketbot.adapters.runtimes.none:NoneRuntime")
RUNTIMES.register("local_shell", "ticketbot.adapters.runtimes.local_shell:LocalShellRuntime")
RUNTIMES.register("solari", "ticketbot.adapters.runtimes.solari:SolariRuntime")
REPOS.register("git_local", "ticketbot.adapters.repos.git_local:GitLocalRepo")
REPOS.register("github", "ticketbot.adapters.repos.github:GithubRepo")
MODELS.register("anthropic", "ticketbot.models.anthropic:AnthropicProvider")
MODELS.register("openai_compat", "ticketbot.models.openai_compat:OpenAICompatProvider")
MODELS.register("fake", "ticketbot.models.fake:FakeModelProvider")
EXECUTORS.register("process", "ticketbot.executors.process:ProcessExecutor")
EXECUTORS.register("api", "ticketbot.executors.api_loop:ApiLoopExecutor")
EXECUTORS.register("stub", "ticketbot.executors.stub:StubExecutor")
