"""``lithos-loom eval review`` — measure review-correctness on seeded defects (#183).

An **on-demand** eval (NOT part of ``make check``): for each case it runs the
reviewer panel K times against a known-defect change via review-only mode (#154),
scores each run, and prints catch-rate / severity-correctness / false-positive.
Needs the host sandbox + agent CLIs — it spends real tokens.

Matching defaults to the **mechanism LLM-judge** (ADR 0005): it confirms each
finding describes the case's specific defect, not just the same topic — without
it the structured matcher over-counts on same-topic changes. ``--no-judge`` falls
back to the cheap structured matcher. ``--report-dir`` retains each run's report.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from .case import load_case
from .harness import DEFAULT_BAR, DEFAULT_K, CaseResult, ReportSink, run_case
from .judge import build_agent_judge

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
    judge: bool = typer.Option(
        True, "--judge/--no-judge", help="Use the mechanism LLM-judge (default on)."
    ),
    judge_tool: str = typer.Option(
        "claude", "--judge-tool", help="Agent for the judge (claude | codex)."
    ),
    report_dir: Path | None = typer.Option(
        None, "--report-dir", help="Retain each run's report JSON under this dir."
    ),
    cases_dir: Path = typer.Option(
        _DEFAULT_CASES_DIR, "--cases-dir", help="Directory of case folders."
    ),
) -> None:
    """Measure the panel's catch-rate on the seeded-defect benchmark."""
    case_dirs = _discover(cases_dir, case)

    judge_fn = build_agent_judge(tool=judge_tool) if judge else None
    sink = _make_report_sink(report_dir) if report_dir is not None else None

    results = []
    for case_dir in case_dirs:
        loaded = load_case(case_dir)
        typer.echo(f"running {loaded.id} × {k} …", err=True)
        result = run_case(loaded, k=k, bar=bar, judge=judge_fn, report_sink=sink)
        results.append(result)
        if report_dir is not None:
            _write_summary(report_dir, result)

    _print_table(results)
    if any(not r.passed for r in results):
        raise typer.Exit(1)


def _make_report_sink(report_dir: Path) -> ReportSink:
    """A sink that writes each run's report to ``<dir>/<case>/<variant>-<i>.json``."""

    def sink(case_id: str, variant: str, i: int, report: dict) -> None:
        out = report_dir / case_id
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{variant}-{i}.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )

    return sink


def _write_summary(report_dir: Path, r: CaseResult) -> None:
    """Write a per-case ``summary.json`` (rates + per-sample booleans + CIs).

    Beside the per-run ``buggy-N.json`` files, so a costly K-sample run is
    re-analysable for variance **without** re-scoring (which would re-invoke the
    paid judge).
    """
    out = report_dir / r.case_id
    out.mkdir(parents=True, exist_ok=True)
    errored = sum(r.errored_per_sample)
    fp_errored = sum(r.false_positive_errored_per_sample)
    (out / "summary.json").write_text(
        json.dumps(
            {
                "case": r.case_id,
                "k": r.n,
                "n_valid": r.n - errored,
                "errored": errored,
                "catch_rate": r.catch_rate,
                "catch_rate_ci": list(r.catch_rate_ci),
                "caught_per_sample": list(r.caught_per_sample),
                "errored_per_sample": list(r.errored_per_sample),
                "severity_correctness": r.severity_correctness,
                "severity_per_sample": list(r.severity_per_sample),
                "false_positive_rate": r.false_positive_rate,
                "false_positive_rate_ci": list(r.false_positive_rate_ci),
                "false_positive_per_sample": list(r.false_positive_per_sample),
                "false_positive_errored": fp_errored,
                "false_positive_errored_per_sample": list(
                    r.false_positive_errored_per_sample
                ),
                "passed": r.passed,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _ci_band(lo: float, hi: float) -> str:
    return f"{lo * 100:.0f}-{hi * 100:.0f}%"


def _err_suffix(n: int) -> str:
    return f" +{n}err" if n else ""


def _print_table(results: list[CaseResult]) -> None:
    header = (
        f"{'case':<28} {'n':>3} {'catch (95% CI)':>20} "
        f"{'sev':>5} {'fp (95% CI)':>20}  result"
    )
    typer.echo(header)
    typer.echo("-" * len(header))
    for r in results:
        # Denominators are the VALID (non-errored) samples; errored counts are
        # shown separately so a crashed reviewer never deflates a rate (#182 A3).
        n_err = sum(r.errored_per_sample)
        n_valid = r.n - n_err
        caught = sum(r.caught_per_sample)
        catch_cell = (
            f"{caught}/{n_valid} {_ci_band(*r.catch_rate_ci)}{_err_suffix(n_err)}"
        )
        if r.false_positive_per_sample:
            fp_err_samples = r.false_positive_errored_per_sample or (
                (False,) * len(r.false_positive_per_sample)
            )
            fp_err = sum(fp_err_samples)
            fp_valid = len(r.false_positive_per_sample) - fp_err
            flagged = sum(
                f
                for f, e in zip(
                    r.false_positive_per_sample, fp_err_samples, strict=True
                )
                if not e
            )
            fp_cell = (
                f"{flagged}/{fp_valid} "
                f"{_ci_band(*r.false_positive_rate_ci)}{_err_suffix(fp_err)}"
            )
        else:
            fp_cell = f"{r.false_positive_rate * 100:.0f}%"
        mark = "PASS" if r.passed else "FAIL"
        typer.echo(
            f"{r.case_id:<28} {r.n:>3} {catch_cell:>20} "
            f"{r.severity_correctness * 100:>4.0f}% {fp_cell:>20}  {mark}"
        )
