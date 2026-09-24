"""On-demand PR review-convergence loop (converge / ADR 0003 §9 "Shape 1").

``converge_pr`` automates the operator's manual review chore — run the reviewer
panel + deterministic gate on an existing PR, feed the findings to a coder that
fixes the PR branch, re-review, and loop until the panel LGTMs and the gate
floor is clean — then fast-forward-push the fixed branch back to the PR head,
ready for the human merge gate.

It is a **thin orchestrator** over three already-tested pieces, deliberately
adding no new loop of its own (ADR 0004 §1 — the fix loop is single-sourced):

1. **Intake** — :func:`review_only.review_head` runs the panel + gate once at the
   PR head and returns the raw pieces; :attr:`~review_only.IntakeResult.blocking`
   (the same rule review-only's report applies) decides whether it blocks. A
   non-blocking intake short-circuits to ``already_clean`` before any coder
   container is built (the cheapest path for the common re-check).
2. **Fix loop** — :func:`develop` entered via a :class:`~.loop_entry.LoopEntry` that
   positions a committable worktree at the PR head, diffs against the PR
   merge-base, and seeds round 1's cold-start coder from the intake review
   (converge PR 2). The loop's own ``approved`` / ``disputed`` / ``stalled`` /
   ``cost_exceeded`` / ``max_rounds`` termination is reused verbatim.
3. **Push epilogue** — on approval, :func:`push_to_pr_ref` fast-forwards the
   reviewed HEAD onto the PR head ref under an atomic lease (never a blind
   ``--force`` / history rewrite); a fork PR is refused *pre-loop* and a mid-run
   remote advance surfaces as ``merge_race``.

v1 is local-panel-only: it converges against loom's in-container codex/claude
panel + check-floor, not the GitHub review bots (a deferred slice).
"""

from __future__ import annotations

import dataclasses
import logging
import math
from collections.abc import Callable

from ...runner import git, worktree
from . import review_only
from .config import DevelopConfig
from .conflict_resolve import (
    StaleTrigger,
    UnsupportedConflict,
    markers_guard,
    prepare_conflict_intake,
    render_review_context,
)
from .converge_result import ConflictSummary, ConvergeResult, ConvergeStatus
from .develop import DevelopResult, develop
from .external_record import record_external_intake
from .external_reviews import (
    ExternalFinding,
    ExternalOutcome,
    ack_instruction,
    claims_nothing_to_change,
    external_intake_reviews,
    final_round_outcomes,
    nothing_to_change,
    outcomes_after_loop,
    render_external_context,
    undecided_note,
)
from .external_triage import approval_eligible_ids, triage_external_findings
from .findings import DeferredFinding
from .generated import post_commit_regenerate
from .loop_entry import LoopEntry
from .pr_delivery import ForkPushUnsupported, MergeRaceDetected, push_to_pr_ref
from .review_resolve import ResolvedChange

logger = logging.getLogger(__name__)

# the verdict types live in converge_result (a leaf); re-exported here so the
# CLI, the eval harness and the tests keep importing them from the entry module
__all__ = ["ConflictSummary", "ConvergeResult", "ConvergeStatus", "converge_pr"]


