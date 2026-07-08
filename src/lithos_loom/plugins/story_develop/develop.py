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
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from ...runner import git, worktree
from . import (
    autoformat,
    containers,
    engines,
    handoff,
    run_outcome,
)
from .agent_session import _CONTINUATION_PROMPT as _CONTINUATION_PROMPT
from .agent_session import (
    PauseBudget,
    build_run_cmd,
    resume_after_from,
    turn_with_limit_pauses,
)
from .check_runner import (
    build_check_set,
    gate_floor_blocks,
    load_gate_ledger,
    merge_check_sets,
    persist_gate_ledger,
    run_check_set,
)
from .check_runner import (
    check_result_blocks as check_result_blocks,
)
from .check_set import (
    CheckSetResult,
    render_check_summary,
)
from .config import (
    HANDOFF_DIRNAME,
    DevelopConfig,
    ReviewerSpec,
    is_valid_reviewer_name,
)
from .gate_findings import GateFinding
from .handoff import (
    HandoffError,
    render_findings,
    render_prompt,
)
from .panel import (
    SEVERITY_CALIBRATION as SEVERITY_CALIBRATION,
)
from .panel import (
    PanelRoundResult as PanelRoundResult,
)
from .panel import (
    ReviewerState,
    ReviewOutcome,
    findings_by_severity,
    run_panel_round,
)
from .panel import (
    _reviewer_brief as _reviewer_brief,
)
from .rounds import Services
from .test_gate import GateResult
from .turns import run_turn

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
        return _render_findings(outcomes[0].findings)
    parts: list[str] = []
    for outcome in outcomes:
        parts.append(f"### From the {outcome.reviewer} reviewer")
        if outcome.findings:
            rendered = _render_findings(outcome.findings)
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


# --- deterministic gate (T4; #131: ordered multi-check check-set) -----------


# ARCH-1.S2: the check-set builders + gate runners moved to check_runner.py.
# These back-compat aliases keep develop()'s internal call sites and external
# importers (review_only, pr_delivery, tests' monkeypatch targets) resolving
# through this module until S8's public-surface flip deletes them.
_run_check_set = run_check_set
_merge_check_sets = merge_check_sets
_load_gate_ledger = load_gate_ledger
_persist_gate_ledger = persist_gate_ledger

# ARCH-1.S4: build_run_cmd + the usage-limit pause loop (turn_with_limit_pauses,
# PauseBudget, resume_after_from, _CONTINUATION_PROMPT) moved to agent_session.py;
# the Services injection seam lives in rounds.py. develop() keeps these aliases
# (deleted in S8) for its own calls + review_only / pr_delivery importers, and
# wires Services from its OWN module globals (below) so the existing
# monkeypatch.setattr(develop_mod, "run_turn"/"_sleep"/"_run_check_set") patches
# — and containers.* patches — keep taking effect. _sleep stays defined here as
# the seam Services.sleep binds when develop() builds its Services.
_build_run_cmd = build_run_cmd
_PauseBudget = PauseBudget
_turn_with_limit_pauses = turn_with_limit_pauses
_resume_after_from = resume_after_from

# ARCH-1.S5: the reviewer panel (ReviewOutcome / PanelRoundResult / ReviewerState /
# run_panel_round / findings_by_severity / _reviewer_brief / SEVERITY_CALIBRATION)
# moved to panel.py, and the generic prompt renderers to handoff.py
# (render_prompt / render_findings). These back-compat aliases keep develop()'s
# own coder-side call sites and external importers (review_only imports
# _ReviewerState + run_panel_round; pr_delivery imports _render + _render_findings;
# lithos_io imports findings_by_severity; test_prompts imports _reviewer_brief +
# SEVERITY_CALIBRATION) resolving through this module until S8's public-name flip.
_ReviewerState = ReviewerState
_render = render_prompt
_render_findings = render_findings


def _develop_services() -> Services:
    """The round pipeline's :class:`Services`, bound from develop's OWN module
    globals. develop() builds it at run start — *after* the tests apply their
    ``monkeypatch.setattr(develop_mod, "run_turn"/"_sleep"/"_run_check_set")`` (and
    ``containers.*``) patches — so each field captures the patched callable and
    every one of those patches keeps taking effect until S8 re-points the tests to
    :meth:`Services.live`."""
    return Services(
        run_turn=run_turn,
        sleep=_sleep,
        start_container=containers.start_container,
        stop_container=containers.stop_container,
        run_check_set=_run_check_set,
    )


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


def _sleep(seconds: float) -> None:
    """Monkeypatch seam — tests must never actually sleep."""
    time.sleep(seconds)


# --- per-turn drivers -------------------------------------------------------


