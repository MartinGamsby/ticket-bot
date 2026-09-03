"""Cross-section seams: the wiring BETWEEN units that were each built and tested
in isolation.

Every other test module covers one unit against its own spec. The holes those
modules structurally cannot see are the joins -- a method one section exposed for
another section to call, a fact the banner assembles from six different adapters,
a PNG that has to travel config -> orchestrator -> runtime -> run dir -> sink
attachment without anyone owning the whole path. This module owns exactly those
joins, and nothing else.

No network, no real coding CLI, no cloud session: a real `tmp_path` git repo, a
`FakeExecutor`, a `FakeRuntime` and (where the point is the sink itself) the real
`FileSink`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from ticketbot.adapters.repos.base import Repo
from ticketbot.adapters.repos.git_local import GitLocalRepo
from ticketbot.adapters.runtimes.base import Runtime
from ticketbot.adapters.sinks.base import Sink
from ticketbot.adapters.sources.base import Source
from ticketbot.config.loader import ConfigError
from ticketbot.config.schema import AdapterConfig, Profile
from ticketbot.core.registry import EXECUTORS, MODELS, REPOS, RUNTIMES, SINKS, SOURCES
from ticketbot.core.run import RunStatus
from ticketbot.core.workitem import Attachment, WorkItem
from ticketbot.engine.orchestrator import Orchestrator, _list_sections
from ticketbot.executors.api_loop import ApiLoopExecutor
from ticketbot.executors.base import ExecRequest
from ticketbot.executors.tools import ToolContext, build_tools
from ticketbot.models.base import ToolResultBlock
from ticketbot.models.fake import FakeModelProvider
from tests.fakes import FakeExecutor, FakeRuntime, FakeSink, text_turn, tool_turn

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"

PNG = b"\x89PNG\r\n\x1a\nfake-image-bytes"

ITEM_TEXT = """---
points: 3
type: Story
acceptance: "Given X, when Y, then Z"
---
# Add a /health endpoint

