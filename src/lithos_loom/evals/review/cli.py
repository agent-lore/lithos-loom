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
from functools import partial
from pathlib import Path

import typer

from ...plugins.story_develop.config import ReviewerSpec
from ...plugins.story_develop.daemon_io import load_tool_default_models
from ...plugins.story_develop.model_policy import (
    apply_panel_default_models,
    require_agent_models,
)
from .app import (
    DEFAULT_CASES_DIR,
    discover_cases,
    eval_app,
    require_rate,
    resolve_judge,
)
from .case import Case, iter_artifact_files, load_case, resolve_artifacts_root
from .harness import (
    DEFAULT_BAR,
    DEFAULT_K,
    CaseResult,
    JudgeSink,
    ReportSink,
    ReviewFn,
    count_valid,
    live_review,
    run_case,
)
from .match import RunScore
from .overrides import parse_reviewer_overrides, resolve_panel
from .report import case_result_payload, print_results_table


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
        DEFAULT_CASES_DIR, "--cases-dir", help="Directory of case folders."
    ),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Override every case's profile: panel := its personas, "
        "check-set := its checks.",
    ),
    reviewer: list[str] | None = typer.Option(
        None,
        "--reviewer",
        help="Explicitly enumerate the panel (repeatable canonical persona "
        "names); wins over --profile's panel.",
    ),
    reviewer_override: list[str] | None = typer.Option(
        None,
        "--reviewer-override",
        help="PERSONA.FIELD=VALUE with FIELD in model|effort|tool "
        "(repeatable); applies where the persona is in the effective panel.",
    ),
    max_known_good_block_rate: float | None = typer.Option(
        None,
        "--max-known-good-block-rate",
        help="Fail the run when a case holds approval on its known-good head "
        "more often than this rate (default: off — record only).",
    ),
) -> None:
    """Measure the panel's catch-rate on the seeded-defect benchmark.

    Cases score in two tiers (RH-6): the headline pools catches over
    **frontier** cases only; **floor** cases (saturated) are a regression gate.
    Exit 1 iff a floor case falls below the bar, a case has no valid samples
    (all-errored infra failure), or — when ``--max-known-good-block-rate`` is
    given — a case exceeds it on the known-good head or has no valid known-good
    sample to judge it by. A frontier FAIL is the measurement, not a failure of
    the run.

    The panel-override axis (RH-7): ``--profile`` / ``--reviewer`` /
    ``--reviewer-override`` vary the panel per run without editing case files;
    the effective panel is recorded in each case's ``summary.json``.

    Beside the defect-specific false-positive rate, each paired case reports its
    **noise** (#310) — the share of known-good runs that reported anything at
    all, and how many held approval. ``--max-known-good-block-rate`` turns the
    latter into a gate; it is off by default because no baseline for it exists
    yet, and gating an unmeasured quantity is how a lever gets chosen blind.
    """
    # Cheapest fail-closed check first: a rate outside [0, 1] silently changes
    # what the run means rather than erroring — `--max-known-good-block-rate 10`
    # (a typo for 0.10) disables the very gate the operator asked for, and
    # `--bar 0` retires the floor regression gate.
    require_rate("--bar", bar)
    require_rate("--max-known-good-block-rate", max_known_good_block_rate)

    case_dirs = discover_cases(cases_dir, case)

    # Fail closed BEFORE any paid run: overrides parse up front, then EVERY
    # selected case's effective panel is resolved (unknown profile/reviewer,
    # gate-only profile, capability crossings like effort-on-codex) — a typo
    # or a no-op lever aborts the whole invocation, not one case into a sweep.
    try:
        overrides = parse_reviewer_overrides(reviewer_override or [])
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    # #304 explicit-model policy: the per-tool defaults from the loom TOML are
    # the lowest-priority explicit layer; a reviewer still on model=None after
    # them would run the sandbox image CLI's invisible builtin — rejected
    # pre-paid like any other no-op arm.
    default_models, dm_frictions = load_tool_default_models()
    for friction in dm_frictions:
        typer.echo(f"[Friction] {friction}", err=True)

    prepared: list[tuple[Case, str, str, tuple[ReviewerSpec, ...]]] = []
    for case_dir in case_dirs:
        loaded = load_case(case_dir)
        try:
            eff_profile, panel = resolve_panel(
                loaded, profile=profile, reviewers=reviewer, overrides=overrides
            )
            panel = apply_panel_default_models(panel, default_models)
            require_agent_models(
                panel=panel,
                default_models=default_models,
                where=f"case {loaded.id}",
            )
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        # Undeclared tier counts as frontier: a case never silently opts INTO
        # the floor (the shipped-case gate test forces a declaration anyway).
        prepared.append((loaded, loaded.tier or "frontier", eff_profile, panel))

    # #305 review (finding 4): the judge is an agent invocation too — its
    # verdicts decide whether findings count, so its model must be explicit
    # and recorded, not the host CLI's drifting default. The TOOL validates
    # first (round 2): unknown default_models keys are accepted for forward
    # compat, so a configured model alone would let an unsupported tool
    # through to crash only when the first finding reaches the judge —
    # after paid reviewer runs.
    judge_info, judge_fn = resolve_judge(
        judge=judge, judge_tool=judge_tool, default_models=default_models
    )
    sink = _make_report_sink(report_dir) if report_dir is not None else None
    verdict_sink = (
        _make_judge_sink(report_dir, judge_info)
        if report_dir is not None and judge
        else None
    )

    results: list[tuple[str, CaseResult]] = []
    for loaded, tier, eff_profile, panel in prepared:
        # The resolved panel (default models applied) ALWAYS drives the run —
        # letting the harness re-resolve personas would resurrect model=None
        # specs and defeat the #304 policy.
        review_fn: ReviewFn | None = partial(
            live_review,
            reviewers=panel,
            profile=eff_profile,
            default_models=dict(default_models),
        )
        note = f" [profile={eff_profile}; panel={_panel_phrase(panel)}]"
        artifacts = _artifact_info(loaded)
        if artifacts is not None:
            # RH-3: the measured surface is the artifact-review pass, not the diff
            note += f" [artifact pass; {artifacts['n_files']} file(s)]"
        typer.echo(f"running {loaded.id} × {k} …{note}", err=True)
        result = run_case(
            loaded,
            k=k,
            bar=bar,
            judge=judge_fn,
            report_sink=sink,
            judge_sink=verdict_sink,
            review_fn=review_fn,
        )
        results.append((tier, result))
        if report_dir is not None:
            _write_summary(
                report_dir,
                result,
                tier,
                eff_profile,
                panel,
                artifacts=artifacts,
                judge_info=judge_info,
            )

    print_results_table(results)
    floor_regressed = any(t == "floor" and not r.passed for t, r in results)
    no_valid = [
        r.case_id
        for _, r in results
        if count_valid(r.caught_per_sample, r.excluded_per_sample)[1] == 0
    ]
    if no_valid:
        typer.echo(
            "no valid samples (reviewer or judge infra failure): "
            + ", ".join(no_valid),
            err=True,
        )
    _report_judge_errors(results, report_dir)
    over, unjudgeable = _block_rate_gate(results, max_known_good_block_rate)
    if over:
        typer.echo(
            "held approval on the known-good head above "
            f"--max-known-good-block-rate {max_known_good_block_rate}: "
            + ", ".join(f"{cid} {b}/{v}" for cid, b, v in over),
            err=True,
        )
    if unjudgeable:
        typer.echo(
            "--max-known-good-block-rate given but the known-good arm produced "
            f"no valid sample to judge: {', '.join(unjudgeable)}",
            err=True,
        )
    if floor_regressed or no_valid or over or unjudgeable:
        raise typer.Exit(1)


