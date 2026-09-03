"""`StubExecutor` — runs the whole pipeline with no model, no network, no CLI.

This is the executor that makes "offline" literally true. `file-text-none.yaml`
still reaches Anthropic (its executor is `api`) and `file-claude-cli.yaml` still
spawns `claude`; this one calls nothing at all. It exists so the pipeline itself —
step ordering, `when:` gating, `for_each` fan-out, gates, the run store, the banner,
commits, the reporting contract — can be exercised and debugged without spending a
token or needing a credential.

**It does no work.** Every step reports that it did nothing, echoes back the prompt
it was handed (truncated) so you can see exactly what each role WOULD have been
asked, and writes placeholder content for whatever the step declares under
`produces:`. What you get is a complete, correctly-shaped `runs/<id>/` tree whose
contents are all obviously fake.

The one piece of real behaviour it must have: satisfying the engine's contracts.
`implement` hard-fails when the planner left no `sections/*.md`, and the engine warns
for any declared artifact that is missing. So the stub writes what `ExecRequest.produces`
names — driven by config, not by hardcoded step ids — and expands a `sections/` entry
into a real section file so the fan-out has something to iterate.

Configuration (all optional):

```yaml
executor:
  default: stub
  kinds:
    stub:
      type: stub
      sections: 2          # how many section-N.md the planner step emits (default 1)
      echo_prompt: true    # include the received prompt in the summary (default true)
      echo_limit: 2000     # max prompt characters echoed (default 2000)
      note: "..."          # override the "did nothing" line
```
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from ..config.schema import AdapterConfig
from .base import ExecRequest, ExecResult, append_log

logger = logging.getLogger(__name__)

DEFAULT_NOTE = (
    "Did nothing: this is the stub executor, not a real agent. "
    "It exists so the pipeline can be exercised offline."
)

# `Security: no` keeps the conditional `security` step off in a stub run. The gate
# fails CLOSED -- an absent line means "unknown", which runs the step -- so the stub
# has to say so explicitly rather than stay silent. Saying "no" is honest here: a
# run that changed nothing has no security surface to review.
_PLAN_TEMPLATE = """# Plan (stub)

{note}

## Goal

Placeholder goal for `{step_id}`. No model was consulted.

## Sections

{section_list}

## Risks & edge cases

None assessed - nothing was analysed.

## Test strategy

None. This run executed no work.

Security: no
"""

_SECTION_TEMPLATE = """# Section {n} (stub)

{note}

Files touched: none.
Key changes: none.
"""

_GENERIC_TEMPLATE = """# {name} (stub)

{note}

