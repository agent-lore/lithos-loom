"""``develop()`` core — the full implement → review → fix → approve loop.

    worktree
      -> start coder (RW) + reviewer (RO) containers, both long-lived
      -> round 1: coder implements, commit, auto-format, test gate, reviewer reviews
      -> round N: coder fixes (resume), commit, auto-format, gate, reviewer re-reviews
      -> stop when the reviewer passes (approved) or max_rounds is hit
      -> tear both containers down; leave the branch + a conversation log.

The test gate (T4) runs each round commit's tree in a fresh throwaway container
— an agent-free check on the coder's self-reported test results. Whether a red
gate blocks approval is the resolved review profile's ``test`` check state (#140;
all canonical profiles declare it required), and its output is fed to the coder
next round.

The two agents keep their sessions **across rounds** (ADR 0002): each round is a
fresh ``docker exec`` that resumes the on-disk session, so the coder remembers
what it tried and the reviewer remembers what it objected to — the whole point
of the conversational model over Ralph++'s fire-and-forget loop.

The side-effecting bits (container start/exec/stop) live in :mod:`containers` /
:mod:`turns` so this orchestration is unit-testable by monkeypatching them.

Unattended runs are bounded (T7): ``max_rounds``, a ``max_cost_usd`` ceiling,
a stall guard keyed off finding identity (empty round commit or an unchanged
blocking set, two rounds running), and a dispute escalation — a coder-disputed
finding the reviewer keeps blocking for 2 rounds stops the run with a
``[ReviewDispute]`` breadcrumb instead of grinding to ``max_rounds``. Finding
identity itself is plugin-enforced via each reviewer's
:class:`~.findings.FindingLedger`.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from ...runner import git, worktree
from . import (
    agent_session,
    autoformat,
    check_runner,
    containers,
    engines,
    handoff,
    panel,
    run_outcome,
)
from .config import (
    DevelopConfig,
    ReviewerSpec,
    is_valid_reviewer_name,
)
from .gate_findings import GateFinding
from .handoff import HandoffError
from .panel import (
    ReviewOutcome,
    findings_by_severity,
    run_panel_round,
)
from .rounds import CycleExit, RoundContext, Services, run_round
from .test_gate import GateResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DevelopResult:
    """Outcome of a ``develop()`` run."""

    # "approved" | "max_rounds" | "failed" | "interrupted"
    # | "stalled" | "disputed" | "cost_exceeded"  (T7 guards)
    status: str
    run_id: str
    worktree: Path
    branch: str
    base_sha: str
    commits: list[str]
    rounds: int
    handoff_present: bool
    coder_cost_usd: float
    review_cost_usd: float
    message: str
    # the final round's outcomes, in panel order (immutable — frozen dataclass)
    reviews: tuple[ReviewOutcome, ...] = ()
    # the coder's session id — the PR-delivery Copilot round resumes it (T9)
    coder_session: str = ""
    test_gate: GateResult | None = None  # the latest round's gate (T4)
    # the latest round's open deterministic gate findings (#132)
    gate_findings: tuple[GateFinding, ...] = ()
    conversation_log: Path | None = None
    # the resolved Review Profile this run ran under (#139/ADR 0003 §11): part
    # of the per-run review-metadata record correlated against outcome signals
    review_profile: str = ""
    # when an INTERRUPTED run is worth retrying: the provider's parsed reset
    # time, or now + a fixed delay when no hint was parseable (T10 — the
    # daemon schedules a re-dispatch at this instant)
    resume_after: datetime | None = None

    @property
    def review(self) -> ReviewOutcome | None:
        """The single-reviewer convenience view (first panel member)."""
        return self.reviews[0] if self.reviews else None

    @property
    def approved(self) -> bool:
        return self.status == "approved"

    @property
    def succeeded(self) -> bool:
        """True only when the reviewer approved (drives the CLI exit code)."""
        return self.status == "approved"

    @property
    def total_cost_usd(self) -> float:
        return self.coder_cost_usd + self.review_cost_usd


# --- prompt / rendering helpers --------------------------------------------


def _render_panel_findings(outcomes: list[ReviewOutcome]) -> str:
    """Consolidate all reviewers' findings into one labelled block (T6).

    Consolidated mode: the coder gets every reviewer's findings in a single
    prompt, grouped per reviewer so disputes can be addressed to the right
    persona. Finding ids are prefixed with the reviewer name when there is
    more than one reviewer, keeping ids unambiguous across the panel.
    """
    if len(outcomes) == 1:
        return handoff.render_findings(outcomes[0].findings)
    parts: list[str] = []
    for outcome in outcomes:
        parts.append(f"### From the {outcome.reviewer} reviewer")
        if outcome.findings:
            rendered = handoff.render_findings(outcome.findings)
            # qualify ids: [f-001] -> [code-quality/f-001]
            rendered = rendered.replace("- [", f"- [{outcome.reviewer}/")
            parts.append(rendered)
        else:
            parts.append(f"(no findings — {outcome.status})")
        parts.append("")
    return "\n".join(parts).rstrip()


def _coder_summary(config: DevelopConfig, round_no: int) -> str:
    """Best-effort read of the coder's round-*round_no* summary (seeds review)."""
    path = config.handoff_dir / handoff.coder_handoff_name(round_no)
    try:
        return handoff.parse_review_handoff(
            path.read_text(encoding="utf-8")
        ).summary or ("(the coder wrote no summary)")
    except (HandoffError, OSError):
        return "(coder summary unavailable)"