def converge_pr(
    config: DevelopConfig,
    change: ResolvedChange,
    *,
    no_push: bool = False,
    coder_timeout: int = 3600,
    reviewer_timeout: int = 3600,
    external_findings: tuple[ExternalFinding, ...] | None = None,
    resolve_conflicts: bool = False,
    expect_base: str | None = None,
) -> ConvergeResult:
    """Run the review-convergence loop against an existing PR *change*.

    See the module docstring for the three-stage flow. Returns a
    :class:`ConvergeResult`; never raises for the expected terminal states
    (fork / merge-race / unapproved) — they are reported via ``status``. Raises
    :class:`ValueError` only for a caller error: an invalid numeric config
    (non-finite / non-positive ``max_cost_usd``, ``max_rounds < 1``) or a
    *change* with no pushable head branch (not a PR).
    """
    # Validate the numeric bounds at this reusable-API boundary, not only in the
    # CLI: a future daemon caller that passes max_cost_usd <= 0 or max_rounds < 1
    # must fail fast here rather than spend on intake and surface the error deep in
    # develop() (or, worse, run an unbounded loop).
    # NaN compares False against everything — `<= 0` here AND every later budget
    # comparison — so a NaN ceiling would silently behave as unlimited; reject
    # non-finite values outright.
    if config.max_cost_usd is not None and (
        not math.isfinite(config.max_cost_usd) or config.max_cost_usd <= 0
    ):
        raise ValueError(
            f"max_cost_usd must be finite and > 0, got {config.max_cost_usd}"
        )
    if config.max_rounds < 1:
        raise ValueError(f"max_rounds must be >= 1, got {config.max_rounds}")
    if resolve_conflicts and external_findings is not None:
        raise ValueError("resolve_conflicts and external_findings are exclusive modes")
    # Same fail-fast rationale for the change itself: converge delivers to a PR
    # head branch. A range/branch-resolved change (no pushable branch) would
    # spend the whole intake + loop and then die in the push epilogue with a
    # misleading fork error from an ls-remote on an empty ref.
    if not change.head_branch:
        raise ValueError(
            f"change {change.head_ref!r} has no pushable head branch "
            "(not a PR?); converge requires a PR"
        )

    # Fork guard, pre-loop: loom pushes fixes under origin credentials, so a PR
    # whose head lives on a fork can never be pushed back. Refuse before spending
    # any reviewer/coder containers on a run we could not deliver.
    # Merged guard, before everything else: a landed PR has nothing to converge
    # and no fix commit pushed to its branch can reach the base. Checked ahead of
    # the fork guard because it is the more fundamental refusal — "push it from
    # your fork" is useless advice for a PR that already merged. Observed
    # 2026-08-27: 5 rounds and 6 fixer commits burned $29.78 against a merged PR
    # because nothing looked. Reviewing a merged PR stays legitimate; only
    # converge, which pushes, refuses.
    if change.is_merged:
        return ConvergeResult(
            status="merged",
            change=change,
            message=(
                f"PR {change.head_ref} is already merged; there is nothing to "
                "converge and any fix commit would be unlandable on it"
            ),
        )

    if change.is_fork:
        return ConvergeResult(
            status="fork_unsupported",
            change=change,
            message=(
                f"PR {change.head_ref} head is on a fork; converge cannot push "
                "fixes back under origin credentials"
            ),
        )

    if resolve_conflicts:
        return _resolve_conflicts(
            config,
            change,
            no_push=no_push,
            coder_timeout=coder_timeout,
            reviewer_timeout=reviewer_timeout,
            expect_base=expect_base,
        )

    if external_findings is not None:
        # --- external mode (PRD S2): triage-then-inject, no local intake ---
        # The local panel's already_clean short-circuit is exactly the panel
        # that missed the defects an external reviewer then found (ADR 0011
        # decisions 1/7) — so external findings SKIP intake entirely and seed
        # the coder directly; the loop's own panel + gate judge the RESULT.
        if not external_findings:
            raise ValueError("external_findings must be non-empty when provided")
        seed, id_map = external_intake_reviews(
            external_findings, current_head_sha=change.head_sha
        )
        triage = triage_external_findings(
            config,
            change,
            seed[0],
            # Which ids triage's third verdict may drop is decided from the
            # rows, not from the verdict sentence (security f-001).
            approval_eligible=approval_eligible_ids(id_map),
            timeout=reviewer_timeout,
        )
        surviving = [f for f in seed[0].findings if f.finding_id in set(triage.proceed)]
        if not surviving:
            # Nothing survives triage: no coder, nothing pushed. Either every
            # claim was refuted with cited evidence (`triage_rejected` — the
            # rejections ride out for the caller's thread replies), or the
            # batch was never a claim at all (an approval: `already_clean` at
            # round 0, the #380 outcome without the paid turns — the watcher
            # refunds the round the same way for both).
            outcomes = outcomes_after_loop(
                id_map,
                triage.rejections,
                {},
                {},
                nothing_to_remediate=triage.nothing_to_remediate,
                loop_approved=False,
            )
            degraded = f" ({triage.note})" if triage.note else ""
            if not triage.rejections:
                logger.info(
                    "converge %s: triage found nothing to remediate in %d "
                    "external finding(s) — the batch only approves",
                    config.run_id,
                    len(id_map),
                )
                return ConvergeResult(
                    status="already_clean",
                    change=change,
                    intake_cost_usd=triage.cost_usd,
                    external_outcomes=outcomes,
                    message="every external finding asks for nothing (an "
                    "approval) — nothing to remediate" + degraded,
                )
            # At least one claim was refuted with evidence, so the reviewer is
            # owed a rejection reply and the status stays `triage_rejected`.
            # The summary names every disposition rather than calling them all
            # rejections (round-5 panel correctness f-003): an approval beside
            # them was not refuted and has no evidence to cite, and this
            # message is what the operator reads on the story.
            refuted, approvals = (
                len(triage.rejections),
                len(triage.nothing_to_remediate),
            )
            if approvals:
                summary = (
                    f"triage left nothing to converge: {refuted} finding(s) "
                    f"refuted with cited evidence, {approvals} asking for "
                    "nothing (an approval)"
                )
            else:
                summary = (
                    "triage rejected every external finding with cited "
                    "evidence — nothing to converge"
                )
            logger.info(
                "converge %s: nothing survives triage in %d external finding(s)"
                " — %d refuted, %d asking for nothing",
                config.run_id,
                len(id_map),
                refuted,
                approvals,
            )
            return ConvergeResult(
                status="triage_rejected",
                change=change,
                intake_cost_usd=triage.cost_usd,
                external_outcomes=outcomes,
                message=summary + degraded,
            )
        # Whole-command budget: the triage spend alone must not meet the
        # ceiling. Checked AFTER the no-survivor terminal above (round-5 panel
        # correctness f-001), unlike the local intake whose check precedes its
        # clean return: the ceiling stops the PAID FOLLOW-UP, and with nothing
        # surviving there is no fix loop to stop — the spend has happened
        # either way. `failed` there would hide the run's real outcome (a
        # round-0 disposition the watcher reports and refunds,
        # REPORTED_NOT_REMEDIATED) behind a status that leaves the S5b round
        # spent and can raise the very human gate this path exists to avoid.
        if config.max_cost_usd is not None and triage.cost_usd >= config.max_cost_usd:
            return ConvergeResult(
                status="failed",
                change=change,
                intake_cost_usd=triage.cost_usd,
                external_outcomes=outcomes_after_loop(
                    id_map,
                    triage.rejections,
                    {},
                    {},
                    nothing_to_remediate=triage.nothing_to_remediate,
                    loop_approved=False,
                ),
                message=f"triage spent ${triage.cost_usd:.2f}, meeting the "
                f"--max-cost ${config.max_cost_usd:.2f} ceiling before the fix loop",
            )
        logger.info(
            "converge %s: %d/%d external finding(s) survive triage — entering fix loop",
            config.run_id,
            len(surviving),
            len(id_map),
        )
        surviving_ids = [f.finding_id for f in surviving]
        # The injected batch, on disk before the first fix round: a run that
        # stops exhausted answers no thread, and `develop converge-push` —
        # the operator's decision to keep its rounds — owes the reviewers the
        # replies this run would have posted.
        record_external_intake(
            config.run_dir,
            id_map=id_map,
            rejections=triage.rejections,
            nothing_to_remediate=triage.nothing_to_remediate,
            surviving_ids=surviving_ids,
            generated_paths=config.generated_paths,
        )
        entry = LoopEntry(
            worktree_factory=lambda cfg: worktree.create_on_branch(
                cfg.repo, change.head_sha, cfg.description, parent=cfg.worktree_parent
            ),
            base_override=git.RangeBase(change.base_sha, change.base_ref),
            intake_reviews=[dataclasses.replace(seed[0], findings=surviving)],
            intake_check_set=None,
            # The per-id acknowledgement contract (PR #345 re-review 1): the
            # coder must state FIXED/DISPUTED for every injected id, and the
            # epilogue below refuses a `fixed` disposition without that ack.
            external_ack=ack_instruction(surviving_ids),
            # PR #396 review (High): a round-1 coder that commits nothing
            # because every injected id needs no change (#380) has made a
            # claim — the loop admits the empty round as a VALIDATION pass:
            # its gate + panel judge the unchanged head, told what was
            # claimed; approval → already_clean, rejection ends the run
            # (never an entry to the fix loop over the whole PR).
            no_change_claim=lambda round_no: claims_nothing_to_change(
                config.handoff_dir, round_no, surviving_ids
            ),
            review_context=render_external_context(
                {fid: id_map[fid] for fid in surviving_ids}
            ),
        )

        def _external_epilogue(result: DevelopResult) -> tuple[ExternalOutcome, ...]:
            # #387/#399: the threads are answered from the coder's acks across
            # every round — the final one wins when decisive
            return final_round_outcomes(
                handoff_dir=config.handoff_dir,
                run_id=config.run_id,
                rounds=result.rounds,
                loop_approved=result.approved,
                worktree=result.worktree,
                head_sha=change.head_sha,
                generated_paths=config.generated_paths,
                id_map=id_map,
                rejections=triage.rejections,
                nothing_to_remediate=triage.nothing_to_remediate,
                surviving_ids=surviving_ids,
            )

        return _loop_and_deliver(
            config,
            change,
            entry,
            no_push=no_push,
            coder_timeout=coder_timeout,
            reviewer_timeout=reviewer_timeout,
            pre_loop_cost=triage.cost_usd,
            intake_deferred=(),
            external_epilogue=_external_epilogue,
        )

    # --- intake: one panel + gate pass at the PR head ---
    # Run intake under a DISTINCT run_id so its round-1 artifacts (handoff dir,
    # gate export at gate_dir/round_01/tree, container names — all run_id-derived)
    # never collide with the fix loop's own round 1. `export_tree` overlays and
    # `seed_handoff_dir` doesn't clear, so a shared run_id would let intake's head
    # export / stale reviewer handoff bleed into the fixed-tree gate + panel
    # (finding #1). The in-memory intake seed (reviews + check-set) carries over
    # regardless of run_id.
    intake_config = dataclasses.replace(config, run_id=f"{config.run_id}-intake")
    intake = review_only.review_head(
        intake_config, change, reviewer_timeout=reviewer_timeout
    )
    intake_cost = intake.panel.cost if intake.panel is not None else 0.0

    # Extract deferrals BEFORE the incomplete/budget exits below (PR #342
    # re-review P2): one reviewer can defer a finding before another fails,
    # and a completed intake can exhaust the ceiling — either way the deferral
    # already happened and must ride out on the ConvergeResult, or it is lost.
    # getattr-tolerant: converge tests stub the intake panel loosely, and a
    # stub without findings simply contributes no deferrals.
    intake_deferred = tuple(
        DeferredFinding(
            reviewer=outcome.reviewer,
            finding_id=f.finding_id,
            severity=f.severity,
            rationale=f.rationale,
            files=tuple(f.files),
            deferral_reason=getattr(f, "deferral_reason", ""),
        )
        for outcome in (intake.panel.round_reviews if intake.panel else [])
        for f in getattr(outcome, "findings", ())
        if f.status == "out-of-scope"
    )

    infra = getattr(intake.panel, "infra_failure", None)
    if infra is not None:
        # #377: the intake panel died on the host (slice B) — the same class
        # as a loop that dies, one phase earlier: say what to fix, not that
        # the panel was "invalid".
        logger.info(
            "converge %s: %s intake stopped on infra: %s",
            config.run_id,
            change.head_ref,
            infra,
        )
        action = str(getattr(intake.panel, "infra_host_action", "") or "")
        return ConvergeResult(
            status="infra_failed",
            change=change,
            intake_cost_usd=intake_cost,
            intake_deferred=intake_deferred,
            host_action=action,
            message=f"INFRA FAILURE during the intake review: {infra} — {action}",
        )
    if intake.incomplete:
        # The panel produced no usable review (interrupted / invalid / absent).
        # There is nothing trustworthy to seed the fix loop from — surface it as a
        # failure rather than fixing against a partial/absent review (finding #2).
        logger.info(
            "converge %s: %s intake did not complete", config.run_id, change.head_ref
        )
        return ConvergeResult(
            status="failed",
            change=change,
            intake_cost_usd=intake_cost,
            intake_deferred=intake_deferred,
            message="intake review did not complete (interrupted / invalid panel) "
            "— cannot seed the fix loop",
        )
    # Whole-command budget: the intake spend alone must not meet the ceiling. If
    # it does, stop with `failed` REGARDLESS of whether the intake was clean or
    # blocking — checked BEFORE the already-clean return so a clean intake can't
    # bypass the budget contract (finding #2). (The intake is one atomic review
    # pass and can't be sub-bounded; --max-cost then bounds only the fix loop.)
    if config.max_cost_usd is not None and intake_cost >= config.max_cost_usd:
        logger.info(
            "converge %s: intake spend $%.2f exhausted --max-cost $%.2f",
            config.run_id,
            intake_cost,
            config.max_cost_usd,
        )
        return ConvergeResult(
            status="failed",
            change=change,
            intake_cost_usd=intake_cost,
            intake_deferred=intake_deferred,
            message=f"intake review spent ${intake_cost:.2f}, meeting the --max-cost "
            f"${config.max_cost_usd:.2f} ceiling before the fix loop",
        )

    if not intake.blocking:
        logger.info(
            "converge %s: %s intake already clean", config.run_id, change.head_ref
        )
        return ConvergeResult(
            status="already_clean",
            change=change,
            intake_cost_usd=intake_cost,
            intake_deferred=intake_deferred,
            message="intake review is already clean — nothing to converge",
        )

    # --- fix loop: enter develop() on the PR branch, seeded from the intake ---
    logger.info(
        "converge %s: %s intake blocks — entering fix loop",
        config.run_id,
        change.head_ref,
    )
    assert intake.panel is not None  # narrowed by the `intake.incomplete` guard
    entry = LoopEntry(
        worktree_factory=lambda cfg: worktree.create_on_branch(
            cfg.repo, change.head_sha, cfg.description, parent=cfg.worktree_parent
        ),
        base_override=git.RangeBase(change.base_sha, change.base_ref),
        intake_reviews=intake.panel.round_reviews,
        intake_check_set=intake.check_set,
    )
    return _loop_and_deliver(
        config,
        change,
        entry,
        no_push=no_push,
        coder_timeout=coder_timeout,
        reviewer_timeout=reviewer_timeout,
        pre_loop_cost=intake_cost,
        intake_deferred=intake_deferred,
        external_epilogue=None,
    )


