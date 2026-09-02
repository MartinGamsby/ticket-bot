"""`ticketbot` command-line entry point.

Only `validate` and `config {list,show,init}` exist in this section — later sections
add `run`, `poll`, `resume`, and `config banner` as one-line additions to
`build_parser()`. Every subcommand function returns an int exit code; nothing calls
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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