# ARCH-1.S8 (public-surface flip): the S2/S4/S5 back-compat aliases and the
# develop-local Services seam (_develop_services / _sleep) are gone. develop()
# now builds :meth:`Services.live` directly and calls the public names —
# check_runner.build_check_set / .load_gate_ledger, agent_session's build_run_cmd
# / PauseBudget / turn_with_limit_pauses / resume_after_from, panel's ReviewerState
# / run_panel_round — so tests patch the real module homes (turns.run_turn,
# time.sleep, check_runner.run_check_set / .build_check_set) rather than develop's
# aliases. review_only + pr_delivery likewise import from those homes.


# --- usage-limit reaction (T5) ----------------------------------------------


def _coder_handoff_nudge(round_no: int) -> str:
    """One-shot re-prompt when the coder ended its turn with work but no handoff.

    The implementation is already in the worktree; only the required handoff
    breadcrumb is missing (lithos-loom#114 — typically the coder backgrounded a
    slow suite and stopped before the handoff step). Ask only for the handoff,
    synchronously, with no further commands.
    """
    return (
        "You changed files under /workspace but never wrote your handoff file, "
        "so the run cannot proceed. Do not run, background, or wait on any "
        "further commands. Right now, synchronously, write your summary to "
        f"/workspace/.handoff/{handoff.coder_handoff_name(round_no)} per "
        "/workspace/.handoff/FORMAT.md (`## Status: LGTM` then `## Summary`). "
        "That is the only remaining step."
    )


# --- per-turn drivers -------------------------------------------------------


def _record_coder_disputes(
    config: DevelopConfig, reviewers: list[panel.ReviewerState], round_no: int
) -> None:
    """Parse the coder's round handoff and record dispute marks (T7).

    The coder may qualify ids as ``<reviewer>/<id>`` (the panel rendering) or
    leave them bare (routed to the sole reviewer; ambiguous in a panel and
    ignored there). Tolerant by design — a malformed coder handoff records
    nothing rather than failing the round.
    """
    path = config.handoff_dir / handoff.coder_handoff_name(round_no)
    try:
        parsed = handoff.parse_review_handoff(path.read_text(encoding="utf-8"))
    except (HandoffError, OSError):
        return
    if not parsed.findings:
        return
    by_name = {r.spec.name: r for r in reviewers}
    for f in parsed.findings:
        fid = f.finding_id
        if "/" in fid:
            prefix, _, bare = fid.partition("/")
            target = by_name.get(prefix)
            if target is not None:
                target.ledger.record_coder_updates(
                    [replace(f, finding_id=bare)], round_no
                )
            continue
        if len(reviewers) == 1:
            reviewers[0].ledger.record_coder_updates([f], round_no)
        else:
            logger.debug(
                "story-develop %s: unqualified coder finding id %r in a panel "
                "run; ignored",
                config.run_id,
                fid,
            )


# --- orchestration ----------------------------------------------------------