def _resolve_conflicts(
    config: DevelopConfig,
    change: ResolvedChange,
    *,
    no_push: bool,
    coder_timeout: int,
    reviewer_timeout: int,
    expect_base: str | None = None,
) -> ConvergeResult:
    """Resolve mode (PRD S5): the merge in progress is the intake; round 1
    resolves it, the loop's own gate + panel judge the composed tree."""
    try:
        intake = prepare_conflict_intake(config, change, expect_base=expect_base)
    except StaleTrigger as exc:
        return ConvergeResult(
            status="base_moved",
            change=change,
            message=(
                f"PR {change.head_ref}: {exc}; nothing spent, nothing pushed — the "
                "caller re-keys on the current pair"
            ),
        )
    except UnsupportedConflict as exc:
        return ConvergeResult(
            status="conflict_unsupported",
            change=change,
            conflict=ConflictSummary(
                paths=exc.paths,
                base_ref=change.base_ref or change.base_sha,
                base_sha=exc.base_sha,
            ),
            message=(
                f"PR {change.head_ref} conflicts with its base in a shape this "
                f"mode cannot resolve by editing — {exc}; no agent ran, nothing "
                "pushed; a human must resolve it"
            ),
        )
    if intake is None:
        return ConvergeResult(
            status="no_conflict",
            change=change,
            message=(
                f"PR {change.head_ref} merges cleanly with its base "
                f"({change.base_ref or config.base_branch}) — nothing to resolve; "
                "the base-move re-gate handles a clean merge"
            ),
        )
    summary = ConflictSummary(
        paths=intake.paths, base_ref=intake.base_ref, base_sha=intake.base_sha
    )
    entry = LoopEntry(
        worktree_factory=lambda _cfg: intake.worktree,
        # the fork point moves to the base tip once round 1 commits the merge
        base_override=git.RangeBase(change.base_sha, intake.base_ref),
        intake_reviews=[],
        intake_check_set=None,
        coder_init_template="resolve_coder_init.md",
        coder_init_extra={"conflict_brief": intake.brief},
        pre_commit_guard=markers_guard(
            intake.paths, head_sha=change.head_sha, base_sha=intake.base_sha
        ),
        post_commit_pass=post_commit_regenerate(config),  # PRD S4, after formatting
        review_context=render_review_context(
            intake.paths,
            head_sha=change.head_sha,
            base_sha=intake.base_sha,
            base_ref=intake.base_ref,
            generated=intake.generated_paths,
        ),
    )
    result = _loop_and_deliver(
        config,
        change,
        entry,
        no_push=no_push,
        coder_timeout=coder_timeout,
        reviewer_timeout=reviewer_timeout,
        pre_loop_cost=0.0,
        intake_deferred=(),
        external_epilogue=None,
        deliverable=_contains_base(intake.base_sha),
    )
    return dataclasses.replace(result, conflict=summary)


