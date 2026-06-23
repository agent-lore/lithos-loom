"""``lithos-loom eval review`` — measure review-correctness on seeded defects (#183).

An **on-demand** eval (NOT part of ``make check``): for each case it runs the
reviewer panel K times against a known-defect change via review-only mode (#154),
scores each run, and prints catch-rate / severity-correctness / false-positive.
Needs the host sandbox + agent CLIs — it spends real tokens.

The structured matcher (file + keyword) runs by default; the LLM-judge fallback
the matcher supports is a documented follow-up (see ADR 0005).
"""

from __future__ import annotations

from pathlib import Path

import typer

from .case import load_case
from .harness import DEFAULT_BAR, DEFAULT_K, run_case

eval_app = typer.Typer(
    name="eval",
    help="On-demand evaluation harnesses (not part of `make check`).",
    no_args_is_help=True,
)

# Cases live as data at the repo root, so adding one is a documented, code-free step.
_DEFAULT_CASES_DIR = Path("evals/review/cases")


@eval_app.callback()
def _eval() -> None:
    """Force the `eval <command>` group form (Typer collapses a lone command)."""


def _discover(cases_dir: Path, case_id: str | None) -> list[Path]:
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


@eval_app.command("review")
def review(
    case: str | None = typer.Option(
        None, "--case", help="Run only this case id (default: all)."
    ),
    k: int = typer.Option(DEFAULT_K, "-k", "--samples", help="Runs per case."),
    bar: float = typer.Option(
        DEFAULT_BAR, "--bar", help="Catch-rate a case must reach to pass."
    ),
    cases_dir: Path = typer.Option(
        _DEFAULT_CASES_DIR, "--cases-dir", help="Directory of case folders."
    ),
) -> None:
    """Measure the panel's catch-rate on the seeded-defect benchmark."""
    case_dirs = _discover(cases_dir, case)

    results = []
    for case_dir in case_dirs:
        loaded = load_case(case_dir)
        typer.echo(f"running {loaded.id} × {k} …", err=True)
        results.append(run_case(loaded, k=k, bar=bar))

    _print_table(results)
    if any(not r.passed for r in results):
        raise typer.Exit(1)


def _print_table(results: list) -> None:
    header = f"{'case':<28} {'n':>3} {'catch':>7} {'sev-ok':>7} {'fp':>6}  result"
    typer.echo(header)
    typer.echo("-" * len(header))
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        typer.echo(
            f"{r.case_id:<28} {r.n:>3} "
            f"{r.catch_rate * 100:>6.0f}% {r.severity_correctness * 100:>6.0f}% "
            f"{r.false_positive_rate * 100:>5.0f}%  {mark}"
        )
