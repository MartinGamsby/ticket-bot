"""`Orchestrator` -- the run loop: select a pipeline, run its steps against a work
item, gate on `when:`/human approval/questions, account cost and wall-clock spend,
commit per step, and persist `run.json` after every step so a crash is resumable.

This module is deliberately the only place that wires a `Profile` to live adapter
objects. It never imports `anthropic` or `httpx`, and reaches concrete adapter
modules only through the registries in `core.registry`, with
`inspect.signature`-based kwarg filtering (see `_instantiate`) so one adapter
construction call site works for every adapter kind, whatever extra kwargs (
`base_dir`, `run_dir`, `client`, ...) a particular adapter's constructor does or
does not accept. The single deliberate exception is the direct `FileSource`
import: `--input-text`/`--input` must be able to override whatever source the
profile configures, which means naming that one class outright.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ..adapters.repos.base import Repo
from ..adapters.runtimes.base import Runtime
from ..adapters.sinks.base import DryRunSink, MultiSink, Sink
from ..adapters.sources.base import Source
from ..adapters.sources.file import FileSource
from ..config.loader import ConfigError, resolved_yaml
from ..config.redact import redact
from ..config.schema import AdapterConfig, Profile
from ..core.banner import BannerFacts, render_banner, source_fact
from ..core.predicate import PredicateError, evaluate_any
from ..core.registry import EXECUTORS, MODELS, REPOS, RUNTIMES, SINKS, SOURCES, Registry
from ..core.run import Run, RunStatus, RunStore, StepResult, StepStatus
from ..core.templating import render
from ..core.workitem import Ambiguity, Attachment, WorkItem
from ..executors.base import ExecRequest, ExecResult, Executor
from ..models.base import ModelProvider
from .budget import Budget, BudgetExceeded
from .context import build_context, prompt_values
from .gates import GateDecision, Gates
from .locks import LockHeld, RunLock
from .pipeline import PipelineDef, StepDef
from .protocol import strip_protocol
from .selector import Selection, select

logger = logging.getLogger(__name__)

_STATUS_BY_STEP_ID = {
    "clarify": RunStatus.CLARIFYING,
    "plan": RunStatus.PLANNING,
    "implement": RunStatus.IMPLEMENTING,
    "verify": RunStatus.VERIFYING,
    "publish": RunStatus.PR_OPEN,
}

_SECURITY_DIFF_KEYWORDS = (
    "auth", "login", "token", "secret", "password", "crypto",
    "subprocess", "shell", "eval", "pickle", "sql",
)

_SECTION_HEADING_RE = re.compile(r"^#[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_PLAN_SECURITY_RE = re.compile(r"^\s*(?:##+\s*)?Security[: ].*?\b(yes|no)\b", re.IGNORECASE | re.MULTILINE)
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)
# A `PR:` line the reporter left blank -- see `_apply_pr_url`.
_EMPTY_PR_LINE_RE = re.compile(r"^[ \t]*PR:[ \t]*$", re.MULTILINE)

_DEFAULT_SYSTEM_PROMPT = (
    "You are a careful, precise software engineering agent operating as one step "
    "of an automated ticket-to-PR pipeline."
)

MAX_DEFERS_SPAWNED_PER_STEP = 2


def _filtered_kwargs(cls: type, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Only the kwargs `cls.__init__` actually declares (or all of them, if it takes
    `**kwargs`). This is what lets one call site build every adapter kind -- a
    `FileSource(cfg, *, base_dir=...)` and a `JiraSource(cfg, *, client=...)` are
    constructed through the exact same helper.
    """
    try:
        sig = inspect.signature(cls.__init__)
    except (TypeError, ValueError):  # pragma: no cover - builtins/C types only
        return {}
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return dict(kwargs)
    accepted = set(sig.parameters) - {"self"}
    return {k: v for k, v in kwargs.items() if k in accepted}


def _instantiate(registry: Registry, cfg: AdapterConfig, **kwargs: Any) -> Any:
    cls = registry.get(cfg.type)
    return cls(cfg, **_filtered_kwargs(cls, kwargs))


def _extract_json_block(text: str) -> str:
    m = _JSON_FENCE_RE.search(text)
    return m.group(1) if m else text


def _diff_touches_security(diff_text: str) -> bool:
    lowered = diff_text.lower()
    return any(kw in lowered for kw in _SECURITY_DIFF_KEYWORDS)