def _contains_base(base_sha: str) -> Callable[[DevelopResult], str | None]:
    """Resolve mode's delivery precondition (PR #364 review F2): the push epilogue
    proves descent from the PR head only, so prove the intended base is an ancestor
    of the approved tree first — approved without the merge = `failed`, not pushed."""

    def check(result: DevelopResult) -> str | None:
        head = git.commit_sha(result.worktree)
        if git.is_ancestor(result.worktree, base_sha, head):
            return None
        return (
            f"the intended base {base_sha[:12]} is not an ancestor of the "
            f"approved HEAD {head[:12]} — the base merge did not land; not pushed"
        )

    return check


def _nothing_to_change_message(outcomes: tuple[ExternalOutcome, ...]) -> str:
    no_change = [o.finding_id for o in outcomes if o.disposition == "no_change_needed"]
    refuted = [o.finding_id for o in outcomes if o.disposition == "rejected"]
    parts = []
    if no_change:
        parts.append(f"no change needed for {', '.join(no_change)}")
    if refuted:
        parts.append(f"{', '.join(refuted)} refuted by triage")
    why = "; ".join(parts)
    return (
        f"every external finding needed no change ({why}); the gate and panel "
        "approved the unchanged head — nothing to converge"
    )


def _loop_and_deliver(
    config: DevelopConfig,
    change: ResolvedChange,
    entry: LoopEntry,
    *,
    no_push: bool,
    coder_timeout: int,
    reviewer_timeout: int,
    pre_loop_cost: float,
    intake_deferred: tuple[DeferredFinding, ...],
    external_epilogue: Callable[[DevelopResult], tuple[ExternalOutcome, ...]] | None,
    deliverable: Callable[[DevelopResult], str | None] | None = None,
) -> ConvergeResult:
    """The shared fix-loop + push tail (single-sourced across all modes).

    ``deliverable``, when set, is a precondition checked on an APPROVED
    result before the push epilogue; a non-None reason makes the run
    ``failed`` (nothing pushed).

    ``pre_loop_cost`` is whatever was spent before the loop — the local-panel
    intake, or external mode's triage turn — and lands in the result's
    ``intake_cost_usd`` slot either way (the budget carry treats them
    identically). ``external_epilogue``, when set, computes the per-injected-
    finding dispositions once the loop has run.
    """
    # Carry the pre-loop spend into the loop budget so --max-cost bounds the
    # WHOLE command, not just the loop. The callers' exhaustion checks
    # guarantee the remainder is > 0 here.
    loop_config = config
    if config.max_cost_usd is not None:
        loop_config = dataclasses.replace(
            config, max_cost_usd=config.max_cost_usd - pre_loop_cost
        )

    result = develop(
        loop_config,
        coder_timeout=coder_timeout,
        reviewer_timeout=reviewer_timeout,
        entry=entry,
    )
    external_outcomes = (
        external_epilogue(result) if external_epilogue is not None else ()
    )
    note = undecided_note(external_outcomes)  # #387: a reverted fix is said

    # Only the fixer's commits (PR head → HEAD), never develop()'s own span
    # (merge-base → HEAD includes the PR's original commits — the reporting gotcha).
    fixer_commits = tuple(git.commits_since(result.worktree, change.head_sha))

    if result.approved and not fixer_commits:
        # The loop approved a tree it never changed — only the admitted
        # round-1 no-change claim reaches here (PR #396 review: round 1 must
        # otherwise commit). Nothing to push either way.
        if nothing_to_change(external_outcomes):
            # #380 (lens #83): every injected finding was refuted or not a
            # defect (an approval verdict ingested as a finding), the coder
            # said so per id, and the loop's own gate + panel APPROVED the
            # unchanged head — reported, not remediated: a success, never
            # the `not_converged` that spent the last budget round and
            # raised a needs-human gate on a mergeable PR, and never on the
            # coder's word alone (an unapproved claim is `unaddressed` and
            # the run `not_converged`).
            return ConvergeResult(
                status="already_clean",
                change=change,
                develop_result=result,
                intake_cost_usd=pre_loop_cost,
                intake_deferred=intake_deferred,
                external_outcomes=external_outcomes,
                message=_nothing_to_change_message(external_outcomes),
            )
        # Defensive — the admitted round's handoff is the one the epilogue
        # reads, so its acks are all no-change; should a claim of a fix ever
        # ride on a tree the run never committed to, it is a claim with no
        # fix behind it, NOT #387's "fixed, then reverted" (opus round 2:
        # the watcher raises a `disputed` gate on `reverted`). Nothing to push.
        unbacked = tuple(
            dataclasses.replace(
                o,
                disposition="unaddressed",
                detail=(
                    "the coder claimed a fix that was never committed in this "
                    "run — a claim with no fix behind it"
                ),
            )
            if o.disposition in ("fixed", "reverted")
            else o
            for o in external_outcomes
        )
        claimed = ", ".join(
            f"{o.finding_id} {o.disposition}"
            for o in unbacked
            if o.disposition not in ("rejected", "no_change_needed")
        )
        return ConvergeResult(
            status="failed",
            change=change,
            develop_result=result,
            intake_cost_usd=pre_loop_cost,
            intake_deferred=intake_deferred,
            external_outcomes=unbacked,
            message=(
                "the loop approved the unchanged PR head, but the final handoff "
                f"does not claim no change for every external finding ({claimed}) "
                "— nothing was committed, so there is nothing to push"
            ),
        )
    if not result.approved:
        return ConvergeResult(
            status="infra_failed"
            if result.status == "infra_failed"
            else "not_converged",
            change=change,
            develop_result=result,
            fixer_commits=fixer_commits,
            intake_cost_usd=pre_loop_cost,
            intake_deferred=intake_deferred,
            external_outcomes=external_outcomes,
            host_action=result.host_action,
            message=result.message,
        )

    refused = deliverable(result) if deliverable is not None else None
    if refused is not None:
        return ConvergeResult(
            status="failed",
            change=change,
            develop_result=result,
            fixer_commits=fixer_commits,
            intake_cost_usd=pre_loop_cost,
            intake_deferred=intake_deferred,
            external_outcomes=external_outcomes,
            message=refused,
        )

    # --- push epilogue: fast-forward the fixed branch onto the PR head ref ---
    if no_push:
        return ConvergeResult(
            status="converged",
            change=change,
            develop_result=result,
            fixer_commits=fixer_commits,
            intake_cost_usd=pre_loop_cost,
            intake_deferred=intake_deferred,
            external_outcomes=external_outcomes,
            message=f"converged — push skipped (--no-push){note}",
        )
    try:
        pushed_sha = push_to_pr_ref(
            result.worktree,
            result.branch,
            change.head_branch,
            expected_remote_sha=change.head_sha,
        )
    except MergeRaceDetected as exc:
        return ConvergeResult(
            status="merge_race",
            change=change,
            develop_result=result,
            fixer_commits=fixer_commits,
            intake_cost_usd=pre_loop_cost,
            intake_deferred=intake_deferred,
            external_outcomes=external_outcomes,
            message=str(exc),
        )
    except ForkPushUnsupported as exc:  # defensive — forks are guarded pre-loop
        return ConvergeResult(
            status="fork_unsupported",
            change=change,
            develop_result=result,
            fixer_commits=fixer_commits,
            intake_cost_usd=pre_loop_cost,
            intake_deferred=intake_deferred,
            external_outcomes=external_outcomes,
            message=str(exc),
        )
    logger.info(
        "converge %s: pushed %s -> %s",
        config.run_id,
        pushed_sha[:12],
        change.head_branch,
    )
    return ConvergeResult(
        status="converged",
        change=change,
        develop_result=result,
        fixer_commits=fixer_commits,
        pushed=True,
        pushed_sha=pushed_sha,
        intake_cost_usd=pre_loop_cost,
        intake_deferred=intake_deferred,
        external_outcomes=external_outcomes,
        message=f"converged and pushed to {change.head_branch}{note}",
    )
