"""Presenting a :class:`~.harness.CaseResult` — as a table row, or as JSON.

Extracted from ``cli.py`` so **every** command renders results the same way.
That matters more than the line count it saves: a re-scored report dir is only
comparable to the run that produced it if both go through one renderer and one
payload builder, so a divergence in the numbers can never be an artefact of two
code paths having drifted.

Rendering deliberately keeps the core columns fixed-width and identical across
commands: report dirs get column-diffed against each other, and a table whose
shape depends on which command printed it breaks that.
"""

from __future__ import annotations

from collections.abc import Sequence

import typer

from .harness import CaseResult, count_valid
from .match import judge_status_errored
from .stats import wilson_interval

# Extra per-case cells a command can append after `result` (header, width, cell).
# Used by `eval rescore` for its stability columns; `eval review` passes none.
ExtraColumn = tuple[str, int, "object"]


def ci_band(lo: float, hi: float) -> str:
    return f"{lo * 100:.0f}-{hi * 100:.0f}%"


def err_suffix(n: int) -> str:
    return f" +{n}err" if n else ""


def judge_err_suffix(n: int) -> str:
    return f" +{n}jerr" if n else ""


def structured_tally(r: CaseResult) -> tuple[int, int]:
    """``(structured catches, valid samples)`` — ``(0, 0)`` when not recorded.

    The denominator excludes reviewer errors only: the structured matcher is
    pure over the stored findings and cannot itself fail, so an all-judge-errored
    case still has its free counterfactual.
    """
    if not r.structured_caught_per_sample:
        return (0, 0)
    return count_valid(r.structured_caught_per_sample, r.errored_per_sample)


def struct_disagrees(r: CaseResult, judged_caught: int) -> bool:
    """Whether the judge and the structured matcher actually disagree.

    Equal totals do **not** imply agreement: a judge that catches sample 0 and
    misses sample 1 while the structured matcher does the reverse both tally
    ``1/2``, and comparing only the totals would hide a case where *every*
    sample disagreed — precisely the per-sample instability the audit trail
    exists to expose. So the totals are checked first, then each comparable
    sample. A sample is comparable when the reviewer produced a verdict and the
    judge actually ruled: where the judge errored there is no answer to disagree
    with (the differing *totals* already surface that case).
    """
    struct, _ = structured_tally(r)
    if struct != judged_caught:
        return True
    errored = r.errored_per_sample or (False,) * r.n
    statuses = r.judge_status_per_sample or ("",) * r.n
    for i in range(min(r.n, len(r.structured_caught_per_sample))):
        if errored[i] or judge_status_errored(statuses[i]):
            continue
        if r.caught_per_sample[i] != r.structured_caught_per_sample[i]:
            return True
    return False


def struct_note(r: CaseResult, judged_caught: int) -> str:
    """``struct N/M`` — but only when the judge-free matcher disagrees (#307).

    Appended rather than given a column: the two agree on almost every row, and
    a permanent column would repeat the catch cell and break column-diffing
    against the pinned baseline's tables. The precedent is ``+Nerr``, likewise
    silent at zero.
    """
    struct, struct_valid = structured_tally(r)
    if not struct_valid or not struct_disagrees(r, judged_caught):
        return ""
    return f"  struct {struct}/{struct_valid}"


def noise_cell(r: CaseResult) -> str:
    """The known-good noise cell: how many runs said anything, how many blocked.

    Sits beside ``fp`` deliberately (#310) — ``fp 0/3`` and ``noise 3/3 blk3``
    describe the same three runs, and only together do they say whether an arm
    got sharper or merely louder. ``—`` for a case with no known-good arm.
    """
    if not r.known_good_findings_per_sample:
        return "—"
    errored = r.false_positive_excluded_per_sample
    noisy, valid = count_valid(
        [n > 0 for n in r.known_good_findings_per_sample], errored
    )
    blocked, _ = count_valid(r.known_good_blocked_per_sample, errored)
    return f"{noisy}/{valid} blk{blocked}"


def catch_cell(r: CaseResult) -> tuple[str, int, int]:
    """The catch cell plus ``(caught, n_valid)`` for the roll-up tallies.

    Denominators are the VALID samples — neither the reviewer nor the judge
    errored — so a crash on either half never deflates a rate (#182 A3, #307).
    """
    n_err = sum(r.errored_per_sample)
    caught, n_valid = count_valid(r.caught_per_sample, r.excluded_per_sample)
    cell = (
        f"{caught}/{n_valid} {ci_band(*r.catch_rate_ci)}{err_suffix(n_err)}"
        f"{judge_err_suffix(sum(r.judge_errored_per_sample))}"
    )
    return cell, caught, n_valid


def fp_cell(r: CaseResult) -> str:
    if not r.false_positive_per_sample:
        return f"{r.false_positive_rate * 100:.0f}%"
    fp_err = sum(r.false_positive_errored_per_sample)
    flagged, fp_valid = count_valid(
        r.false_positive_per_sample, r.false_positive_excluded_per_sample
    )
    return (
        f"{flagged}/{fp_valid} "
        f"{ci_band(*r.false_positive_rate_ci)}{err_suffix(fp_err)}"
        f"{judge_err_suffix(sum(r.false_positive_judge_errored_per_sample))}"
    )