def _record_coder_disputes(
    config: DevelopConfig, reviewers: list[_ReviewerState], round_no: int
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

    coder_name, coder_cmd = _build_run_cmd(
        config,
        agent="coder",
        engine=coder_engine,
        config_dir=config.coder_config_dir,
        wt=wt,
        read_only=False,
    )
    reviewers: list[_ReviewerState] = []
    for spec in specs:
        rname, rcmd = _build_run_cmd(
            config,
            agent=f"review-{spec.name}",
            engine=engines.get_engine(spec.tool),
            config_dir=config.reviewer_config_dir(spec.name),
            wt=wt,
            read_only=True,
        )
        reviewers.append(_ReviewerState(spec, rname, rcmd, wt))
    coder_session = str(uuid.uuid4())

    status = "failed"
    failure_reason = "no rounds ran"
    final_reviews: list[ReviewOutcome] = []
    # The per-round gate is an ordered check-set (#131); ``gate`` stays the
    # ``test`` check's back-compat view so the prompt/summary/result sites below
    # are unchanged. ``checks`` is the Review-Profile-selected set, resolved once
    # (detection probes the image). #140/ADR §4: ``fast`` checks run every round
    # for tight coder feedback; ``candidate`` checks (expensive — dep-audit /
    # coverage / semgrep) run only on the approval candidate, the round that would
    # otherwise pass.
    gate: GateResult | None = None
    check_set: CheckSetResult | None = None
    checks = build_check_set(config, wt)
    fast_checks = tuple(c for c in checks if c.stage == "fast")
    candidate_checks = tuple(c for c in checks if c.stage == "candidate")
    # #134/ADR §4: the auto-format pass rewrites the round commit in place before the
    # gate + panel. Resolve the runnable formatters once (detection + one image probe),
    # like the check-set; empty for a markerless repo, when the pass is a no-op.
    formatters = autoformat.resolve_formatters(config, wt)
    gate_ledger = _load_gate_ledger(config)  # #132: one per run; survives resume
    rounds_completed = 0
    coder_cost = 0.0
    review_cost = 0.0
    budget = _PauseBudget(config.max_pause_minutes * 60)
    services = _develop_services()  # ARCH-1.S4: injected into the coder turn loop
    stall_strikes = 0  # T7: consecutive no-progress rounds
    prev_signature: frozenset | None = None
    resume_after: datetime | None = None  # set only on interrupted (T10)
    gated_sha: str | None = None  # latest committed tree (the approval candidate)
    candidate_ran_for_sha: str | None = None  # dedup the candidate run per commit

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
            rounds_completed = round_no
            # --- coder turn ------------------------------------------------
            if round_no == 1:
                # T8: an EXPLICIT acceptance criteria (flag / task metadata)
                # gets its own section; when it merely falls back to the
                # description, repeating it would be noise.
                ac_section = (
                    f"\n## Acceptance criteria\n\n{config.acceptance_criteria}\n"
                    if config.acceptance_criteria
                    else ""
                )
                coder_prompt = _render(
                    handoff.load_prompt("coder_init.md"),
                    description=config.description,
                    acceptance_criteria_section=ac_section,
                    handoff_file=handoff.coder_handoff_name(1),
                )
                coder_resume = False
            else:
                assert final_reviews  # set by the prior round's reviews
                review_files = ", ".join(
                    f"`{handoff.reviewer_handoff_name(round_no - 1, n)}`" for n in names
                )
                coder_prompt = _render(
                    handoff.load_prompt("coder_fix.md"),
                    round_no=str(round_no),
                    acceptance_criteria=config.effective_acceptance_criteria,
                    findings=_render_panel_findings(final_reviews),
                    gate_summary=render_check_summary(
                        check_set, for_coder=True, gate_ledger=gate_ledger
                    ),
                    review_files=review_files,
                    handoff_file=handoff.coder_handoff_name(round_no),
                )
                coder_resume = True

            coder_turn, coder_interrupted, attempt_cost = _turn_with_limit_pauses(
                config,
                budget,
                services=services,
                agent="coder",
                container=coder_name,
                config_dir=config.coder_config_dir,
                prompt=coder_prompt,
                session_id=coder_session,
                resume=coder_resume,
                round_no=round_no,
                timeout=coder_timeout,
                engine=coder_engine,
            )
            coder_cost += attempt_cost
            # Codex mints its session handle (thread_id) on turn 1; reuse the
            # returned handle for resumes + persist it (no-op for claude, which
            # echoes the supplied uuid). Drives daemon-resume + PR delivery.
            if coder_turn.session_id:
                coder_session = coder_turn.session_id
            if coder_interrupted:
                failure_reason = (
                    f"round {round_no}: coder usage-limited; pause budget exhausted"
                )
                status = "interrupted"
                resume_after = _resume_after_from(coder_turn)
                break
            done_present = (
                config.handoff_dir / handoff.coder_handoff_name(round_no)
            ).is_file()
            # The turn whose success gates the handoff for this round. The
            # salvage nudge (below) replaces it, so a re-prompt is judged on the
            # NUDGE's own outcome — a nudge that writes the file but then exits
            # failed/non-zero is not a clean recovery.
            handoff_turn = coder_turn
            # Salvage (lithos-loom#114): the coder ended its turn cleanly and
            # left work in the worktree but never wrote its handoff (classic
            # case: it backgrounded a slow suite and stopped before the handoff
            # step). The implementation is done; only the required breadcrumb is
            # missing. Re-prompt once to write it before failing — the prompt
            # already forbids this (#115); this recovers the slips-through. Only
            # for a clean turn (a crashed/errored turn can't be resumed) and
            # only when there is uncommitted work to save (else a nudge is
            # wasted); between rounds the worktree is clean, so the flag
            # reflects this round's coder work.
            if (
                coder_turn.succeeded
                and not done_present
                and git.has_uncommitted_changes(wt)
            ):
                logger.warning(
                    "story-develop %s: round %d coder ended its turn with "
                    "uncommitted changes but no handoff — re-prompting once to "
                    "write it",
                    config.run_id,
                    round_no,
                )
                handoff_turn = run_turn(
                    container=coder_name,
                    prompt=_coder_handoff_nudge(round_no),
                    session_id=coder_session,
                    resume=True,
                    timeout=coder_timeout,
                    engine=coder_engine,
                    model=config.coder_model,
                    effort=config.coder_effort,
                )
                coder_cost += handoff_turn.cost_usd
                if handoff_turn.session_id:
                    coder_session = handoff_turn.session_id
                done_present = (
                    config.handoff_dir / handoff.coder_handoff_name(round_no)
                ).is_file()
            if not (handoff_turn.succeeded and done_present):
                reasons = []
                if not handoff_turn.succeeded:
                    reasons.append(f"coder turn failed (exit {handoff_turn.exit_code})")
                if not done_present:
                    reasons.append("no coder handoff file")
                failure_reason = f"round {round_no}: " + "; ".join(reasons)
                status = "failed"
                break

            # T7: record the coder's dispute marks (its handoff may carry a
            # Findings block updating ids with status: disputed). Tolerant —
            # an unparseable coder handoff just records nothing.
            if round_no >= 2:
                _record_coder_disputes(config, reviewers, round_no)

            new_commit = git.commit_all(
                wt,
                f"story-develop r{round_no}: {config.description}",
                exclude=[HANDOFF_DIRNAME],
            )
            if round_no == 1 and new_commit is None:
                failure_reason = "round 1: coder produced no commit"
                status = "failed"
                break
            if new_commit is not None:
                # #134/ADR §4: auto-format the round's commit BEFORE the gate + panel.
                # The formatter rewrites source in place; any change is a SEPARATE
                # commit whose SHA supersedes new_commit, so the gate runs on — and the
                # reviewers review — that exact formatted tree. Best-effort: a no-op
                # (already clean, or no formatter) leaves new_commit untouched.
                format_sha = autoformat.run_format_pass(
                    config, wt, round_no, formatters
                )
                if format_sha is not None:
                    new_commit = format_sha
                # Track the latest committed tree so the approval-candidate gate
                # (#140) can run candidate-staged checks against it even on a later
                # round that produced no fresh commit.
                gated_sha = new_commit

            # T7: cost ceiling — check before spending more on reviews.
            if (
                config.max_cost_usd is not None
                and coder_cost + review_cost >= config.max_cost_usd
            ):
                failure_reason = (
                    f"round {round_no}: cost ceiling reached "
                    f"(${coder_cost + review_cost:.2f} >= ${config.max_cost_usd:.2f})"
                )
                status = "cost_exceeded"
                break

            # --- deterministic gate (only when there is a new commit to gate) -
            # #140/ADR §4: the per-round gate runs the FAST checks only; the
            # candidate-staged checks are deferred to the approval candidate below.
            if fast_checks and new_commit is not None:
                # Overwrite unconditionally: on a gate infra error this clears
                # to None rather than letting a PRIOR commit's result (e.g. a
                # stale RED) stand in for this commit. A
                # round with no new commit keeps the prior result — the tree is
                # unchanged, so it still describes HEAD.
                check_set = _run_check_set(
                    config, wt, new_commit, round_no, fast_checks, gate_ledger
                )
                gate = check_set.test_gate if check_set is not None else None
                _persist_gate_ledger(config, gate_ledger)

            # --- reviewer turns (panel order, sequential) -------------------
            # The single shared panel primitive (#154): review-only mode drives
            # the SAME call. Round 1 hands the coder's summary to the panel;
            # later rounds resume each reviewer with its open findings.
            panel = run_panel_round(
                config,
                reviewers,
                wt=wt,
                base=base,
                round_no=round_no,
                check_set=check_set,
                gate_ledger=gate_ledger,
                budget=budget,
                reviewer_timeout=reviewer_timeout,
                coder_summary=_coder_summary(config, 1) if round_no == 1 else "",
                # ARCH-1.S5: reviewer turns run through develop's own seam
                services=services,
            )
            round_reviews = panel.round_reviews
            review_cost += panel.cost
            final_reviews = round_reviews

            if panel.interrupted:
                failure_reason = (
                    f"round {round_no}: reviewer usage-limited; pause budget exhausted"
                )
                status = "interrupted"
                resume_after = panel.resume_after
                break
            if panel.invalid_reviewer is not None:
                failure_reason = (
                    f"round {round_no}: reviewer "
                    f"[{panel.invalid_reviewer}] handoff invalid"
                )
                status = "failed"
                break

            # Approval requires ALL reviewers to pass their OWN threshold in
            # the SAME round (PRD decision #7). Approval deliberately takes
            # precedence over the cost ceiling when both land in the same
            # round: the ceiling exists to stop FURTHER spend on unfinished
            # work, and the spend has already happened — relabelling a
            # finished, approved run as cost_exceeded would discard a good
            # branch for no protective benefit.
            if all(r.passed for r in round_reviews):
                # #140/ADR §4: the approval candidate — run the expensive
                # candidate-staged checks once on this tree before sealing approval.
                # A *required* candidate (e.g. thorough's dep-audit) now blocks via
                # :func:`gate_floor_blocks` below, so its findings merge into
                # ``check_set`` + the ledger + the ``[DevelopResult]`` and, when it
                # blocks, hold approval so a later round surfaces them to the
                # coder/panel. Run once per committed tree (dedup on the sha).
                if (
                    candidate_checks
                    and gated_sha is not None
                    and candidate_ran_for_sha != gated_sha
                ):
                    candidate_ran_for_sha = gated_sha
                    candidate_set = _run_check_set(
                        config, wt, gated_sha, round_no, candidate_checks, gate_ledger
                    )
                    check_set = _merge_check_sets(check_set, candidate_set)
                    gate = check_set.test_gate if check_set is not None else None
                    _persist_gate_ledger(config, gate_ledger)
                # #140 floor: a *required* check blocks approval — its verdict read
                # from the ledger severity for adapter tools, the raw exit otherwise
                # (informational checks never block, even if RED).
                if gate_floor_blocks(check_set, gate_ledger):
                    logger.info(
                        "story-develop %s: round %d reviews passed but a required "
                        "check blocks approval; continuing",
                        config.run_id,
                        round_no,
                    )
                else:
                    status = "approved"
                    break

            # --- T7 termination guards (not approved this round) ------------
            # Dispute escalation: a coder-disputed finding the reviewer kept
            # blocking for 2 consecutive rounds -> stop with a human
            # breadcrumb rather than grinding to max_rounds.
            deadlocked = [
                f"{r.spec.name}/{fid}"
                for r in reviewers
                for fid in r.ledger.disputed_deadlocks(r.spec.block_threshold)
            ]
            if deadlocked:
                logger.warning(
                    "[ReviewDispute] story-develop %s: round %d dispute deadlock "
                    "on %s — stopping for human review",
                    config.run_id,
                    round_no,
                    ", ".join(deadlocked),
                )
                failure_reason = (
                    f"round {round_no}: dispute deadlock on "
                    f"{', '.join(deadlocked)} (coder disputes, reviewer keeps "
                    "blocking)"
                )
                status = "disputed"
                break
            # Stall guard, keyed off finding IDENTITY: an empty round commit
            # or an unchanged blocking set, two rounds running -> stop.
            signature = frozenset(
                (r.spec.name, fid, fstatus)
                for r in reviewers
                for fid, fstatus in r.ledger.blocking_signature(r.spec.block_threshold)
            )
            if round_no >= 2 and (new_commit is None or signature == prev_signature):
                stall_strikes += 1
            else:
                stall_strikes = 0
            prev_signature = signature
            if stall_strikes >= 2:
                failure_reason = f"round {round_no}: stalled — " + (
                    "no new commit and/or blocking findings unchanged "
                    "across 2 consecutive rounds"
                )
                status = "stalled"
                break
            # Cost ceiling after the round's reviews.
            if (
                config.max_cost_usd is not None
                and coder_cost + review_cost >= config.max_cost_usd
            ):
                failure_reason = (
                    f"round {round_no}: cost ceiling reached "
                    f"(${coder_cost + review_cost:.2f} >= ${config.max_cost_usd:.2f})"
                )
                status = "cost_exceeded"
                break
            # otherwise: loop to the next round (if any remain)
        else:
            # loop exhausted without an approval / failure break
            status = "max_rounds"
    finally:
        containers.stop_container(coder_name)
        for rstate in reviewers:
            containers.stop_container(rstate.container)

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
