"""Review-only mode — run the panel + gate on an existing change (#154).

``develop()`` *produces* a change (worktree off a base, coder commits onto it);
review-only *consumes* one: materialise a worktree at the change's head, run the
deterministic gate once on that tree, run the reviewer panel once (no coder, no
fix loop), and consolidate the result into a :class:`ReviewReport`.

There is intentionally **one** panel implementation: this calls the same
:func:`~.develop.run_panel_round` primitive ``develop()`` uses, so a
prompt / severity / lifecycle fix can never land in one review path and silently
miss the other. The deterministic gate (:func:`~.develop.build_check_set` /
:func:`~.develop._run_check_set`) and the per-check block decision
(:func:`~.develop.check_result_blocks`) are likewise reused verbatim.

This function is also the execution primitive the review-correctness eval
harness (#183) drives: it returns structured findings for an arbitrary change.
"""

from __future__ import annotations

import logging

from ...runner import worktree
from . import containers, engines
from .check_set import CheckSetResult
from .config import HANDOFF_MOUNT_NAME, DevelopConfig, is_valid_reviewer_name
from .develop import (
    _build_run_cmd,
    _PauseBudget,
    _ReviewerState,
    _run_check_set,
    build_check_set,
    check_result_blocks,
    gate_floor_blocks,
    run_panel_round,
)
from .gate_findings import GateLedger
from .handoff import seed_handoff_dir
from .review_report import (
    GateCheckReport,
    ReviewerReport,
    ReviewFinding,
    ReviewReport,
)
from .review_resolve import ResolvedChange

logger = logging.getLogger(__name__)

# The reviewer prompt's ``{coder_summary}`` slot. There is no coder in
# review-only mode, so we say so plainly rather than fabricate a summary.
_REVIEW_ONLY_CODER_SUMMARY = (
    "This change was authored outside the develop loop (review-only mode): "
    "there is no coder turn and no fix loop. Review the external change on its "
    "own merits, end to end, against the acceptance criteria above."
)


def review_change(
    config: DevelopConfig,
    change: ResolvedChange,
    *,
    reviewer_timeout: int = 3600,
    keep_worktree: bool = False,
) -> ReviewReport:
    """Run the panel + deterministic gate against an existing *change*.

    Materialises a read-only worktree at ``change.head_sha``, runs the resolved
    profile's check-set once, runs each reviewer once (round 1), and returns the
    consolidated :class:`ReviewReport`. The worktree + reviewer containers are
    torn down on exit unless *keep_worktree* is set.
    """
    specs = config.effective_reviewers
    for spec in specs:
        if not engines.is_supported(spec.tool):
            raise ValueError(
                f"unsupported tool {spec.tool!r} for reviewer {spec.name!r}: "
                f"expected {engines.supported_tools_phrase()}"
            )
        if not is_valid_reviewer_name(spec.name):
            raise ValueError(f"invalid reviewer name {spec.name!r}")
    names = [s.name for s in specs]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate reviewer names: {names}")

    config.worktree_parent.mkdir(parents=True, exist_ok=True)
    for spec in specs:
        config.reviewer_config_dir(spec.name).mkdir(parents=True, exist_ok=True)
    seed_handoff_dir(config.handoff_dir)

    wt = worktree.create_at(
        config.repo, change.head_sha, config.description, parent=config.worktree_parent
    )
    logger.info(
        "review-only %s: worktree %s at %s",
        config.run_id,
        wt,
        change.head_sha[:12],
    )

    # Reviewer containers mount the worktree READ-ONLY and bind the handoff dir
    # at /workspace/.handoff — docker cannot create that mountpoint inside a RO
    # /workspace, so it must already exist in the worktree. In the develop loop
    # the RW coder container (started first) creates it; review-only has no
    # coder, so create it here before the RO reviewers start.
    (wt / HANDOFF_MOUNT_NAME).mkdir(parents=True, exist_ok=True)

    reviewers: list[_ReviewerState] = []
    for spec in specs:
        rname, rcmd = _build_run_cmd(
            config,
            agent=f"review-{spec.name}",
            tool=spec.tool,
            config_dir=config.reviewer_config_dir(spec.name),
            wt=wt,
            read_only=True,
        )
        reviewers.append(_ReviewerState(spec, rname, rcmd, wt))

    gate_ledger = GateLedger()
    check_set: CheckSetResult | None = None
    panel = None
    try:
        checks = build_check_set(config, wt)
        for rstate in reviewers:
            containers.start_container(rstate.run_cmd)
        # Gate first so the panel prompt carries the deterministic summary, then
        # one reviewer round — the SAME primitive develop() drives.
        if checks:
            check_set = _run_check_set(
                config, wt, change.head_sha, 1, checks, gate_ledger
            )
        panel = run_panel_round(
            config,
            reviewers,
            wt=wt,
            base=change.base_sha,
            round_no=1,
            check_set=check_set,
            gate_ledger=gate_ledger,
            budget=_PauseBudget(config.max_pause_minutes * 60),
            reviewer_timeout=reviewer_timeout,
            coder_summary=_REVIEW_ONLY_CODER_SUMMARY,
        )
    finally:
        for rstate in reviewers:
            containers.stop_container(rstate.container)
        if not keep_worktree:
            try:
                worktree.remove(wt, force=True)
            except RuntimeError:
                logger.warning("review-only %s: worktree cleanup failed", config.run_id)

    return _build_report(config, change, reviewers, panel, check_set, gate_ledger)


def _build_report(
    config: DevelopConfig,
    change: ResolvedChange,
    reviewers: list[_ReviewerState],
    panel,
    check_set: CheckSetResult | None,
    gate_ledger: GateLedger,
) -> ReviewReport:
    reviewer_reports: list[ReviewerReport] = []
    for rstate in reviewers:
        outcome = rstate.outcome
        if outcome is None:
            # the panel short-circuited before this reviewer ran
            reviewer_reports.append(
                ReviewerReport(
                    name=rstate.spec.name, status="not-run", passed=False, findings=[]
                )
            )
            continue
        findings = [
            ReviewFinding(
                reviewer=rstate.spec.name,
                severity=f.severity,
                files=list(f.files),
                rationale=f.rationale,
                finding_id=f.finding_id,
            )
            for f in outcome.findings
        ]
        reviewer_reports.append(
            ReviewerReport(
                name=rstate.spec.name,
                status=outcome.status,
                passed=outcome.passed,
                findings=findings,
            )
        )

    gate_reports: list[GateCheckReport] = []
    if check_set is not None:
        gate_reports = [
            GateCheckReport(
                name=r.check.name,
                outcome=r.execution_outcome,
                blocked=check_result_blocks(r, gate_ledger),
            )
            for r in check_set.results
        ]

    incomplete = (
        panel is None or panel.interrupted or panel.invalid_reviewer is not None
    )
    reviewers_pass = panel is not None and all(r.passed for r in panel.round_reviews)
    blocking = (
        incomplete or not reviewers_pass or gate_floor_blocks(check_set, gate_ledger)
    )

    return ReviewReport(
        head_ref=change.head_ref or change.head_sha[:12],
        base_sha=change.base_sha,
        head_sha=change.head_sha,
        profile=config.review_profile,
        reviewers=reviewer_reports,
        gate=gate_reports,
        blocking=blocking,
    )
