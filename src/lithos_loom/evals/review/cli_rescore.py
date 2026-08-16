"""``lithos-loom eval rescore`` — score retained reports again, offline (#307).

Never runs a reviewer. It reads a ``--report-dir`` written by ``eval review``
and re-scores it, so the only cost is judge calls — and with ``--judge-repeats``
it asks the judge the same question N times over identical stored findings to
measure how often it answers differently.

Measurement never sets the exit code: a floor case reading ``REGRESSED`` under
re-scoring, or drift against the recorded summary, are this command's *products*.
Exit 2 is reserved for usage failures, all raised before any judge call — which
is why the output target and the scoring bar are resolved up front, beside the
flag checks, rather than at the point they are used.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import typer

from ...plugins.story_develop.daemon_io import load_tool_default_models
from .app import (
    DEFAULT_CASES_DIR,
    discover_cases,
    eval_app,
    require_rate,
    resolve_judge,
)
from .case import Case, load_case
from .harness import DEFAULT_BAR
from .judge import MAX_ATTEMPTS_PER_REQUEST
from .report import case_result_payload, judge_err_suffix, print_results_table
from .rescore import (
    RESCORE_FILENAME,
    CaseReports,
    CaseRescore,
    JudgeSite,
    RescoreError,
    drift_vs_summary,
    identity_of,
    judge_call_count,
    load_report_dir,
    rescore_case,
    resolve_bar,
)


@eval_app.command("rescore")
def rescore(
    report_dir: Path = typer.Argument(
        ..., help="A --report-dir written by `eval review`."
    ),
    case: str | None = typer.Option(
        None, "--case", help="Rescore only this case id (default: all)."
    ),
    cases_dir: Path = typer.Option(
        DEFAULT_CASES_DIR, "--cases-dir", help="Directory of case folders."
    ),
    bar: float | None = typer.Option(
        None,
        "--bar",
        help="Catch-rate a case must reach (default: the bar the run recorded).",
    ),
    judge: bool = typer.Option(
        True, "--judge/--no-judge", help="Use the mechanism LLM-judge (default on)."
    ),
    judge_tool: str = typer.Option(
        "claude", "--judge-tool", help="Agent for the judge (claude | codex)."
    ),
    judge_repeats: int = typer.Option(
        1,
        "--judge-repeats",
        help="Ask the judge each question N times and report verdict stability.",
    ),
    out: Path | None = typer.Option(
        None,
        "--out",
        help="Where to write the payload (default: <report-dir>/rescore.json).",
    ),
    allow_changed_cases: bool = typer.Option(
        False,
        "--allow-changed-cases",
        help="Rescore even where the case's [[expected]] has changed since the run.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the judge verdict-request count and stop."
    ),
) -> None:
    """Re-score a retained report dir — no reviewer runs, judge calls only.

    ``--judge-repeats N`` measures the judge's own variance: identical stored
    findings, the same question N times. Repeat 0 stays authoritative, because
    this command exists to *measure* variance, not to quietly change the
    estimator while measuring it.

    Exit 2 on a usage error, always before any judge call. **A measurement never
    sets the exit code** — a re-scored floor case reading REGRESSED is a finding
    about the judge, not a failure of this command, and wiring it into a gate
    would gate on the judge's mood.
    """
    require_rate("--bar", bar)
    if judge_repeats < 1:
        raise typer.BadParameter(
            f"--judge-repeats must be at least 1 (got {judge_repeats})"
        )
    if judge_repeats > 1 and not judge:
        raise typer.BadParameter(
            f"--judge-repeats {judge_repeats} with --no-judge measures nothing — "
            "the structured matcher is deterministic"
        )

    try:
        reports = load_report_dir(report_dir, case_id=case)
    except RescoreError as exc:
        raise typer.BadParameter(str(exc)) from exc

    cases = _load_cases(cases_dir, [r.case_id for r in reports])
    identities = {r.case_id: identity_of(cases[r.case_id], r.summary) for r in reports}
    _check_identities(identities, allow_changed_cases)
    bars = {r.case_id: resolve_bar(r.summary, bar) for r in reports}
    _report_bars(reports, bars)
    target = _resolve_out(report_dir, out, reports)

    default_models, frictions = load_tool_default_models()
    for friction in frictions:
        typer.echo(f"[Friction] {friction}", err=True)
    judge_info, judge_fn = resolve_judge(
        judge=judge, judge_tool=judge_tool, default_models=default_models
    )

    requests = judge_call_count(reports, cases, judge_repeats) if judge else 0
    _announce(report_dir, len(reports), requests, judge_repeats, judged=judge)
    if dry_run:
        return

    results: list[CaseRescore] = []
    for case_reports in reports:
        case_bar, bar_source = bars[case_reports.case_id]
        scored = rescore_case(
            cases[case_reports.case_id],
            case_reports,
            bar=case_bar,
            judge=judge_fn,
            repeats=judge_repeats,
        )
        results.append(
            replace(
                scored,
                bar_source=bar_source,
                identity=identities[case_reports.case_id],
                drift=drift_vs_summary(
                    scored.judged, case_reports.summary, compare_judge_status=judge
                ),
            )
        )

    _print(results, reports, cases, judge_repeats, judged=judge)
    _write(results, target, judge_info, judge_repeats, requests)


def _load_cases(cases_dir: Path, case_ids: list[str]) -> dict[str, Case]:
    """Load every needed case, reporting **all** missing ids at once."""
    available = {d.name for d in discover_cases(cases_dir, None)}
    missing = [cid for cid in case_ids if cid not in available]
    if missing:
        raise typer.BadParameter(
            f"case(s) missing from {cases_dir}: {', '.join(sorted(missing))} — "
            "rescore compares against the current tree; add them or narrow with "
            "--case"
        )
    out: dict[str, Case] = {}
    for cid in case_ids:
        try:
            out[cid] = load_case(cases_dir / cid)
        except (ValueError, OSError) as exc:
            raise typer.BadParameter(f"case {cid}: {exc}") from exc
    return out


def _check_identities(identities: dict[str, str], allow_changed: bool) -> None:
    changed = sorted(c for c, s in identities.items() if s == "changed")
    if changed and not allow_changed:
        raise typer.BadParameter(
            f"[[expected]] has changed since these reports were written: "
            f"{', '.join(changed)} — rescoring would answer a different question "
            "than the run did; pass --allow-changed-cases to proceed"
        )
    unverifiable = sorted(c for c, s in identities.items() if s == "unverifiable")
    if unverifiable:
        # Loud, but not fatal: every report dir written before the fingerprint
        # existed lands here, and refusing them would make the whole retained
        # corpus unrescorable — defeating the point of the command.
        typer.echo(
            "case identity unverifiable (report dir predates the fingerprint): "
            + ", ".join(unverifiable),
            err=True,
        )


def _report_bars(
    reports: Sequence[CaseReports], bars: dict[str, tuple[float, str]]
) -> None:
    """Say where each case's bar came from, whenever it isn't the run's own.

    Both notes exist because the bar decides PASS / REGRESSED without appearing
    in the table: a silent default (or a silent override) would present a verdict
    about the judge that was really a verdict about a flag.
    """
    defaulted = sorted(cid for cid, (_, src) in bars.items() if src == "default")
    if defaulted:
        typer.echo(
            f"no bar recorded by these runs: {', '.join(defaulted)} — scoring at "
            f"the default {DEFAULT_BAR}",
            err=True,
        )
    recorded = {r.case_id: (r.summary or {}).get("bar") for r in reports}
    overridden = [
        f"{cid} (recorded {recorded[cid]})"
        for cid, (value, src) in sorted(bars.items())
        if src == "flag" and recorded[cid] is not None and recorded[cid] != value
    ]
    if overridden:
        typer.echo(
            f"--bar overrides the bar these runs recorded: {', '.join(overridden)}",
            err=True,
        )


def _resolve_out(
    report_dir: Path, out: Path | None, reports: Sequence[CaseReports]
) -> Path:
    """Where the payload goes — resolved and checked **before** any judge call.

    A ``--out`` aimed at a retained report or its summary would destroy a paid
    input in exchange for a cheap one, and a missing parent directory would fail
    only once the whole measurement had been bought. Both are usage errors, so
    both belong in the preflight.

    A case dir is also refused any name but ``rescore.json``: the loader accepts
    exactly ``summary.json``, ``rescore.json`` and ``<variant>-<i>.json``, so a
    stray file there would make a *successful* run leave the dir unloadable by
    the next one — the command poisoning its own input.
    """
    target = out or (report_dir / RESCORE_FILENAME)
    resolved = target.resolve()
    if resolved.is_dir():
        raise typer.BadParameter(f"--out {target} is a directory")
    inputs = {p.resolve() for r in reports for p in r.input_paths()}
    if resolved in inputs:
        raise typer.BadParameter(
            f"--out {target} is one of the retained reports this command reads — "
            "refusing to overwrite a paid input with a re-score of it"
        )
    case_dirs = {r.case_dir.resolve() for r in reports if r.case_dir is not None}
    if resolved.parent in case_dirs and resolved.name != RESCORE_FILENAME:
        raise typer.BadParameter(
            f"--out {target} would leave {resolved.parent.name} unloadable by the "
            f"next rescore — inside a case dir the only allowed name is "
            f"{RESCORE_FILENAME}"
        )
    if not resolved.parent.is_dir():
        raise typer.BadParameter(f"--out {target}: no directory {resolved.parent}")
    return target


def _announce(
    report_dir: Path, n_cases: int, requests: int, repeats: int, *, judged: bool
) -> None:
    """The pre-paid cost line: what will be asked, and what it can cost.

    Two numbers, not one: ``requests`` is exact as a count of questions, but a
    failed call retries once, so a flaky sweep can invoke the agent twice per
    question. Printing only the first would understate the worst case by 2x.
    """
    head = f"rescoring {n_cases} case(s) from {report_dir}"
    if not judged:
        typer.echo(f"{head} — no judge (structured matcher only)", err=True)
        return
    detail = f" ({repeats} repeats)" if repeats > 1 else ""
    typer.echo(
        f"{head} — {requests} judge verdict request(s){detail}, up to "
        f"{requests * MAX_ATTEMPTS_PER_REQUEST} agent invocations "
        "(a failed call retries once)",
        err=True,
    )


def _stability_cell(scored: CaseRescore) -> str:
    """``flipped/measured`` — and the sites that could not be measured at all.

    A judged site whose repeats did not all answer is neither stable nor
    flipped; it is missing data, and folding it into the denominator would let
    an all-timed-out case report perfect stability.

    The suffix counts every site that hit a judge error, not just the
    unmeasurable ones — a site that flipped AND errored stays in the denominator
    (the flip is real), so keying the suffix on exclusion alone would hide its
    failed repeat from every aggregate.
    """
    if not scored.judged_sites:
        return "—"
    return (
        f"{scored.flipped_sites}/{scored.measured_sites}"
        f"{judge_err_suffix(scored.errored_sites)}"
    )


def _spread_cell(scored: CaseRescore) -> str:
    """Catch count across the repeat universes, over each one's own denominator."""
    if not scored.catch_per_repeat:
        return "—"
    lo, hi = min(scored.catch_per_repeat), max(scored.catch_per_repeat)
    valid = scored.valid_per_repeat or (scored.judged.n,)
    vlo, vhi = min(valid), max(valid)
    # A varying denominator IS the signal that the judge failed on some repeats;
    # printing a fixed K would hide it behind a plausible-looking drop.
    denom = f"{vlo}" if vlo == vhi else f"{vlo}-{vhi}"
    return f"{lo}-{hi}/{denom}"