def _apply_pr_url(text: str, pr_url: str) -> str:
    """Put the real pull request URL into a ticket comment the reporter wrote
    BEFORE that URL existed.

    `builtin/prompts/roles/reporter.md` asks for a line written exactly as
    `PR: {pr_url}` and states outright that "the orchestrator substitutes the real
    PR URL into the written ticket_comment.md after open_pr() returns" -- the
    `publish` step runs before `repo.open_pr()`, so `{pr_url}` renders empty for
    the reporter and the line it writes is a dangling `PR:`. Handle both the
    literal placeholder (a model that copied the token verbatim) and the blank
    line, and fall back to appending the link so the comment always carries it.
    """
    if "{pr_url}" in text:
        return text.replace("{pr_url}", pr_url)
    if _EMPTY_PR_LINE_RE.search(text):
        return _EMPTY_PR_LINE_RE.sub(lambda _m: f"PR: {pr_url}", text, count=1)
    if pr_url in text:
        return text
    separator = "" if text.endswith("\n") else "\n"
    return f"{text}{separator}\nPR: {pr_url}\n"


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _work_item_text(item: WorkItem) -> str:
    """The work item rendered as plain text, for the `source.read` tool.

    The executors never see a `WorkItem` -- `ExecRequest.work_item_text` is how it
    reaches `executors/tools.py: _source_read`, which is the only tool the
    `intake` step is granted in every built-in pipeline.
    """
    lines = [f"{item.key}: {item.title}", f"Type: {item.issue_type}"]
    if item.story_points is not None:
        lines.append(f"Story points: {item.story_points}")
    if item.labels:
        lines.append(f"Labels: {', '.join(item.labels)}")
    if item.url:
        lines.append(f"URL: {item.url}")
    lines.append("")
    lines.append(item.description or "(no description)")
    if item.acceptance:
        lines += ["", "Acceptance criteria:", item.acceptance]
    if item.comments:
        lines += ["", "Comments:"] + [f"- {c.author}: {c.body}" for c in item.comments]
    return "\n".join(lines)


