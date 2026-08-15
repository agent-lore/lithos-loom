"""The ``lithos-loom eval`` command group and the checks every command shares.

Split out of ``cli.py`` so a second command (``eval rescore``) reuses the
**fail-closed-before-paid-work** rules rather than re-implementing them. That
convention is the reason this module exists: a second copy of "reject an
out-of-range rate" or "reject a judge with no explicit model" is a copy that can
drift, and the failure mode of drift here is a run that silently measures
something other than what was asked for.
"""

from __future__ import annotations

import math
from pathlib import Path

import typer

from ...plugins.story_develop import engines
from .judge import build_agent_judge
from .match import Judge

eval_app = typer.Typer(
    name="eval",
    help="On-demand evaluation harnesses (not part of `make check`).",
    no_args_is_help=True,
)

# Cases live as data at the repo root, so adding one is a documented, code-free step.
DEFAULT_CASES_DIR = Path("evals/review/cases")


@eval_app.callback()
def _eval() -> None:
    """Force the `eval <command>` group form (Typer collapses a lone command)."""


def discover_cases(cases_dir: Path, case_id: str | None) -> list[Path]:
    if not cases_dir.is_dir():
        raise typer.BadParameter(f"no cases directory at {cases_dir}")
    dirs = sorted(d for d in cases_dir.iterdir() if (d / "case.toml").is_file())
    if case_id is not None:
        dirs = [d for d in dirs if d.name == case_id]
        if not dirs:
            raise typer.BadParameter(f"no case {case_id!r} under {cases_dir}")
    if not dirs:
        raise typer.BadParameter(f"no cases found under {cases_dir}")
    return dirs


def require_rate(flag: str, value: float | None) -> None:
    """Reject a rate option that is not a finite fraction in ``[0, 1]``.

    Out-of-range values do not error on their own — they quietly redefine the
    run (a bar above 1 fails every case; below 0 passes every case; NaN compares
    false against everything), which is the same silent no-op class the panel
    overrides already fail closed on.
    """
    if value is None:
        return
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise typer.BadParameter(
            f"{flag} must be a finite rate between 0 and 1 (got {value})"
        )


def resolve_judge(
    *, judge: bool, judge_tool: str, default_models: dict[str, str]
) -> tuple[dict | None, Judge | None]:
    """``(judge_info, judge_fn)`` — validated **before** any paid work.

    The #304 explicit-model policy reaches the judge too: its verdicts decide
    what counts as a catch, so a judge silently running the image CLI's builtin
    default would make every number unattributable. An unsupported tool or a
    tool with no configured default model aborts the whole invocation here,
    rather than one case into a sweep.
    """
    if judge and not engines.is_supported(judge_tool):
        # Wording preserved verbatim from cli.py. `supported_tools_phrase()`
        # would read more like the repo's other validation errors, but changing
        # operator-visible text inside a no-behaviour-change extraction is how a
        # refactor stops being one — that belongs in a change that says so.
        raise typer.BadParameter(
            f"--judge-tool {judge_tool!r} is not a supported agent tool "
            f"(known: {', '.join(sorted(engines.supported_tools()))})"
        )
    judge_model = default_models.get(judge_tool)
    if judge and judge_model is None:
        raise typer.BadParameter(
            f"judge (tool {judge_tool!r}) resolves to no explicit model — set"
            f' [story_develop.default_models] {judge_tool} = "<model-id>" in the'
            " loom config, or run --no-judge"
        )
    if not judge:
        return None, None
    return (
        {"tool": judge_tool, "model": judge_model},
        build_agent_judge(tool=judge_tool, model=judge_model),
    )