def _print(
    results: list[CaseRescore],
    reports: Sequence[CaseReports],
    cases: dict[str, Case],
    repeats: int,
    *,
    judged: bool,
) -> None:
    by_id = {r.case_id: r for r in results}
    # Tier from the case in the CURRENT tree, like `eval review` — a recorded
    # summary may be absent (most retained dirs have none) or stale. Undeclared
    # counts as frontier: a case never silently opts INTO the floor.
    tiers = [
        (cases[rep.case_id].tier or "frontier", by_id[rep.case_id].judged)
        for rep in reports
    ]
    extras = []
    if repeats > 1:
        # Only meaningful above one draw: a single verdict cannot flip.
        extras = [
            ("flip", 11, lambda r: _stability_cell(by_id[r.case_id])),
            ("spread", 11, lambda r: _spread_cell(by_id[r.case_id])),
        ]
    print_results_table(tiers, extra_columns=extras)

    if repeats > 1:
        _print_stability(results, repeats)
    _print_drift(results, reports, judged=judged)


def _print_stability(results: list[CaseRescore], repeats: int) -> None:
    if not sum(r.judged_sites for r in results):
        # Nothing failed here — the judge was never consulted, because no run
        # produced a finding to rule on. Saying "UNMEASURED" would read as
        # missing answers rather than an absence of questions.
        typer.echo("judge stability: — no site required judging (no findings)")
        return
    measured = sum(r.measured_sites for r in results)
    flipped = sum(r.flipped_sites for r in results)
    unmeasured = sum(r.unmeasured_sites for r in results)
    errored = sum(r.errored_sites for r in results)
    if not measured:
        typer.echo(
            f"judge stability: UNMEASURED — no site answered all {repeats} repeats "
            f"({errored} site(s) hit a judge error)"
        )
        return
    pct = 100 * (1 - flipped / measured)
    line = (
        f"judge stability: {flipped}/{measured} judged sites flipped over "
        f"{repeats} repeats ({pct:.1f}% stable)"
    )
    if errored:
        # Both numbers, always: a site can flip AND error, so "unmeasured" alone
        # would silently drop the failed repeats that still got an answer.
        line += f"; {errored} site(s) hit a judge error ({unmeasured} unmeasurable)"
    typer.echo(line)