Add a simple health check endpoint.
"""


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #


def _base_dir(tmp_path: Path) -> Path:
    """A throwaway copy of the pipeline + role-prompt fixtures, used as the
    profile's `base_dir`. Copied (rather than pointing `base_dir` at
    `tests/fixtures`) so tests that write next to the profile -- the `inbox/`
    poll tests below -- can never touch this checkout.
    """
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURE_DIR / "pipelines", work / "pipelines", dirs_exist_ok=True)
    shutil.copytree(FIXTURE_DIR / "prompts", work / "prompts", dirs_exist_ok=True)
    return work


def _profile(
    tmp_path: Path,
    repo_path: Path,
    *,
    source: dict | None = None,
    sink: dict | None = None,
    runtime: dict | None = None,
    pipeline: str = "pipelines/tiny.yaml",
) -> Profile:
    data = {
        "name": "seam-profile",
        "source": source or {"type": "file"},
        "sink": sink or {"type": "file"},
        "repo": {"type": "git_local", "path": str(repo_path)},
        "model": {
            "default": "default",
            "providers": {"default": {"type": "fake", "name": "test-model"}},
        },
        "executor": {
            "default": "default",
            "kinds": {"default": {"type": "api", "model": "default"}},
        },
        "runtime": runtime or {"type": "none"},
        "pipeline_selector": {"rules": [], "default": pipeline},
        "gates": {"on_unclear": "comment_and_unassign", "on_pr_ready": "auto", "max_clarify_rounds": 2},
        "budget": {},
    }
    profile = Profile.model_validate(data)
    profile.base_dir = _base_dir(tmp_path)
    return profile


def _executor() -> FakeExecutor:
    """Scripted for `pipelines/tiny.yaml`: a plan with one section, one workspace
    write per implement fan-out, and the two reporter artifacts."""
    return FakeExecutor(
        artifact_writes={
            "plan": [
                ("plan.md", "# Plan\n\nSecurity: no\n"),
                ("sections/section-1.md", "# Section 1 title\n\nDo thing 1.\n"),
            ],
            "publish": [
                ("pr.md", "# PR\n\nFull write-up.\n"),
                ("ticket_comment.md", "Short update.\n"),
            ],
        },
        writes={"implement": [("src/impl.py", "print('hello')\n")]},
    )


def _executor_writing_comment(comment: str) -> FakeExecutor:
    """`_executor()`, but the reporter writes `comment` as `ticket_comment.md` --
    for exercising what the engine does to that text once the PR URL exists."""
    executor = _executor()
    executor.artifact_writes["publish"] = [
        ("pr.md", "# PR\n\nFull write-up.\n"),
        ("ticket_comment.md", comment),
    ]
    return executor


def _orchestrator(
    profile: Profile,
    runs_dir: Path,
    *,
    executor: FakeExecutor | None = None,
    sink: FakeSink | None = None,
    runtime: FakeRuntime | None = None,
    pr_url: str | None = None,
) -> Orchestrator:
    orch = Orchestrator(profile, runs_dir=runs_dir)
    orch._executor = lambda kind=None, step=None: executor or _executor()  # type: ignore[method-assign]
    if sink is not None:
        orch._build_sink = lambda run_dir: sink  # type: ignore[method-assign]
    if runtime is not None:
        orch._runtime_obj = runtime
    if pr_url is not None:
        # The REAL `git_local` repo (so the workspace, commits and diff stay real),
        # with only `open_pr()` answering as a PR host would -- `GitLocalRepo`
        # itself always returns None, having nowhere to open one.
        build_repo = orch._build_repo

        def _with_pr(run_dir: Path) -> Repo:
            repo = build_repo(run_dir)
            repo.open_pr = lambda title, body: pr_url  # type: ignore[method-assign]
            return repo

        orch._build_repo = _with_pr  # type: ignore[method-assign]
    return orch


# --------------------------------------------------------------------------- #
# 1. screenshot_on: config (S1) -> orchestrator (S8) -> runtime (S5) -> sink (S6)
#
# Every other screenshot test in the suite proves the NEGATIVE half of this path
# (`NoneRuntime.screenshot()` returns None, so a pipeline with `screenshot_on:`
# still completes). Nothing proved the positive half: that a runtime which
# actually returns PNG bytes gets those bytes all the way onto the ticket
# comment.
# --------------------------------------------------------------------------- #


def test_screenshot_on_writes_one_png_per_named_step_into_the_run_dir(
    tmp_path: Path, git_repo: Path
) -> None:
    profile = _profile(tmp_path, git_repo, runtime={"type": "none", "screenshot_on": ["verify", "publish"]})
    runs_dir = tmp_path / "runs"
    runtime = FakeRuntime(png=PNG)
    orch = _orchestrator(profile, runs_dir, sink=FakeSink(), runtime=runtime)

    run = orch.run_once(input_text=ITEM_TEXT)

    assert run.status == RunStatus.DONE
    run_dir = runs_dir / run.id
    assert (run_dir / "screenshots" / "verify-01.png").read_bytes() == PNG
    assert (run_dir / "screenshots" / "publish-01.png").read_bytes() == PNG
    assert run.extra["screenshots"] == ["screenshots/verify-01.png", "screenshots/publish-01.png"]


def test_reporter_attaches_every_captured_screenshot_to_the_ticket_comment(
    tmp_path: Path, git_repo: Path
) -> None:
    """Including `publish`'s OWN screenshot -- the capture has to happen before
    `_after_reporter` posts the comment, or the publish step's shot is written to
    disk and then never sent anywhere.
    """
    profile = _profile(tmp_path, git_repo, runtime={"type": "none", "screenshot_on": ["verify", "publish"]})
    sink = FakeSink()
    orch = _orchestrator(profile, tmp_path / "runs", sink=sink, runtime=FakeRuntime(png=PNG))

    run = orch.run_once(input_text=ITEM_TEXT)

    assert len(sink.comments) == 1
    _key, _markdown, attachments = sink.comments[0]
    assert [a.filename for a in attachments] == ["verify-01.png", "publish-01.png"]
    assert {a.content_type for a in attachments} == {"image/png"}
    for att in attachments:
        assert att.path is not None
        assert att.path.is_relative_to(tmp_path / "runs" / run.id)
        assert att.read_bytes() == PNG


def test_screenshots_reach_the_real_file_sink_as_copied_attachment_files(
    tmp_path: Path, git_repo: Path
) -> None:
    """The same path again with NO sink seam: the real `FileSink` must be able to
    consume what the orchestrator hands it (`Attachment(path=...)` -> `read_bytes()`
    -> `attachments/<name>`).
    """
    profile = _profile(tmp_path, git_repo, runtime={"type": "none", "screenshot_on": ["verify"]})
    runs_dir = tmp_path / "runs"
    orch = _orchestrator(profile, runs_dir, runtime=FakeRuntime(png=PNG))

    run = orch.run_once(input_text=ITEM_TEXT)

    run_dir = runs_dir / run.id
    assert (run_dir / "attachments" / "verify-01.png").read_bytes() == PNG
    assert "attachments: verify-01.png" in (run_dir / "result.md").read_text(encoding="utf-8")


def test_steps_not_named_in_screenshot_on_never_ask_the_runtime_for_one(
    tmp_path: Path, git_repo: Path
) -> None:
    profile = _profile(tmp_path, git_repo, runtime={"type": "none", "screenshot_on": []})
    runtime = FakeRuntime(png=PNG)
    orch = _orchestrator(profile, tmp_path / "runs", sink=FakeSink(), runtime=runtime)

    run = orch.run_once(input_text=ITEM_TEXT)

    assert run.status == RunStatus.DONE
    assert [c for c in runtime.calls if c[0] == "screenshot"] == []
    assert run.extra.get("screenshots", []) == []


def test_a_runtime_that_returns_no_png_creates_no_screenshots_dir(
    tmp_path: Path, git_repo: Path
) -> None:
    """`screenshot_on` naming a step whose runtime can't produce an image (a
    Solari *sandbox* session, say) is a no-op, not a failure -- the same contract
    `NoneRuntime` has, held by any runtime returning None.
    """
    profile = _profile(tmp_path, git_repo, runtime={"type": "none", "screenshot_on": ["verify"]})
    runs_dir = tmp_path / "runs"
    runtime = FakeRuntime(png=None)
    orch = _orchestrator(profile, runs_dir, sink=FakeSink(), runtime=runtime)

    run = orch.run_once(input_text=ITEM_TEXT)

    assert run.status == RunStatus.DONE
    assert [c for c in runtime.calls if c[0] == "screenshot"]  # it WAS asked
    assert not (runs_dir / run.id / "screenshots").exists()


# --------------------------------------------------------------------------- #
# 2. Source retirement: FileSource.mark_processed (S6) <- poll() (S8)
#
# `FileSource.mark_processed()` exists precisely so the poller can retire an
# inbox file, and its docstring says "the orchestrator decides WHEN to call
# this". Without that call every sweep re-yields every file forever.
# --------------------------------------------------------------------------- #


def _inbox_profile(tmp_path: Path, git_repo: Path) -> Profile:
    return _profile(
        tmp_path,
        git_repo,
        source={"type": "file", "glob": "inbox/*.md", "processed_dir": "inbox/processed"},
    )


def _write_inbox_item(base_dir: Path, name: str, title: str) -> Path:
    inbox = base_dir / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / name
    path.write_text(ITEM_TEXT.replace("Add a /health endpoint", title), encoding="utf-8")
    return path


def test_poll_retires_each_handled_inbox_file_into_processed_dir(
    tmp_path: Path, git_repo: Path
) -> None:
    profile = _inbox_profile(tmp_path, git_repo)
    base_dir = profile.base_dir
    assert base_dir is not None
    item_path = _write_inbox_item(base_dir, "item-1.md", "Add a /health endpoint")
    orch = _orchestrator(profile, tmp_path / "runs", sink=FakeSink())

    runs = orch.poll(once=True)

    assert len(runs) == 1
    assert runs[0].status == RunStatus.DONE
    assert not item_path.exists()
    assert (base_dir / "inbox" / "processed" / "item-1.md").is_file()


def test_a_second_poll_sweep_does_not_reprocess_an_already_handled_file(
    tmp_path: Path, git_repo: Path
) -> None:
    profile = _inbox_profile(tmp_path, git_repo)
    base_dir = profile.base_dir
    assert base_dir is not None
    _write_inbox_item(base_dir, "item-1.md", "Add a /health endpoint")
    orch = _orchestrator(profile, tmp_path / "runs", sink=FakeSink())

    first = orch.poll(once=True)
    second = orch.poll(once=True)

    assert len(first) == 1
    assert second == []


# --------------------------------------------------------------------------- #
# 3. Adapter lifecycle: close() (S6) <- the run loop (S8)
#
# `JiraSource`/`JiraSink`/`GithubPrSink` each own an `httpx.Client`, `MultiSink`
# implements `close()` to fan out to all of them, and both shared fakes track a
# `.closed` flag -- all of which is dead weight unless the engine actually calls
# it at the end of a run.
# --------------------------------------------------------------------------- #


def test_every_sink_is_closed_when_the_run_ends(tmp_path: Path, git_repo: Path) -> None:
    profile = _profile(tmp_path, git_repo)
    sink = FakeSink()
    orch = _orchestrator(profile, tmp_path / "runs", sink=sink)

    orch.run_once(input_text=ITEM_TEXT)

    assert sink.closed is True


def test_sinks_are_closed_even_when_the_run_fails(tmp_path: Path, git_repo: Path) -> None:
    from ticketbot.executors.base import ExecResult

    profile = _profile(tmp_path, git_repo)
    sink = FakeSink()
    executor = _executor()
    executor.results["plan"] = ExecResult(text="nope", error="boom: planner crashed")
    orch = _orchestrator(profile, tmp_path / "runs", executor=executor, sink=sink)

    run = orch.run_once(input_text=ITEM_TEXT)

    assert run.status == RunStatus.FAILED
    assert sink.closed is True


def test_the_source_is_closed_after_run_once(tmp_path: Path, git_repo: Path) -> None:
    profile = _profile(tmp_path, git_repo)
    orch = _orchestrator(profile, tmp_path / "runs", sink=FakeSink())

    closed: list[str] = []
    real_resolve = orch._resolve_source

    def _tracked(**kwargs):
        source = real_resolve(**kwargs)
        original_close = source.close

        def _close() -> None:
            closed.append("source")
            original_close()

        source.close = _close  # type: ignore[method-assign]
        return source

    orch._resolve_source = _tracked  # type: ignore[method-assign]
    orch.run_once(input_text=ITEM_TEXT)

    assert closed == ["source"]


def test_the_source_is_closed_after_a_poll_sweep(tmp_path: Path, git_repo: Path) -> None:
    profile = _inbox_profile(tmp_path, git_repo)
    base_dir = profile.base_dir
    assert base_dir is not None
    _write_inbox_item(base_dir, "item-1.md", "Add a /health endpoint")
    orch = _orchestrator(profile, tmp_path / "runs", sink=FakeSink())

    source = orch._source()
    closed: list[str] = []
    source.close = lambda: closed.append("source")  # type: ignore[method-assign]

    orch.poll(once=True)

    assert closed == ["source"]


# --------------------------------------------------------------------------- #
# 4. Registry (S2) <-> the adapter classes it names (S3-S7)
#
# `core/registry.py` was written in section 2 as strings naming classes that did
# not exist for another five sections. `test_core_registry.py` only asserts the
# NAMES are present -- it never imports a single target, so a stale module path
# or a renamed class would surface at run time, in production, not here.
# --------------------------------------------------------------------------- #

_ALL_REGISTRATIONS = [
    (registry, name)
    for registry in (SOURCES, SINKS, RUNTIMES, REPOS, MODELS, EXECUTORS)
    for name in registry.names()
]


@pytest.mark.parametrize(
    "registry,name", _ALL_REGISTRATIONS, ids=[f"{r.family}:{n}" for r, n in _ALL_REGISTRATIONS]
)
def test_every_registered_target_imports_and_describes_itself(registry, name) -> None:
    cls = registry.get(name)  # raises RegistryError on a stale module path / class name
    assert callable(getattr(cls, "describe", None)), f"{registry.family} {name!r} has no describe()"


@pytest.mark.parametrize(
    "registry,protocol",
    [(SOURCES, Source), (SINKS, Sink), (RUNTIMES, Runtime), (REPOS, Repo)],
    ids=["source", "sink", "runtime", "repo"],
)
def test_every_registered_adapter_satisfies_its_protocol(registry, protocol) -> None:
    for name in registry.names():
        assert issubclass(registry.get(name), protocol), f"{registry.family} {name!r}"


# --------------------------------------------------------------------------- #
# 4b. Shipped profiles (S9) <-> shipped pipelines (S9) <-> slot resolution (S8)
#
# A pipeline names model slots (`model: peer`) and a profile defines them; the two
# only meet inside `Orchestrator._provider()`, at run time, several steps in. A
# profile that never declares a slot its own selectable pipelines ask for fails
# mid-run, after the repo has been checked out and code has been written.
# --------------------------------------------------------------------------- #

SHIPPED_PROFILES = sorted((Path(__file__).resolve().parents[1] / "profiles").glob("*.yaml"))


@pytest.mark.parametrize(
    "profile_path", SHIPPED_PROFILES, ids=[p.stem for p in SHIPPED_PROFILES]
)
def test_every_shipped_profile_defines_the_model_slots_its_pipelines_name(
    profile_path: Path,
) -> None:
    from ticketbot.config.loader import load_profile
    from ticketbot.engine.pipeline import PipelineDef

    profile = load_profile(profile_path)
    base_dir = profile.base_dir or profile_path.parent
    refs = [profile.pipeline_selector.default] + [r.use for r in profile.pipeline_selector.rules]

    for ref in refs:
        pipeline = PipelineDef.load(ref, base_dir)
        slots = {step.model for step in pipeline.steps if step.model}
        if pipeline.defaults.get("model"):
            slots.add(str(pipeline.defaults["model"]))
        missing = sorted(slots - set(profile.model.providers))
        assert not missing, f"{profile_path.name} + {ref}: undefined model slot(s) {missing}"


@pytest.mark.parametrize(
    "profile_path", SHIPPED_PROFILES, ids=[p.stem for p in SHIPPED_PROFILES]
)
def test_every_shipped_profile_defines_the_executor_kinds_its_pipelines_name(
    profile_path: Path,
) -> None:
    from ticketbot.config.loader import load_profile
    from ticketbot.engine.pipeline import PipelineDef

    profile = load_profile(profile_path)
    base_dir = profile.base_dir or profile_path.parent
    refs = [profile.pipeline_selector.default] + [r.use for r in profile.pipeline_selector.rules]

    for ref in refs:
        pipeline = PipelineDef.load(ref, base_dir)
        kinds = {step.executor for step in pipeline.steps if step.executor}
        if pipeline.defaults.get("executor"):
            kinds.add(str(pipeline.defaults["executor"]))
        missing = sorted(kinds - set(profile.executor.kinds))
        assert not missing, f"{profile_path.name} + {ref}: undefined executor kind(s) {missing}"


# --------------------------------------------------------------------------- #
# 5. Banner: assembled by S8 from facts produced by S2-S7
# --------------------------------------------------------------------------- #


def test_the_banner_reports_the_runtime_that_actually_ran_not_the_configured_type(
    tmp_path: Path, git_repo: Path
) -> None:
    """The banner's whole promise is "what was used, not what the config says".
    The runtime line is the one place that is checkable in a test: the profile
    says `type: none`, the object that actually ran describes itself as "fake",
    and the banner must show the object's answer.
    """
    profile = _profile(tmp_path, git_repo, runtime={"type": "none"})
    runs_dir = tmp_path / "runs"
    orch = _orchestrator(profile, runs_dir, sink=FakeSink(), runtime=FakeRuntime(png=None))

    run = orch.run_once(input_text=ITEM_TEXT)

    banner = (runs_dir / run.id / "banner.txt").read_text(encoding="utf-8")
    assert "runtime=fake" in banner
    assert "runtime=none" not in banner
    assert banner == run.banner


def test_the_banner_reports_the_repo_override_rather_than_the_configured_path(
    tmp_path: Path, git_repo: Path
) -> None:
    """`--repo <path>` moves the run into a different checkout; the banner's repo
    line has to follow it, for the same reason the runtime line does.
    """
    configured = tmp_path / "not-the-repo-actually-used"
    profile = _profile(tmp_path, configured)
    runs_dir = tmp_path / "runs"
    orch = Orchestrator(profile, runs_dir=runs_dir, repo_override=git_repo)
    orch._executor = lambda kind=None, step=None: _executor()  # type: ignore[method-assign]
    orch._build_sink = lambda run_dir: FakeSink()  # type: ignore[method-assign]

    run = orch.run_once(input_text=ITEM_TEXT)

    assert run.status == RunStatus.DONE
    banner = (runs_dir / run.id / "banner.txt").read_text(encoding="utf-8")
    assert str(git_repo.resolve()) in banner
    assert str(configured) not in banner


def test_a_step_naming_an_undefined_model_slot_fails_with_a_config_error_listing_the_slots(
    tmp_path: Path, git_repo: Path
) -> None:
    """`builtin/pipelines/standard.yaml` sends `review` to a `peer` slot, which is
    why `profiles/_base.yaml` defines one. A profile that drops the slot must fail
    with a message that says which slots exist -- and the banner, built before any
    step runs, must degrade instead of crashing first.
    """
    profile = _profile(tmp_path, git_repo, pipeline="pipelines/slotless.yaml")
    base_dir = profile.base_dir
    assert base_dir is not None
    (base_dir / "pipelines" / "slotless.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "slotless",
                "defaults": {"timeout_s": 60},
                "steps": [
                    {
                        "id": "intake",
                        "role": "ingest",
                        "model": "peer",
                        "prompt": "prompts/roles/ingest.md",
                        "tools": ["source.read"],
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    runs_dir = tmp_path / "runs"
    orch = Orchestrator(profile, runs_dir=runs_dir)
    orch._build_sink = lambda run_dir: FakeSink()  # type: ignore[method-assign]

    with pytest.raises(ConfigError) as excinfo:
        orch.run_once(input_text=ITEM_TEXT)

    assert "peer" in str(excinfo.value)
    assert "default" in str(excinfo.value)  # the available slots are named

    banner = next(runs_dir.glob("*/banner.txt")).read_text(encoding="utf-8")
    assert "peer" not in banner  # degraded, not crashed


# --------------------------------------------------------------------------- #
# 6. Work-item attachments (S6) <- the run loop (S8)
# --------------------------------------------------------------------------- #


def test_a_source_attachment_with_no_local_copy_never_reaches_a_sink(
    tmp_path: Path, git_repo: Path
) -> None:
    """`JiraSource` maps `fields.attachment[]` to `Attachment(filename,
    content_type)` with NO `path`/`data` -- fetching the bytes needs a separate
    `download_attachment()` call that the engine does not make. Such an
    attachment therefore raises from `read_bytes()`, so the reporter must send
    only the screenshots it captured itself, never `item.attachments`.
    """
    remote_only = Attachment(filename="design.png", content_type="image/png")
    with pytest.raises(ValueError):
        remote_only.read_bytes()

    profile = _profile(tmp_path, git_repo, runtime={"type": "none", "screenshot_on": ["verify"]})
    sink = FakeSink()
    orch = _orchestrator(profile, tmp_path / "runs", sink=sink, runtime=FakeRuntime(png=PNG))

    real_fetch = orch._resolve_source

    def _with_attachment(**kwargs):
        source = real_fetch(**kwargs)
        inner = source.fetch

        def _fetch(external_id: str | None = None) -> WorkItem:
            item = inner(external_id)
            item.attachments = [remote_only]
            return item

        source.fetch = _fetch  # type: ignore[method-assign]
        return source

    orch._resolve_source = _with_attachment  # type: ignore[method-assign]
    run = orch.run_once(input_text=ITEM_TEXT)

    assert run.status == RunStatus.DONE
    _key, _markdown, attachments = sink.comments[0]
    assert [a.filename for a in attachments] == ["verify-01.png"]


# --------------------------------------------------------------------------- #
# 7. The PR url: repo.open_pr() (S7) -> the reporter (S8) -> the sink (S6)
#
# The URL does not exist until `open_pr()` returns, which is AFTER the `publish`
# step wrote its ticket comment and after the sink was constructed. Two separate
# sections left the engine a hook for closing that gap and neither hook had a
# caller: `GithubPrSink.set_pr_url()` (without it the sink drops every comment as
# a no-op) and the `PR: {pr_url}` line `prompts/roles/reporter.md` promises the
# orchestrator will fill in.
# --------------------------------------------------------------------------- #

PR_URL = "https://github.com/acme/app/pull/42"


class RecordingSink(FakeSink):
    """A `FakeSink` that also accepts the PR url, logging the ORDER of what it was
    told -- `set_pr_url` landing after the first `comment()` is exactly the bug
    that makes a `github_pr` sink silently report nothing."""

    def __init__(self) -> None:
        super().__init__()
        self.ordered: list[str] = []
        self.pr_url: str | None = None

    def set_pr_url(self, url: str) -> None:
        self.pr_url = url
        self.ordered.append("set_pr_url")

    def comment(self, item, markdown, attachments=()) -> None:  # type: ignore[no-untyped-def]
        self.ordered.append("comment")
        super().comment(item, markdown, attachments)

    def link(self, item, url, title) -> None:  # type: ignore[no-untyped-def]
        self.ordered.append("link")
        super().link(item, url, title)


def test_the_sink_is_told_the_pr_url_before_it_is_asked_to_report_anything(
    tmp_path: Path, git_repo: Path
) -> None:
    profile = _profile(tmp_path, git_repo)
    sink = RecordingSink()
    orch = _orchestrator(profile, tmp_path / "runs", sink=sink, pr_url=PR_URL)

    run = orch.run_once(input_text=ITEM_TEXT)

    assert run.status == RunStatus.DONE
    assert sink.pr_url == PR_URL
    assert sink.ordered[0] == "set_pr_url"  # before link() and comment(), not after
    assert sink.ordered == ["set_pr_url", "link", "comment"]
    assert run.extra["pr_url"] == PR_URL


def test_a_sink_that_does_not_want_a_pr_url_is_skipped_not_crashed(
    tmp_path: Path, git_repo: Path
) -> None:
    """`set_pr_url` is not part of the `Sink` protocol -- `file` and `jira` sinks
    do not implement it, and handing them one must be a no-op rather than an
    `AttributeError` that fails a finished run."""
    profile = _profile(tmp_path, git_repo)
    sink = FakeSink()
    assert not hasattr(sink, "set_pr_url")
    orch = _orchestrator(profile, tmp_path / "runs", sink=sink, pr_url=PR_URL)

    run = orch.run_once(input_text=ITEM_TEXT)

    assert run.status == RunStatus.DONE
    assert sink.links == [(run.work_item_key, PR_URL, "Pull request")]


def test_multisink_passes_the_pr_url_to_the_sinks_that_take_one(
    tmp_path: Path, git_repo: Path
) -> None:
    """A profile's `also:` list mixes sinks that want the url with sinks that have
    no such method -- `MultiSink` has to reach the former without tripping over
    the latter, wherever they sit in the list."""
    from ticketbot.adapters.sinks.base import MultiSink

    plain_primary, wants_url, plain_secondary = FakeSink(), RecordingSink(), FakeSink()
    multi = MultiSink(plain_primary, [wants_url, plain_secondary])

    multi.set_pr_url(PR_URL)

    assert wants_url.pr_url == PR_URL


def test_multisink_survives_a_sink_that_raises_while_taking_the_pr_url() -> None:
    from ticketbot.adapters.sinks.base import MultiSink

    class Exploding(RecordingSink):
        def set_pr_url(self, url: str) -> None:
            raise RuntimeError("boom")

    good = RecordingSink()
    errors: list[tuple[str, str]] = []
    multi = MultiSink(
        Exploding(), [good], on_error=lambda sink, method, exc: errors.append((sink.describe(), method))
    )

    multi.set_pr_url(PR_URL)  # must not propagate

    assert good.pr_url == PR_URL  # the healthy sink was still reached
    assert errors == [("fake", "set_pr_url")]


def test_a_github_pr_sink_reports_nothing_until_it_is_given_the_url() -> None:
    """The real sink, the real reason the hand-off exists: `GithubPrSink.comment()`
    is a logged no-op while it does not know which PR to post onto. No network --
    `httpx.MockTransport`."""
    import httpx

    from ticketbot.adapters.sinks.github_pr import GithubPrSink
    from ticketbot.config.schema import AdapterConfig

    requests: list[httpx.Request] = []
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: (requests.append(r), httpx.Response(201, json={}))[1])
    )
    sink = GithubPrSink(AdapterConfig(type="github_pr", prefer_gh=False), client=client)
    item = WorkItem(id="ENG-1", title="T", external_id="ENG-1")

    sink.comment(item, "before the hand-off")
    assert requests == []  # dropped, exactly as the engine bug used to leave it

    sink.set_pr_url(PR_URL)
    sink.comment(item, "after the hand-off")

    assert [str(r.url) for r in requests] == [
        "https://api.github.com/repos/acme/app/issues/42/comments"
    ]


def test_the_blank_pr_line_the_reporter_left_gets_the_real_url(
    tmp_path: Path, git_repo: Path
) -> None:
    """`prompts/roles/reporter.md` renders `{pr_url}` as EMPTY at publish time (the
    PR is not open yet), so the reporter writes a dangling `PR:` line and the
    prompt's own trailing note promises the orchestrator will complete it."""
    profile = _profile(tmp_path, git_repo)
    sink = RecordingSink()
    executor = _executor_writing_comment("Added /health.\nVerified by tests.\nPR: \n")
    orch = _orchestrator(profile, tmp_path / "runs", executor=executor, sink=sink, pr_url=PR_URL)

    orch.run_once(input_text=ITEM_TEXT)

    _key, markdown, _attachments = sink.comments[0]
    assert f"PR: {PR_URL}" in markdown
    assert "PR: \n" not in markdown


