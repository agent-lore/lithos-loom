"""Presenting a :class:`~.harness.CaseResult` — as a table row, or as JSON.

Extracted from ``cli.py`` so **every** command renders results the same way.
That matters more than the line count it saves: a re-scored report dir is only
comparable to the run that produced it if both go through one renderer and one
payload builder, so a divergence in the numbers can never be an artefact of two
code paths having drifted.

Rendering deliberately keeps the core columns fixed-width and identical across
commands: report dirs get column-diffed against each other, and a table whose
shape depends on which command printed it breaks that. The one deliberate
exception (#404) is the ``└``-prefixed row per ``[[expected]]`` under a
multi-expected case — the same rows from every command, and a prefix a diff
can filter on.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import typer

from .harness import CaseResult, count_valid
from .match import judge_status_errored
from .stats import wilson_interval

# Extra per-case cells a command can append after `result` (header, width, cell
# function). Used by `eval rescore` for its stability columns; `eval review`
# passes none. Typed as a real callable contract so a miswired column is a
# typecheck error rather than a crash mid-render on a paid run.
ExtraColumn = tuple[str, int, Callable[[CaseResult], str]]


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


def per_expected_rows(r: CaseResult) -> list[str]:
    """One indented row per ``[[expected]]`` under a MULTI-expected case (#404).

    The case row is the conjunction ("the PR would have been blocked"); these
    say which defect was missed. A single-expected case gets none — its row
    already is the diagnosis. Same valid-sample denominator as the case row.
    """
    if len(r.caught_per_expected) < 2:
        return []
    rows = []
    for j, flags in enumerate(r.caught_per_expected):
        caught, n_valid = count_valid(flags, r.excluded_per_sample)
        ci = wilson_interval(caught, n_valid)
        cls = r.expected_classes[j] if j < len(r.expected_classes) else None
        label = f"[{j}] {cls or 'unclassed'}"
        rows.append(f"  └ {label:<26} {caught}/{n_valid} {ci_band(*ci)}")
    return rows


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
    extra_columns: Sequence[ExtraColumn] = (),
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
        extra_cells = "".join(f" {fn(r):>{w}}" for _, w, fn in extra_columns)
        typer.echo(
            f"{r.case_id:<28} {tier:<8} {r.n:>3} {cell:>20} "
            f"{r.severity_correctness * 100:>4.0f}% {fp_cell(r):>20} "
            f"{noise_cell(r):>12}{extra_cells}  {mark}{struct_note(r, caught)}"
        )
        for row in per_expected_rows(r):
            typer.echo(row)
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
        typer.echo(class_balanced_line([r for r, _, _ in frontier]))
    floor = tallies["floor"]
    if floor:
        regressed = [(r, c, v) for r, c, v in floor if not r.passed]
        if regressed:
            detail = ", ".join(f"{r.case_id} {c}/{v}" for r, c, v in regressed)
            typer.echo(f"floor: REGRESSED — {detail}")
        else:
            typer.echo(f"floor: OK ({len(floor)} {_plural('case', len(floor))} at bar)")


def class_tallies(results: Sequence[CaseResult]) -> dict[str, tuple[int, int]]:
    """``{class: (caught, valid)}`` over SAMPLES, pooled across the cases that
    declare the class (#404).

    Within one case a class is caught in a sample when every expected of that
    class is caught in it — the conjunction, as the case row — so a class
    declared twice on one case measures its K runs once, never as 2K
    observations of the same runs (opus round 1 M2); across cases the per-
    sample counts pool as the frontier line's do. Unclassed expecteds are not
    here: the balanced roll-up excludes them and says how many (opus round 1
    H2 — a singleton per expected weights a case by how many defects its
    author seeded).
    """
    tallies: dict[str, list[int]] = {}
    for r in results:
        per_expected = r.caught_per_expected or (r.caught_per_sample,) * len(
            r.expected_classes
        )
        by_class: dict[str, list[tuple[bool, ...]]] = {}
        for j, cls in enumerate(r.expected_classes):
            if cls is None or j >= len(per_expected):
                continue
            by_class.setdefault(cls, []).append(per_expected[j])
        for cls, rows in by_class.items():
            conjunction = tuple(all(col) for col in zip(*rows, strict=True))
            c, v = count_valid(conjunction, r.excluded_per_sample)
            t = tallies.setdefault(cls, [0, 0])
            t[0] += c
            t[1] += v
    return {k: (c, v) for k, (c, v) in tallies.items()}


def unclassed_expected_count(results: Sequence[CaseResult]) -> int:
    """How many frontier expecteds carry no class — the part of the corpus
    the balanced line does NOT cover."""
    return sum(
        sum(1 for cls in r.expected_classes if cls is None)
        or (0 if r.expected_classes else len(r.caught_per_expected) or 1)
        for r in results
    )


def class_balanced_line(results: Sequence[CaseResult]) -> str:
    """The class-balanced frontier line (#404, PR #401 review): each declared
    class's pooled per-sample catch rate with its Wilson CI, and their mean,
    so a class that recurred across PRs counts once. The mean is a point
    summary with NO interval — a mean of ratios over unequal, non-independent
    denominators has no pooled-binomial sampling model, so it is never the
    figure an A/B tests; the per-class ``c/v`` figures are. A class with no
    valid sample is named and left out of the mean; unclassed expecteds are
    excluded and counted, so the line always says what corpus it covers."""
    tallies = class_tallies(results)
    unclassed = unclassed_expected_count(results)
    rated = {k: c / v for k, (c, v) in tallies.items() if v}
    suffix = (
        f"; {unclassed} unclassed {'expected' if unclassed == 1 else 'expecteds'} "
        "excluded"
        if unclassed
        else ""
    )
    parts = []
    for k, (c, v) in sorted(tallies.items()):
        if v:
            parts.append(f"{k} {c}/{v} {ci_band(*wilson_interval(c, v))}")
        else:
            parts.append(f"{k} 0/0 (no valid sample, excluded)")
    if not rated:
        # "declared but every sample errored" is not "none declared": the
        # class list is what makes two arms comparable, so it is printed
        # even when nothing can be rated (review of PR #413).
        listed = (" — " + ", ".join(parts)) if parts else ""
        return (
            "frontier (class-balanced): no classed expected with a valid sample"
            + listed
            + suffix
        )
    mean = sum(rated.values()) / len(rated)
    return (
        f"frontier (class-balanced): {mean * 100:.0f}% mean over {len(rated)} "
        f"{'class' if len(rated) == 1 else 'classes'} — " + ", ".join(parts) + suffix
    )


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
        # #404: the per-expected diagnosis beside the per-sample conjunction,
        # and the classes the balanced roll-up keys on.
        "caught_per_expected": [list(t) for t in r.caught_per_expected],
        "catch_rate_per_expected": list(r.catch_rate_per_expected),
        "catch_rate_ci_per_expected": [list(ci) for ci in r.catch_rate_ci_per_expected],
        "expected_classes": list(r.expected_classes),
        "false_positive_structured_per_sample": list(
            r.false_positive_structured_per_sample
        ),
        "passed": r.passed,
    }