def _print_drift(
    results: list[CaseRescore], reports: Sequence[CaseReports], *, judged: bool
) -> None:
    drifted = [r for r in results if _has_drift(r)]
    if not drifted:
        return
    # Under --no-judge against a judge-scored run the scorer itself differs, so
    # calling the difference "drift" would invite reading a known, expected
    # judge/structured delta as instability in the reports.
    was_judged = any((rep.summary or {}).get("judge") for rep in reports)
    if judged or not was_judged:
        typer.echo(f"drift vs recorded summary.json: {len(drifted)} case(s) differ")
    else:
        typer.echo(
            f"structured vs recorded (judge-scored) run: {len(drifted)} case(s) "
            "differ — this is the judge/structured delta, not drift"
        )
    for r in drifted:
        for key, cmp in r.drift.items():
            if cmp and cmp["flipped"]:
                typer.echo(f"  {r.case_id}  {key}  samples {cmp['flipped']}")


def _has_drift(r: CaseRescore) -> bool:
    return any(cmp and cmp["flipped"] for cmp in r.drift.values())


def _site_payload(site: JudgeSite) -> dict:
    """One decision point's full audit record.

    The whole verdict is kept per repeat — status, matched ids, detail and the
    raw reply — because the question this command answers ("why did two
    identical asks differ?") is unanswerable from the ids alone, and because a
    veto and a timeout are indistinguishable once the status is dropped.
    """
    return {
        "variant": site.variant,
        "sample": site.sample,
        "expected": site.expected,
        "produced": list(site.produced_ids),
        "verdicts": [
            {
                "status": v.status,
                "matched": list(v.matched_ids),
                "detail": v.detail,
                "reply": v.reply,
            }
            for v in site.verdicts
        ],
        "judged": site.judged,
        "stable": site.stable,
        "flipped": site.flipped,
        "errored": site.errored,
    }