def _block_rate_gate(
    results: list[tuple[str, CaseResult]], bar: float | None
) -> tuple[list[tuple[str, int, int]], list[str]]:
    """Apply the known-good block gate: ``(over the bar, unjudgeable)``.

    Both empty when the gate is off (*bar* is ``None``) or a case has no
    known-good arm at all: a catch-only case measures no false positives, so a
    zero bar must not convict it. Errored samples are excluded from the rate —
    an incomplete panel blocks *by definition* (:func:`intake_blocks`), so
    counting a crash as a block would convict the arm for infra flakiness.

    A case whose known-good arm ran but produced **no valid sample** is
    *unjudgeable* and fails the gate. Without a gate that stays a reporting gap
    (the documented default — the infra-failure exit keys on the buggy side),
    but the known-good arm runs *after* the buggy one, so an exhausted quota
    wipes out exactly the evidence an explicitly requested clean-head gate
    exists to weigh. "No evidence" must not read as "no violation".
    """
    if bar is None:
        return [], []
    over: list[tuple[str, int, int]] = []
    unjudgeable: list[str] = []
    for _, r in results:
        if not r.known_good_blocked_per_sample:
            continue
        blocked, valid = count_valid(
            r.known_good_blocked_per_sample, r.false_positive_excluded_per_sample
        )
        if not valid:
            unjudgeable.append(r.case_id)
        elif blocked / valid > bar:
            over.append((r.case_id, blocked, valid))
    return over, unjudgeable


def _make_report_sink(report_dir: Path) -> ReportSink:
    """A sink that writes each run's report to ``<dir>/<case>/<variant>-<i>.json``."""

    def sink(case_id: str, variant: str, i: int, report: dict) -> None:
        out = report_dir / case_id
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{variant}-{i}.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )

    return sink


