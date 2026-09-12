"""``lithos-loom eval triage`` — measure the S5a triage step on known verdicts.

On-demand, host-only, spends tokens (one read-only container turn per
sample). NOT part of ``make check``; the shipped fixtures are preflighted
hermetically by ``tests/test_eval_triage_shipped.py``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import typer

from ...plugins.story_develop import engines
from ...plugins.story_develop.config import parse_effort
from ...plugins.story_develop.daemon_io import load_tool_default_models
from ...plugins.story_develop.model_policy import require_agent_models
from ..review.app import discover_cases, eval_app, require_rate
from ..review.report import ci_band, err_suffix
from ..review.stats import wilson_interval
from .case import TriageCase, load_triage_case
from .harness import (
    DEFAULT_BAR,
    DEFAULT_K,
    DEFAULT_TIMEOUT,
    TriageCaseResult,
    TriageSink,
    expected_fingerprint,
    live_triage,
    run_triage_case,
)

DEFAULT_TRIAGE_CASES_DIR = Path("evals/triage/cases")


@eval_app.command("triage")
def triage(
    case: str | None = typer.Option(
        None, "--case", help="Run only this case id (default: all)."
    ),
    k: int = typer.Option(DEFAULT_K, "-k", "--samples", help="Triage turns per case."),
    bar: float = typer.Option(
        DEFAULT_BAR,
        "--bar",
        help="Known-false reject rate (with the right citation) a case must reach.",
    ),
    max_over_suppression: float = typer.Option(
        0.0,
        "--max-over-suppression",
        help="Highest tolerated rate of must-proceed findings rejected (default 0: "
        "one wrongly rejected true finding fails the case).",
    ),
    tool: str = typer.Option(
        "claude", "--tool", help="Agent that runs the triage turn (claude | codex)."
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        help="Explicit model for the triage agent (default: the loom config's "
        "\\[story_develop.default_models] entry for --tool; required either way).",
    ),
    effort: str | None = typer.Option(
        None,
        "--effort",
        help="Effort lever (low|medium|high|xhigh|max), if the engine has one.",
    ),
    timeout: int = typer.Option(
        DEFAULT_TIMEOUT,
        "--timeout",
        help="Seconds per triage turn (default: converge's reviewer timeout).",
    ),
    report_dir: Path | None = typer.Option(
        None,
        "--report-dir",
        help="Retain each sample's verdicts + a summary under this dir.",
    ),
    cases_dir: Path = typer.Option(
        DEFAULT_TRIAGE_CASES_DIR,
        "--cases-dir",
        help="Directory of triage case folders.",
    ),
) -> None:
    """Measure triage on batches of findings with known verdicts (PRD S8).

    Each sample is ONE read-only triage turn over a case's whole batch, the
    production shape. Two rates per case: **reject** — known-false findings
    rejected with a citation into their declared refutation files — and
    **over-supp** — must-proceed findings (known-true or ambiguous) that were
    rejected, the expensive direction. A case passes at the bar on the first
    with the second at or under ``--max-over-suppression``. A FAIL is the
    measurement; exit 1 only when a case has no valid sample (the triage turn
    degraded to all-proceed every time — no verdict was ever given).
    """
    require_rate("--bar", bar)
    require_rate("--max-over-suppression", max_over_suppression)
    if timeout < 1:
        raise typer.BadParameter(
            f"--timeout must be a positive number of seconds (got {timeout})"
        )
    case_dirs = discover_cases(cases_dir, case)

    # Fail closed before any paid turn (#304): the triage agent's model must be
    # explicit — it decides which claims a coder never sees.
    if not engines.is_supported(tool):
        raise typer.BadParameter(
            f"--tool {tool!r} is not a supported agent tool "
            f"(known: {', '.join(sorted(engines.supported_tools()))})"
        )
    default_models, frictions = load_tool_default_models()
    for friction in frictions:
        typer.echo(f"[Friction] {friction}", err=True)
    # A blank --model is "not given", never "the empty model" (an implicit
    # fallback would be exactly the silent no-op #304 forbids).
    resolved_model = (model.strip() if model else "") or default_models.get(tool)
    try:
        effort = parse_effort(effort, where="eval triage --effort")
        require_agent_models(
            panel=(),
            coder=tool,
            coder_model=resolved_model,
            default_models=default_models,
            where="eval triage",
        )
    except ValueError as exc:
        raise typer.BadParameter(f"{exc} (or pass --model)") from exc
    assert resolved_model is not None  # require_agent_models raised otherwise

    loaded = [load_triage_case(d) for d in case_dirs]
    triage_info = {
        "tool": tool,
        "model": resolved_model,
        "effort": effort,
        "timeout": timeout,
    }
    results: list[TriageCaseResult] = []
    for tc in loaded:
        typer.echo(
            f"triaging {tc.id} × {k} … [{len(tc.known_false)} known-false, "
            f"{len(tc.known_true)} must-proceed; {tool}/{resolved_model}"
            f"{'/' + effort if effort else ''}]",
            err=True,
        )
        result = run_triage_case(
            tc,
            k=k,
            bar=bar,
            max_over_suppression=max_over_suppression,
            triage_fn=lambda c, sha: live_triage(
                c,
                sha,
                tool=tool,
                model=resolved_model,
                effort=effort,
                timeout=timeout,
                default_models=dict(default_models),
            ),
            sink=_make_sink(report_dir) if report_dir is not None else None,
        )
        results.append(result)
        if report_dir is not None:
            _write_summary(
                report_dir,
                tc,
                result,
                bar=bar,
                max_over_suppression=max_over_suppression,
                triage_info=triage_info,
            )

    print_triage_table(results)
    no_valid = [r.case_id for r in results if r.n_valid == 0]
    if no_valid:
        typer.echo(
            "no valid samples (the triage turn degraded to all-proceed on every "
            "sample): " + ", ".join(no_valid),
            err=True,
        )
        raise typer.Exit(1)


def _make_sink(report_dir: Path) -> TriageSink:
    def sink(case_id: str, i: int, payload: dict) -> None:
        out = report_dir / case_id
        out.mkdir(parents=True, exist_ok=True)
        (out / f"sample-{i}.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )

    return sink


def _write_summary(
    report_dir: Path,
    case: TriageCase,
    r: TriageCaseResult,
    *,
    bar: float,
    max_over_suppression: float,
    triage_info: dict,
) -> None:
    out = report_dir / case.id
    out.mkdir(parents=True, exist_ok=True)
    payload = {
        "case": case.id,
        "repo": case.repo,
        "tree": case.tree_label,
        # Pins what the SCORER consumed (cf. eval review, #307): a reworded
        # claim, a widened refutation or a flipped expectation changes it.
        "expected_fingerprint": expected_fingerprint(case),
        "bar": bar,
        "max_over_suppression": max_over_suppression,
        "triage": triage_info,
        "n": r.n,
        "n_valid": r.n_valid,
        "reject_rate": r.reject_rate,
        "reject_rate_ci": list(r.reject_rate_ci),
        "rejected_known_false": r.rejected_known_false,
        "known_false_opportunities": r.known_false_opportunities,
        "over_suppression_rate": r.over_suppression_rate,
        "over_suppression_ci": list(r.over_suppression_ci),
        "suppressed_known_true": r.suppressed_known_true,
        "known_true_opportunities": r.known_true_opportunities,
        "samples_with_suppression": r.samples_with_suppression,
        "passed": r.passed,
        "errored_per_sample": list(r.errored_per_sample),
        "rejected_known_false_per_sample": list(r.rejected_known_false_per_sample),
        "suppressed_known_true_per_sample": list(r.suppressed_known_true_per_sample),
        "cost_usd_per_sample": list(r.cost_usd_per_sample),
        "notes_per_sample": list(r.notes_per_sample),
        "per_finding_correct": r.per_finding_correct,
        "per_finding_expected": r.per_finding_expected,
        "per_finding_ambiguous": r.per_finding_ambiguous,
    }
    (out / "summary.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _rate_cell(count: int, opps: int, ci: tuple[float, float]) -> str:
    if opps == 0:
        return "—"
    return f"{count}/{opps} {ci_band(*ci)}"


def print_triage_table(results: Sequence[TriageCaseResult]) -> None:
    header = (
        f"{'case':<28} {'n':>3} {'valid':>5} {'reject (95% CI)':>22} "
        f"{'over-supp (95% CI)':>22} {'cost':>8}  result"
    )
    typer.echo(header)
    typer.echo("-" * len(header))
    tot_rej = tot_kf = tot_sup = tot_kt = 0
    for r in results:
        cost = sum(r.cost_usd_per_sample)
        mark = ("PASS" if r.passed else "FAIL") + err_suffix(sum(r.errored_per_sample))
        rej = _rate_cell(
            r.rejected_known_false, r.known_false_opportunities, r.reject_rate_ci
        )
        sup = _rate_cell(
            r.suppressed_known_true, r.known_true_opportunities, r.over_suppression_ci
        )
        typer.echo(
            f"{r.case_id:<28} {r.n:>3} {r.n_valid:>5} {rej:>22} {sup:>22} "
            f"{'$' + format(cost, '.2f'):>8}  {mark}"
        )
        tot_rej += r.rejected_known_false
        tot_kf += r.known_false_opportunities
        tot_sup += r.suppressed_known_true
        tot_kt += r.known_true_opportunities
    if len(results) > 1:
        rej_ci = wilson_interval(tot_rej, tot_kf) if tot_kf else (0.0, 0.0)
        sup_ci = wilson_interval(tot_sup, tot_kt) if tot_kt else (0.0, 0.0)
        typer.echo(
            f"pooled: known-false rejected {_rate_cell(tot_rej, tot_kf, rej_ci)}; "
            f"must-proceed suppressed {_rate_cell(tot_sup, tot_kt, sup_ci)} "
            f"over {len(results)} cases"
        )