def test_a_literal_pr_url_placeholder_in_the_comment_is_substituted(
    tmp_path: Path, git_repo: Path
) -> None:
    """The other thing a model does with "written exactly as: PR: {pr_url}" --
    copy the token through verbatim. It must not reach the ticket."""
    profile = _profile(tmp_path, git_repo)
    sink = RecordingSink()
    executor = _executor_writing_comment("Added /health.\nPR: {pr_url}\n")
    orch = _orchestrator(profile, tmp_path / "runs", executor=executor, sink=sink, pr_url=PR_URL)

    orch.run_once(input_text=ITEM_TEXT)

    _key, markdown, _attachments = sink.comments[0]
    assert markdown == "Added /health.\nPR: " + PR_URL + "\n"
    assert "{pr_url}" not in markdown


def test_a_comment_with_no_pr_line_at_all_still_carries_the_link(
    tmp_path: Path, git_repo: Path
) -> None:
    """"A PR plus a short ticket comment" is the product; a comment that never
    names the PR fails that regardless of what the reporter chose to write."""
    profile = _profile(tmp_path, git_repo)
    sink = RecordingSink()
    executor = _executor_writing_comment("Added /health. Tests pass.")
    orch = _orchestrator(profile, tmp_path / "runs", executor=executor, sink=sink, pr_url=PR_URL)

    orch.run_once(input_text=ITEM_TEXT)

    _key, markdown, _attachments = sink.comments[0]
    assert markdown.rstrip().endswith(f"PR: {PR_URL}")
    assert markdown.startswith("Added /health. Tests pass.")


