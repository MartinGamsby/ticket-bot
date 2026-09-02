"""`ticketbot` command-line entry point.

`run`, `poll` and `resume` drive the engine (`ticketbot.engine.orchestrator`);
`validate` and `config {list,show,init,banner}` are config-only and never touch the
engine. Every subcommand function returns an int exit code; nothing calls
`sys.exit()` from deep inside.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from .config.loader import ConfigError, load_profile, load_profile_dict, resolved_yaml
from .config.redact import redact
from .core.banner import facts_from_profile, render_banner
from .core.run import Run, RunStatus, RunStore
from .engine.locks import LockHeld
from .engine.orchestrator import Orchestrator, resolve_runs_dir

# Exit codes, shared by `run`, `poll` and `resume`.
_EXIT_OK = 0
_EXIT_CONFIG_ERROR = 2
_EXIT_BLOCKED = 3
_EXIT_FAILED = 4

CONFIG_INIT_TEMPLATE = """\
name: __NAME__
version: 1
source: {type: file}
sink:   {type: file}
repo:   {type: git_local, path: "."}
model:
  default: main
  providers:
    main: {type: anthropic, model: claude-opus-5, effort: high}
executor:
  default: inline
  kinds:
    inline: {type: api, model: main, max_iterations: 40}
runtime: {type: none}
pipeline_selector:
  default: builtin:pipelines/standard.yaml