def _write(
    results: list[CaseRescore],
    target: Path,
    judge_info: dict | None,
    repeats: int,
    requests: int,
) -> None:
    """Write the payload to the pre-validated *target*, atomically.

    Temp file + ``os.replace`` so a re-score that dies mid-write cannot leave a
    truncated payload where a complete one was — the same rule the vault writers
    follow, for the same reason. The temp name carries a random suffix: two
    invocations writing the same target would otherwise share one scratch file,
    and the loser would fail *after* paying for its whole measurement.
    """
    payload = {
        "schema_version": 1,
        "tool": "lithos-loom eval rescore",
        "judge": {**(judge_info or {}), "repeats": repeats} if judge_info else None,
        # Questions asked vs what they can cost: a failed request retries once.
        "judge_verdict_requests": requests,
        "max_agent_invocations": requests * MAX_ATTEMPTS_PER_REQUEST,
        "cases": [
            {
                "case": r.case_id,
                "expected_fingerprint": r.fingerprint,
                "case_identity": r.identity,
                "bar": r.bar,
                "bar_source": r.bar_source,
                # Same field names as summary.json (one payload builder), so a
                # drift comparison is field-for-field rather than a mapping job.
                "judged": case_result_payload(r.judged),
                "structured": case_result_payload(r.structured),
                "stability": {
                    "repeats": repeats,
                    "judged_sites": r.judged_sites,
                    "measured_sites": r.measured_sites,
                    "flipped_sites": r.flipped_sites,
                    "unmeasured_sites": r.unmeasured_sites,
                    "errored_sites": r.errored_sites,
                    "catch_per_repeat": list(r.catch_per_repeat),
                    "valid_per_repeat": list(r.valid_per_repeat),
                    # Recorded even at one repeat: a site whose verdict matched
                    # nothing while findings were produced IS the veto audit trail.
                    "sites": [_site_payload(s) for s in r.sites],
                },
                "drift": r.drift,
            }
            for r in results
        ],
    }
    tmp = target.parent / f".{target.name}.tmp.{uuid.uuid4().hex[:8]}"
    try:
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    typer.echo(f"wrote {target}", err=True)