def test_a_comment_that_already_names_the_pr_is_left_alone(
    tmp_path: Path, git_repo: Path
) -> None:
    profile = _profile(tmp_path, git_repo)
    sink = RecordingSink()
    original = f"Added /health.\nSee the PR at {PR_URL} for detail.\n"
    orch = _orchestrator(
        profile,
        tmp_path / "runs",
        executor=_executor_writing_comment(original),
        sink=sink,
        pr_url=PR_URL,
    )

    orch.run_once(input_text=ITEM_TEXT)

    _key, markdown, _attachments = sink.comments[0]
    assert markdown == original


def test_the_run_dir_ticket_comment_records_exactly_what_was_posted(
    tmp_path: Path, git_repo: Path
) -> None:
    """`runs/<id>/ticket_comment.md` is the record of the report. The reporter
    could not have known the URL, so the engine has to write the substituted text
    back rather than leaving the artifact disagreeing with the ticket."""
    profile = _profile(tmp_path, git_repo)
    sink = RecordingSink()
    runs_dir = tmp_path / "runs"
    orch = _orchestrator(
        profile,
        runs_dir,
        executor=_executor_writing_comment("Added /health.\nPR: \n"),
        sink=sink,
        pr_url=PR_URL,
    )

    run = orch.run_once(input_text=ITEM_TEXT)

    on_disk = (runs_dir / run.id / "ticket_comment.md").read_text(encoding="utf-8")
    _key, posted, _attachments = sink.comments[0]
    assert on_disk == posted
    assert f"PR: {PR_URL}" in on_disk


