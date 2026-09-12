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
:func:`agent_session.turn_with_reactions`; S6 grew this module into the
round/phase pipeline. :class:`RoundContext` is the explicit successor of
``develop()``'s locals bag; each phase function ``(ctx, round_no) -> CycleExit |
None`` maps 1:1 onto a phase of a develop round and returns a :class:`CycleExit`
at exactly one site per terminal condition (replacing the old status-assignment
+ ``break`` pairs); :func:`run_round` sequences them. ``develop()`` shrinks to
validation → setup → ``for round: run_round`` → epilogue.

To keep the pipeline a leaf that imports neither ``panel`` (which imports
``Services`` from here) nor ``agent_session`` (ditto) nor ``develop`` — no import
cycle — the boundary collaborators (``run_panel_round``,
``turn_with_reactions``, ``resume_after_from`` and the coder-side prompt
helpers) are **injected** onto :class:`RoundContext` by ``develop()`` from its own
module globals. That also keeps the ``develop_mod``-level ``monkeypatch`` targets
(``run_panel_round`` / ``_run_check_set`` via :class:`Services` / ``run_turn`` /
``_sleep``) live without any test change.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ...runner import git
from . import (
    autoformat,
    check_artifacts,
    check_runner,
    coder_salvage,
    containers,
    engines,
    handoff,
    turns,
)
from .check_set import Check, CheckSetResult, render_check_summary
from .config import HANDOFF_DIRNAME, DevelopConfig
from .gate_findings import GateLedger
from .handoff import max_severity, render_prompt
from .sandbox_facts import for_prompt as _sandbox_section
from .test_gate import GateResult
from .turns import TurnAttempt, TurnResult

