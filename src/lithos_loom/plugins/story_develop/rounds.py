"""The develop round pipeline's shared injection seam (ARCH-1.S4).

:class:`Services` is the frozen bundle of side-effecting seams the round
machinery calls through instead of reaching module globals directly, so the loop
is unit-testable by constructing a ``Services`` with fakes.

:meth:`Services.live` wires the real module callables — captured when it is
built. Both it and ``develop()``'s own ``_develop_services()`` are constructed at
``develop()`` start, *after* any test applies its ``monkeypatch.setattr`` of
``turns.run_turn`` / ``containers.start_container`` / ``develop_mod.run_turn`` / … ,
so each field binds the patched callable (a patch applied *after* construction is
not observed — nothing does that). ``develop()`` does *not* use ``live()`` yet —
it builds a ``Services`` from its own module globals so the existing
``monkeypatch.setattr(develop_mod, "run_turn"/"_sleep"/…)`` patches keep taking
effect until S8 re-points the tests (see the compat note in :mod:`develop`).

S4 introduced the seam and threaded it through
:func:`agent_session.turn_with_limit_pauses`; S6 grew this module into the
round/phase pipeline. :class:`RoundContext` is the explicit successor of
``develop()``'s locals bag; each phase function ``(ctx, round_no) -> CycleExit |
None`` maps 1:1 onto a phase of a develop round and returns a :class:`CycleExit`
at exactly one site per terminal condition (replacing the old status-assignment
+ ``break`` pairs); :func:`run_round` sequences them. ``develop()`` shrinks to
validation → setup → ``for round: run_round`` → epilogue.

To keep the pipeline a leaf that imports neither ``panel`` (which imports
``Services`` from here) nor ``agent_session`` (ditto) nor ``develop`` — no import
cycle — the boundary collaborators (``run_panel_round``,
``turn_with_limit_pauses``, ``resume_after_from`` and the coder-side prompt
helpers) are **injected** onto :class:`RoundContext` by ``develop()`` from its own
module globals. That also keeps the ``develop_mod``-level ``monkeypatch`` targets
(``run_panel_round`` / ``_run_check_set`` via :class:`Services` / ``run_turn`` /
``_sleep``) live without any test change.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ...runner import git
from . import autoformat, check_runner, containers, engines, handoff, turns
from .check_set import Check, CheckSetResult, render_check_summary
from .config import HANDOFF_DIRNAME, DevelopConfig
from .gate_findings import GateLedger
from .handoff import render_prompt
from .test_gate import GateResult
from .turns import TurnResult

if TYPE_CHECKING:
    from .agent_session import PauseBudget
    from .panel import PanelRoundResult, ReviewerState, ReviewOutcome

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Services:
    """The side-effecting seams the round pipeline depends on, injected so the
    loop is testable with fakes (ARCH-1.S4).

    ``run_turn`` and ``sleep`` are consumed by
    :func:`agent_session.turn_with_limit_pauses` today; ``start_container`` /
    ``stop_container`` / ``run_check_set`` are wired now for the S6 phase
    pipeline.
    """

    run_turn: Callable[..., TurnResult]
    sleep: Callable[[float], None]
    start_container: Callable[[Sequence[str]], str]
    stop_container: Callable[[str], None]
    run_check_set: Callable[..., CheckSetResult | None]

    @classmethod
    def live(cls) -> Services:
        """The concrete production seams — the real module callables. Built at
        ``develop()`` start (once S8 switches to it), *after* any test patch of
        ``turns.run_turn`` / ``containers.*`` is applied, so each field captures
        the patched callable."""
        return cls(
            run_turn=turns.run_turn,
            sleep=time.sleep,
            start_container=containers.start_container,
            stop_container=containers.stop_container,
            run_check_set=check_runner.run_check_set,
        )


# --- the round pipeline (ARCH-1.S6) -----------------------------------------


@dataclass(frozen=True)
class CycleExit:
    """A terminal outcome of the develop loop.

    Every terminal condition constructs exactly one of these — the successor of
    ``develop()``'s old ``status = "…"`` / ``failure_reason = …`` / ``break``
    triples. ``failure_reason`` is the empty string for the self-describing
    statuses (``approved`` / ``max_rounds``); ``resume_after`` is set only for
    ``interrupted`` (the T10 daemon re-dispatch surface).
    """

    status: str
    failure_reason: str
    resume_after: datetime | None


@dataclass
class RoundContext:
    """The explicit successor of ``develop()``'s locals bag (ARCH-1.S6).

    Carries the per-run inputs, the injected boundary collaborators (see the
    module docstring — bound from ``develop()``'s own globals to avoid an import
    cycle and keep the ``develop_mod`` monkeypatch targets live), and the mutable
    run state the phases thread across a round and across rounds. ``new_commit``
    is the one genuinely round-scoped field — ``commit_phase`` sets it fresh each
    round and ``fast_gate_phase`` / ``stall_phase`` read it.
    """

    # --- per-run inputs ---
    config: DevelopConfig
    wt: Path
    base: str
    names: list[str]
    services: Services
    reviewers: list[ReviewerState]
    coder_container: str
    coder_engine: engines.Engine
    coder_timeout: int
    reviewer_timeout: int
    fast_checks: tuple[Check, ...]
    candidate_checks: tuple[Check, ...]
    formatters: list[str]
    gate_ledger: GateLedger
    budget: PauseBudget
    coder_session: str
    # --- injected boundary collaborators (from develop's own module globals) ---
    turn_with_limit_pauses: Callable[..., tuple[TurnResult, bool, float]]
    run_panel_round: Callable[..., PanelRoundResult]
    resume_after_from: Callable[[TurnResult | None], datetime]
    render_panel_findings: Callable[[list[ReviewOutcome]], str]
    coder_summary: Callable[[DevelopConfig, int], str]
    record_coder_disputes: Callable[[DevelopConfig, list[ReviewerState], int], None]
    coder_handoff_nudge: Callable[[int], str]
    # --- mutable run state (read by develop()'s epilogue after the loop) ---
    coder_cost: float = 0.0
    review_cost: float = 0.0
    check_set: CheckSetResult | None = None
    gate: GateResult | None = None
    gated_sha: str | None = None
    candidate_ran_for_sha: str | None = None
    stall_strikes: int = 0
    prev_signature: frozenset | None = None
    final_reviews: list[ReviewOutcome] = field(default_factory=list)
    new_commit: str | None = None  # round-scoped: set by commit_phase
    rounds_completed: int = 0


def coder_phase(ctx: RoundContext, round_no: int) -> CycleExit | None:
    """Build the coder prompt, run its (limit-paused) turn, salvage a missing
    handoff once (#114), and gate the round on a clean turn + a written handoff.

    Exits: A ``interrupted`` (pause budget exhausted), B ``failed`` (turn failed
    or no handoff).
    """
    config = ctx.config
    if round_no == 1:
        # T8: an EXPLICIT acceptance criteria (flag / task metadata) gets its own
        # section; when it merely falls back to the description, repeating it
        # would be noise.
        ac_section = (
            f"\n## Acceptance criteria\n\n{config.acceptance_criteria}\n"
            if config.acceptance_criteria
            else ""
        )
        coder_prompt = render_prompt(
            handoff.load_prompt("coder_init.md"),
            description=config.description,
            acceptance_criteria_section=ac_section,
            handoff_file=handoff.coder_handoff_name(1),
        )
        coder_resume = False
    else:
        assert ctx.final_reviews  # set by the prior round's reviews
        review_files = ", ".join(
            f"`{handoff.reviewer_handoff_name(round_no - 1, n)}`" for n in ctx.names
        )
        coder_prompt = render_prompt(
            handoff.load_prompt("coder_fix.md"),
            round_no=str(round_no),
            acceptance_criteria=config.effective_acceptance_criteria,
            findings=ctx.render_panel_findings(ctx.final_reviews),
            gate_summary=render_check_summary(
                ctx.check_set, for_coder=True, gate_ledger=ctx.gate_ledger
            ),
            review_files=review_files,
            handoff_file=handoff.coder_handoff_name(round_no),
        )
        coder_resume = True

    coder_turn, coder_interrupted, attempt_cost = ctx.turn_with_limit_pauses(
        config,
        ctx.budget,
        services=ctx.services,
        agent="coder",
        container=ctx.coder_container,
        config_dir=config.coder_config_dir,
        prompt=coder_prompt,
        session_id=ctx.coder_session,
        resume=coder_resume,
        round_no=round_no,
        timeout=ctx.coder_timeout,
        engine=ctx.coder_engine,
    )
    ctx.coder_cost += attempt_cost
    # Codex mints its session handle (thread_id) on turn 1; reuse the returned
    # handle for resumes + persist it (no-op for claude, which echoes the
    # supplied uuid). Drives daemon-resume + PR delivery.
    if coder_turn.session_id:
        ctx.coder_session = coder_turn.session_id
    if coder_interrupted:
        return CycleExit(
            status="interrupted",
            failure_reason=(
                f"round {round_no}: coder usage-limited; pause budget exhausted"
            ),
            resume_after=ctx.resume_after_from(coder_turn),
        )
    done_present = (config.handoff_dir / handoff.coder_handoff_name(round_no)).is_file()
    # The turn whose success gates the handoff for this round. The salvage nudge
    # (below) replaces it, so a re-prompt is judged on the NUDGE's own outcome —
    # a nudge that writes the file but then exits failed/non-zero is not a clean
    # recovery.
    handoff_turn = coder_turn
    # Salvage (lithos-loom#114): the coder ended its turn cleanly and left work in
    # the worktree but never wrote its handoff (classic case: it backgrounded a
    # slow suite and stopped before the handoff step). The implementation is done;
    # only the required breadcrumb is missing. Re-prompt once to write it before
    # failing. Only for a clean turn (a crashed/errored turn can't be resumed) and
    # only when there is uncommitted work to save (else a nudge is wasted);
    # between rounds the worktree is clean, so the flag reflects this round.
    if (
        coder_turn.succeeded
        and not done_present
        and git.has_uncommitted_changes(ctx.wt)
    ):
        logger.warning(
            "story-develop %s: round %d coder ended its turn with uncommitted "
            "changes but no handoff — re-prompting once to write it",
            config.run_id,
            round_no,
        )
        handoff_turn = ctx.services.run_turn(
            container=ctx.coder_container,
            prompt=ctx.coder_handoff_nudge(round_no),
            session_id=ctx.coder_session,
            resume=True,
            timeout=ctx.coder_timeout,
            engine=ctx.coder_engine,
            model=config.coder_model,
            effort=config.coder_effort,
        )
        ctx.coder_cost += handoff_turn.cost_usd
        if handoff_turn.session_id:
            ctx.coder_session = handoff_turn.session_id
        done_present = (
            config.handoff_dir / handoff.coder_handoff_name(round_no)
        ).is_file()
    if not (handoff_turn.succeeded and done_present):
        reasons = []
        if not handoff_turn.succeeded:
            reasons.append(f"coder turn failed (exit {handoff_turn.exit_code})")
        if not done_present:
            reasons.append("no coder handoff file")
        return CycleExit(
            status="failed",
            failure_reason=f"round {round_no}: " + "; ".join(reasons),
            resume_after=None,
        )
    return None


def dispute_phase(ctx: RoundContext, round_no: int) -> CycleExit | None:
    """T7: record the coder's dispute marks from its handoff (round >= 2).

    Tolerant — an unparseable coder handoff records nothing. Never terminal.
    """
    if round_no >= 2:
        ctx.record_coder_disputes(ctx.config, ctx.reviewers, round_no)
    return None


def commit_phase(ctx: RoundContext, round_no: int) -> CycleExit | None:
    """Commit the round's work (excluding the handoff dir) and auto-format it in
    place (#134). Sets ``ctx.new_commit`` / ``ctx.gated_sha``.

    Exit: C ``failed`` (round 1 produced no commit).
    """
    new_commit = git.commit_all(
        ctx.wt,
        f"story-develop r{round_no}: {ctx.config.description}",
        exclude=[HANDOFF_DIRNAME],
    )
    if round_no == 1 and new_commit is None:
        return CycleExit(
            status="failed",
            failure_reason="round 1: coder produced no commit",
            resume_after=None,
        )
    if new_commit is not None:
        # #134/ADR §4: auto-format the round's commit BEFORE the gate + panel. The
        # formatter rewrites source in place; any change is a SEPARATE commit whose
        # SHA supersedes new_commit, so the gate runs on — and the reviewers review
        # — that exact formatted tree. Best-effort: a no-op leaves new_commit as is.
        format_sha = autoformat.run_format_pass(
            ctx.config, ctx.wt, round_no, ctx.formatters
        )
        if format_sha is not None:
            new_commit = format_sha
        # Track the latest committed tree so the approval-candidate gate (#140)
        # can run candidate-staged checks against it even on a later round that
        # produced no fresh commit.
        ctx.gated_sha = new_commit
    ctx.new_commit = new_commit
    return None


def cost_ceiling_phase(
    ctx: RoundContext, round_no: int, *, when: str
) -> CycleExit | None:
    """T7 cost ceiling. Called TWICE per round — ``when="pre_review"`` (before
    spending on reviews) and ``when="post_review"`` (after). The two calls are
    kept separate on purpose: approval (:func:`approval_phase`) runs between them
    and deliberately takes precedence when both an approval and the ceiling land
    in the same round.

    Exit: D / J ``cost_exceeded``.
    """
    config = ctx.config
    if (
        config.max_cost_usd is not None
        and ctx.coder_cost + ctx.review_cost >= config.max_cost_usd
    ):
        return CycleExit(
            status="cost_exceeded",
            failure_reason=(
                f"round {round_no}: cost ceiling reached "
                f"(${ctx.coder_cost + ctx.review_cost:.2f} >= "
                f"${config.max_cost_usd:.2f})"
            ),
            resume_after=None,
        )
    return None


def fast_gate_phase(ctx: RoundContext, round_no: int) -> CycleExit | None:
    """#140/ADR §4: run the FAST deterministic checks on the round's new commit
    (candidate-staged checks are deferred to :func:`approval_phase`). Never
    terminal."""
    if ctx.fast_checks and ctx.new_commit is not None:
        # Overwrite unconditionally: on a gate infra error this clears to None
        # rather than letting a PRIOR commit's result (e.g. a stale RED) stand in
        # for this commit. A round with no new commit keeps the prior result — the
        # tree is unchanged, so it still describes HEAD.
        check_set = ctx.services.run_check_set(
            ctx.config,
            ctx.wt,
            ctx.new_commit,
            round_no,
            ctx.fast_checks,
            ctx.gate_ledger,
        )
        ctx.check_set = check_set
        ctx.gate = check_set.test_gate if check_set is not None else None
        check_runner.persist_gate_ledger(ctx.config, ctx.gate_ledger)
    return None


def panel_phase(ctx: RoundContext, round_no: int) -> CycleExit | None:
    """Run the reviewer panel — the one shared primitive (#154). Sets
    ``ctx.final_reviews`` / accrues ``ctx.review_cost``.

    Exits: E ``interrupted``, F ``failed`` (invalid reviewer handoff).
    """
    config = ctx.config
    panel = ctx.run_panel_round(
        config,
        ctx.reviewers,
        wt=ctx.wt,
        base=ctx.base,
        round_no=round_no,
        check_set=ctx.check_set,
        gate_ledger=ctx.gate_ledger,
        budget=ctx.budget,
        reviewer_timeout=ctx.reviewer_timeout,
        coder_summary=ctx.coder_summary(config, 1) if round_no == 1 else "",
        services=ctx.services,
    )
    ctx.review_cost += panel.cost
    ctx.final_reviews = panel.round_reviews
    if panel.interrupted:
        return CycleExit(
            status="interrupted",
            failure_reason=(
                f"round {round_no}: reviewer usage-limited; pause budget exhausted"
            ),
            resume_after=panel.resume_after,
        )
    if panel.invalid_reviewer is not None:
        return CycleExit(
            status="failed",
            failure_reason=(
                f"round {round_no}: reviewer [{panel.invalid_reviewer}] handoff invalid"
            ),
            resume_after=None,
        )
    return None


def approval_phase(ctx: RoundContext, round_no: int) -> CycleExit | None:
    """Seal approval when ALL reviewers pass their OWN threshold this round (PRD
    #7). Runs the expensive candidate-staged checks once per committed tree (#140)
    and holds approval while a *required* check blocks (floor). Approval takes
    precedence over the same-round cost ceiling (the spend already happened).

    Exit: G ``approved``.
    """
    config = ctx.config
    if all(r.passed for r in ctx.final_reviews):
        # #140/ADR §4: the approval candidate — run the expensive candidate-staged
        # checks once on this tree before sealing approval. A *required* candidate
        # (e.g. thorough's dep-audit) blocks via gate_floor_blocks below, so its
        # findings merge into check_set + the ledger + the [DevelopResult] and, when
        # it blocks, hold approval so a later round surfaces them. Dedup on the sha.
        if (
            ctx.candidate_checks
            and ctx.gated_sha is not None
            and ctx.candidate_ran_for_sha != ctx.gated_sha
        ):
            ctx.candidate_ran_for_sha = ctx.gated_sha
            candidate_set = ctx.services.run_check_set(
                config,
                ctx.wt,
                ctx.gated_sha,
                round_no,
                ctx.candidate_checks,
                ctx.gate_ledger,
            )
            ctx.check_set = check_runner.merge_check_sets(ctx.check_set, candidate_set)
            ctx.gate = ctx.check_set.test_gate if ctx.check_set is not None else None
            check_runner.persist_gate_ledger(config, ctx.gate_ledger)
        # #140 floor: a *required* check blocks approval — its verdict read from
        # the ledger severity for adapter tools, the raw exit otherwise
        # (informational checks never block, even if RED).
        if check_runner.gate_floor_blocks(ctx.check_set, ctx.gate_ledger):
            logger.info(
                "story-develop %s: round %d reviews passed but a required check "
                "blocks approval; continuing",
                config.run_id,
                round_no,
            )
        else:
            return CycleExit(status="approved", failure_reason="", resume_after=None)
    return None


def deadlock_phase(ctx: RoundContext, round_no: int) -> CycleExit | None:
    """T7 dispute escalation: a coder-disputed finding the reviewer kept blocking
    for 2 consecutive rounds stops the run with a human breadcrumb rather than
    grinding to max_rounds.

    Exit: H ``disputed``.
    """
    deadlocked = [
        f"{r.spec.name}/{fid}"
        for r in ctx.reviewers
        for fid in r.ledger.disputed_deadlocks(r.spec.block_threshold)
    ]
    if deadlocked:
        logger.warning(
            "[ReviewDispute] story-develop %s: round %d dispute deadlock on %s — "
            "stopping for human review",
            ctx.config.run_id,
            round_no,
            ", ".join(deadlocked),
        )
        return CycleExit(
            status="disputed",
            failure_reason=(
                f"round {round_no}: dispute deadlock on "
                f"{', '.join(deadlocked)} (coder disputes, reviewer keeps blocking)"
            ),
            resume_after=None,
        )
    return None


def stall_phase(ctx: RoundContext, round_no: int) -> CycleExit | None:
    """T7 stall guard, keyed off finding IDENTITY: an empty round commit or an
    unchanged blocking set, two rounds running, stops the run.

    Exit: I ``stalled``.
    """
    signature = frozenset(
        (r.spec.name, fid, fstatus)
        for r in ctx.reviewers
        for fid, fstatus in r.ledger.blocking_signature(r.spec.block_threshold)
    )
    if round_no >= 2 and (ctx.new_commit is None or signature == ctx.prev_signature):
        ctx.stall_strikes += 1
    else:
        ctx.stall_strikes = 0
    ctx.prev_signature = signature
    if ctx.stall_strikes >= 2:
        return CycleExit(
            status="stalled",
            failure_reason=f"round {round_no}: stalled — "
            + (
                "no new commit and/or blocking findings unchanged "
                "across 2 consecutive rounds"
            ),
            resume_after=None,
        )
    return None


def run_round(ctx: RoundContext, round_no: int) -> CycleExit | None:
    """Sequence one develop round's phases. Returns the first phase's
    :class:`CycleExit` (terminating the loop), or ``None`` to continue to the next
    round. The order — and the TWO ``cost_ceiling_phase`` calls straddling
    approval — is load-bearing (see :func:`cost_ceiling_phase`)."""
    ctx.rounds_completed = round_no
    phases: tuple[Callable[[RoundContext, int], CycleExit | None], ...] = (
        coder_phase,
        dispute_phase,
        commit_phase,
        lambda c, r: cost_ceiling_phase(c, r, when="pre_review"),
        fast_gate_phase,
        panel_phase,
        approval_phase,
        deadlock_phase,
        stall_phase,
        lambda c, r: cost_ceiling_phase(c, r, when="post_review"),
    )
    for phase in phases:
        exit_ = phase(ctx, round_no)
        if exit_ is not None:
            return exit_
    return None