def test_no_pr_url_means_the_comment_is_posted_untouched(
    tmp_path: Path, git_repo: Path
) -> None:
    """`git_local` has nowhere to open a PR and returns None; `--dry-run` suppresses
    the call entirely. Neither may invent a link or mangle the comment."""
    profile = _profile(tmp_path, git_repo)
    sink = RecordingSink()
    orch = _orchestrator(
        profile,
        tmp_path / "runs",
        executor=_executor_writing_comment("Added /health.\nPR: \n"),
        sink=sink,
    )

    run = orch.run_once(input_text=ITEM_TEXT)

    assert "pr_url" not in run.extra
    assert sink.pr_url is None
    _key, markdown, _attachments = sink.comments[0]
    assert markdown == "Added /health.\nPR: \n"


# --------------------------------------------------------------------------- #
# 8. The `api` executor (S4) <-> the role prompts (S9) <-> the run loop (S8)
#
# The role prompts tell an agent to write its artifacts into the RUN DIR --
# `engine/context.py` renders `{plan_file}` as `<run_dir>/plan.md`, `{sections_dir}`
# as `<run_dir>/sections`, and `reporter.md` names `{run_dir}/pr.md` outright --
# and the run loop then reads `plan.sections` back from exactly there. Neither
# side is wrong on its own; what has to hold is that the ONE executor which
# enforces a path jail can actually reach that directory, and that what it
# reports having written is still only what landed in the workspace.
#
# `tests/test_e2e_offline.py` cannot see any of this: `FakeExecutor` writes
# run-dir artifacts with `Path.write_text`, never through a tool.
# --------------------------------------------------------------------------- #


