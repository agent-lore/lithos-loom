"""``lithos-loom eval rescore`` — score retained reports again, offline (#307).

Never runs a reviewer. It reads a ``--report-dir`` written by ``eval review``
and re-scores it, so the only cost is judge calls — and with ``--judge-repeats``
it asks the judge the same question N times over identical stored findings to
measure how often it answers differently.

Measurement never sets the exit code: a floor case reading ``REGRESSED`` under
re-scoring, or drift against the recorded summary, are this command's *products*.
Exit 2 is reserved for usage failures, all raised before any judge call.
"""

from __future__ import annotations

import json
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
from .report import case_result_payload, print_results_table
from .rescore import (
    CaseRescore,
    RescoreError,
    drift_vs_summary,
    identity_of,
    judge_call_count,
    load_report_dir,
    rescore_case,
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
    bar: float = typer.Option(
        DEFAULT_BAR, "--bar", help="Catch-rate a case must reach to pass."
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
        None, "--out", help="Where to write the payload (default: <dir>/rescore.json)."
    ),
    allow_changed_cases: bool = typer.Option(
        False,
        "--allow-changed-cases",
        help="Rescore even where the case's [[expected]] has changed since the run.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the exact judge-call count and stop."
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

    default_models, frictions = load_tool_default_models()
    for friction in frictions:
        typer.echo(f"[Friction] {friction}", err=True)
    judge_info, judge_fn = resolve_judge(
        judge=judge, judge_tool=judge_tool, default_models=default_models
    )

    calls = judge_call_count(reports, cases, judge_repeats) if judge else 0
    typer.echo(
        f"rescoring {len(reports)} case(s) from {report_dir} — {calls} judge call(s)"
        f"{f' ({judge_repeats} repeats)' if judge_repeats > 1 else ''}",
        err=True,
    )
    if dry_run:
        return

    results: list[CaseRescore] = []
    for case_reports in reports:
        loaded = cases[case_reports.case_id]
        scored = rescore_case(
            loaded,
            case_reports,
            bar=bar,
            judge=judge_fn,
            repeats=judge_repeats,
        )
        results.append(
            CaseRescore(
                case_id=scored.case_id,
                judged=scored.judged,
                structured=scored.structured,
                sites=scored.sites,
                catch_per_repeat=scored.catch_per_repeat,
                fingerprint=scored.fingerprint,
                identity=identities[case_reports.case_id],
                drift=drift_vs_summary(scored.judged, case_reports.summary),
            )
        )

    _print(results, reports, cases, judge_repeats, judged=judge)
    _write(results, report_dir, out, judge_info, judge_repeats, calls)


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


def _stability_cell(scored: CaseRescore) -> str:
    if not scored.judged_sites:
        return "—"
    return f"{scored.flipped_sites}/{scored.judged_sites}"


def _spread_cell(scored: CaseRescore) -> str:
    if not scored.catch_per_repeat:
        return "—"
    lo, hi = min(scored.catch_per_repeat), max(scored.catch_per_repeat)
    return f"{lo}-{hi}/{scored.judged.n}"


def _print(
    results: list[CaseRescore],
    reports,
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
            ("flip", 7, lambda r: _stability_cell(by_id[r.case_id])),
            ("spread", 9, lambda r: _spread_cell(by_id[r.case_id])),
        ]
    print_results_table(tiers, extra_columns=extras)

    if repeats > 1:
        n_judged = sum(r.judged_sites for r in results)
        flipped = sum(r.flipped_sites for r in results)
        pct = 100 * (1 - flipped / n_judged) if n_judged else 100.0
        typer.echo(
            f"judge stability: {flipped}/{n_judged} judged sites flipped over "
            f"{repeats} repeats ({pct:.1f}% stable)"
        )
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


def _write(
    results: list[CaseRescore],
    report_dir: Path,
    out: Path | None,
    judge_info: dict | None,
    repeats: int,
    calls: int,
) -> None:
    """Write the payload beside — never over — the run's own ``summary.json``."""
    payload = {
        "schema_version": 1,
        "tool": "lithos-loom eval rescore",
        "report_dir": str(report_dir),
        "judge": {**(judge_info or {}), "repeats": repeats} if judge_info else None,
        "judge_calls": calls,
        "cases": [
            {
                "case": r.case_id,
                "expected_fingerprint": r.fingerprint,
                "case_identity": r.identity,
                # Same field names as summary.json (one payload builder), so a
                # drift comparison is field-for-field rather than a mapping job.
                "judged": case_result_payload(r.judged),
                "structured": case_result_payload(r.structured),
                "stability": {
                    "repeats": repeats,
                    "judged_sites": r.judged_sites,
                    "flipped_sites": r.flipped_sites,
                    "catch_per_repeat": list(r.catch_per_repeat),
                    # Recorded even at one repeat: a site whose verdict is empty
                    # while findings were produced IS the veto audit trail.
                    "sites": [
                        {
                            "variant": s.variant,
                            "sample": s.sample,
                            "expected": s.expected,
                            "produced": list(s.produced_ids),
                            "verdicts": [sorted(v) for v in s.verdicts],
                            "stable": s.stable,
                        }
                        for s in r.sites
                    ],
                },
                "drift": r.drift,
            }
            for r in results
        ],
    }
    target = out or (report_dir / "rescore.json")
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    typer.echo(f"wrote {target}", err=True)