def _artifact_info(case: Case) -> dict | None:
    """The artifact block for an RH-3 case: seeded file count + provenance.

    ``None`` for ordinary diff cases (the summary key is then omitted, so old
    summaries and diff-case summaries read identically). Counts via the same
    shared walk the loader and seeder use (#302 review), so the recorded
    ``n_files`` is exactly what the pass reviews. A paired case (RH-1) also
    records its known-good capture count — whether an artifact case measured
    false positives at all is part of what makes two report dirs comparable.
    """
    if case.artifacts_dir is None or case.case_dir is None:
        return None
    root = resolve_artifacts_root(case.case_dir, case.artifacts_dir, case.id)
    info = {
        "n_files": len(iter_artifact_files(root, case.id)),
        "provenance": case.artifact_provenance,
    }
    if case.known_good_artifacts_dir is not None:
        kg_root = resolve_artifacts_root(
            case.case_dir,
            case.known_good_artifacts_dir,
            case.id,
            label="[known_good] artifacts_dir",
        )
        info["known_good_n_files"] = len(
            iter_artifact_files(kg_root, case.id, label="known-good artifact")
        )
    return info


def _panel_phrase(panel: tuple[ReviewerSpec, ...]) -> str:
    """A compact one-line panel rendering for the per-case stderr note."""

    def one(s: ReviewerSpec) -> str:
        extras = [s.tool] + [v for v in (s.model, s.effort) if v]
        return f"{s.name}({','.join(extras)})"

    return ", ".join(one(s) for s in panel)


def _write_summary(
    report_dir: Path,
    r: CaseResult,
    tier: str,
    profile: str,
    panel: tuple[ReviewerSpec, ...],
    *,
    artifacts: dict | None = None,
    judge_info: dict | None = None,
) -> None:
    """Write a per-case ``summary.json`` (rates + per-sample booleans + CIs).

    Beside the per-run ``buggy-N.json`` files, so a costly K-sample run is
    re-analysable for variance **without** re-scoring (which would re-invoke the
    paid judge). Records the **effective** profile + panel (RH-7) — and, for an
    artifact case, the measured surface (RH-3) — with per-run overrides in
    play, this is what makes two report dirs comparable.
    """
    out = report_dir / r.case_id
    out.mkdir(parents=True, exist_ok=True)
    payload = {
        "case": r.case_id,
        "tier": tier,
        "profile": profile,
        "panel": [
            {
                "name": s.name,
                "tool": s.tool,
                "model": s.model,
                "effort": s.effort,
                "block_threshold": s.block_threshold,
            }
            for s in panel
        ],
        **case_result_payload(r),
    }
    if artifacts is not None:
        payload["artifacts"] = artifacts
    payload["judge"] = judge_info
    (out / "summary.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _make_judge_sink(report_dir: Path, judge_info: dict | None) -> JudgeSink:
    """Persist each sample's judge verdicts to ``<case>/judge/<variant>-<i>.json``.

    A **subdirectory**, deliberately: ``<case>/<variant>-<i>.json`` is the
    reviewers' ``ReviewReport`` — a documented stable contract that offline
    re-scoring globs — so the eval's own scorer must not write into that
    namespace. A sibling ``buggy-0.judge.json`` would match ``buggy-*.json`` and
    silently feed judge records to a finding counter; a subdir matches nothing.
    Written per sample, so a run killed mid-sweep keeps the verdicts it has.
    """

    def sink(case: Case, variant: str, i: int, score: RunScore) -> None:
        judged = [
            (expected, m)
            for expected, m in zip(case.expected, score.matches, strict=True)
            if m.judge
        ]
        if not judged:
            return  # no judge ran — absence of the file is meaningful
        out = report_dir / case.id / "judge"
        out.mkdir(parents=True, exist_ok=True)
        payload = {
            "case": case.id,
            "variant": variant,
            "sample": i,
            "judge": judge_info,
            "caught": score.caught,
            "structured_caught": score.structured_caught,
            "judge_status": score.judge_status,
            "expected": [
                {
                    "index": n,
                    "file": expected.file,
                    "mechanism": expected.mechanism,
                    "status": m.judge.status if m.judge else "",
                    "matched_ids": list(m.judge.matched_ids) if m.judge else [],
                    "caught": m.caught,
                    "method": m.method,
                    "finding_id": m.finding_id,
                    "structured_caught": m.structured_caught,
                    "structured_finding_id": m.structured_finding_id,
                    "detail": m.judge.detail if m.judge else "",
                    "reply": m.judge.reply if m.judge else "",
                }
                for n, (expected, m) in enumerate(judged)
            ],
        }
        (out / f"{variant}-{i}.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )

    return sink


def _report_judge_errors(
    results: list[tuple[str, CaseResult]], report_dir: Path | None
) -> None:
    """Say when the *judge* — not the reviewer — failed to answer (#307)."""
    hit = []
    for _, r in results:
        statuses = [
            *r.judge_status_per_sample,
            *r.false_positive_judge_status_per_sample,
        ]
        failed = sum(s == "failed" for s in statuses)
        unparsed = sum(s == "unparsed" for s in statuses)
        if failed or unparsed:
            hit.append(f"{r.case_id} ({failed} failed, {unparsed} unparsed)")
    if not hit:
        return
    where = f", see {report_dir}/<case>/judge/" if report_dir is not None else ""
    typer.echo(
        f"judge gave no verdict on some samples: {', '.join(hit)} — "
        f"those samples are excluded{where}",
        err=True,
    )
