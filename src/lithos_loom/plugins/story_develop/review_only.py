"""Review-only mode — run the panel + gate on an existing change (#154).

``develop()`` *produces* a change (worktree off a base, coder commits onto it);
review-only *consumes* one: materialise a worktree at the change's head, run the
deterministic gate once on that tree, run the reviewer panel once (no coder, no
fix loop), and consolidate the result into a :class:`ReviewReport`.

There is intentionally **one** panel implementation: this calls the same
:func:`~.panel.run_panel_round` primitive ``develop()`` uses, so a
prompt / severity / lifecycle fix can never land in one review path and silently
miss the other. The deterministic gate (:func:`~.check_runner.build_check_set` /
:func:`~.check_runner.run_check_set`) and the per-check block decision
(:func:`~.check_runner.check_result_blocks`) are likewise reused verbatim.

This function is also the execution primitive the review-correctness eval
harness (#183) drives: it returns structured findings for an arbitrary change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ...runner import worktree
from . import containers, engines
from .agent_session import PauseBudget, build_run_cmd
from .check_runner import (
    build_check_set,
    check_result_blocks,
    gate_floor_blocks,
    run_check_set,
)
from .check_set import CheckSetResult
from .config import HANDOFF_MOUNT_NAME, DevelopConfig, is_valid_reviewer_name
from .gate_findings import GateLedger
from .handoff import seed_handoff_dir
from .panel import PanelRoundResult, ReviewerState, run_panel_round
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


def panel_incomplete(panel: PanelRoundResult | None) -> bool:
    """Whether the panel produced no usable review this pass.

    True when it never ran (``None``), was **interrupted** (usage limit), or hit
    an **invalid reviewer**. review-only folds this into a blocking report;
    converge treats it as a hard **failure** — there is no trustworthy review to
    seed the fix loop from, so fixing against a partial/absent panel would be
    worse than stopping. (Note ``review_head`` *raises* on an exception rather
    than returning ``panel=None``, so in practice this catches the
    interrupted / invalid cases; ``None`` is the defensive floor.)
    """
    return panel is None or panel.interrupted or panel.invalid_reviewer is not None


def intake_blocks(
    panel: PanelRoundResult | None,
    check_set: CheckSetResult | None,
    gate_ledger: GateLedger,
) -> bool:
    """Whether one intake pass blocks approval — the single blocking rule.

    Blocks when the panel is **incomplete** (:func:`panel_incomplete`), when
    **any reviewer did not pass**, or when the deterministic **floor blocks**
    (:func:`gate_floor_blocks`). Used by both :func:`_build_report` (review-only's
    report) and :attr:`IntakeResult.blocking` (converge's already-clean
    short-circuit), so the two can never diverge.
    """
    reviewers_pass = panel is not None and all(r.passed for r in panel.round_reviews)
    return (
        panel_incomplete(panel)
        or not reviewers_pass
        or gate_floor_blocks(check_set, gate_ledger)
    )


@dataclass(frozen=True)
class IntakeResult:
    """The raw pieces of one review pass at a change head.

    :func:`review_change` consolidates them into a :class:`ReviewReport`; the
    converge loop seeds its round-1 coder from ``panel.round_reviews`` and shows
    ``check_set`` in the fix prompt, so it needs the raw outcomes the report
    would otherwise discard, and reads :attr:`blocking` for its already-clean
    short-circuit.
    """

    reviewers: list[ReviewerState]
    panel: PanelRoundResult | None
    check_set: CheckSetResult | None
    gate_ledger: GateLedger

    @property
    def incomplete(self) -> bool:
        """Whether the panel produced no usable review (:func:`panel_incomplete`)."""
        return panel_incomplete(self.panel)

    @property
    def blocking(self) -> bool:
        """Whether this intake blocks — the same rule the review report applies."""
        return intake_blocks(self.panel, self.check_set, self.gate_ledger)


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
    intake = review_head(
        config, change, reviewer_timeout=reviewer_timeout, keep_worktree=keep_worktree
    )
    return _build_report(
        config,
        change,
        intake.reviewers,
        intake.panel,
        intake.check_set,
        intake.gate_ledger,
    )


def review_head(
    config: DevelopConfig,
    change: ResolvedChange,
    *,
    reviewer_timeout: int = 3600,
    keep_worktree: bool = False,
) -> IntakeResult:
    """Run the panel + gate once at the change head and return the RAW pieces.

    The shared intake driven by both :func:`review_change` (which consolidates
    the pieces into a :class:`ReviewReport`) and the converge loop (which seeds
    its round-1 coder from ``panel.round_reviews`` and the intake ``check_set``).
    Materialises a read-only worktree at ``change.head_sha``, runs the profile's
    check-set once and the reviewer panel once (round 1, no coder), and tears the
    worktree + reviewer containers down unless *keep_worktree*.
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

    reviewers: list[ReviewerState] = []
    for spec in specs:
        rname, rcmd = build_run_cmd(
            config,
            agent=f"review-{spec.name}",
            engine=engines.get_engine(spec.tool),
            config_dir=config.reviewer_config_dir(spec.name),
            wt=wt,
            read_only=True,
        )
        reviewers.append(ReviewerState(spec, rname, rcmd, wt))

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
            check_set = run_check_set(
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
            budget=PauseBudget(config.max_pause_minutes * 60),
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

    return IntakeResult(
        reviewers=reviewers, panel=panel, check_set=check_set, gate_ledger=gate_ledger
    )


def _build_report(
    config: DevelopConfig,
    change: ResolvedChange,
    reviewers: list[ReviewerState],
    panel: PanelRoundResult | None,
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

    blocking = intake_blocks(panel, check_set, gate_ledger)

    return ReviewReport(
        head_ref=change.head_ref or change.head_sha[:12],
        base_sha=change.base_sha,
        head_sha=change.head_sha,
        profile=config.review_profile,
        reviewers=reviewer_reports,
        gate=gate_reports,
        blocking=blocking,
    )