def _api_executor(script: list, *, runtime=None) -> ApiLoopExecutor:
    return ApiLoopExecutor(
        AdapterConfig(type="api", model="default", max_iterations=10),
        provider=FakeModelProvider(script=script),
        runtime=runtime,
    )


def _exec_request(workspace: Path, run_dir: Path, *, tools: list[str], step_id: str) -> ExecRequest:
    return ExecRequest(
        system="s",
        prompt="p",
        workspace=workspace,
        artifacts_dir=run_dir,
        tools=tools,
        timeout_s=60,
        step_id=step_id,
    )


def test_the_planner_can_write_the_run_dir_artifacts_the_fan_out_reads_back(
    tmp_path: Path, git_repo: Path
) -> None:
    """The join that broke every shipped profile using the `api` executor: the
    planner is told to write `{plan_file}`/`{sections_dir}/section-N.md` (absolute
    run-dir paths), and `Orchestrator._list_sections(run_dir)` is what turns those
    files into `implement`'s fan-out. A workspace-only path jail refuses the write,
    the fan-out then finds nothing, and the run dies on "the planner produced no
    sections" -- with the model having reported success.
    """
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    plan_file = run_dir / "plan.md"
    sections_dir = run_dir / "sections"

    executor = _api_executor(
        [
            tool_turn("fs_write", {"path": str(plan_file), "content": "# Plan\n\nSecurity: no\n"}),
            tool_turn("fs_write", {"path": str(sections_dir / "section-1.md"), "content": "# One\n"}),
            tool_turn("fs_write", {"path": str(sections_dir / "section-2.md"), "content": "# Two\n"}),
            text_turn("Plan written: 2 sections."),
        ]
    )

    result = executor.run(
        _exec_request(git_repo, run_dir, tools=["fs.read", "fs.write", "fs.list"], step_id="plan")
    )

    assert result.error is None
    assert plan_file.is_file()
    # the run loop's own reader, on the real directory the tools just wrote
    assert [p.name for p in _list_sections(run_dir)] == ["section-1.md", "section-2.md"]


