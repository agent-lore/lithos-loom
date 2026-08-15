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
import math
from functools import partial
from pathlib import Path

import typer

from ...plugins.story_develop import engines
from ...plugins.story_develop.config import ReviewerSpec
from ...plugins.story_develop.daemon_io import load_tool_default_models
from ...plugins.story_develop.model_policy import (
    apply_panel_default_models,
    require_agent_models,
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
from .judge import build_agent_judge
from .match import RunScore
from .overrides import parse_reviewer_overrides, resolve_panel
from .stats import wilson_interval

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
    _require_rate("--bar", bar)
    _require_rate("--max-known-good-block-rate", max_known_good_block_rate)

    case_dirs = _discover(cases_dir, case)

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
    if judge and not engines.is_supported(judge_tool):
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
    judge_info = {"tool": judge_tool, "model": judge_model} if judge else None
    judge_fn = build_agent_judge(tool=judge_tool, model=judge_model) if judge else None
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

    _print_table(results)
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


def _require_rate(flag: str, value: float | None) -> None:
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
    errored = sum(r.errored_per_sample)
    fp_errored = sum(r.false_positive_errored_per_sample)
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
        "false_positive_errored_per_sample": list(r.false_positive_errored_per_sample),
        # #310: the FP rate is defect-specific — these say what ELSE the run
        # reported and whether it held approval, on both arms.
        "noise_rate": r.noise_rate,
        "noise_rate_ci": list(r.noise_rate_ci),
        "known_good_findings_per_sample": list(r.known_good_findings_per_sample),
        "known_good_blocked_per_sample": list(r.known_good_blocked_per_sample),
        "known_good_blocked": count_valid(
            r.known_good_blocked_per_sample, r.false_positive_errored_per_sample
        )[0],
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
        "structured_caught": _structured_tally(r)[0],
        "false_positive_structured_per_sample": list(
            r.false_positive_structured_per_sample
        ),
        "passed": r.passed,
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


def _ci_band(lo: float, hi: float) -> str:
    return f"{lo * 100:.0f}-{hi * 100:.0f}%"


def _err_suffix(n: int) -> str:
    return f" +{n}err" if n else ""


def _print_table(results: list[tuple[str, CaseResult]]) -> None:
    header = (
        f"{'case':<28} {'tier':<8} {'n':>3} {'catch (95% CI)':>20} "
        f"{'sev':>5} {'fp (95% CI)':>20} {'noise':>12}  result"
    )
    typer.echo(header)
    typer.echo("-" * len(header))
    # (caught, n_valid) per case, keyed by tier, for the roll-up lines.
    tallies: dict[str, list[tuple[CaseResult, int, int]]] = {
        "floor": [],
        "frontier": [],
    }
    for tier, r in results:
        # Denominators are the VALID samples — neither the reviewer nor the judge
        # errored — so a crash on either half never deflates a rate (#182 A3, #307).
        n_err = sum(r.errored_per_sample)
        caught, n_valid = count_valid(r.caught_per_sample, r.excluded_per_sample)
        tallies[tier].append((r, caught, n_valid))
        catch_cell = (
            f"{caught}/{n_valid} {_ci_band(*r.catch_rate_ci)}{_err_suffix(n_err)}"
            f"{_judge_err_suffix(sum(r.judge_errored_per_sample))}"
        )
        if r.false_positive_per_sample:
            fp_err = sum(r.false_positive_errored_per_sample)
            flagged, fp_valid = count_valid(
                r.false_positive_per_sample, r.false_positive_excluded_per_sample
            )
            fp_cell = (
                f"{flagged}/{fp_valid} "
                f"{_ci_band(*r.false_positive_rate_ci)}{_err_suffix(fp_err)}"
            )
        else:
            fp_cell = f"{r.false_positive_rate * 100:.0f}%"
        noise_cell = _noise_cell(r)
        # Floor rows read ok/REGRESSED — the floor is a regression gate, not a
        # pass/fail measurement (RH-6).
        if tier == "floor":
            mark = "ok" if r.passed else "REGRESSED"
        else:
            mark = "PASS" if r.passed else "FAIL"
        typer.echo(
            f"{r.case_id:<28} {tier:<8} {r.n:>3} {catch_cell:>20} "
            f"{r.severity_correctness * 100:>4.0f}% {fp_cell:>20} "
            f"{noise_cell:>12}  {mark}{_struct_note(r, caught)}"
        )
    _print_rollups(tallies)


def _judge_err_suffix(n: int) -> str:
    return f" +{n}jerr" if n else ""


def _structured_tally(r: CaseResult) -> tuple[int, int]:
    """``(structured catches, valid samples)`` — ``(0, 0)`` when not recorded.

    The denominator excludes reviewer errors only: the structured matcher is
    pure over the stored findings and cannot itself fail, so an all-judge-errored
    case still has its free counterfactual.
    """
    if not r.structured_caught_per_sample:
        return (0, 0)
    return count_valid(r.structured_caught_per_sample, r.errored_per_sample)


def _struct_note(r: CaseResult, judged_caught: int) -> str:
    """``struct N/M`` — but only when the judge-free matcher disagrees (#307).

    Appended rather than given a column: the two agree on almost every row, and
    a permanent column would repeat the catch cell and break column-diffing
    against the pinned baseline's tables. The precedent is ``+Nerr``, likewise
    silent at zero. The structured denominator excludes reviewer errors only —
    the structured matcher cannot itself fail — so an all-judge-errored case
    still prints its free counterfactual.
    """
    struct, struct_valid = _structured_tally(r)
    if not struct_valid or struct == judged_caught:
        return ""
    return f"  struct {struct}/{struct_valid}"


def _noise_cell(r: CaseResult) -> str:
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


def _plural(word: str, n: int) -> str:
    return word if n == 1 else f"{word}s"


def _print_rollups(tallies: dict[str, list[tuple[CaseResult, int, int]]]) -> None:
    """The two tier roll-up lines (RH-6): frontier headline, floor gate."""
    frontier = tallies["frontier"]
    if frontier:
        caught = sum(c for _, c, _ in frontier)
        valid = sum(v for _, _, v in frontier)
        ci = wilson_interval(caught, valid) if valid else (0.0, 0.0)
        typer.echo(
            f"frontier: {caught}/{valid} pooled catch (95% CI {_ci_band(*ci)}) "
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