Produced by step `{step_id}` with no model call.
"""


class StubExecutor:
    def __init__(self, cfg: AdapterConfig | None = None) -> None:
        opt = cfg.opt if cfg is not None else (lambda _k, d=None: d)
        self.sections: int = max(1, int(opt("sections", 1) or 1))
        self.echo_prompt: bool = bool(opt("echo_prompt", True))
        self.echo_limit: int = max(0, int(opt("echo_limit", 2000) or 0))
        self.note: str = str(opt("note", DEFAULT_NOTE) or DEFAULT_NOTE)

    def describe(self) -> str:
        return "stub: no model, no network"

    def run(self, req: ExecRequest) -> ExecResult:
        written = self._write_artifacts(req)
        text = self._summary(req, written)

        if req.log_path is not None:
            append_log(req.log_path, f"[stub] step={req.step_id} wrote={[str(p) for p in written]}")

        # Built directly rather than through `finish_result()`, which parses
        # QUESTION:/DEFER: out of the text. The echoed prompt CONTAINS both markers
        # -- every role prompt carries the "end your turn with QUESTION:" protocol
        # -- so parsing it would make the stub raise a question it never asked and
        # block the run. A stub must never pause the pipeline for a human or spawn
        # a fixer: there is no finding behind it.
        # `files_written` is WORKSPACE files, and the engine's landing check fails
        # a step whose declared paths sit outside the workspace. Everything the
        # stub writes is a run-dir artifact, so this stays empty -- reporting them
        # here made `verify` kill its own run, the same way a captured screenshot
        # once did. The paths are still in the summary and the step log.
        return ExecResult(
            text=text,
            question=None,
            defers=[],
            files_written=[],
            exit_code=0,
        )

    # ------------------------------------------------------------------ #

    def _write_artifacts(self, req: ExecRequest) -> list[Path]:
        written: list[Path] = []
        for name in req.produces:
            target = Path(req.artifacts_dir) / name
            if name.endswith("/") or name.endswith("\\"):
                written.extend(self._write_dir(target, req))
                continue
            if target.exists():
                continue  # a real step already produced it; never clobber
            written.append(self._write_file(target, name, req))
        return written

    def _write_dir(self, directory: Path, req: ExecRequest) -> list[Path]:
        """Expand a `produces:` directory entry into files.

        `sections/` is the one that carries a hard contract: `implement` fans out
        over `section-*.md` and the run FAILS with "the planner produced no
        sections" if the directory is empty, so an empty mkdir is not enough.
        """
        directory.mkdir(parents=True, exist_ok=True)
        if directory.name != "sections":
            return []

        written: list[Path] = []
        for n in range(1, self.sections + 1):
            path = directory / f"section-{n}.md"
            if path.exists():
                continue
            path.write_text(_SECTION_TEMPLATE.format(n=n, note=self.note), encoding="utf-8")
            written.append(path)
        return written

    def _write_file(self, target: Path, name: str, req: ExecRequest) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        if Path(name).suffix == ".json":
            target.write_text(
                json.dumps({"stub": True, "step": req.step_id or "?", "note": self.note}, indent=2),
                encoding="utf-8",
            )
            return target
        if Path(name).name == "plan.md":
            section_list = "\n".join(
                f"{n}. Stub section {n} - no files, no changes."
                for n in range(1, self.sections + 1)
            )
            body = _PLAN_TEMPLATE.format(
                note=self.note, step_id=req.step_id or "?", section_list=section_list
            )
        else:
            body = _GENERIC_TEMPLATE.format(
                name=Path(name).name, note=self.note, step_id=req.step_id or "?"
            )
        target.write_text(body, encoding="utf-8")
        return target

    def _summary(self, req: ExecRequest, written: list[Path]) -> str:
        lines = [self.note, "", f"step: {req.step_id or '?'}"]
        if req.model:
            lines.append(f"model slot it would have used: {req.model}")
        if req.tools:
            lines.append(f"tools it was granted: {', '.join(req.tools)}")
        if written:
            names = ", ".join(p.name for p in written)
            lines.append(f"placeholder artifacts written: {names}")

        if self._wants_json(req):
            # `Orchestrator._after_ingest` parses this step's OUTPUT TEXT as JSON
            # and warns when it cannot. A fenced block satisfies that without
            # giving up the prose echo, but it MUST come before the echo:
            # `_JSON_FENCE_RE` takes the FIRST fenced block in the text, and every
            # role prompt contains fenced examples of its own.
            #
            # `acceptance` is filled and `ambiguity` low on purpose, so the
            # `clarify` step's `when:` is false and a stub run takes the full happy
            # path through implement/verify/review/publish rather than stopping at
            # the clarification gate.
            lines += [
                "",
                "```json",
                json.dumps(
                    {
                        "acceptance": "(stub) no acceptance criteria required",
                        "ambiguity": "low",
                        "note": self.note,
                    },
                    indent=2,
                ),
                "```",
            ]

        if self.echo_prompt and req.prompt:
            prompt = req.prompt
            if len(prompt) > self.echo_limit:
                prompt = prompt[: self.echo_limit] + f"\n... [truncated, {len(req.prompt)} chars]"
            lines += ["", "--- prompt it received ---", prompt]

        return "\n".join(lines)

    @staticmethod
    def _wants_json(req: ExecRequest) -> bool:
        """True when the step declares a `.json` artifact -- i.e. `intake`, whose
        output text the engine parses as JSON."""
        return any(Path(name).suffix == ".json" for name in req.produces)