if TYPE_CHECKING:
    from .agent_session import PauseBudget
    from .panel import PanelRoundResult, ReviewerState, ReviewOutcome

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Services:
    """The side-effecting seams the round pipeline depends on, injected so the
    loop is testable with fakes (ARCH-1.S4).

    ``run_turn`` and ``sleep`` are consumed by
    :func:`agent_session.turn_with_reactions` today; ``start_container`` /
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
    base: git.RangeBase
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
    turn_with_reactions: Callable[..., TurnAttempt]
    run_panel_round: Callable[..., PanelRoundResult]
    resume_after_from: Callable[[TurnResult | None], datetime]
    render_panel_findings: Callable[[list[ReviewOutcome]], str]
    coder_summary: Callable[[DevelopConfig, int], str]
    record_coder_disputes: Callable[[DevelopConfig, list[ReviewerState], int], None]
    coder_handoff_nudge: Callable[[int], str]
    # --- converge entry (both None on the story-develop path) ---
    # When set (converge / ADR 0003 §9 Shape 1), round 1 is a cold-start fix of an
    # existing PR: the coder gets converge_coder_init.md seeded from the intake
    # review + the PR's own commit log instead of coder_init.md. See LoopEntry.
    intake_reviews: list[ReviewOutcome] | None = None
    intake_check_set: CheckSetResult | None = None
    # extra block appended to the converge round-1 coder prompt — external
    # mode's per-id acknowledgement contract (PR #345 re-review 1); empty on
    # the local-panel path.
    external_ack: str = ""
    # conflict resolution (PRD S5) — see LoopEntry
    coder_init_template: str = "converge_coder_init.md"
    coder_init_extra: Mapping[str, str] = field(default_factory=dict)
    pre_commit_guard: Callable[[Path], str | None] | None = None
    review_context: str = ""
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
    # 793edc9f: set when approval was held because no capture from the current
    # tree exists (stale/failed re-capture); appended to the next coder
    # prompt's gate summary so the loop can fix the capture instead of
    # stalling silently. Cleared once delivered.
    artifact_capture_notice: str | None = None


def _combine_review_outcomes(
    regular: list[ReviewOutcome], artifact: list[ReviewOutcome]
) -> list[ReviewOutcome]:
    """Merge a reviewer's regular verdict with its artifact-pass verdict.

    #291 round 4: a specialized pass must never REPLACE the round's full
    assessment — the regular review's findings are retained, the pass's new
    visual findings append, ``passed`` is the conjunction of both verdicts, and
    status / max_severity re-derive from the combined findings. A reviewer the
    pass never reached (interrupted panel) keeps its regular outcome.
    """
    by_name = {o.reviewer: o for o in artifact}
    combined: list[ReviewOutcome] = []
    for reg in regular:
        art = by_name.get(reg.reviewer)
        if art is None:
            combined.append(reg)
            continue
        findings = list(reg.findings) + list(art.findings)
        # open findings only — max_severity mirrors ReviewOutcome's normal
        # derivation; a resolved major must not headline the final outcome.
        severities = [f.severity for f in findings if f.is_open]
        combined.append(
            dataclasses.replace(
                reg,
                status="LGTM" if not findings else "FINDINGS",
                passed=reg.passed and art.passed,
                max_severity=max_severity(severities),
                findings=findings,
                cost_usd=reg.cost_usd + art.cost_usd,
            )
        )
    return combined


def round1_coder_prompt(ctx: RoundContext) -> str:
    """The cold-start coder prompt: story-develop's ``coder_init.md``, or — on a
    converge entry (``intake_reviews`` set) — the entry's template seeded from
    the intake review + the PR's own commit log so the coder reconstructs
    intent before changing anything (ADR 0003 §9 Shape 1); a conflict
    resolution (PRD S5) swaps the template and adds its brief as extra slots."""
    config = ctx.config
    if ctx.intake_reviews is not None:
        return render_prompt(
            handoff.load_prompt(ctx.coder_init_template),
            acceptance_criteria=config.effective_acceptance_criteria,
            commit_log=(
                git.log_between(ctx.wt, git.fork_point(ctx.wt, ctx.base))
                or "(no commits in range)"
            ),
            findings=ctx.render_panel_findings(ctx.intake_reviews),
            gate_summary=render_check_summary(
                ctx.intake_check_set, for_coder=True, gate_ledger=ctx.gate_ledger
            ),
            handoff_file=handoff.coder_handoff_name(1),
            sandbox_facts=_sandbox_section(config.image, for_coder=True),
            external_ack=ctx.external_ack,
            **ctx.coder_init_extra,
        )
    # T8: an EXPLICIT acceptance criteria (flag / task metadata) gets its own
    # section; when it merely falls back to the description, repeating it
    # would be noise.
    ac_section = (
        f"\n## Acceptance criteria\n\n{config.acceptance_criteria}\n"
        if config.acceptance_criteria
        else ""
    )
    return render_prompt(
        handoff.load_prompt("coder_init.md"),
        description=config.description,
        acceptance_criteria_section=ac_section,
        handoff_file=handoff.coder_handoff_name(1),
        sandbox_facts=_sandbox_section(config.image, for_coder=True),
    )


def coder_phase(ctx: RoundContext, round_no: int) -> CycleExit | None:
    """Build the coder prompt, run its turn through the reaction wrapper,
    nudge a missing handoff once (#114), and gate the round on a clean turn +
    a written handoff — or on a handoff the dying turn itself wrote
    (:mod:`coder_salvage`, slice B).

    Exits: A ``interrupted`` (pause budget exhausted), B ``failed`` (turn failed
    or no handoff), B' ``infra_failed`` (a retry-class failure persisted).
    """
    config = ctx.config
    if round_no == 1:
        coder_prompt = round1_coder_prompt(ctx)
        coder_resume = False
    else:
        assert ctx.final_reviews  # set by the prior round's reviews
        # #291: when an artifact pass drove the continuation, its handoff is
        # the authoritative write-up — reference it alongside the regular one.
        ref_names: list[str] = []
        for n in ctx.names:
            ref_names.append(handoff.reviewer_handoff_name(round_no - 1, n))
            art = handoff.reviewer_handoff_name(round_no - 1, f"{n}_artifacts")
            if (ctx.config.handoff_dir / art).is_file():
                ref_names.append(art)
        review_files = ", ".join(f"`{n}`" for n in ref_names)
        gate_summary_value = render_check_summary(
            ctx.check_set, for_coder=True, gate_ledger=ctx.gate_ledger
        )
        if ctx.artifact_capture_notice:
            # 793edc9f: approval was held on stale captures last round — tell
            # the coder in the gate slot (no template change) so it can fix
            # the capture instead of guessing why nothing sealed.
            gate_summary_value = (
                f"{gate_summary_value}\n\n{ctx.artifact_capture_notice}"
                if gate_summary_value
                else ctx.artifact_capture_notice
            )
            ctx.artifact_capture_notice = None
        coder_prompt = render_prompt(
            handoff.load_prompt("coder_fix.md"),
            round_no=str(round_no),
            acceptance_criteria=config.effective_acceptance_criteria,
            findings=ctx.render_panel_findings(ctx.final_reviews),
            gate_summary=gate_summary_value,
            review_files=review_files,
            handoff_file=handoff.coder_handoff_name(round_no),
            sandbox_facts=_sandbox_section(config.image, for_coder=True),
        )
        coder_resume = True

    done_path = config.handoff_dir / handoff.coder_handoff_name(round_no)
    pre_turn = handoff.file_fingerprint(done_path)  # salvage provenance
    attempt = ctx.turn_with_reactions(
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
    ctx.coder_cost += attempt.cost
    # Codex mints its session handle (thread_id) on turn 1; reuse the returned
    # handle for resumes + persist it (no-op for claude, which echoes the
    # supplied uuid). Drives daemon-resume + PR delivery.
    if attempt.turn.session_id:
        ctx.coder_session = attempt.turn.session_id
    # Salvage nudge (#114): a clean turn that left work but no handoff is
    # re-prompted once; the nudge's OWN outcome then judges the round.
    if (
        not attempt.interrupted
        and attempt.turn.succeeded
        and not done_path.is_file()
        and git.has_uncommitted_changes(ctx.wt)
    ):
        attempt = coder_salvage.nudge_for_handoff(ctx, round_no)
    if attempt.interrupted:
        return CycleExit(
            status="interrupted",
            failure_reason=(
                f"round {round_no}: coder usage-limited; pause budget exhausted"
            ),
            resume_after=ctx.resume_after_from(attempt.turn),
        )
    outcome = coder_salvage.verdict(config.run_id, attempt, done_path, pre_turn)
    if outcome is None:
        return None
    status, reason = outcome
    return CycleExit(
        status=status, failure_reason=f"round {round_no}: {reason}", resume_after=None
    )


def dispute_phase(ctx: RoundContext, round_no: int) -> CycleExit | None:
    """T7: record the coder's dispute marks from its handoff (round >= 2).

    Tolerant — an unparseable coder handoff records nothing. Never terminal.
    """
    if round_no >= 2:
        ctx.record_coder_disputes(ctx.config, ctx.reviewers, round_no)
    return None


def commit_round(wt: Path, message: str) -> str | None:
    """Commit a round's work as one commit, excluding the handoff dir.

    The single shared commit primitive (ARCH-1.S7): both ``develop()``'s
    :func:`commit_phase` calls it, so the
    handoff-dir exclusion is single-sourced on :data:`HANDOFF_DIRNAME` rather than
    drifting as a bare ``".handoff"`` literal on the delivery side. Returns the new
    commit SHA, or ``None`` when nothing (outside the handoff dir) was staged.
    """
    return git.commit_all(wt, message, exclude=[HANDOFF_DIRNAME])


def commit_phase(ctx: RoundContext, round_no: int) -> CycleExit | None:
    """Commit the round's work (excluding the handoff dir) and auto-format it in
    place (#134). Sets ``ctx.new_commit`` / ``ctx.gated_sha``.

    Exit: C ``failed`` (round 1 produced no commit, or the entry's pre-commit
    guard refused the tree — PRD S5: conflict markers left behind).
    """
    if ctx.pre_commit_guard is not None:
        refused = ctx.pre_commit_guard(ctx.wt)
        if refused:
            ctx.new_commit = None
            return CycleExit(
                status="failed",
                failure_reason=f"round {round_no}: {refused}",
                resume_after=None,
            )
    new_commit = commit_round(
        ctx.wt, f"story-develop r{round_no}: {ctx.config.description}"
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

    Exits: E ``interrupted``, F ``failed`` (invalid reviewer handoff), F'
    ``infra_failed`` (a reviewer's retry-class failure persisted, slice B).
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
        review_context=ctx.review_context,
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
    if panel.infra_failure is not None:
        return CycleExit(
            status="infra_failed",
            failure_reason=f"round {round_no}: {panel.infra_failure}",
            resume_after=None,
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
        # #283 (PR #291 review): the panel ran BEFORE the candidate checks, so
        # anything they collect (e2e screenshots) would otherwise seal unseen.
        # Snapshot the artifacts view the panel saw; if the candidate run
        # changes it, approval is held for one panel-only artifact pass below.
        artifacts_seen_by_panel = check_artifacts.render_artifacts_note(config)
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
        elif _hold_for_stale_captures(ctx, round_no):
            # 793edc9f: captures exist but none is from the tree under review
            # (the re-capture was skipped, errored, or produced nothing).
            # Sealing here would let reviewers' "fixed" verdicts rest on an
            # earlier round's pixels — a silent false-verify. Hold approval;
            # the next coder round carries the notice.
            return None
        else:
            exit_ = _artifact_review_pass(ctx, round_no, artifacts_seen_by_panel)
            if exit_ is not None:
                return exit_
            if not all(r.passed for r in ctx.final_reviews):
                # the artifact pass filed findings — the normal fix loop
                # continues; the coder answers them next round.
                return None
            return CycleExit(status="approved", failure_reason="", resume_after=None)
    return None


def _hold_for_stale_captures(ctx: RoundContext, round_no: int) -> bool:
    """793edc9f: hold approval when the artifact evidence does not describe
    the tree under review — see :func:`check_artifacts.stale_capture_hold`.
    Queues the coder-facing notice for the next round's gate summary."""
    notice = check_artifacts.stale_capture_hold(
        ctx.config,
        gated_sha=ctx.gated_sha,
        check_results=ctx.check_set.results if ctx.check_set is not None else (),
        round_no=round_no,
    )
    if notice is None:
        return False
    ctx.artifact_capture_notice = notice
    return True


def _artifact_review_pass(
    ctx: RoundContext, round_no: int, artifacts_seen_by_panel: str
) -> CycleExit | None:
    """Hold approval until a reviewer has seen candidate-collected artifacts.

    #283 / PR #291 review (High): candidate-stage checks run AFTER the panel and
    a green candidate sealed immediately — so on the success path the rendered-
    page screenshots lens's parity ``make e2e`` collects were never reviewed.
    When this round's candidate run changed the artifacts view (vs what the
    panel's prompts contained), run ONE panel-only pass (``artifact_pass=True``:
    the ``reviewer_artifacts.md`` prompt, resumed sessions, its own handoff
    files) before sealing. The tree is unchanged — the pass costs one reviewer
    turn each, never re-runs checks, and cannot loop: a pass that LGTMs seals
    this round; a pass that files findings feeds the ordinary fix loop, whose
    next candidate sha re-triggers collection afresh.

    Updates ``ctx.final_reviews`` (the pass IS the panel's final word this
    round) and mirrors ``panel_phase``'s interrupted/invalid exits.
    """
    config = ctx.config
    artifacts_now = check_artifacts.render_artifacts_note(config)
    if not artifacts_now or artifacts_now == artifacts_seen_by_panel:
        return None
    logger.info(
        "story-develop %s: round %d candidate checks collected new artifacts; "
        "holding approval for an artifact-review panel pass",
        config.run_id,
        round_no,
    )
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
        coder_summary="",
        services=ctx.services,
        artifact_pass=True,
        review_context=ctx.review_context,
    )
    ctx.review_cost += panel.cost
    # #291 round 4: COMBINE each reviewer's regular and artifact outcomes —
    # replacing final_reviews with the pass's outcomes made the regular
    # review's surviving non-blocking findings vanish from DevelopResult /
    # state.json metadata (the ledger kept them; the structured outcome lied).
    ctx.final_reviews = _combine_review_outcomes(ctx.final_reviews, panel.round_reviews)
    if panel.interrupted:
        return CycleExit(
            status="interrupted",
            failure_reason=(
                f"round {round_no}: reviewer usage-limited during the "
                "artifact-review pass; pause budget exhausted"
            ),
            resume_after=panel.resume_after,
        )
    if panel.invalid_reviewer is not None:
        return CycleExit(
            status="failed",
            failure_reason=(
                f"round {round_no}: reviewer [{panel.invalid_reviewer}] handoff "
                "invalid during the artifact-review pass"
            ),
            resume_after=None,
        )
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