def test_the_coder_can_read_the_section_file_the_planner_left_in_the_run_dir(
    tmp_path: Path, git_repo: Path
) -> None:
    """`prompts/roles/coder.md` says "Read {section_file}" -- and `prompt_values()`
    renders that as a run-dir path, not a workspace one."""
    run_dir = tmp_path / "runs" / "r1"
    (run_dir / "sections").mkdir(parents=True)
    section = run_dir / "sections" / "section-1.md"
    section.write_text("# One\n\nImplement the health endpoint.\n", encoding="utf-8")

    provider = FakeModelProvider(
        script=[tool_turn("fs_read", {"path": str(section)}), text_turn("done")]
    )
    executor = ApiLoopExecutor(
        AdapterConfig(type="api", model="default", max_iterations=10), provider=provider
    )

    result = executor.run(
        _exec_request(git_repo, run_dir, tools=["fs.read", "fs.write"], step_id="implement")
    )

    assert result.error is None
    # the tool_result fed back to the model carried the section's real text
    followup = provider.calls[-1]["messages"][-1]
    contents = "".join(
        block.content for block in followup.content if isinstance(block, ToolResultBlock)
    )
    assert "Implement the health endpoint." in contents


def test_a_run_dir_artifact_is_never_reported_as_a_workspace_write(
    tmp_path: Path, git_repo: Path
) -> None:
    """`ExecResult.files_written` goes straight to `repo.verify_landed()`, which
    calls anything outside the workspace a file that failed to land. The `verify`
    step holds both `runtime.screenshot` and a `commit:` template, so a tester that
    takes one screenshot would otherwise fail its own run.
    """
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)

    # The real repo adapter, and the real worktree it hands the run as a workspace
    # -- `verify_landed()` measures against exactly that directory.
    repo = GitLocalRepo(AdapterConfig(type="git_local", path=str(git_repo)))
    workspace = repo.checkout("agent/seam-files-written")
    try:
        executor = _api_executor(
            [
                tool_turn("runtime_screenshot", {}),
                tool_turn(
                    "fs_write",
                    {"path": str(run_dir / "test-report.md"), "content": "2 passed\n"},
                ),
                tool_turn("fs_write", {"path": "VERIFIED.txt", "content": "ok\n"}),
                text_turn("2 passed, 0 failed."),
            ],
            runtime=FakeRuntime(png=PNG),
        )

        result = executor.run(
            _exec_request(
                workspace,
                run_dir,
                tools=["fs.read", "fs.write", "runtime.screenshot"],
                step_id="verify",
            )
        )

        assert result.error is None
        assert (run_dir / "screenshots").is_dir()  # the screenshot really was taken
        assert (run_dir / "test-report.md").is_file()
        assert (workspace / "VERIFIED.txt").is_file()  # and the workspace edit landed
        assert not any(str(run_dir) in str(p) for p in result.files_written)

        # the real check the orchestrator runs before every `commit:` step
        assert repo.verify_landed(result.files_written) == []
    finally:
        repo.cleanup()