def print_results_table(
    results: Sequence[tuple[str, CaseResult]],
    *,
    extra_columns: Sequence[tuple[str, int, object]] = (),
) -> None:
    """Print the results table + the two tier roll-ups.

    *extra_columns* are ``(header, width, cell_fn)`` triples rendered between
    ``noise`` and ``result``; the core columns keep their widths regardless, so
    one command's table still column-diffs against another's.
    """
    extras = "".join(f" {h:>{w}}" for h, w, _ in extra_columns)
    header = (
        f"{'case':<28} {'tier':<8} {'n':>3} {'catch (95% CI)':>20} "
        f"{'sev':>5} {'fp (95% CI)':>20} {'noise':>12}{extras}  result"
    )
    typer.echo(header)
    typer.echo("-" * len(header))
    # (caught, n_valid) per case, keyed by tier, for the roll-up lines.
    tallies: dict[str, list[tuple[CaseResult, int, int]]] = {
        "floor": [],
        "frontier": [],
    }
    for tier, r in results:
        cell, caught, n_valid = catch_cell(r)
        tallies[tier].append((r, caught, n_valid))
        # Floor rows read ok/REGRESSED — the floor is a regression gate, not a
        # pass/fail measurement (RH-6).
        if tier == "floor":
            mark = "ok" if r.passed else "REGRESSED"
        else:
            mark = "PASS" if r.passed else "FAIL"
        extra_cells = "".join(
            f" {fn(r):>{w}}"  # type: ignore[operator]
            for _, w, fn in extra_columns
        )
        typer.echo(
            f"{r.case_id:<28} {tier:<8} {r.n:>3} {cell:>20} "
            f"{r.severity_correctness * 100:>4.0f}% {fp_cell(r):>20} "
            f"{noise_cell(r):>12}{extra_cells}  {mark}{struct_note(r, caught)}"
        )
    print_rollups(tallies)


def _plural(word: str, n: int) -> str:
    return word if n == 1 else f"{word}s"


def print_rollups(tallies: dict[str, list[tuple[CaseResult, int, int]]]) -> None:
    """The two tier roll-up lines (RH-6): frontier headline, floor gate."""
    frontier = tallies["frontier"]
    if frontier:
        caught = sum(c for _, c, _ in frontier)
        valid = sum(v for _, _, v in frontier)
        ci = wilson_interval(caught, valid) if valid else (0.0, 0.0)
        typer.echo(
            f"frontier: {caught}/{valid} pooled catch (95% CI {ci_band(*ci)}) "
            f"over {len(frontier)} {_plural('case', len(frontier))}"
        )
    floor = tallies["floor"]
    if floor:
        regressed = [(r, c, v) for r, c, v in floor if not r.passed]
        if regressed:
            detail = ", ".join(f"{r.case_id} {c}/{v}" for r, c, v in regressed)
            typer.echo(f"floor: REGRESSED — {detail}")
        else:
            typer.echo(f"floor: OK ({len(floor)} {_plural('case', len(floor))} at bar)")


def case_result_payload(r: CaseResult) -> dict:
    """The rate / per-sample half of a case's ``summary.json``.

    Shared with ``eval rescore`` so a re-score's JSON uses the **same field
    names** as the run it re-scores — which is what makes a field-for-field
    drift comparison meaningful rather than a mapping exercise.
    """
    errored = sum(r.errored_per_sample)
    return {
        "k": r.n,
        # Validity is the combined rule (#307): a sample the JUDGE could not rule
        # on is excluded exactly like a crashed reviewer, so this must agree with
        # catch_rate, the table and the block-rate gate. `errored` keeps its
        # narrower reviewer-only meaning, so older report dirs stay comparable.
        "n_valid": sum(1 for e in r.excluded_per_sample if not e),
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
        "false_positive_errored": sum(r.false_positive_errored_per_sample),
        "false_positive_errored_per_sample": list(r.false_positive_errored_per_sample),
        # #310: the FP rate is defect-specific — these say what ELSE the run
        # reported and whether it held approval, on both arms.
        "noise_rate": r.noise_rate,
        "noise_rate_ci": list(r.noise_rate_ci),
        "known_good_findings_per_sample": list(r.known_good_findings_per_sample),
        "known_good_blocked_per_sample": list(r.known_good_blocked_per_sample),
        "known_good_blocked": count_valid(
            r.known_good_blocked_per_sample, r.false_positive_excluded_per_sample
        )[0],
        "false_positive_n_valid": count_valid(
            r.false_positive_per_sample, r.false_positive_excluded_per_sample
        )[1],
        "findings_per_sample": list(r.findings_per_sample),
        "blocked_per_sample": list(r.blocked_per_sample),
        # #307: which samples the JUDGE could not rule on (distinct from a
        # crashed reviewer above), and what the free structured matcher said —
        # so a judge/structured divergence is in the record, not just the table.
        "judge_errored": sum(r.judge_errored_per_sample),
        "judge_errored_per_sample": list(r.judge_errored_per_sample),
        "judge_status_per_sample": list(r.judge_status_per_sample),
        "false_positive_judge_errored": sum(r.false_positive_judge_errored_per_sample),
        "false_positive_judge_status_per_sample": list(
            r.false_positive_judge_status_per_sample
        ),
        "structured_caught_per_sample": list(r.structured_caught_per_sample),
        "structured_caught": structured_tally(r)[0],
        "false_positive_structured_per_sample": list(
            r.false_positive_structured_per_sample
        ),
        "passed": r.passed,
    }