gates: {on_unclear: comment_and_unassign, on_pr_ready: human_review, max_clarify_rounds: 2}
budget: {max_cost_usd: 25, max_wall_clock_s: 5400}
"""


def _print_load_error(prefix: str, error: Exception) -> None:
    print(f"error: {prefix}: {redact(str(error))}", file=sys.stderr)


def _cmd_validate(args: argparse.Namespace) -> int:
    try:
        profile = load_profile(args.config)
    except ConfigError as e:
        _print_load_error(f"invalid config {args.config}", e)
        return 2
    except ValidationError as e:
        _print_load_error(f"invalid profile {args.config}", e)
        return 2

    print(f"OK  {profile.name}  ({args.config})")
    return 0


def _cmd_config_list(args: argparse.Namespace) -> int:
    directory = Path(args.dir)
    if not directory.is_dir():
        return 0

    for path in sorted(directory.glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        try:
            merged, _ = load_profile_dict(path)
            name = merged.get("name", path.stem)
        except ConfigError:
            name = path.stem
        print(f"{name}  {path}")
    return 0


def _cmd_config_show(args: argparse.Namespace) -> int:
    try:
        profile = load_profile(args.profile)
    except ConfigError as e:
        _print_load_error(f"invalid config {args.profile}", e)
        return 2
    except ValidationError as e:
        _print_load_error(f"invalid profile {args.profile}", e)
        return 2

    print(redact(resolved_yaml(profile)), end="")
    return 0


def _cmd_config_banner(args: argparse.Namespace) -> int:
    """Load the profile, build BannerFacts via facts_from_profile(), print
    redact(render_banner(facts))."""
    try:
        profile = load_profile(args.profile)
    except ConfigError as e:
        _print_load_error(f"invalid config {args.profile}", e)
        return 2
    except ValidationError as e:
        _print_load_error(f"invalid profile {args.profile}", e)
        return 2

    facts = facts_from_profile(profile)
    print(redact(render_banner(facts)), end="")
    return 0


def _cmd_config_init(args: argparse.Namespace) -> int:
    directory = Path(args.dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{args.name}.yaml"

    if target.exists() and not args.force:
        print(f"error: {target} already exists (use --force to overwrite)", file=sys.stderr)
        return 2

    target.write_text(CONFIG_INIT_TEMPLATE.replace("__NAME__", args.name), encoding="utf-8")
    print(f"wrote {target}")
    return 0


def _run_exit_code(run: Run) -> int:
    if run.status == RunStatus.DONE:
        return _EXIT_OK
    if run.status == RunStatus.BLOCKED:
        return _EXIT_BLOCKED
    if run.status == RunStatus.FAILED:
        return _EXIT_FAILED
    return _EXIT_OK


def _print_run_result(run: Run, run_dir: Path) -> None:
    print(f"run {run.id}  {run_dir}  ({run.status.value})")


def _load_profile_or_none(path: str) -> tuple[object, int | None]:
    """`load_profile(path)` -> `(profile, None)`, or `(None, 2)` after printing the
    error -- the shared "load a profile or bail with exit code 2" step for `run`/
    `poll`/`resume`."""
    try:
        return load_profile(path), None
    except ConfigError as e:
        _print_load_error(f"invalid config {path}", e)
        return None, _EXIT_CONFIG_ERROR
    except ValidationError as e:
        _print_load_error(f"invalid profile {path}", e)
        return None, _EXIT_CONFIG_ERROR


def _cmd_run(args: argparse.Namespace) -> int:
    profile, err = _load_profile_or_none(args.config)
    if err is not None:
        return err

    runs_dir = Path(args.runs_dir) if args.runs_dir else None
    repo_override = Path(args.repo) if args.repo else None
    input_path = Path(args.input) if args.input else None

    orchestrator = Orchestrator(
        profile,
        runs_dir=runs_dir,
        dry_run=args.dry_run,
        repo_override=repo_override,
        pause_at=args.pause_at,
    )
    try:
        run = orchestrator.run_once(
            external_id=args.once,
            input_text=args.input_text,
            input_path=input_path,
            force_lock=args.force_lock,
        )
    except LockHeld as e:
        print(f"error: {redact(str(e))}", file=sys.stderr)
        return _EXIT_CONFIG_ERROR
    except (ConfigError, ValidationError) as e:
        _print_load_error("run failed", e)
        return _EXIT_CONFIG_ERROR

    _print_run_result(run, orchestrator.store.dir(run.id))
    return _run_exit_code(run)


def _cmd_poll(args: argparse.Namespace) -> int:
    profile, err = _load_profile_or_none(args.config)
    if err is not None:
        return err

    runs_dir = Path(args.runs_dir) if args.runs_dir else None
    orchestrator = Orchestrator(profile, runs_dir=runs_dir, dry_run=args.dry_run)
    try:
        runs = orchestrator.poll(once=args.once, max_items=args.max_items)
    except (ConfigError, ValidationError) as e:
        _print_load_error("poll failed", e)
        return _EXIT_CONFIG_ERROR

    if not runs:
        print("poll: no runs")
        return _EXIT_OK

    worst = _EXIT_OK
    for run in runs:
        _print_run_result(run, orchestrator.store.dir(run.id))
        worst = max(worst, _run_exit_code(run))
    return worst


def _cmd_resume(args: argparse.Namespace) -> int:
    # Two-pass, because the two facts depend on each other: the run has to be
    # loaded to learn which profile it belongs to, but the PROFILE owns the
    # runs dir the run lives in. `--runs-dir` settles it outright; failing that,
    # an explicit `-c` lets the profile's own `runs_dir` be resolved before the
    # store is opened (`resolve_runs_dir`, shared with `Orchestrator`); with
    # neither, `runs/` is the only thing that can be known here.
    profile: object | None = None
    if args.runs_dir:
        runs_dir = Path(args.runs_dir)
    elif args.config:
        profile, err = _load_profile_or_none(args.config)
        if err is not None:
            return err
        runs_dir = resolve_runs_dir(profile)  # type: ignore[arg-type]
    else:
        runs_dir = Path("runs")

    store = RunStore(runs_dir)
    try:
        run = store.load(args.run_id)
    except (OSError, ValueError) as e:
        print(f"error: no such run {args.run_id!r} under {runs_dir}: {redact(str(e))}", file=sys.stderr)
        return _EXIT_CONFIG_ERROR

    if profile is None:
        config_path = args.config if args.config else str(Path("profiles") / f"{run.profile_name}.yaml")
        profile, err = _load_profile_or_none(config_path)
        if err is not None:
            return err

    orchestrator = Orchestrator(profile, runs_dir=runs_dir)
    try:
        resumed = orchestrator.resume(args.run_id, force_lock=args.force_lock)
    except LockHeld as e:
        print(f"error: {redact(str(e))}", file=sys.stderr)
        return _EXIT_CONFIG_ERROR
    except (ConfigError, ValidationError) as e:
        _print_load_error("resume failed", e)
        return _EXIT_CONFIG_ERROR

    _print_run_result(resumed, orchestrator.store.dir(resumed.id))
    return _run_exit_code(resumed)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ticketbot")
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="load and validate a profile")
    p_validate.add_argument("-c", "--config", required=True, help="path to the profile YAML")
    p_validate.set_defaults(func=_cmd_validate)

    p_config = sub.add_parser("config", help="inspect and manage profiles")
    config_sub = p_config.add_subparsers(dest="config_command", required=True)

    p_list = config_sub.add_parser("list", help="list profiles in a directory")
    p_list.add_argument("--dir", default="profiles", help="directory to scan (default: profiles)")
    p_list.set_defaults(func=_cmd_config_list)

    p_show = config_sub.add_parser("show", help="print the resolved profile as YAML")
    p_show.add_argument("profile", help="path to the profile YAML")
    p_show.set_defaults(func=_cmd_config_show)

    p_banner = config_sub.add_parser("banner", help="print the 'what was used' banner for a profile")
    p_banner.add_argument("profile", help="path to the profile YAML")
    p_banner.set_defaults(func=_cmd_config_banner)

    p_init = config_sub.add_parser("init", help="scaffold a minimal offline profile")
    p_init.add_argument("name", help="profile name")
    p_init.add_argument("--dir", default="profiles", help="directory to write into (default: profiles)")
    p_init.add_argument("--force", action="store_true", help="overwrite an existing file")
    p_init.set_defaults(func=_cmd_config_init)

    p_run = sub.add_parser("run", help="run the pipeline once for a single work item")
    p_run.add_argument("-c", "--config", required=True, help="path to the profile YAML")
    p_run.add_argument("--once", metavar="EXTERNAL_ID", default=None, help="fetch this id from the configured source")
    p_run.add_argument("--input", default=None, help="path to a file, forces a FileSource")
    p_run.add_argument("--input-text", default=None, help="inline text, forces a FileSource")
    p_run.add_argument("--repo", default=None, help="override repo.path")
    p_run.add_argument("--dry-run", action="store_true", help="suppress outward-facing sink/repo effects")
    p_run.add_argument("--pause-at", metavar="STEP_ID", default=None, help="await human approval at this step's optional_human gate")
    p_run.add_argument("--force-lock", action="store_true", help="break an existing (possibly stale) lock")
    p_run.add_argument("--runs-dir", default=None, help="override the profile's runs_dir")
    p_run.set_defaults(func=_cmd_run)

    p_poll = sub.add_parser("poll", help="poll the configured source and run the pipeline for new items")
    p_poll.add_argument("-c", "--config", required=True, help="path to the profile YAML")
    p_poll.add_argument("--once", action="store_true", help="do a single poll sweep instead of looping")
    p_poll.add_argument("--max-items", type=int, default=None, help="cap the number of items per sweep")
    p_poll.add_argument("--dry-run", action="store_true", help="suppress outward-facing sink/repo effects")
    p_poll.add_argument("--runs-dir", default=None, help="override the profile's runs_dir")
    p_poll.set_defaults(func=_cmd_poll)

    p_resume = sub.add_parser("resume", help="resume an interrupted or blocked run")
    p_resume.add_argument("run_id", help="the run id under runs-dir to resume")
    p_resume.add_argument("-c", "--config", default=None, help="path to the profile YAML (default: profiles/<run's profile_name>.yaml)")
    p_resume.add_argument("--runs-dir", default=None, help="directory containing runs/<run_id> (default: the -c profile's runs_dir, else runs)")
    p_resume.add_argument("--force-lock", action="store_true", help="break an existing (possibly stale) lock")
    p_resume.set_defaults(func=_cmd_resume)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