def test_the_ingest_step_is_handed_the_ticket_text_its_only_tool_returns(
    tmp_path: Path, git_repo: Path
) -> None:
    """`source.read` is the ONLY tool `intake` is granted in every built-in
    pipeline, and `executors/tools.py` reads it out of `ToolContext.work_item_text`
    -- which only the engine can fill, because an executor never sees a `WorkItem`.
    """
    profile = _profile(tmp_path, git_repo)
    executor = _executor()
    orch = _orchestrator(profile, tmp_path / "runs", executor=executor)

    orch.run_once(input_text=ITEM_TEXT)

    intake = next(r for r in executor.requests if r.step_id == "intake")
    assert "Add a /health endpoint" in intake.work_item_text
    assert "Add a simple health check endpoint." in intake.work_item_text
    assert "Given X, when Y, then Z" in intake.work_item_text  # the acceptance criteria

    # ...and it survives the last hop, into the tool the model actually calls
    ctx = ToolContext(
        workspace=git_repo,
        artifacts_dir=tmp_path,
        allow={"source.read"},
        work_item_text=intake.work_item_text,
    )
    _defs, dispatch = build_tools(["source.read"], ctx)
    out, is_err = dispatch("source_read", {})
    assert is_err is False
    assert "Add a /health endpoint" in out


def test_the_file_sink_and_the_engine_do_not_both_leave_a_ticket_comment(
    tmp_path: Path, git_repo: Path
) -> None:
    """Both own `runs/<id>/ticket_comment.md`: the engine writes the record of what
    it posted, and `FileSink.comment()` APPENDS every comment it is handed to that
    same path. Driven with the REAL `FileSink` (the default sink, and the `also:`
    companion of every other shipped sink), the short ticket comment -- one of the
    two things this system exists to produce -- came out doubled.
    """
    profile = _profile(tmp_path, git_repo)
    runs_dir = tmp_path / "runs"
    orch = _orchestrator(profile, runs_dir, executor=_executor_writing_comment("Added /health.\n"))

    run = orch.run_once(input_text=ITEM_TEXT)

    on_disk = (runs_dir / run.id / "ticket_comment.md").read_text(encoding="utf-8")
    assert on_disk == "Added /health.\n"
    assert "---" not in on_disk
    assert on_disk.count("Added /health.") == 1
    # the sink still logged the call it received
    assert "- comment" in (runs_dir / run.id / "result.md").read_text(encoding="utf-8")
