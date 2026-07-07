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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ...runner import git, worktree
from . import (
    autoformat,
    containers,
    engines,
    handoff,
    limits,
    run_outcome,
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
from .findings import FindingLedger
from .gate_findings import GateFinding, GateLedger
from .handoff import Finding, HandoffError, ReviewHandoff
from .test_gate import GateResult
from .turns import TurnResult, run_turn

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReviewOutcome:
    """The result of a single reviewer's pass in one round."""

    reviewer: str
    status: str  # "LGTM" | "FINDINGS" | "invalid"
    passed: bool  # by THIS reviewer's block_threshold (per-reviewer, T6)
    max_severity: str | None
    findings: list[Finding] = field(default_factory=list)
    cost_usd: float = 0.0

    @property
    def findings_count(self) -> int:
        return len(self.findings)


def findings_by_severity(reviews: Sequence[ReviewOutcome]) -> dict[str, int]:
    """Count a panel's findings by severity (ADR 0003 §11 review-metadata record).

    Spans every reviewer's findings, all statuses. Canonical severities are
    always present (zero-filled) so the record has a stable shape; an off-rubric
    severity an ``invalid`` review emits is still counted under its own key. The
    single source of truth shared by the Lithos metadata patch (``lithos_io``)
    and the durable ``state.json``, so both carry an identical record.
    """
    counts: dict[str, int] = {"critical": 0, "major": 0, "minor": 0}
    for review in reviews:
        for f in review.findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
    return counts


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


@dataclass(frozen=True)
class PanelRoundResult:
    """One round of the reviewer panel — the shared review primitive's result.

    Returned by :func:`run_panel_round`, which ``develop()`` calls every round
    of its implement→review→fix loop and review-only mode (#154) calls once
    against an existing change. ``round_reviews`` is in panel order; ``cost`` is
    the round's total reviewer spend. ``interrupted`` / ``invalid_reviewer``
    short-circuit the panel (the loop stops at the offending reviewer);
    ``resume_after`` is set only when ``interrupted`` is True (the T10 daemon
    re-dispatch surface).
    """

    round_reviews: list[ReviewOutcome]
    cost: float
    interrupted: bool
    resume_after: datetime | None
    invalid_reviewer: str | None


# --- prompt / rendering helpers --------------------------------------------


def _render(template: str, **values: str) -> str:
    """Placeholder substitution that is safe against braces in the values."""
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return out


def _render_findings(findings: list[Finding]) -> str:
    """Render a reviewer's findings as a compact block for the coder's prompt."""
    if not findings:
        return "(no structured findings were listed)"
    lines: list[str] = []
    for f in findings:
        files = ", ".join(f.files) if f.files else "(unspecified)"
        lines.append(f"- [{f.finding_id}] severity={f.severity} status={f.status}")
        lines.append(f"  files: {files}")
        if f.rationale:
            lines.append(f"  rationale: {f.rationale}")
    return "\n".join(lines)


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


# Shared severity rubric injected into every reviewer prompt (#137, ADR 0003 §8)
# so the panel calibrates the same way; the orchestrator then applies each
# reviewer's per-persona ``block_threshold`` to decide what actually blocks.
SEVERITY_CALIBRATION = """## Severity calibration

Give each finding the severity the orchestrator will weigh against this
reviewer's threshold. Calibrate consistently across the panel:

- **critical** — a security vulnerability, a data-loss risk, or a correctness
  defect that breaks an acceptance criterion.
- **major** — a real bug or a significant quality / maintainability problem that
  should be fixed before merge.
- **minor** — a style, naming, or low-impact maintainability nit; recorded but
  usually non-blocking.

Assign the honest severity — do not inflate to force a block or deflate to dodge
one. The threshold decision is the orchestrator's, not yours."""


def _reviewer_brief(spec) -> str:
    """The optional per-reviewer focus paragraph + lane discipline for its prompts.

    A focused persona (``system_prompt`` set) is told to stay strictly in its
    dimension so the panel does not produce N overlapping general reviews. The
    generalist default (no ``system_prompt``) is unchanged — empty string.
    """
    if not spec.system_prompt:
        return ""
    return (
        f"\n## Your focus\n\n{spec.system_prompt}\n\n"
        "**Stay strictly within this focus.** Record only findings in this "
        "dimension — another reviewer owns the rest; do not report outside your "
        "lane.\n"
    )


def _build_run_cmd(
    config: DevelopConfig,
    *,
    agent: str,
    engine: engines.Engine,
    config_dir: Path,
    wt: Path,
    read_only: bool,
) -> tuple[str, list[str]]:
    """Build (container_name, docker-run-argv) for an agent container.

    Model + reasoning effort (#93) are per-TURN flags applied in
    :func:`run_turn`, not container env, so the idle container itself carries no
    agent tuning. All per-tool provisioning — config mount + env var + auth
    source/files + skills — comes off *engine* (#94, ARCH-2.E3): claude
    (``CLAUDE_CONFIG_DIR`` + ``.credentials.json`` + operator skills) vs codex
    (``CODEX_HOME`` + ``auth.json``, no skills — codex honours the worktree
    ``AGENTS.md``). ``build_run_command`` stays engine-blind.
    """
    name = containers.container_name(config.run_id, agent)
    cmd = containers.build_run_command(
        name=name,
        image=config.image,
        worktree=wt,
        config_dir=config_dir,
        handoff_dir=config.handoff_dir,
        config_mount=engine.config_mount,
        config_env_var=engine.config_env_var,
        auth_source_dir=engine.auth_source_dir(config),
        auth_files=engine.auth_files(config),
        skills_dir=engine.skills_dir(config),
        read_only_worktree=read_only,
        # #109: mount the linked worktree's shared .git (RO) so in-container
        # `git diff`/`log`/`show` resolve — reviewers inspect the actual change.
        git_common_dir=worktree.git_common_dir(wt),
    )
    return name, cmd


def _read_review(path: Path) -> tuple[ReviewHandoff | None, str | None]:
    """Read + parse a reviewer handoff. Returns (handoff, error_message)."""
    if not path.is_file():
        return None, "no handoff file was written at the expected path"
    try:
        return handoff.parse_review_handoff(path.read_text(encoding="utf-8")), None
    except HandoffError as exc:
        return None, str(exc)


def _coder_summary(config: DevelopConfig, round_no: int) -> str:
    """Best-effort read of the coder's round-*round_no* summary (seeds review)."""
    path = config.handoff_dir / handoff.coder_handoff_name(round_no)
    try:
        return handoff.parse_review_handoff(
            path.read_text(encoding="utf-8")
        ).summary or ("(the coder wrote no summary)")
    except (HandoffError, OSError):
        return "(coder summary unavailable)"


def _prior_review_text(config: DevelopConfig, round_no: int, reviewer: str) -> str:
    """The outgoing reviewer's most recent handoff text (reseed payload)."""
    for r in range(round_no - 1, 0, -1):
        path = config.handoff_dir / handoff.reviewer_handoff_name(r, reviewer)
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8").strip()
            except OSError:
                break
    return "(no prior review — the limit hit on the first review attempt)"


# --- deterministic gate (T4; #131: ordered multi-check check-set) -----------


# ARCH-1.S2: the check-set builders + gate runners moved to check_runner.py.
# These back-compat aliases keep develop()'s internal call sites and external
# importers (review_only, pr_delivery, tests' monkeypatch targets) resolving
# through this module until S8's public-surface flip deletes them.
_run_check_set = run_check_set
_merge_check_sets = merge_check_sets
_load_gate_ledger = load_gate_ledger
_persist_gate_ledger = persist_gate_ledger


# --- usage-limit reaction (T5) ----------------------------------------------

_CONTINUATION_PROMPT = (
    "You were interrupted by a provider usage limit, which has now lifted. "
    "Continue the task from where you left off. If you had already finished, "
    "just write the handoff file as previously instructed."
)


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


class _PauseBudget:
    """The run's shared usage-limit pause budget, in seconds."""

    def __init__(self, seconds: float) -> None:
        self.remaining = seconds


def _sleep(seconds: float) -> None:
    """Monkeypatch seam — tests must never actually sleep."""
    time.sleep(seconds)


# When a usage-limited run checkpoints WITHOUT a parseable reset hint, suggest
# retrying after this long. Provider windows are typically 1-5h; an hourly
# re-dispatch is bounded and cheap, where re-trying at the pause-poll cadence
# (minutes) would burn a full container spin-up per attempt.
_RESUME_FALLBACK_MINUTES = 60


def _resume_after_from(turn: TurnResult | None) -> datetime:
    """When an interrupted run should be retried (PRD decision #5, T10).

    The provider's parsed reset time when available, else now + a fixed
    fallback delay. Always returns a value — an ``interrupted`` status is by
    definition retryable, so the daemon contract gets a concrete timestamp.
    """
    hint = limits.reset_hint(turn) if turn is not None else None
    return hint or (datetime.now(UTC) + timedelta(minutes=_RESUME_FALLBACK_MINUTES))


def _turn_with_limit_pauses(
    config: DevelopConfig,
    budget: _PauseBudget,
    *,
    agent: str,
    container: str,
    config_dir: Path,
    prompt: str,
    session_id: str,
    resume: bool,
    round_no: int,
    timeout: int,
    engine: engines.Engine,
) -> tuple[TurnResult, bool, float]:
    """Run a turn, pausing-and-retrying through provider usage limits.

    Returns ``(turn, interrupted, total_cost)``: *interrupted* is True when
    the turn was usage-limited and the pause budget ran out — the caller
    checkpoints rather than treating it as an agent failure. Non-limit
    failures return immediately (the existing failure paths own those).
    *total_cost* sums every attempt, not just the last. Every failed turn is
    recorded as a classification fixture (G4 capture harness).
    """
    attempt_prompt, attempt_resume = prompt, resume
    total_cost = 0.0
    while True:
        turn = run_turn(
            container=container,
            prompt=attempt_prompt,
            session_id=session_id,
            resume=attempt_resume,
            timeout=timeout,
            engine=engine,
            model=config.coder_model,
            effort=config.coder_effort,
        )
        total_cost += turn.cost_usd
        # Codex mints its handle (thread_id) on turn 1; rebind so a retry after
        # a usage-limit pause resumes the SAME session (and the transcript
        # check below globs the right id) rather than the stale pre-mint uuid.
        # No-op for claude (echoes the supplied uuid); dormant for codex until
        # codex usage-limits are classified (G4), but kept correct — mirrors
        # the reviewer path's `cur_session` rebind in `_review_turn`.
        if turn.session_id:
            session_id = turn.session_id
        if turn.succeeded:
            return turn, False, total_cost
        limits.record_failure_fixture(
            config.failures_dir, agent=agent, round_no=round_no, turn=turn
        )
        if limits.classify_failure(turn) != limits.USAGE_LIMITED:
            return turn, False, total_cost
        plan = limits.pause_plan(
            turn,
            poll_seconds=config.pause_poll_minutes * 60,
            remaining_seconds=budget.remaining,
        )
        if plan is None:
            logger.warning(
                "story-develop %s: %s usage-limited and the pause budget is "
                "exhausted — checkpointing",
                config.run_id,
                agent,
            )
            return turn, True, total_cost
        logger.info(
            "story-develop %s: %s usage-limited; pausing %.0fs (%s; %.0f min "
            "of pause budget left)",
            config.run_id,
            agent,
            plan.wait_seconds,
            plan.reason,
            budget.remaining / 60,
        )
        _sleep(plan.wait_seconds)
        budget.remaining -= plan.wait_seconds
        # Resume the SAME session when its transcript survived the interruption
        # (the in-session context is the thing we are protecting); otherwise
        # re-issue the original prompt fresh.
        if engine.session_transcript_exists(config_dir, session_id):
            attempt_prompt, attempt_resume = _CONTINUATION_PROMPT, True
        else:
            attempt_prompt, attempt_resume = prompt, resume


# --- per-turn drivers -------------------------------------------------------


def _review_turn(
    config: DevelopConfig,
    *,
    reviewer: str,
    block_threshold: str,
    container: str,
    session_id: str,
    round_no: int,
    resume: bool,
    prompt: str,
    timeout: int,
    engine: engines.Engine,
    model: str | None = None,
    effort: str | None = None,
    validate: Callable[[ReviewHandoff], str | None] | None = None,
) -> tuple[ReviewOutcome, TurnResult | None, str]:
    """Run one reviewer turn against an already-running reviewer container.

    Re-prompts the *same* session once if the handoff is malformed — or, T7,
    if it fails the *validate* callback (the finding-lifecycle check: unknown
    or dropped ids). The handoff is only authoritative if the turn that
    produced it SUCCEEDED (clean exit + structured result) — a failed turn
    that happens to leave a parseable file is rejected, preserving the
    exit-code contract (ADR 0002).

    Returns ``(outcome, failed_turn, session_handle)``: *failed_turn* is the
    TurnResult of a turn-level failure (for usage-limit classification by the
    caller), or ``None`` when the turns ran cleanly (even if the handoff stayed
    invalid). *session_handle* is the handle to resume next round — the inbound
    one for claude, the tool-minted ``thread_id`` for codex (#94).
    """
    review_file = handoff.reviewer_handoff_name(round_no, reviewer)
    review_path = config.handoff_dir / review_file

    def _read_checked() -> tuple[ReviewHandoff | None, str | None]:
        parsed, err = _read_review(review_path)
        if parsed is not None and validate is not None:
            verr = validate(parsed)
            if verr is not None:
                return None, verr
        return parsed, err

    cost = 0.0
    parsed: ReviewHandoff | None = None
    err: str | None = "reviewer did not run"
    failed_turn: TurnResult | None = None

    turn = run_turn(
        container=container,
        prompt=prompt,
        session_id=session_id,
        resume=resume,
        timeout=timeout,
        engine=engine,
        model=model,
        effort=effort,
    )
    cost += turn.cost_usd
    # Codex mints its handle (thread_id) on turn 1; carry the returned handle
    # forward to the in-function retry and back to the caller (no-op for claude,
    # which echoes the supplied uuid).
    cur_session = turn.session_id or session_id
    if not turn.succeeded:
        err = f"reviewer turn failed (exit {turn.exit_code})"
        failed_turn = turn
        limits.record_failure_fixture(
            config.failures_dir,
            agent=f"review-{reviewer}",
            round_no=round_no,
            turn=turn,
        )
    else:
        parsed, err = _read_checked()
        if parsed is None:
            correction = (
                f"Your review at .handoff/{review_file} was not valid: {err}. "
                f"Please rewrite only that file per /workspace/.handoff/FORMAT.md."
            )
            retry = run_turn(
                container=container,
                prompt=correction,
                session_id=cur_session,
                resume=True,
                timeout=timeout,
                engine=engine,
                model=model,
                effort=effort,
            )
            cur_session = retry.session_id or cur_session
            cost += retry.cost_usd
            if retry.succeeded:
                parsed, err = _read_checked()
            else:
                err = f"reviewer retry turn failed (exit {retry.exit_code})"
                failed_turn = retry
                limits.record_failure_fixture(
                    config.failures_dir,
                    agent=f"review-{reviewer}",
                    round_no=round_no,
                    turn=retry,
                )

    if parsed is None:
        logger.warning(
            "story-develop %s: round %d reviewer handoff invalid: %s",
            config.run_id,
            round_no,
            err,
        )
        return (
            ReviewOutcome(
                reviewer=reviewer,
                status="invalid",
                passed=False,
                max_severity=None,
                cost_usd=cost,
            ),
            failed_turn,
            cur_session,
        )
    return (
        ReviewOutcome(
            reviewer=reviewer,
            status=parsed.status,
            passed=parsed.passes(block_threshold),
            max_severity=parsed.max_open_severity,
            findings=parsed.findings,
            cost_usd=cost,
        ),
        None,
        cur_session,
    )


class _ReviewerState:
    """Mutable per-reviewer run state (container, session, tool, ledger)."""

    def __init__(self, spec, container: str, run_cmd: list[str], wt: Path) -> None:
        self.spec = spec
        self.container = container
        self.run_cmd = run_cmd
        # The worktree path, kept so a usage-limit tool switch can rebuild the
        # run command for the NEW tool's env/auth/mount (#94).
        self.wt = wt
        self.session = str(uuid.uuid4())
        # spec.tool is validated (engines.is_supported) before any _ReviewerState
        # is built, so get_engine is safe here. state.json still serialises
        # engine_now.name — the reviewers.<name>.tool string contract is unchanged.
        self.engine_now: engines.Engine = engines.get_engine(spec.tool)
        self.outcome: ReviewOutcome | None = None  # latest completed round
        self.ledger = FindingLedger(spec.name)  # T7: plugin-owned finding ids
        # order-preserving dedupe (see T5 review): never self-switch
        self.chain: tuple[str, ...] = tuple(
            dict.fromkeys((spec.tool, *spec.fallback_chain))
        )


def _run_reviewer_with_reaction(
    config: DevelopConfig,
    budget: _PauseBudget,
    rstate: _ReviewerState,
    *,
    round_no: int,
    resume: bool,
    prompt: str,
    timeout: int,
    base: str,
) -> tuple[ReviewOutcome, float, bool, datetime | None]:
    """One reviewer's round, with the T5 usage-limit reaction wrapped around it.

    Switch first (replace ONLY this reviewer's container, reseed a fresh
    session from the handoff history), pause last (shared budget). Returns
    ``(outcome, cost, interrupted, resume_after)`` — *resume_after* is set
    only when *interrupted* is True (T10 daemon re-dispatch surface).
    """
    name = rstate.spec.name
    review, rev_failed, rstate.session = _review_turn(
        config,
        reviewer=name,
        block_threshold=rstate.spec.block_threshold,
        container=rstate.container,
        session_id=rstate.session,
        round_no=round_no,
        resume=resume,
        prompt=prompt,
        timeout=timeout,
        engine=rstate.engine_now,
        model=rstate.spec.model,
        effort=rstate.spec.effort,
        validate=rstate.ledger.check,
    )
    cost = review.cost_usd

    while (
        rev_failed is not None
        and limits.classify_failure(rev_failed) == limits.USAGE_LIMITED
    ):
        nxt = limits.next_fallback_tool(rstate.chain, rstate.engine_now.name)
        while nxt is not None and not engines.is_supported(nxt):
            logger.warning(
                "story-develop %s: fallback tool %r not supported yet; skipping",
                config.run_id,
                nxt,
            )
            nxt = limits.next_fallback_tool(rstate.chain, nxt)
        if nxt is not None:
            # Replace ONLY this reviewer's container; reseed a fresh session
            # from the handoff history (PRD decision #4).
            logger.info(
                "story-develop %s: reviewer [%s] usage-limited; switching "
                "tool %s -> %s",
                config.run_id,
                name,
                rstate.engine_now.name,
                nxt,
            )
            containers.stop_container(rstate.container)
            rstate.engine_now = engines.get_engine(nxt)
            rstate.session = str(uuid.uuid4())
            # Rebuild the run command for the NEW tool — its env var
            # (CODEX_HOME vs CLAUDE_CONFIG_DIR), auth file, and mount differ, so
            # the original (claude) run_cmd would mis-configure a codex
            # container (#94).
            rstate.container, rstate.run_cmd = _build_run_cmd(
                config,
                agent=f"review-{name}",
                engine=rstate.engine_now,
                config_dir=config.reviewer_config_dir(name),
                wt=rstate.wt,
                read_only=True,
            )
            containers.start_container(rstate.run_cmd)
            reseed_prompt = _render(
                handoff.load_prompt("reviewer_reseed.md"),
                reviewer=name,
                reviewer_brief=_reviewer_brief(rstate.spec),
                round_no=str(round_no),
                acceptance_criteria=config.effective_acceptance_criteria,
                base_sha=base[:12],
                coder_handoff_file=handoff.coder_handoff_name(round_no),
                prior_findings=_render_findings(
                    rstate.outcome.findings if rstate.outcome else []
                ),
                prior_review=_prior_review_text(config, round_no, name),
                review_file=handoff.reviewer_handoff_name(round_no, name),
            )
            review, rev_failed, rstate.session = _review_turn(
                config,
                reviewer=name,
                block_threshold=rstate.spec.block_threshold,
                container=rstate.container,
                session_id=rstate.session,
                round_no=round_no,
                resume=False,
                prompt=reseed_prompt,
                timeout=timeout,
                engine=rstate.engine_now,
                model=rstate.spec.model,
                effort=rstate.spec.effort,
                validate=rstate.ledger.check,
            )
            cost += review.cost_usd
            continue
        # No alternate tool: pause-and-retry within the shared budget.
        plan = limits.pause_plan(
            rev_failed,
            poll_seconds=config.pause_poll_minutes * 60,
            remaining_seconds=budget.remaining,
        )
        if plan is None:
            return review, cost, True, _resume_after_from(rev_failed)
        logger.info(
            "story-develop %s: reviewer [%s] usage-limited; pausing %.0fs "
            "(%s; %.0f min of pause budget left)",
            config.run_id,
            name,
            plan.wait_seconds,
            plan.reason,
            budget.remaining / 60,
        )
        _sleep(plan.wait_seconds)
        budget.remaining -= plan.wait_seconds
        if rstate.engine_now.session_transcript_exists(
            config.reviewer_config_dir(name), rstate.session
        ):
            retry_prompt, retry_resume = _CONTINUATION_PROMPT, True
        else:
            retry_prompt, retry_resume = prompt, resume
        review, rev_failed, rstate.session = _review_turn(
            config,
            reviewer=name,
            block_threshold=rstate.spec.block_threshold,
            container=rstate.container,
            session_id=rstate.session,
            round_no=round_no,
            resume=retry_resume,
            prompt=retry_prompt,
            timeout=timeout,
            engine=rstate.engine_now,
            model=rstate.spec.model,
            effort=rstate.spec.effort,
            validate=rstate.ledger.check,
        )
        cost += review.cost_usd

    return review, cost, False, None


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


def run_panel_round(
    config: DevelopConfig,
    reviewers: list[_ReviewerState],
    *,
    wt: Path,
    base: str,
    round_no: int,
    check_set: CheckSetResult | None,
    gate_ledger: GateLedger,
    budget: _PauseBudget,
    reviewer_timeout: int,
    coder_summary: str,
) -> PanelRoundResult:
    """Drive the reviewer panel for a single round — the one shared primitive.

    ``develop()`` calls this once per round; review-only mode (#154) calls it
    once against an existing change. There is intentionally **one** panel
    implementation — both callers share this so a prompt / severity / lifecycle
    fix can never land in one review path and silently miss the other.

    Each reviewer takes one turn (wrapped in the usage-limit reaction), its
    review is committed to its :class:`~.findings.FindingLedger`, and the
    round's outcomes are returned in panel order. Round 1 renders
    ``reviewer_round.md`` (with the coder's ``coder_summary``); later rounds
    render ``reviewer_rereview.md`` (with the reviewer's open findings + the
    coder's handoff) and resume the reviewer's session. The panel stops early at
    the first interrupted or invalid reviewer.
    """
    round_reviews: list[ReviewOutcome] = []
    cost = 0.0
    interrupted = False
    resume_after: datetime | None = None
    invalid_reviewer: str | None = None
    for rstate in reviewers:
        name = rstate.spec.name
        if round_no == 1:
            review_prompt = _render(
                handoff.load_prompt("reviewer_round.md"),
                reviewer=name,
                reviewer_brief=_reviewer_brief(rstate.spec),
                acceptance_criteria=config.effective_acceptance_criteria,
                coder_summary=coder_summary,
                base_sha=base[:12],
                diff_stat=git.diff_stat(wt, base),
                gate_summary=render_check_summary(
                    check_set, for_coder=False, gate_ledger=gate_ledger
                ),
                severity_calibration=SEVERITY_CALIBRATION,
                review_file=handoff.reviewer_handoff_name(1, name),
            )
            review_resume = False
        else:
            review_prompt = _render(
                handoff.load_prompt("reviewer_rereview.md"),
                reviewer=name,
                reviewer_brief=_reviewer_brief(rstate.spec),
                round_no=str(round_no),
                acceptance_criteria=config.effective_acceptance_criteria,
                base_sha=base[:12],
                coder_handoff_file=handoff.coder_handoff_name(round_no),
                open_findings=rstate.ledger.render_open(),
                diff_stat=git.diff_stat(wt, base),
                gate_summary=render_check_summary(
                    check_set, for_coder=False, gate_ledger=gate_ledger
                ),
                severity_calibration=SEVERITY_CALIBRATION,
                review_file=handoff.reviewer_handoff_name(round_no, name),
            )
            review_resume = True

        review, rev_cost, rev_interrupted, rev_resume_after = (
            _run_reviewer_with_reaction(
                config,
                budget,
                rstate,
                round_no=round_no,
                resume=review_resume,
                prompt=review_prompt,
                timeout=reviewer_timeout,
                base=base,
            )
        )
        cost += rev_cost
        if review.status != "invalid":
            # T7: commit the (already check()-validated) review into the ledger;
            # downstream sees ledger-canonical ids.
            applied = rstate.ledger.apply_review(
                ReviewHandoff(
                    status=review.status,
                    summary="",
                    findings=review.findings,
                ),
                round_no,
            )
            review = replace(review, findings=applied)
        round_reviews.append(review)
        rstate.outcome = review
        if rev_interrupted:
            interrupted = True
            resume_after = rev_resume_after
            break
        if review.status == "invalid":
            invalid_reviewer = name
            break

    return PanelRoundResult(
        round_reviews=round_reviews,
        cost=cost,
        interrupted=interrupted,
        resume_after=resume_after,
        invalid_reviewer=invalid_reviewer,
    )


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
