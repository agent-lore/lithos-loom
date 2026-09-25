"""The develop round pipeline + its shared injection seam (ARCH-1.S4/S6).

:class:`Services` is the frozen bundle of side-effecting seams the round
machinery calls through instead of module globals, so the loop is
unit-testable with fakes. Both :meth:`Services.live` and ``develop()``'s own
``_develop_services()`` are constructed at ``develop()`` start, *after* any
test's ``monkeypatch.setattr`` of ``turns.run_turn`` / ``containers.*`` /
``develop_mod.run_turn`` / …, so each field binds the patched callable.
``develop()`` builds its ``Services`` from its own module globals so the
existing ``develop_mod``-level patches keep taking effect (compat note in
:mod:`develop`).

:class:`RoundContext` is the explicit successor of ``develop()``'s locals bag;
each phase function ``(ctx, round_no) -> CycleExit | None`` maps 1:1 onto a
phase of a develop round and returns a :class:`CycleExit` at exactly one site
per terminal condition; :func:`run_round` sequences them. ``develop()`` is
validation → setup → ``for round: run_round`` → epilogue.

To keep the pipeline a leaf that imports neither ``panel`` nor
``agent_session`` (both import ``Services`` from here) nor ``develop`` — no
import cycle — the boundary collaborators (``run_panel_round``,
``turn_with_reactions``, ``resume_after_from`` and the coder-side prompt
helpers) are **injected** onto :class:`RoundContext` by ``develop()`` from its
own module globals, which also keeps those ``monkeypatch`` targets live.
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
    checkpoint,
    coder_salvage,
    containers,
    engines,
    handoff,
    turns,
)
from .check_set import Check, CheckResult, CheckSetResult, render_check_summary
from .config import HANDOFF_DIRNAME, DevelopConfig
from .findings import admitted_decisions, collect_pending_decisions
from .gate_findings import GateLedger
from .handoff import max_severity, render_prompt
from .loop_entry import PostCommitOutcome
from .sandbox_facts import for_prompt as _sandbox_section
from .test_gate import GateResult
from .turns import TurnAttempt, TurnResult

if TYPE_CHECKING:
    from .agent_session import PauseBudget
    from .panel import PanelRoundResult, ReviewerState, ReviewOutcome

logger = logging.getLogger(__name__)


def no_resync(
    container: str, engine: engines.Engine, config: DevelopConfig
) -> list[str]:
    """The ``Services.resync_auth`` default: nothing landed (fakes, and any
    constructor that does not name the seam)."""
    return []


@dataclass(frozen=True)
class Services:
    """The side-effecting seams the round pipeline depends on, injected so the
    loop is testable with fakes (ARCH-1.S4): ``run_turn`` / ``sleep`` feed
    :func:`agent_session.turn_with_reactions`, the rest the phase pipeline."""

    run_turn: Callable[..., TurnResult]
    sleep: Callable[[float], None]
    start_container: Callable[[Sequence[str]], str]
    stop_container: Callable[[str], None]
    run_check_set: Callable[..., CheckSetResult | None]
    # #403: write the host's current auth file into the container's mounted
    # inode before an `auth_failed` retry — (container, engine, config) →
    # the files that landed. Defaults to a no-op so fakes need not name it.
    resync_auth: Callable[[str, engines.Engine, DevelopConfig], list[str]] = no_resync

    @classmethod
    def live(cls) -> Services:
        """The concrete production seams — the real module callables, bound
        at ``develop()`` start (after any test patch, see the module doc)."""
        return cls(
            run_turn=turns.run_turn,
            sleep=time.sleep,
            start_container=containers.start_container,
            stop_container=containers.stop_container,
            run_check_set=check_runner.run_check_set,
            resync_auth=resync_auth_live,
        )


def resync_auth_live(
    container: str, engine: engines.Engine, config: DevelopConfig
) -> list[str]:
    """The production ``Services.resync_auth``: the engine's auth files from
    its operator dir into its config mount (#403)."""
    return containers.resync_auth_files(
        container,
        config_mount=engine.config_mount,
        auth_source_dir=engine.auth_source_dir(config),
        auth_files=engine.auth_files(config),
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
    # ``infra_failed`` only: what to fix on the host before completing the
    # gate — carried beside the reason so the gate's capped summary never
    # truncates it (slice B)
    host_action: str = ""


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
    post_commit_pass: Callable[[Path, int], PostCommitOutcome] | None = None
    review_context: str = ""
    # external mode (PR #396 review): admits a round-1 no-change claim for
    # review instead of exit C — see LoopEntry
    no_change_claim: Callable[[int], bool] | None = None
    # correctness/f-003: whether the cheap `needs-decision` escalation is live
    # this run — the story-develop path only (`develop()` sets it from
    # ``entry is None``). False keeps the escape out of the coder's prompt and,
    # via the ledgers, out of the ledger: converge records such a mark as the
    # ordinary dispute it also is.
    decisions_enabled: bool = True
    # 5dbeb0c8 slice C: the rounds / spend of a run this one RESUMES (see
    # LoopEntry.carried_*), so each round's checkpoint records the branch's
    # running totals and not just this session's.
    carried_rounds: int = 0
    carried_cost_usd: float = 0.0
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
    # round-scoped: commit_phase admitted round 1 with no commit as a
    # no-change VALIDATION pass (PR #396 review) — no_change_verdict_phase
    # then ends the run unless approval sealed it
    no_change_round: bool = False
    # PRD S4 / PR #388 review: the post-commit pass's verdict for the latest
    # committed tree, joined to the check-set by fast_gate_phase (a required
    # row — red holds approval); None until a pass reports one
    post_commit_row: CheckResult | None = None
    rounds_completed: int = 0
    # 793edc9f: set when approval was held because no capture from the current
    # tree exists (stale/failed re-capture); appended to the next coder
    # prompt's gate summary so the loop can fix the capture instead of
    # stalling silently. Cleared once delivered.
    artifact_capture_notice: str | None = None
    # 9d5ebca6: `needs-decision` marks the escalation could not carry whole
    # (labels only) — named on the operator's surfaces as NOT admitted, which
    # is what they are: ordinary disputes on this run (correctness/f-002).
    decisions_not_admitted: tuple[str, ...] = ()


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
            external_ack=ctx.external_ack,  # every round (#387); "" off external
            decision_escape=(
                handoff.load_prompt("coder_decision_escape.md")
                if ctx.decisions_enabled
                else ""
            ),
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
    # Codex mints its session handle (thread_id) on turn 1; the wrapper's
    # rebound handle (not the last turn's, which a fresh retry may leave empty)
    # is what resumes + state.json persist (no-op for claude, which echoes the
    # supplied uuid). Drives daemon-resume + PR delivery.
    ctx.coder_session = attempt.session_id or ctx.coder_session
    # Salvage nudge (#114): a clean turn that left work but no handoff is
    # re-prompted once; the nudge's OWN outcome then judges the round.
    if (
        not attempt.interrupted
        and attempt.turn.succeeded
        and not done_path.is_file()
        and git.has_uncommitted_changes(ctx.wt)
    ):
        pre_turn = handoff.file_fingerprint(done_path)  # re-snapshot per attempt
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
        status=status,
        failure_reason=f"round {round_no}: {reason}",
        resume_after=None,
        host_action=attempt.host_action if status == "infra_failed" else "",
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

    Exits: C ``failed`` (round 1 produced no commit — unless the entry's
    ``no_change_claim`` admits the empty round for review, PR #396 — or the
    entry's pre-commit guard refused the tree — PRD S5: conflict markers left
    behind), C' ``infra_failed`` (the entry's post-commit pass could not run
    — PRD S4).
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
        if ctx.no_change_claim is not None and ctx.no_change_claim(round_no):
            # PR #396 review (High): a round-1 coder that changed nothing
            # because every injected finding needs no change has made a
            # CLAIM about the PR head, and the loop's own gate + panel judge
            # it there (the external reviewer proposes, the loop gate
            # disposes) — never the coder alone. The unchanged head is the
            # gated tree; the round goes on to the checks and the panel.
            ctx.gated_sha = git.commit_sha(ctx.wt)
            ctx.new_commit = None
            ctx.no_change_round = True
            return None
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
        # PRD S4: the entry's post-commit pass (resolve mode regenerates the
        # project's generated paths) runs AFTER formatting — the generator's
        # output counts what the formatter moved — and its commit supersedes
        # the round's like the format commit does. It fails CLOSED (PR #388
        # review): its verdict is a required check row (fast_gate_phase joins
        # it), and a pass that could not run ends the round — the committed
        # tree carries the intake's copies, so silence would gate a stale one.
        if ctx.post_commit_pass is not None:
            outcome = ctx.post_commit_pass(ctx.wt, round_no)
            ctx.post_commit_row = outcome.row
            if outcome.sha is not None:
                new_commit = outcome.sha
            if outcome.infra_error:
                ctx.new_commit = ctx.gated_sha = new_commit
                return CycleExit(
                    status="infra_failed",
                    failure_reason=f"round {round_no}: {outcome.infra_error}",
                    resume_after=None,
                    host_action=outcome.host_action,
                )
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
    target = ctx.new_commit
    if target is None and round_no == 1 and ctx.check_set is None:
        # The admitted no-change round (commit_phase, PR #396 review): no
        # commit, and — in external mode — no intake check-set describing
        # the head, so the checks run on the gated head itself; the floor
        # would otherwise judge the claim on nothing.
        target = ctx.gated_sha
    if ctx.fast_checks and target is not None:
        # Overwrite unconditionally: on a gate infra error this clears to None
        # rather than letting a PRIOR commit's result (e.g. a stale RED) stand in
        # for this commit. A round with no new commit keeps the prior result — the
        # tree is unchanged, so it still describes HEAD.
        check_set = ctx.services.run_check_set(
            ctx.config,
            ctx.wt,
            target,
            round_no,
            ctx.fast_checks,
            ctx.gate_ledger,
        )
        ctx.check_set = check_set
        ctx.gate = check_set.test_gate if check_set is not None else None
        check_runner.persist_gate_ledger(ctx.config, ctx.gate_ledger)
    if ctx.new_commit is not None and ctx.post_commit_row is not None:
        # PRD S4 / PR #388 review: the post-commit pass's verdict rides with
        # this commit's check-set (replacing the prior commit's row) so the
        # floor, the coder's next prompt and the epilogue all read it.
        ctx.check_set = check_runner.with_result(ctx.check_set, ctx.post_commit_row)
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
            host_action=panel.infra_host_action,
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
    if panel.infra_failure is not None:
        return CycleExit(
            status="infra_failed",
            failure_reason=(
                f"round {round_no}: {panel.infra_failure} during the "
                "artifact-review pass"
            ),
            resume_after=None,
            host_action=panel.infra_host_action,
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


def no_change_verdict_phase(ctx: RoundContext, round_no: int) -> CycleExit | None:
    """The admitted no-change round (commit_phase, PR #396 review) is a
    VALIDATION pass, not an entry to the fix loop: approval sealed it in
    :func:`approval_phase`; reaching here means the panel rejected the
    coder's claim, or a required check is red on the unchanged head and the
    floor held. End the run with that rationale — a trigger that asked for
    nothing (an approval comment ingested as a finding, #380) must never
    drive paid rounds, nor push unrelated commits onto a delivered PR (opus
    round 2); the converge epilogue reports the claim unaddressed.

    Exit: C'' ``failed``.
    """
    if round_no != 1 or not ctx.no_change_round:
        return None
    rejections = [
        f"{r.reviewer}: {f.rationale}"
        for r in ctx.final_reviews
        for f in r.findings
        if f.is_open
    ]
    if rejections:
        why = "the panel rejected the coder's no-change claim — " + "; ".join(
            rejections
        )
    else:
        why = (
            "the panel passed the unchanged head but a required check blocks "
            "approval on it — the no-change claim is not validated"
        )
    return CycleExit(
        status="failed", failure_reason=f"round 1: {why}", resume_after=None
    )


def decision_phase(ctx: RoundContext, round_no: int) -> CycleExit | None:
    """The cheap escalation (9d5ebca6): a coder ``needs-decision`` the reviewer
    just declined to contest stops the run NOW — before another coder turn is
    paid — because the question is a product decision neither agent can settle
    by re-reading the code.

    Runs before :func:`deadlock_phase`: the ordinary dispute guard costs two
    more review rounds to reach the same human, and both observed cases
    ($26.44 / $89.41) spent them saying the same thing again. A reviewer that
    DID contest (citing the acceptance line the finding meets) left no pending
    decision here, and the deadlock guard below applies unchanged.

    The decisions are **admitted** here, before the exit: what the run claims
    is exactly what both operator surfaces carry whole (:func:`
    ~.findings.admitted_decisions`), and a mark that does not fit the
    publication budget is named as not-admitted rather than published as an id
    (correctness/f-002).

    At most ONE escalation can happen per run — this exit ends it — and the
    next one needs the operator to complete the gate first, so "how many
    decisions may a story raise" is bounded by operator consent rather than a
    counter (security/f-003); within the run, a contested decision is sticky,
    so the same finding cannot re-arm it either.

    Exit: H' ``needs_decision``.
    """
    decisions, not_admitted = admitted_decisions(
        collect_pending_decisions(
            (r.ledger, r.spec.block_threshold) for r in ctx.reviewers
        )
    )
    if not decisions:
        return None
    ctx.decisions_not_admitted = tuple(d.label for d in not_admitted)
    logger.warning(
        "[ReviewDispute] story-develop %s: round %d needs a product decision on "
        "%s — stopping before another coder turn",
        ctx.config.run_id,
        round_no,
        ", ".join(d.label for d in decisions),
    )
    if not_admitted:
        # Named, not silently dropped: these marks are NOT decisions this run
        # (correctness/f-002 — the escalation publishes what it admits, whole).
        logger.warning(
            "[ReviewDispute] story-develop %s: %s did not fit the escalation's "
            "publication budget and stay ordinary disputes",
            ctx.config.run_id,
            ", ".join(d.label for d in not_admitted),
        )
    # The reason line is LOOM-AUTHORED on purpose (security/f-002): it becomes
    # `escalation.summary`, which the needs-human notifier publishes as an
    # `@operator` comment on the story's public GitHub issue / PR and passes to
    # `notify-send`'s argv. Every other stop's summary is a loom template; the
    # coder's question must not be the first free-form agent text on that
    # channel, and it does not need to be — it reaches the operator whole on
    # the `[ReviewDispute]` finding and in the gate's brief, both Lithos-only.
    # Naming the finding(s) is what the summary is for: where to look.
    marked = ", ".join(f"{d.label} (reviewer: {d.reviewer_verdict})" for d in decisions)
    return CycleExit(
        status="needs_decision",
        failure_reason=(
            # security/f-004: the per-decision verdict token rides here too.
            # This line is `escalation_summary` — the one the `gates` CLI
            # shows and the only one reaching the GitHub `@mention` — so it is
            # the cheapest place to say whether a reviewer answered at all,
            # and it stays loom-authored (the tokens are a closed vocabulary).
            f"round {round_no}: the coder marked "
            f"{marked} "
            "needs-decision and no reviewer contested it — the question is on "
            "the story's [ReviewDispute] finding and in the gate brief"
        ),
        resume_after=None,
    )


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


def record_boundary(ctx: RoundContext, round_no: int) -> None:
    """Checkpoint what round *round_no* left on the branch (5dbeb0c8 slice C).

    The round boundary is the resumable point: whatever this round committed is
    on the branch, and the process may not live to write anything else — an
    expired credential or a vanished container kills it between here and
    ``develop()``'s epilogue. So it is recorded here, for the TERMINAL round as
    well: the round that stops the run is also the last one a resume can build
    on.

    ``carried_*`` is what a run this one resumed had already used, so the
    recorded BRANCH totals span every session of the work while the run's own
    numbers stay its own. A round that crashed mid-pipeline is recorded too (see
    :func:`run_round`): it counts as spent — the conservative direction for the
    budget — and the resume's intake falls back to the last round that was
    actually reviewed. Best-effort in both directions: reading HEAD is a git
    call inside a live loop, so a failure is logged and the round goes on — a
    missing checkpoint costs a resume, never the rounds already committed.
    """
    cost = ctx.coder_cost + ctx.review_cost
    try:
        head_sha = git.commit_sha(ctx.wt)
    except (RuntimeError, OSError):
        logger.warning(
            "story-develop %s: could not read HEAD for the round %d checkpoint",
            ctx.config.run_id,
            round_no,
        )
        return
    checkpoint.record_round_checkpoint(
        ctx.config.run_dir,
        round_no=round_no,
        branch=ctx.wt.name,
        head_sha=head_sha,
        base_sha=ctx.base.start_sha,
        base_ref=ctx.base.ref,
        commit=ctx.new_commit or "",
        repo=str(ctx.config.repo),
        worktree=str(ctx.wt),
        cost_usd=cost,
        branch_rounds=ctx.carried_rounds + round_no,
        branch_cost_usd=ctx.carried_cost_usd + cost,
    )


def run_round(ctx: RoundContext, round_no: int) -> CycleExit | None:
    """Sequence one develop round's phases. Returns the first phase's
    :class:`CycleExit` (terminating the loop), or ``None`` to continue to the next
    round. The order — and the TWO ``cost_ceiling_phase`` calls straddling
    approval — is load-bearing (see :func:`cost_ceiling_phase`).

    However the round ends, the boundary it reached is checkpointed
    (:func:`record_boundary`) before the exit is handed back — in a ``finally``,
    so a round that CRASHED (exit L: the exception bypasses ``develop()``'s
    epilogue, which is precisely the run with no other record) still records the
    head its commit reached rather than leaving that commit for nobody."""
    ctx.rounds_completed = round_no
    phases: tuple[Callable[[RoundContext, int], CycleExit | None], ...] = (
        coder_phase,
        dispute_phase,
        commit_phase,
        lambda c, r: cost_ceiling_phase(c, r, when="pre_review"),
        fast_gate_phase,
        panel_phase,
        approval_phase,
        no_change_verdict_phase,
        decision_phase,
        deadlock_phase,
        stall_phase,
        lambda c, r: cost_ceiling_phase(c, r, when="post_review"),
    )
    exit_: CycleExit | None = None
    try:
        for phase in phases:
            exit_ = phase(ctx, round_no)
            if exit_ is not None:
                break
    finally:
        record_boundary(ctx, round_no)
    return exit_