# Statuses whose human message embeds ``failure_reason`` (the message-building
# elif chain in ``develop()``). ``approved`` and ``max_rounds`` describe
# themselves and never consume it — so it stays at its "no rounds ran" sentinel
# for a max_rounds run. ``state.json`` records the reason only for these, so the
# offline ``attach`` summary (#188) never shows a stale reason for max_rounds.
_REASON_BEARING_STATUSES = frozenset(
    {"failed", "interrupted", "stalled", "disputed", "cost_exceeded"}
)


def _warn_if_ceiling_unmetered(
    config: DevelopConfig, specs: Sequence[ReviewerSpec]
) -> None:
    """Warn once when ``max_cost_usd`` is set but a participant can't meter USD.

    #102: an engine with ``meters_cost_usd=False`` (today only codex, which
    reports token usage, not USD) contributes ``$0.00`` to the ceiling — the
    knob then bounds only the USD-reporting participants. The message is
    capability-driven: it names whatever non-metering tools are actually in the
    run, so a future non-metering engine reads correctly without a code edit.
    Visibility only; the ceiling checks (approved-precedence + two-site ordering)
    are untouched. Measuring token→USD cost is #102's own problem, not this.
    Unsupported fallback-chain entries are skipped (they never run).
    """
    if config.max_cost_usd is None:
        return
    tools = {config.coder}
    for spec in specs:
        tools.add(spec.tool)
        tools.update(spec.fallback_chain)
    unmetered = sorted(
        t
        for t in tools
        if engines.is_supported(t) and not engines.get_engine(t).meters_cost_usd
    )
    if unmetered:
        logger.warning(
            "story-develop %s: max_cost_usd=$%.2f does not meter %s "
            "(reports token usage, not USD; meters_cost_usd=False), so those "
            "turns add $0.00 to the ceiling — it bounds USD-reporting tools "
            "only. See #102.",
            config.run_id,
            config.max_cost_usd,
            ", ".join(unmetered),
        )