def _section_title(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return path.stem
    m = _SECTION_HEADING_RE.search(text)
    return m.group(1).strip() if m else path.stem


def _list_sections(run_dir: Path) -> list[Path]:
    sections_dir = run_dir / "sections"
    if not sections_dir.is_dir():
        return []

    def _key(p: Path) -> int:
        m = re.search(r"section-(\d+)", p.name)
        return int(m.group(1)) if m else 0

    return sorted(sections_dir.glob("section-*.md"), key=_key)


def _load_role_prompt(path: Path) -> tuple[str, str]:
    """Split a role prompt file into (system, body). A leading `---`-delimited
    front-matter block's `system:` key becomes the system prompt when present; the
    text after it (or the whole file, when there is no front matter) is the body
    template. Malformed front matter falls back to the default system prompt rather
    than failing the step.
    """
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---"):
        return _DEFAULT_SYSTEM_PROMPT, raw

    lines = raw.split("\n")
    closing = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            closing = i
            break
    if closing is None:
        return _DEFAULT_SYSTEM_PROMPT, raw

    fm_text = "\n".join(lines[1:closing])
    body = "\n".join(lines[closing + 1 :]).lstrip("\n")
    try:
        fm = yaml.safe_load(fm_text)
    except yaml.YAMLError:
        fm = None
    system = _DEFAULT_SYSTEM_PROMPT
    if isinstance(fm, dict) and fm.get("system"):
        system = str(fm["system"])
    return system, body


class _DryRunRepo:
    """Wraps a `Repo`; every state-changing local operation (checkout, commit,
    verify_landed, diff, cleanup) passes straight through -- so `--dry-run` still
    exercises real commits and worktree checkout -- but `push()`/`open_pr()` are
    logging no-ops, so nothing outward-facing (a real push, a real PR) ever happens.
    """

    def __init__(self, inner: Repo, log_path: Path | None = None) -> None:
        self.inner = inner
        self.log_path = log_path
        self.calls: list[str] = []

    def _record(self, line: str) -> None:
        self.calls.append(line)
        if self.log_path is not None:
            p = Path(self.log_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8", newline="\n") as f:
                f.write(line + "\n")

    def describe(self) -> str:
        return f"dry-run({self.inner.describe()})"

    def checkout(self, branch: str) -> Path:
        return self.inner.checkout(branch)

    def workspace(self) -> Path:
        return self.inner.workspace()

    def status(self) -> list[str]:
        return self.inner.status()

    def diff(self, base: str | None = None) -> str:
        return self.inner.diff(base)

    def commit(self, message: str, body: str = "") -> Any:
        return self.inner.commit(message, body)

    def branch_name(self, item: WorkItem) -> str:
        return self.inner.branch_name(item)  # type: ignore[attr-defined]

    def verify_landed(self, paths: Any) -> list[str]:
        return self.inner.verify_landed(paths)

    def parent_clone_hint(self) -> str:
        fn = getattr(self.inner, "parent_clone_hint", None)
        return fn() if fn is not None else ""

    def cleanup(self) -> None:
        self.inner.cleanup()

    def push(self) -> None:
        self._record(f"repo.push {self.inner.describe()}")

    def open_pr(self, title: str, body: str) -> str | None:
        self._record(f"repo.open_pr {self.inner.describe()} title={title!r}")
        return None


class Orchestrator:
    def __init__(
        self,
        profile: Profile,
        *,
        runs_dir: Path | None = None,
        dry_run: bool = False,
        interactive: bool = False,
        repo_override: Path | None = None,
        pause_at: str | None = None,
    ) -> None:
        self.profile = profile
        if runs_dir is not None:
            base = Path(runs_dir)
        elif profile.base_dir is not None:
            base = Path(profile.base_dir) / profile.runs_dir
        else:
            base = Path(profile.runs_dir)
        self.runs_dir = base.resolve()
        self.store = RunStore(self.runs_dir)
        self.dry_run = dry_run
        self.interactive = interactive
        self.repo_override = Path(repo_override).resolve() if repo_override else None
        # `--pause-at <step-id>`: interactive only for that one step's optional_human
        # gate, without forcing every optional_human gate in the pipeline to pause.
        self.pause_at = pause_at
        self.gates = Gates(profile.gates)

        self._source_obj: Source | None = None
        self._runtime_obj: Runtime | None = None
        self._providers: dict[str, ModelProvider] = {}
        self._executors: dict[tuple[str, str | None], Executor] = {}

    # ------------------------------------------------------------------ #
    # adapter resolution -- every one of these is a legitimate test seam
    # ------------------------------------------------------------------ #

    def _source(self) -> Source:
        if self._source_obj is None:
            self._source_obj = _instantiate(SOURCES, self.profile.source, base_dir=self.profile.base_dir)
        return self._source_obj

    def _resolve_source(self, *, input_text: str | None, input_path: Path | str | None) -> Source:
        """`--input-text`/`--input` force a `FileSource` regardless of the profile's
        configured source type, so any profile can be driven from the command line.
        """
        if input_text is not None or input_path is not None:
            cfg = AdapterConfig(
                type="file",
                text=input_text,
                path=str(input_path) if input_path is not None else None,
            )
            return FileSource(cfg, base_dir=self.profile.base_dir)
        return self._source()

    def _runtime(self) -> Runtime:
        if self._runtime_obj is None:
            self._runtime_obj = _instantiate(RUNTIMES, self.profile.runtime)
        return self._runtime_obj

    def _repo_cfg(self) -> AdapterConfig:
        cfg = self.profile.repo
        if self.repo_override is None:
            return cfg
        data = cfg.model_dump()
        data["path"] = str(self.repo_override)
        return AdapterConfig.model_validate(data)

    def _build_repo(self, run_dir: Path) -> Repo:
        return _instantiate(REPOS, self._repo_cfg(), base_dir=self.profile.base_dir, run_dir=run_dir)

    def _close_source(self, source: Source) -> None:
        """Release whatever the source holds open (`JiraSource` owns an
        `httpx.Client`). Each entry point closes the source IT opened, and clears
        the cache so the next call to `_source()` builds a fresh one -- a closed
        client must never be handed to a second run.
        """
        close = getattr(source, "close", None)
        if close is not None:
            try:
                close()
            except Exception as exc:  # noqa: BLE001 - closing must never fail a finished run
                logger.warning("source close() failed: %s", exc)
        if source is self._source_obj:
            self._source_obj = None

    def _mark_processed(self, source: Source, item: WorkItem) -> None:
        """Tell a source that can retire an item it handed out to do so, now that
        the item's run has reached a terminal state. `FileSource` moves the file
        into `processed_dir`; without this call every poll sweep re-yields every
        file in the inbox forever. Sources with nothing to retire (`JiraSource`,
        whose `claim()` already transitioned the issue out of the polled JQL) do
        not implement the method at all.
        """
        mark = getattr(source, "mark_processed", None)
        if mark is None:
            return
        try:
            mark(item)
        except OSError as exc:
            logger.warning("could not mark %s processed: %s", item.key, exc)

    def _set_pr_url(self, sink: Sink, pr_url: str) -> None:
        """Tell the sink which pull request the run just opened.

        `GithubPrSink` reports ONTO the PR, so until it holds the URL every
        `comment()`/`link()` it receives is a logged no-op. It has to be told
        before the reporter's FIRST sink call, not after -- otherwise the whole
        report is silently dropped for `sink: {type: github_pr}`.
        """
        setter = getattr(sink, "set_pr_url", None)
        if setter is None:
            return
        try:
            setter(pr_url)
        except Exception as exc:  # noqa: BLE001 - a hand-off must never fail the run
            logger.warning("could not hand the PR url to sink %s: %s", sink.describe(), exc)

    def _sink_error(self, sink: Sink, method: str, exc: Exception) -> None:
        logger.warning("sink %s failed on %s(): %s", sink.describe(), method, exc)

    def _build_sink(self, run_dir: Path) -> Sink:
        primary = _instantiate(SINKS, self.profile.sink, run_dir=run_dir)
        others = [_instantiate(SINKS, c, run_dir=run_dir) for c in self.profile.sink.also]
        return MultiSink(primary, others, on_error=self._sink_error)

    def _provider(self, slot: str | None) -> ModelProvider:
        """`slot -> profile.model.providers[slot]`; `None -> profile.model.default`.
        Cached per slot. Unknown slot -> `ConfigError` listing the available slots.
        """
        key = slot or self.profile.model.default
        cached = self._providers.get(key)
        if cached is not None:
            return cached
        if key not in self.profile.model.providers:
            available = ", ".join(sorted(self.profile.model.providers)) or "(none)"
            raise ConfigError(f"unknown model slot {key!r} (available: {available})")
        cfg = self.profile.model.providers[key]
        provider = _instantiate(MODELS, cfg)
        self._providers[key] = provider
        return provider

    def _executor(self, kind: str | None, step: StepDef | None = None) -> Executor:
        """`kind -> profile.executor.kinds[kind]`; `None -> profile.executor.default`.
        For type 'api': `ApiLoopExecutor(cfg, provider=self._provider(step.model or
        cfg.opt('model')), runtime=self.runtime)`. For type 'process':
        `ProcessExecutor(cfg)`. Cached per (kind, model slot).
        """
        key = kind or self.profile.executor.default
        if key not in self.profile.executor.kinds:
            available = ", ".join(sorted(self.profile.executor.kinds)) or "(none)"
            raise ConfigError(f"unknown executor kind {key!r} (available: {available})")
        cfg = self.profile.executor.kinds[key]

        resolved_model_slot: str | None = None
        if cfg.type == "api":
            step_model = step.model if step is not None else None
            resolved_model_slot = step_model or cfg.opt("model")

        cache_key = (key, resolved_model_slot)
        cached = self._executors.get(cache_key)
        if cached is not None:
            return cached

        kwargs: dict[str, Any] = {"runtime": self._runtime()}
        if cfg.type == "api":
            kwargs["provider"] = self._provider(resolved_model_slot)
        executor = _instantiate(EXECUTORS, cfg, **kwargs)
        self._executors[cache_key] = executor
        return executor

    # ------------------------------------------------------------------ #
    # entry points
    # ------------------------------------------------------------------ #

    def run_once(
        self,
        *,
        external_id: str | None = None,
        input_text: str | None = None,
        input_path: Path | None = None,
        force_lock: bool = False,
    ) -> Run:
        source = self._resolve_source(input_text=input_text, input_path=input_path)
        try:
            item = source.fetch(external_id)
            return self._start_run(item, source, force_lock=force_lock)
        finally:
            self._close_source(source)

    def _start_run(self, item: WorkItem, source: Source, *, force_lock: bool) -> Run:
        # `store.new_run()` is a pure, side-effect-free dataclass construction (no
        # directory is created, nothing is saved) -- so it is safe to call BEFORE
        # acquiring the lock purely to obtain the run id the lock file will record,
        # while every actual disk write still happens strictly after the lock.
        run = self.store.new_run(profile_name=self.profile.name, item=item)
        lock = RunLock(self.runs_dir, item.key)
        lock.acquire(run.id, force=force_lock)
        try:
            return self._run_pipeline(run, item, source=source, fresh=True)
        finally:
            lock.release()

    def resume(self, run_id: str, *, force_lock: bool = False) -> Run:
        run = self.store.load(run_id)
        source = self._source()
        try:
            if run.external_id:
                item = source.fetch(run.external_id)
            else:
                raw = json.loads(self.store.read_artifact(run, "workitem.json"))
                item = WorkItem.from_dict(raw)

            lock = RunLock(self.runs_dir, run.work_item_key)
            lock.acquire(run.id, force=force_lock)
            try:
                return self._run_pipeline(run, item, source=source, fresh=False)
            finally:
                lock.release()
        finally:
            self._close_source(source)

    def poll(self, *, once: bool = False, max_items: int | None = None) -> list[Run]:
        source = self._source()
        poll_seconds = float(self.profile.source.opt("poll_seconds", 60))
        runs: list[Run] = []
        try:
            while True:
                count = 0
                for item in source.poll():
                    if max_items is not None and count >= max_items:
                        break
                    lock = RunLock(self.runs_dir, item.key)
                    if lock.is_locked():
                        continue  # someone else is already working this item
                    try:
                        run = self._start_run(item, source, force_lock=False)
                    except LockHeld:
                        continue
                    runs.append(run)
                    count += 1
                    # The run has reached a terminal state (done, blocked or
                    # failed); retire the item so the next sweep moves on rather
                    # than picking the same one up again forever.
                    self._mark_processed(source, item)
                if once:
                    break
                time.sleep(poll_seconds)
        except KeyboardInterrupt:
            logger.info("poll: interrupted, stopping cleanly")
        finally:
            self._close_source(source)
        return runs

    # ------------------------------------------------------------------ #
    # the run loop
    # ------------------------------------------------------------------ #

    def _run_pipeline(self, run: Run, item: WorkItem, *, source: Source, fresh: bool) -> Run:
        # `built_sink` is the sink whose resources (an httpx client, per Jira/
        # GitHub sink) were actually acquired; `sink` is what the run talks to.
        # `--dry-run` wraps the former in a `DryRunSink` that deliberately never
        # touches it, so closing the WRAPPER would release nothing -- the
        # `finally` below closes what was really opened.
        run_dir = self.store.dir(run.id)
        built_sink: Sink = self._build_sink(run_dir)
        try:
            return self._run_pipeline_inner(
                run, item, source=source, fresh=fresh, run_dir=run_dir, built_sink=built_sink
            )
        finally:
            try:
                built_sink.close()
            except Exception as exc:  # noqa: BLE001 - closing must never fail a finished run
                logger.warning("sink close() failed: %s", exc)

    def _run_pipeline_inner(
        self,
        run: Run,
        item: WorkItem,
        *,
        source: Source,
        fresh: bool,
        run_dir: Path,
        built_sink: Sink,
    ) -> Run:
        sink: Sink = built_sink
        repo: Repo = self._build_repo(run_dir)
        runtime = self._runtime()

        if self.dry_run:
            sink = DryRunSink(sink, log_path=run_dir / "dryrun.log")
            repo = _DryRunRepo(repo, log_path=run_dir / "dryrun.log")  # type: ignore[assignment]

        if fresh:
            pipeline, workspace = self._begin_fresh_run(run, item, source=source, sink=sink, repo=repo, runtime=runtime)
            if workspace is None:
                return run  # claim lost; already finalized
        else:
            pipeline = PipelineDef.load(run.pipeline_ref, self.profile.base_dir or Path.cwd())
            branch = run.extra.get("branch")
            if not branch:
                raise ConfigError(f"run {run.id!r} has no recorded branch to resume from")
            workspace = repo.checkout(branch)
            run.extra["workspace"] = str(workspace)

        budget = Budget(self.profile.budget)
        stopped = False
        try:
            runtime.start()
            budget.start()
            for step in pipeline.steps:
                try:
                    outcome = self._run_step(
                        step,
                        run=run,
                        item=item,
                        pipeline=pipeline,
                        workspace=Path(workspace),
                        run_dir=run_dir,
                        sink=sink,
                        repo=repo,
                        runtime=runtime,
                        budget=budget,
                    )
                except KeyboardInterrupt:
                    self.store.save(run)
                    raise
                if outcome != "continue":
                    stopped = True
                    break
            if not stopped:
                run.status = RunStatus.DONE
        finally:
            runtime.stop()

        self.store.save(run)
        repo.cleanup()
        return run

    def _begin_fresh_run(
        self, run: Run, item: WorkItem, *, source: Source, sink: Sink, repo: Repo, runtime: Runtime
    ) -> tuple[PipelineDef, Path | None]:
        self.store.write_artifact(run, "workitem.json", json.dumps(item.to_dict(), indent=2))
        self.store.write_artifact(run, "config.resolved.yaml", resolved_yaml(self.profile))

        selection = select(self.profile, item)
        run.pipeline_ref = selection.ref
        run.pipeline_reason = selection.reason
        pipeline = PipelineDef.load(selection.ref, self.profile.base_dir or Path.cwd())

        branch = repo.branch_name(item) if hasattr(repo, "branch_name") else item.slug()
        banner = render_banner(self._banner_facts(item, selection, pipeline, branch, sink, repo, runtime))
        run.banner = banner
        self.store.write_artifact(run, "banner.txt", banner)
        print(redact(banner), end="" if banner.endswith("\n") else "\n")

        if not source.claim(item):
            run.status = RunStatus.FAILED
            run.extra["error"] = "claim lost"
            self.store.save(run)
            return pipeline, None

        workspace = repo.checkout(branch)
        run.extra["branch"] = branch
        run.extra["workspace"] = str(workspace)
        self.store.save(run)
        return pipeline, workspace

    # ------------------------------------------------------------------ #
    # banner
    # ------------------------------------------------------------------ #

    def _source_kind_label(self) -> str:
        return "Jira" if self.profile.source.type == "jira" else self.profile.source.type

    def _banner_facts(
        self,
        item: WorkItem,
        selection: Selection,
        pipeline: PipelineDef,
        branch: str,
        sink: Sink,
        repo: Repo,
        runtime: Runtime,
    ) -> BannerFacts:
        pipeline_text = f"{selection.ref}  (rule: {selection.reason})"

        models: list[str] = []
        seen_roles: set[str] = set()
        default_model_slot = pipeline.defaults.get("model")
        for step in pipeline.steps:
            if step.role in seen_roles:
                continue
            seen_roles.add(step.role)
            slot = step.model or default_model_slot
            try:
                provider = self._provider(slot)
            except ConfigError:
                continue
            models.append(f"{step.role}:{provider.describe()}")

        executor_text = ""
        try:
            executor_text = self._executor(None).describe()
        except Exception as exc:  # noqa: BLE001 - banner must never crash a run
            logger.warning("banner: could not describe default executor: %s", exc)

        # `_repo_cfg()`, not `profile.repo`: with `--repo <path>` the run happens
        # in the OVERRIDE, and the banner reports what was used, not what was
        # configured.
        repo_cfg = self._repo_cfg()
        repo_label = repo_cfg.opt("clone") or repo_cfg.opt("path") or repo_cfg.type

        return BannerFacts(
            source=source_fact(item, self._source_kind_label()),
            pipeline=pipeline_text,
            models=models,
            executor=executor_text,
            runtime=runtime.describe(),
            repo=f"{repo_label} @ {branch}",
        )

    # ------------------------------------------------------------------ #
    # one pipeline step (possibly fanned out over `for_each` sections)
    # ------------------------------------------------------------------ #

    def _save_step(self, run: Run, sr: StepResult, texts: list[str]) -> None:
        if texts:
            sr.text = "\n\n---\n\n".join(texts) if len(texts) > 1 else texts[0]
            self.store.write_artifact(run, f"steps/{sr.id}.md", sr.text)
            rel = f"steps/{sr.id}.md"
            if rel not in sr.artifacts:
                sr.artifacts.append(rel)
        self.store.save(run)

    def _run_step(
        self,
        step: StepDef,
        *,
        run: Run,
        item: WorkItem,
        pipeline: PipelineDef,
        workspace: Path,
        run_dir: Path,
        sink: Sink,
        repo: Repo,
        runtime: Runtime,
        budget: Budget,
    ) -> str:
        """Returns 'continue' to proceed to the next step, or 'stop' when the run
        loop must end (`run.status` has already been set to BLOCKED or FAILED).
        """
        # 1. resume skip
        if run.is_complete(step.id):
            logger.info("run %s: skip step %r (already done)", run.id, step.id)
            return "continue"

        sr = run.step(step.id)
        sr.role = step.role
        sr.started_at = _iso_now()

        if step.role == "reviewer":
            self._before_review(run, run_dir, repo)

        # 2. when: gate
        ctx = build_context(item=item, run=run, profile=self.profile, workspace=workspace, run_dir=run_dir)
        try:
            should_run = evaluate_any(step.when, ctx)
        except PredicateError as exc:  # already validated at load time; defensive only
            logger.warning("run %s: step %r when: re-evaluation failed: %s", run.id, step.id, exc)
            should_run = True
        if not should_run:
            sr.status = StepStatus.SKIPPED
            sr.text = f"skipped: when {step.when!r} is false"
            sr.ended_at = _iso_now()
            self.store.save(run)
            return "continue"

        # 3. human gate
        step_interactive = self.interactive or (self.pause_at is not None and self.pause_at == step.id)
        gate_decision = self.gates.on_step_gate(step, run, interactive=step_interactive)
        if gate_decision.action == "await_human":
            note = f"Step {step.id!r} ({step.role}) is gated for human approval before continuing.\n"
            self.store.write_artifact(run, "question.md", note)
            run.status = RunStatus.BLOCKED
            sr.status = StepStatus.BLOCKED
            sr.ended_at = _iso_now()
            self.store.save(run)
            print(note.strip())
            return "stop"

        # 4. fan-out
        sections: list[dict[str, Any] | None]
        if step.for_each == "plan.sections":
            section_paths = _list_sections(run_dir)
            if not section_paths:
                sr.status = StepStatus.FAILED
                sr.error = "the planner produced no sections"
                sr.ended_at = _iso_now()
                run.status = RunStatus.FAILED
                self.store.save(run)
                return "stop"
            n = len(section_paths)
            sections = [
                {"file": str(p), "title": _section_title(p), "index": i + 1, "count": n}
                for i, p in enumerate(section_paths)
            ]
        else:
            sections = [None]

        executor_kind = step.executor or pipeline.defaults.get("executor")
        executor_step = step
        if step.model is None and pipeline.defaults.get("model"):
            executor_step = replace(step, model=pipeline.defaults.get("model"))
        executor = self._executor(executor_kind, executor_step)

        prompt_ref = step.prompt or f"builtin:prompts/roles/{step.role}.md"
        texts: list[str] = []
        defer_spawned = 0
        result: ExecResult | None = None

        for section in sections:
            prompt_path = self._resolve_role_prompt(prompt_ref)
            system, body_template = _load_role_prompt(prompt_path)
            values = prompt_values(
                item=item, run=run, profile=self.profile, pipeline=pipeline, step=step,
                workspace=workspace, run_dir=run_dir, section=section,
            )
            prompt = render(body_template, values)

            try:
                timeout = budget.step_timeout(
                    int(step.timeout_s or pipeline.defaults.get("timeout_s", 1800))
                )
            except BudgetExceeded as exc:
                sr.status = StepStatus.FAILED
                sr.error = str(exc)
                sr.ended_at = _iso_now()
                run.status = RunStatus.FAILED
                self._save_step(run, sr, texts)
                return "stop"

            max_cost = None
            if self.profile.budget.max_cost_usd is not None:
                max_cost = max(0.0, self.profile.budget.max_cost_usd - budget.spent_usd)

            req = ExecRequest(
                system=system,
                prompt=prompt,
                workspace=workspace,
                artifacts_dir=run_dir,
                tools=list(step.tools),
                timeout_s=timeout,
                max_cost_usd=max_cost,
                step_id=step.id,
                log_path=run_dir / "logs" / f"{step.id}.log",
                model=step.model or pipeline.defaults.get("model"),
                work_item_text=_work_item_text(item),
            )

            t0 = time.monotonic()
            result = executor.run(req)
            sr.duration_s += time.monotonic() - t0

            # 7. account
            budget.charge(result.usage)
            run.cost_usd += result.usage.cost_usd
            sr.cost_usd += result.usage.cost_usd
            texts.append(result.text)

            try:
                budget.check(where=step.id)
            except BudgetExceeded as exc:
                sr.status = StepStatus.FAILED
                sr.error = str(exc)
                sr.ended_at = _iso_now()
                run.status = RunStatus.FAILED
                self._save_step(run, sr, texts)
                return "stop"

            # a per-iteration executor failure stops this step's remaining
            # sections immediately -- checked here, per iteration, so an early
            # section's failure in a for_each step is never masked by a later
            # section that happens to succeed.
            if result.error:
                sr.status = StepStatus.FAILED
                sr.error = result.error
                sr.ended_at = _iso_now()
                if not step.optional:
                    run.status = RunStatus.FAILED
                    self._save_step(run, sr, texts)
                    return "stop"
                self._save_step(run, sr, texts)
                return "continue"

            # 10. verify landed + commit
            if step.commit:
                missing = repo.verify_landed(result.files_written)
                if missing:
                    hint_fn = getattr(repo, "parent_clone_hint", None)
                    hint = hint_fn() if hint_fn is not None else ""
                    sr.status = StepStatus.FAILED
                    sr.error = (
                        f"declared files were not found under the workspace: {missing}. "
                        f"{hint}"
                    ).strip()
                    sr.ended_at = _iso_now()
                    run.status = RunStatus.FAILED
                    self._save_step(run, sr, texts)
                    return "stop"

                commit_values = dict(values)
                if section is not None:
                    commit_values["section"] = section
                message = render(step.commit, commit_values)
                cr = repo.commit(message, body=strip_protocol(result.text))
                if cr.sha is not None:
                    sr.commits.append(cr.sha)

            # 11. QUESTION
            if result.question:
                print(result.question)
                if pipeline.on_question == "pause_and_relay":
                    outcome = self._handle_question(
                        run=run, sr=sr, texts=texts, item=item, sink=sink, step=step,
                        question=result.question,
                    )
                    if outcome is not None:
                        return outcome
                elif pipeline.on_question == "fail":
                    sr.status = StepStatus.FAILED
                    sr.question = result.question
                    sr.error = "step raised a QUESTION and pipeline.on_question=fail"
                    sr.ended_at = _iso_now()
                    run.status = RunStatus.FAILED
                    self._save_step(run, sr, texts)
                    return "stop"
                else:  # 'ignore'
                    sr.question = result.question

            # 12. DEFER
            if result.defers and pipeline.on_defer == "spawn_fixer":
                for defer_line in result.defers:
                    sr.defers.append(defer_line)
                    if defer_spawned >= MAX_DEFERS_SPAWNED_PER_STEP:
                        continue
                    defer_spawned += 1
                    self._spawn_fixer(
                        defer_line=defer_line, step=step, run=run, item=item, pipeline=pipeline,
                        workspace=workspace, run_dir=run_dir, executor=executor, repo=repo,
                    )

            self._save_step(run, sr, texts)  # crash-safety after every iteration

        # -- post-loop bookkeeping (all iterations of this step completed) ---- #

        for name in step.produces:
            if not (run_dir / name).exists() and not (workspace / name).exists():
                logger.warning(
                    "run %s: step %r declares produces %r but it was not found", run.id, step.id, name
                )

        # 9. screenshots
        if step.id in (self.profile.runtime.screenshot_on or []):
            png = runtime.screenshot()
            if png is not None:
                shots_dir = run_dir / "screenshots"
                existing = len(list(shots_dir.glob(f"{step.id}-*.png"))) if shots_dir.is_dir() else 0
                rel = f"screenshots/{step.id}-{existing + 1:02d}.png"
                self.store.write_artifact(run, rel, png)
                run.extra.setdefault("screenshots", []).append(rel)

        if step.role == "ingest":
            self._after_ingest(item, texts[-1] if texts else "")
            self.store.write_artifact(run, "workitem.json", json.dumps(item.to_dict(), indent=2))

        if step.role == "planner":
            self._after_planner(run, run_dir)

        assert result is not None  # sections always has at least one (possibly None) entry

        if step.role == "reporter":
            decision = self._after_reporter(run, run_dir, item, repo, sink, texts[-1] if texts else "")
            if decision.action == "await_human":
                run.status = RunStatus.BLOCKED
                self.store.write_artifact(
                    run, "question.md", "The pull request is ready and awaiting human review.\n"
                )
                sr.status = StepStatus.OK
                sr.ended_at = _iso_now()
                self._save_step(run, sr, texts)
                return "stop"

        # 13. status + save -- a per-iteration `result.error` already returned
        # 'stop'/'continue' above, so every iteration here succeeded.
        sr.ended_at = _iso_now()
        sr.status = StepStatus.OK
        run.status = _STATUS_BY_STEP_ID.get(step.id, run.status)
        self._save_step(run, sr, texts)
        return "continue"

    def _resolve_role_prompt(self, ref: str) -> Path:
        from ..config.loader import resolve_ref

        return resolve_ref(ref, self.profile.base_dir or Path.cwd())

    def _handle_question(
        self, *, run: Run, sr: StepResult, texts: list[str], item: WorkItem, sink: Sink,
        step: StepDef, question: str,
    ) -> str | None:
        """Returns 'stop' when the run must end here, or None to keep going (the
        `on_unclear` mode was 'proceed')."""
        decision = self.gates.on_unclear(run, question)
        self.store.write_artifact(run, "question.md", question + "\n")
        sr.question = question

        if decision.action == "block":
            if decision.comment:
                sink.comment(item, decision.comment)
            if decision.unassign:
                sink.unassign(item)
            transition_target = decision.transition or step.on_block or ""
            if transition_target:
                sink.transition(item, transition_target)
            run.status = RunStatus.BLOCKED
            sr.status = StepStatus.BLOCKED
            sr.ended_at = _iso_now()
            self._save_step(run, sr, texts)
            return "stop"

        if decision.action == "fail":
            run.status = RunStatus.FAILED
            sr.status = StepStatus.FAILED
            sr.error = decision.comment or "clarification limit reached"
            sr.ended_at = _iso_now()
            self._save_step(run, sr, texts)
            return "stop"

        return None  # 'continue' -- recorded, ignored

    def _spawn_fixer(
        self, *, defer_line: str, step: StepDef, run: Run, item: WorkItem, pipeline: PipelineDef,
        workspace: Path, run_dir: Path, executor: Executor, repo: Repo,
    ) -> None:
        fixer_prompt_ref = "builtin:prompts/roles/fixer.md"
        pipeline_fixer = next((s for s in pipeline.steps if s.role == "fixer"), None)
        if pipeline_fixer is not None and pipeline_fixer.prompt:
            fixer_prompt_ref = pipeline_fixer.prompt

        fixer_step = StepDef(id=f"{step.id}-fixer", role="fixer", tools=list(step.tools))
        try:
            prompt_path = self._resolve_role_prompt(fixer_prompt_ref)
            system, body_template = _load_role_prompt(prompt_path)
        except ConfigError as exc:
            logger.warning("run %s: could not load fixer prompt: %s", run.id, exc)
            return

        values = prompt_values(
            item=item, run=run, profile=self.profile, pipeline=pipeline, step=fixer_step,
            workspace=workspace, run_dir=run_dir, extra={"defer_line": defer_line},
        )
        prompt = render(body_template, values)

        req = ExecRequest(
            system=system, prompt=prompt, workspace=workspace, artifacts_dir=run_dir,
            tools=list(step.tools), timeout_s=step.timeout_s or 900,
            step_id=fixer_step.id, log_path=run_dir / "logs" / f"{fixer_step.id}.log",
            work_item_text=_work_item_text(item),
        )
        result = executor.run(req)

        if step.commit:
            missing = repo.verify_landed(result.files_written)
            if not missing:
                commit_message = f"fix: {defer_line[:60]}"
                repo.commit(commit_message, body=strip_protocol(result.text))

        self.store.write_artifact(run, f"steps/{fixer_step.id}.md", result.text)

    # ------------------------------------------------------------------ #
    # special-step behavior beyond running the role
    # ------------------------------------------------------------------ #

    def _after_ingest(self, item: WorkItem, text: str) -> None:
        try:
            data = json.loads(_extract_json_block(text))
        except (ValueError, TypeError) as exc:
            logger.warning("ingest: could not parse JSON from step output: %s", exc)
            return
        if not isinstance(data, dict):
            logger.warning("ingest: expected a JSON object, got %s", type(data).__name__)
            return

        if not item.acceptance and data.get("acceptance"):
            item.acceptance = str(data["acceptance"])
        if data.get("ambiguity"):
            try:
                item.ambiguity = Ambiguity(str(data["ambiguity"]).lower())
            except ValueError:
                logger.warning("ingest: unknown ambiguity value %r", data.get("ambiguity"))

    def _after_planner(self, run: Run, run_dir: Path) -> None:
        sections_dir = run_dir / "sections"
        count = len(list(sections_dir.glob("section-*.md"))) if sections_dir.is_dir() else 0
        run.extra["section_count"] = count

        plan_path = run_dir / "plan.md"
        security = "no"
        if plan_path.is_file():
            text = plan_path.read_text(encoding="utf-8", errors="replace")
            m = _PLAN_SECURITY_RE.search(text)
            if m:
                security = m.group(1).lower()
        run.extra["plan_security"] = security

    def _before_review(self, run: Run, run_dir: Path, repo: Repo) -> None:
        diff_text = repo.diff()
        self.store.write_artifact(run, "patch.diff", diff_text)
        run.extra["diff_files"] = diff_text.count("diff --git ")
        run.extra["diff_touches_security"] = _diff_touches_security(diff_text)

    def _after_reporter(
        self, run: Run, run_dir: Path, item: WorkItem, repo: Repo, sink: Sink, result_text: str
    ) -> GateDecision:
        pr_path = run_dir / "pr.md"
        comment_path = run_dir / "ticket_comment.md"
        if not pr_path.is_file():
            self.store.write_artifact(run, "pr.md", result_text)
        if not comment_path.is_file():
            self.store.write_artifact(run, "ticket_comment.md", result_text)

        repo.push()
        title = f"{item.key}: {item.title}"
        body = pr_path.read_text(encoding="utf-8", errors="replace") if pr_path.is_file() else result_text
        pr_url = repo.open_pr(title, body)
        if pr_url:
            run.extra["pr_url"] = pr_url
            # Before any sink call: a `github_pr` sink drops everything it is
            # handed until it knows which PR to post onto.
            self._set_pr_url(sink, pr_url)
            sink.link(item, pr_url, "Pull request")

        comment_text = (
            comment_path.read_text(encoding="utf-8", errors="replace")
            if comment_path.is_file()
            else result_text
        )
        if pr_url:
            comment_text = _apply_pr_url(comment_text, pr_url)
        attachments = []
        for rel in run.extra.get("screenshots", []):
            p = run_dir / rel
            if p.is_file():
                attachments.append(Attachment(filename=p.name, content_type="image/png", path=p))
        try:
            sink.comment(item, comment_text, attachments=attachments)
            sink.transition(item, "In Review")
        finally:
            # LAST, and in a `finally` so a failing sink still leaves the record:
            # `runs/<id>/ticket_comment.md` is what was posted, and the reporter
            # could not have known the PR URL. It has to be written AFTER
            # `sink.comment()` because a `file` sink -- the default, and the
            # `also:` companion of every other shipped sink -- writes into the run
            # dir too and APPENDS each comment it is handed to this same path.
            # Writing only before left the offline default profile's headline
            # artifact holding the comment twice, separated by a `---`.
            self.store.write_artifact(run, "ticket_comment.md", comment_text)

        return self.gates.on_pr_ready(run)
