"""The reviewer panel — the one shared review primitive (ARCH-1.S5).

Extracted from ``develop.py``:

* :class:`ReviewOutcome` / :class:`PanelRoundResult` — the per-reviewer and
  per-round result records; :func:`findings_by_severity` — the panel's
  severity-count record (ADR 0003 §11);
* :class:`ReviewerState` (né ``_ReviewerState``) — mutable per-reviewer run state;
* :func:`run_panel_round` — the #154 primitive ``develop()`` calls once per round
  and review-only mode calls once against an existing change. There is
  intentionally **one** panel implementation so a prompt / severity / lifecycle
  fix can never land in one review path and miss the other.

The reviewer turns + usage-limit reaction (:func:`_review_turn`,
:func:`_run_reviewer_with_reaction`) run their turn + pause sleep through the
injected :class:`~.rounds.Services` (ARCH-1.S4), so the panel is testable with
fakes; the reviewer tool-switch reseed replaces only that reviewer's container
through a direct :func:`containers.start_container` call (a call-time attr lookup
the tests patch on the ``containers`` module).

``develop.py`` re-exports the names its callers (``review_only`` / ``pr_delivery``
/ ``lithos_io`` / tests) import from it today; the public-name flip is S8.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from ...runner import git
from . import containers, engines, handoff, limits
from .agent_session import (
    _CONTINUATION_PROMPT,
    PauseBudget,
    build_run_cmd,
    resume_after_from,
)
from .check_set import CheckSetResult, render_check_summary
from .config import DevelopConfig
from .findings import FindingLedger
from .gate_findings import GateLedger
from .handoff import (
    Finding,
    HandoffError,
    ReviewHandoff,
    render_findings,
    render_prompt,
)
from .rounds import Services
from .turns import TurnResult

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


def _read_review(path: Path) -> tuple[ReviewHandoff | None, str | None]:
    """Read + parse a reviewer handoff. Returns (handoff, error_message)."""
    if not path.is_file():
        return None, "no handoff file was written at the expected path"
    try:
        return handoff.parse_review_handoff(path.read_text(encoding="utf-8")), None
    except HandoffError as exc:
        return None, str(exc)


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


# --- reviewer turns ---------------------------------------------------------


def _review_turn(
    config: DevelopConfig,
    *,
    services: Services,
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
    one for claude, the tool-minted ``thread_id`` for codex (#94). The turn runs
    through *services* (ARCH-1.S4) so the loop is testable with fakes.
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

    turn = services.run_turn(
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
            retry = services.run_turn(
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


class ReviewerState:
    """Mutable per-reviewer run state (container, session, tool, ledger)."""

    def __init__(self, spec, container: str, run_cmd: list[str], wt: Path) -> None:
        self.spec = spec
        self.container = container
        self.run_cmd = run_cmd
        # The worktree path, kept so a usage-limit tool switch can rebuild the
        # run command for the NEW tool's env/auth/mount (#94).
        self.wt = wt
        self.session = str(uuid.uuid4())
        # spec.tool is validated (engines.is_supported) before any ReviewerState
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
    budget: PauseBudget,
    rstate: ReviewerState,
    *,
    services: Services,
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
    only when *interrupted* is True (T10 daemon re-dispatch surface). Turns +
    the pause sleep run through *services*; the tool-switch container replace
    calls ``containers.*`` directly (ARCH-1.S5).
    """
    name = rstate.spec.name
    review, rev_failed, rstate.session = _review_turn(
        config,
        services=services,
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
            rstate.container, rstate.run_cmd = build_run_cmd(
                config,
                agent=f"review-{name}",
                engine=rstate.engine_now,
                config_dir=config.reviewer_config_dir(name),
                wt=rstate.wt,
                read_only=True,
            )
            containers.start_container(rstate.run_cmd)
            reseed_prompt = render_prompt(
                handoff.load_prompt("reviewer_reseed.md"),
                reviewer=name,
                reviewer_brief=_reviewer_brief(rstate.spec),
                round_no=str(round_no),
                acceptance_criteria=config.effective_acceptance_criteria,
                base_sha=base[:12],
                coder_handoff_file=handoff.coder_handoff_name(round_no),
                prior_findings=render_findings(
                    rstate.outcome.findings if rstate.outcome else []
                ),
                prior_review=_prior_review_text(config, round_no, name),
                review_file=handoff.reviewer_handoff_name(round_no, name),
            )
            review, rev_failed, rstate.session = _review_turn(
                config,
                services=services,
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
            return review, cost, True, resume_after_from(rev_failed)
        logger.info(
            "story-develop %s: reviewer [%s] usage-limited; pausing %.0fs "
            "(%s; %.0f min of pause budget left)",
            config.run_id,
            name,
            plan.wait_seconds,
            plan.reason,
            budget.remaining / 60,
        )
        services.sleep(plan.wait_seconds)
        budget.remaining -= plan.wait_seconds
        if rstate.engine_now.session_transcript_exists(
            config.reviewer_config_dir(name), rstate.session
        ):
            retry_prompt, retry_resume = _CONTINUATION_PROMPT, True
        else:
            retry_prompt, retry_resume = prompt, resume
        review, rev_failed, rstate.session = _review_turn(
            config,
            services=services,
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


# --- orchestration ----------------------------------------------------------


def run_panel_round(
    config: DevelopConfig,
    reviewers: list[ReviewerState],
    *,
    wt: Path,
    base: str,
    round_no: int,
    check_set: CheckSetResult | None,
    gate_ledger: GateLedger,
    budget: PauseBudget,
    reviewer_timeout: int,
    coder_summary: str,
    services: Services | None = None,
) -> PanelRoundResult:
    """Drive the reviewer panel for a single round — the one shared primitive.

    ``develop()`` calls this once per round (passing its own ``services`` so the
    tests' ``run_turn`` / ``_sleep`` patches take effect); review-only mode
    (#154) calls it once against an existing change (defaulting to
    :meth:`Services.live`). There is intentionally **one** panel implementation —
    both callers share this so a prompt / severity / lifecycle fix can never land
    in one review path and silently miss the other.

    Each reviewer takes one turn (wrapped in the usage-limit reaction), its
    review is committed to its :class:`~.findings.FindingLedger`, and the
    round's outcomes are returned in panel order. Round 1 renders
    ``reviewer_round.md`` (with the coder's ``coder_summary``); later rounds
    render ``reviewer_rereview.md`` (with the reviewer's open findings + the
    coder's handoff) and resume the reviewer's session. The panel stops early at
    the first interrupted or invalid reviewer.
    """
    resolved = services if services is not None else Services.live()
    round_reviews: list[ReviewOutcome] = []
    cost = 0.0
    interrupted = False
    resume_after: datetime | None = None
    invalid_reviewer: str | None = None
    for rstate in reviewers:
        name = rstate.spec.name
        if round_no == 1:
            review_prompt = render_prompt(
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
            review_prompt = render_prompt(
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
                services=resolved,
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