def develop(
    config: DevelopConfig,
    *,
    coder_timeout: int = 3600,
    reviewer_timeout: int = 3600,
) -> DevelopResult:
    """Run the develop loop and return a result.

    The worktree, per-run state, and conversation log are preserved on exit
    (approved, max_rounds, failed, or interrupted) for inspection; only the
    containers are torn down.
    """
    specs = config.effective_reviewers
    if not engines.is_supported(config.coder):
        raise ValueError(
            f"unsupported coder tool {config.coder!r}: "
            f"expected {engines.supported_tools_phrase()}"
        )
    coder_engine = engines.get_engine(config.coder)
    for spec in specs:
        if not engines.is_supported(spec.tool):
            raise ValueError(
                f"unsupported tool {spec.tool!r} for reviewer {spec.name!r}: "
                f"expected {engines.supported_tools_phrase()}"
            )
        if not is_valid_reviewer_name(spec.name):
            raise ValueError(
                f"invalid reviewer name {spec.name!r}: must be lowercase "
                "alphanumerics + hyphens (e.g. 'code-quality')"
            )
    names = [s.name for s in specs]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate reviewer names: {names}")
    if config.max_rounds < 1:
        raise ValueError(f"max_rounds must be >= 1 (got {config.max_rounds})")
    if config.pause_poll_minutes < 1:
        # 0 would spin forever on zero-second "pauses"; negative would crash
        # time.sleep(). The budget (max_pause_minutes) MAY be 0 ("never wait").
        raise ValueError(
            f"pause_poll_minutes must be >= 1 (got {config.pause_poll_minutes})"
        )
    if config.max_pause_minutes < 0:
        raise ValueError(
            f"max_pause_minutes must be >= 0 (got {config.max_pause_minutes})"
        )
    if config.max_cost_usd is not None and config.max_cost_usd <= 0:
        raise ValueError(f"max_cost_usd must be > 0 (got {config.max_cost_usd})")
    _warn_if_ceiling_unmetered(config, specs)

    config.coder_config_dir.mkdir(parents=True, exist_ok=True)
    for spec in specs:
        config.reviewer_config_dir(spec.name).mkdir(parents=True, exist_ok=True)
    config.worktree_parent.mkdir(parents=True, exist_ok=True)
    handoff.seed_handoff_dir(config.handoff_dir)

    wt = worktree.create(
        config.repo,
        config.base_branch,
        config.description,
        parent=config.worktree_parent,
    )
    branch = wt.name
    base = git.base_sha(wt)
    logger.info("story-develop %s: worktree %s (branch %s)", config.run_id, wt, branch)

    coder_name, coder_cmd = agent_session.build_run_cmd(
        config,
        agent="coder",
        engine=coder_engine,
        config_dir=config.coder_config_dir,
        wt=wt,
        read_only=False,
    )
    reviewers: list[panel.ReviewerState] = []
    for spec in specs:
        rname, rcmd = agent_session.build_run_cmd(
            config,
            agent=f"review-{spec.name}",
            engine=engines.get_engine(spec.tool),
            config_dir=config.reviewer_config_dir(spec.name),
            wt=wt,
            read_only=True,
        )
        reviewers.append(panel.ReviewerState(spec, rname, rcmd, wt))
    coder_session = str(uuid.uuid4())

    # The per-round gate is an ordered check-set (#131). ``fast`` checks run every
    # round for tight coder feedback; ``candidate`` checks (expensive — dep-audit /
    # coverage / semgrep) run only on the approval candidate (#140/ADR §4). #134:
    # resolve the runnable formatters once, like the check-set (empty = no-op).
    # build_check_set is reached via the module so tests can patch
    # check_runner.build_check_set (S8).
    checks = check_runner.build_check_set(config, wt)
    fast_checks = tuple(c for c in checks if c.stage == "fast")
    candidate_checks = tuple(c for c in checks if c.stage == "candidate")
    formatters = autoformat.resolve_formatters(config, wt)
    # #132: one gate ledger per run; survives resume.
    gate_ledger = check_runner.load_gate_ledger(config)
    budget = agent_session.PauseBudget(config.max_pause_minutes * 60)

    # RoundContext is the explicit successor of this function's locals bag (S6).
    # The boundary collaborators are injected from THIS module's globals — so the
    # develop_mod.run_panel_round patch (the exit-L test) stays live — and rounds.py
    # imports neither panel nor agent_session nor develop (see rounds.py). The
    # side-effecting seams come from Services.live() (S8): tests patch the real
    # module homes (turns.run_turn / time.sleep / check_runner.run_check_set). Run
    # state (costs, check_set, gate, final_reviews, coder_session, rounds_completed,
    # …) is mutated on ctx and read back into locals below for the unchanged epilogue.
    ctx = RoundContext(
        config=config,
        wt=wt,
        base=base,
        names=names,
        services=Services.live(),
        reviewers=reviewers,
        coder_container=coder_name,
        coder_engine=coder_engine,
        coder_timeout=coder_timeout,
        reviewer_timeout=reviewer_timeout,
        fast_checks=fast_checks,
        candidate_checks=candidate_checks,
        formatters=formatters,
        gate_ledger=gate_ledger,
        budget=budget,
        coder_session=coder_session,
        turn_with_limit_pauses=agent_session.turn_with_limit_pauses,
        run_panel_round=run_panel_round,
        resume_after_from=agent_session.resume_after_from,
        render_panel_findings=_render_panel_findings,
        coder_summary=_coder_summary,
        record_coder_disputes=_record_coder_disputes,
        coder_handoff_nudge=_coder_handoff_nudge,
    )

    # The default outcome is "max_rounds" — the exit the loop lands on when it
    # completes without any round returning an early CycleExit (exit K). A round
    # that DOES terminate early overrides this and breaks. (An exception inside the
    # try bypasses the epilogue entirely — exit L — so this value is never read on
    # that path.)
    exit_state = CycleExit(status="max_rounds", failure_reason="", resume_after=None)
    try:
        containers.start_container(coder_cmd)
        for rstate in reviewers:
            containers.start_container(rstate.run_cmd)
        logger.info(
            "story-develop %s: coder %s + %d reviewer(s) [%s] started",
            config.run_id,
            coder_name,
            len(reviewers),
            ", ".join(names),
        )
        for round_no in range(1, config.max_rounds + 1):
            round_exit = run_round(ctx, round_no)
            if round_exit is not None:
                exit_state = round_exit
                break
    finally:
        containers.stop_container(coder_name)
        for rstate in reviewers:
            containers.stop_container(rstate.container)

    # Unpack ctx's run-state + the exit into the exact local names the epilogue
    # below already uses, so that block stays byte-for-byte unchanged.
    status = exit_state.status
    failure_reason = exit_state.failure_reason
    resume_after = exit_state.resume_after
    coder_cost = ctx.coder_cost
    review_cost = ctx.review_cost
    gate = ctx.gate
    final_reviews = ctx.final_reviews
    coder_session = ctx.coder_session
    rounds_completed = ctx.rounds_completed

    commits = git.commits_since(wt, base)
    handoff_present = (config.handoff_dir / handoff.coder_handoff_name(1)).is_file()

    log_path = config.run_dir / run_outcome.CONVERSATION_LOG
    log_path.write_text(
        handoff.conversation_log(config.handoff_dir, rounds_completed, names),
        encoding="utf-8",
    )

    def _reviews_part(outcomes: list[ReviewOutcome] | tuple) -> str:
        bits = []
        for r in outcomes:
            sev = f" max {r.max_severity}" if r.max_severity else ""
            bits.append(
                f"[{r.reviewer}]={r.status}({'pass' if r.passed else 'blocks'}{sev})"
            )
        return " ".join(bits)

    total = coder_cost + review_cost
    gate_part = f"; test gate {gate.verdict} (`{gate.command}`)" if gate else ""
    if status == "approved":
        message = (
            f"approved by {_reviews_part(final_reviews)} in {rounds_completed} "
            f"round(s){gate_part}; {len(commits)} commit(s) on {branch}; "
            f"cost ${total:.4f}"
        )
    elif status == "max_rounds":
        message = (
            f"NOT approved after {rounds_completed} round(s) (max_rounds); "
            f"last reviews: {_reviews_part(final_reviews)}"
            f"{gate_part}; {len(commits)} commit(s) on {branch}; cost ${total:.4f}"
        )
    elif status == "interrupted":
        message = (
            f"INTERRUPTED: {failure_reason}; {len(commits)} commit(s) on {branch}; "
            f"sessions + handoffs preserved in {config.run_dir} (re-run to retry); "
            f"cost ${total:.4f}"
        )
    elif status in ("stalled", "disputed", "cost_exceeded"):
        message = (
            f"STOPPED ({status}): {failure_reason}; "
            f"last reviews: {_reviews_part(final_reviews)}{gate_part}; "
            f"{len(commits)} commit(s) on {branch}; cost ${total:.4f}"
        )
    else:  # failed
        message = f"{failure_reason}{gate_part}; {len(commits)} commit(s) on {branch}"

    # Durable run state (PRD decision #5: resume state is ~free — session ids
    # + handoffs are on disk). Written on every exit, primarily for
    # `interrupted` runs and the future daemon re-dispatch (T10).
    (config.run_dir / run_outcome.STATE_FILE).write_text(
        json.dumps(
            {
                "status": status,
                "run_id": config.run_id,
                "branch": branch,
                "worktree": str(wt),
                "base_sha": base,
                "rounds": rounds_completed,
                # Why a non-approved run stopped, for the offline `attach` summary
                # (#188). Only the reason-bearing statuses set a real reason;
                # approved + max_rounds describe themselves (and would otherwise
                # leak the "no rounds ran" sentinel for an exhausted run).
                "failure_reason": (
                    failure_reason if status in _REASON_BEARING_STATUSES else None
                ),
                # Review-metadata record (ADR 0003 §11) — the same profile +
                # panel + findings-by-severity written to Lithos metadata, kept
                # in the durable run-state for local outcome correlation.
                "review_profile": config.review_profile,
                "review_panel": [r.reviewer for r in final_reviews],
                "findings_by_severity": findings_by_severity(final_reviews),
                "coder_session": coder_session,
                "reviewers": {
                    r.spec.name: {"session": r.session, "tool": r.engine_now.name}
                    for r in reviewers
                },
                "pause_budget_remaining_s": round(budget.remaining, 1),
                "resume_after": (
                    resume_after.isoformat(timespec="seconds")
                    if resume_after is not None
                    else None
                ),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    return DevelopResult(
        status=status,
        run_id=config.run_id,
        worktree=wt,
        branch=branch,
        base_sha=base,
        commits=commits,
        rounds=rounds_completed,
        handoff_present=handoff_present,
        coder_cost_usd=coder_cost,
        review_cost_usd=review_cost,
        message=message,
        reviews=tuple(final_reviews),
        coder_session=coder_session,
        test_gate=gate,
        gate_findings=tuple(gate_ledger.open_findings()),
        conversation_log=log_path,
        review_profile=config.review_profile,
        resume_after=resume_after,
    )
