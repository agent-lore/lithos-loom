"""``develop()`` core — the full implement → review → fix → approve loop.

    worktree
      -> start coder (RW) + reviewer (RO) containers, both long-lived
      -> round 1: coder implements, commit, test gate, reviewer reviews
      -> round N: coder fixes (resume), commit, gate, reviewer re-reviews (resume)
      -> stop when the reviewer passes (approved) or max_rounds is hit
      -> tear both containers down; leave the branch + a conversation log.

The test gate (T4) runs each round commit's tree in a fresh throwaway container
— an agent-free check on the coder's self-reported test results. By default it
is recorded but non-blocking; with ``block_on_red`` a red gate prevents
approval and its output is fed to the coder next round.

The two agents keep their sessions **across rounds** (ADR 0002): each round is a
fresh ``docker exec`` that resumes the on-disk session, so the coder remembers
what it tried and the reviewer remembers what it objected to — the whole point
of the conversational model over Ralph++'s fire-and-forget loop.

The side-effecting bits (container start/exec/stop) live in :mod:`containers` /
:mod:`turns` so this orchestration is unit-testable by monkeypatching them.
Stall / dispute / cost guards are deliberately *not* here — they arrive with T7;
T3's only loop bound is ``max_rounds``.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ...runner import detection, git, worktree
from . import containers, handoff, limits, test_gate
from .config import (
    CLAUDE_AUTH_FILES,
    HANDOFF_DIRNAME,
    DevelopConfig,
    is_valid_reviewer_name,
)
from .handoff import Finding, HandoffError, ReviewHandoff
from .test_gate import GateResult
from .turns import TurnResult, run_turn

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReviewOutcome:
    """The result of a single reviewer pass in one round."""

    reviewer: str
    status: str  # "LGTM" | "FINDINGS" | "invalid"
    passed: bool  # by the configured block_threshold
    max_severity: str | None
    findings: list[Finding] = field(default_factory=list)
    cost_usd: float = 0.0

    @property
    def findings_count(self) -> int:
        return len(self.findings)


@dataclass(frozen=True)
class DevelopResult:
    """Outcome of a ``develop()`` run."""

    status: str  # "approved" | "max_rounds" | "failed" | "interrupted"
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
    review: ReviewOutcome | None = None  # the final round's review
    test_gate: GateResult | None = None  # the latest round's gate (T4)
    conversation_log: Path | None = None

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


def _build_run_cmd(
    config: DevelopConfig, *, agent: str, config_dir: Path, wt: Path, read_only: bool
) -> tuple[str, list[str]]:
    """Build (container_name, docker-run-argv) for an agent container."""
    name = containers.container_name(config.run_id, agent)
    cmd = containers.build_run_command(
        name=name,
        image=config.image,
        worktree=wt,
        config_dir=config_dir,
        handoff_dir=config.handoff_dir,
        auth_source_dir=config.claude_config_dir,
        auth_files=containers.resolve_auth_files(config, CLAUDE_AUTH_FILES),
        skills_dir=config.operator_skills_dir,
        read_only_worktree=read_only,
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


def _prior_review_text(config: DevelopConfig, round_no: int) -> str:
    """The outgoing reviewer's most recent handoff text (reseed payload)."""
    for r in range(round_no - 1, 0, -1):
        path = config.handoff_dir / handoff.reviewer_handoff_name(r, config.reviewer)
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8").strip()
            except OSError:
                break
    return "(no prior review — the limit hit on the first review attempt)"


# --- test gate (T4) ---------------------------------------------------------


def _resolve_gate_command(config: DevelopConfig, wt: Path) -> str | None:
    """Pick the test command the gate will run, or ``None`` to skip the gate.

    An explicit ``test_command`` is trusted as-is; otherwise candidates are
    auto-detected from the worktree and the first one whose tool exists in the
    container image wins (the image may lack e.g. ``make`` — see
    :mod:`...runner.detection`).
    """
    if not config.test_gate:
        return None
    if config.test_command:
        return config.test_command
    candidates = detection.detect_test_commands(wt)
    if not candidates:
        logger.info(
            "story-develop %s: test gate skipped (no test command detected)",
            config.run_id,
        )
        return None
    tools = list(dict.fromkeys(c.split()[0] for c in candidates))
    chosen = test_gate.select_command(
        candidates, test_gate.probe_tools(config.image, tools)
    )
    if chosen is None:
        logger.warning(
            "story-develop %s: test gate skipped — none of %s runnable in %s; "
            "set --test-command explicitly",
            config.run_id,
            candidates,
            config.image,
        )
    return chosen


def _run_gate(
    config: DevelopConfig, wt: Path, sha: str, round_no: int, command: str
) -> GateResult | None:
    """Run the gate for one round commit. Infra errors skip the gate (with a
    warning) rather than failing the run — the gate is an independent check,
    not a dependency."""
    round_dir = config.gate_dir / f"round_{round_no:02d}"
    name = containers.container_name(config.run_id, f"gate-r{round_no}")
    try:
        test_gate.export_tree(wt, sha, round_dir / "tree")
        cache = config.gate_dir / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        gate_cmd = test_gate.build_gate_command(
            name=name,
            image=config.image,
            tree=round_dir / "tree",
            cache_dir=cache,
            command=command,
        )
        result = test_gate.run_gate_container(
            gate_cmd, name=name, command=command, timeout=config.test_timeout
        )
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(
            "story-develop %s: round %d test gate errored (skipping): %s",
            config.run_id,
            round_no,
            exc,
        )
        return None
    (round_dir / "output.txt").write_text(
        f"$ {result.command}\nexit: {result.exit_code} ({result.verdict})\n\n"
        f"{result.output_tail}\n",
        encoding="utf-8",
    )
    logger.info(
        "story-develop %s: round %d test gate %s (`%s`, exit %d)",
        config.run_id,
        round_no,
        result.verdict,
        result.command,
        result.exit_code,
    )
    return result


def _gate_note(gate: GateResult | None) -> str:
    """A prompt section describing a red gate (empty when green/absent)."""
    if gate is None or gate.passed:
        return ""
    how = "timed out" if gate.timed_out else f"exit {gate.exit_code}"
    return (
        "\n## Independent test gate (FAILED)\n\n"
        f"The orchestrator independently ran `{gate.command}` against your last "
        f"commit in a clean container and it failed ({how}). This result is "
        "authoritative — fix the failures regardless of how the tests behaved "
        "in your own environment. Output tail:\n\n"
        "```\n" + gate.output_tail + "\n```\n"
    )


# --- usage-limit reaction (T5) ----------------------------------------------

_CONTINUATION_PROMPT = (
    "You were interrupted by a provider usage limit, which has now lifted. "
    "Continue the task from where you left off. If you had already finished, "
    "just write the handoff file as previously instructed."
)


class _PauseBudget:
    """The run's shared usage-limit pause budget, in seconds."""

    def __init__(self, seconds: float) -> None:
        self.remaining = seconds


def _sleep(seconds: float) -> None:
    """Monkeypatch seam — tests must never actually sleep."""
    time.sleep(seconds)


def _tool_supported(tool: str) -> bool:
    """Whether the container/exec layer can run *tool* (codex arrives with T6)."""
    return tool == "claude"


def _session_transcript_exists(config_dir: Path, session_id: str) -> bool:
    """True when the agent's on-disk transcript for *session_id* exists.

    Decides whether a limit-interrupted turn is retried as a ``--resume``
    continuation (partial progress is in the transcript) or re-issued fresh
    (the process died before the session was created).
    """
    projects = config_dir / "projects"
    if not projects.is_dir():
        return False
    return any(projects.glob(f"*/{session_id}.jsonl"))


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
        )
        total_cost += turn.cost_usd
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
        if _session_transcript_exists(config_dir, session_id):
            attempt_prompt, attempt_resume = _CONTINUATION_PROMPT, True
        else:
            attempt_prompt, attempt_resume = prompt, resume


# --- per-turn drivers -------------------------------------------------------


def _review_turn(
    config: DevelopConfig,
    *,
    container: str,
    session_id: str,
    round_no: int,
    resume: bool,
    prompt: str,
    timeout: int,
    tool: str = "claude",
) -> tuple[ReviewOutcome, TurnResult | None]:
    """Run one reviewer turn against an already-running reviewer container.

    Re-prompts the *same* session once if the handoff is malformed. The handoff
    is only authoritative if the turn that produced it SUCCEEDED (clean exit +
    structured result) — a failed turn that happens to leave a parseable file is
    rejected, preserving the exit-code contract (ADR 0002).

    Returns ``(outcome, failed_turn)``: *failed_turn* is the TurnResult of a
    turn-level failure (for usage-limit classification by the caller), or
    ``None`` when the turns ran cleanly (even if the handoff stayed invalid).
    """
    reviewer = config.reviewer
    review_file = handoff.reviewer_handoff_name(round_no, reviewer)
    review_path = config.handoff_dir / review_file
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
        tool=tool,
    )
    cost += turn.cost_usd
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
        parsed, err = _read_review(review_path)
        if parsed is None:
            correction = (
                f"Your review at .handoff/{review_file} was not valid: {err}. "
                f"Please rewrite only that file per /workspace/.handoff/FORMAT.md."
            )
            retry = run_turn(
                container=container,
                prompt=correction,
                session_id=session_id,
                resume=True,
                timeout=timeout,
                tool=tool,
            )
            cost += retry.cost_usd
            if retry.succeeded:
                parsed, err = _read_review(review_path)
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
        )
    return (
        ReviewOutcome(
            reviewer=reviewer,
            status=parsed.status,
            passed=parsed.passes(config.block_threshold),
            max_severity=parsed.max_open_severity,
            findings=parsed.findings,
            cost_usd=cost,
        ),
        None,
    )


# --- orchestration ----------------------------------------------------------


def develop(
    config: DevelopConfig,
    *,
    coder_timeout: int = 3600,
    reviewer_timeout: int = 3600,
) -> DevelopResult:
    """Run the T3 develop loop and return a result.

    The worktree, per-run state, and conversation log are preserved on exit
    (approved, max_rounds, or failed) for inspection; only the containers are
    torn down.
    """
    if config.coder != "claude" or config.reviewer_tool != "claude":
        raise ValueError("unsupported tool: only 'claude' (codex arrives with T6)")
    if not is_valid_reviewer_name(config.reviewer):
        raise ValueError(
            f"invalid reviewer name {config.reviewer!r}: must be lowercase "
            "alphanumerics + hyphens (e.g. 'code-quality')"
        )
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

    config.coder_config_dir.mkdir(parents=True, exist_ok=True)
    config.reviewer_config_dir(config.reviewer).mkdir(parents=True, exist_ok=True)
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
        config_dir=config.coder_config_dir,
        wt=wt,
        read_only=False,
    )
    reviewer_name, reviewer_cmd = _build_run_cmd(
        config,
        agent=f"review-{config.reviewer}",
        config_dir=config.reviewer_config_dir(config.reviewer),
        wt=wt,
        read_only=True,
    )
    coder_session = str(uuid.uuid4())
    reviewer_session = str(uuid.uuid4())

    status = "failed"
    failure_reason = "no rounds ran"
    final_review: ReviewOutcome | None = None
    gate: GateResult | None = None
    gate_command = _resolve_gate_command(config, wt)
    rounds_completed = 0
    coder_cost = 0.0
    review_cost = 0.0
    budget = _PauseBudget(config.max_pause_minutes * 60)
    # Order-preserving dedupe: a fallback chain that repeats the primary tool
    # (e.g. --reviewer-fallback claude --reviewer-fallback codex) must not
    # trap the reviewer in a claude->claude self-switch loop that never
    # reaches the later entries or the pause path.
    reviewer_chain = tuple(
        dict.fromkeys((config.reviewer_tool, *config.reviewer_fallback_chain))
    )
    reviewer_tool_now = config.reviewer_tool

    try:
        containers.start_container(coder_cmd)
        containers.start_container(reviewer_cmd)
        logger.info(
            "story-develop %s: coder %s + reviewer %s started",
            config.run_id,
            coder_name,
            reviewer_name,
        )

        for round_no in range(1, config.max_rounds + 1):
            rounds_completed = round_no
            # --- coder turn ------------------------------------------------
            if round_no == 1:
                coder_prompt = _render(
                    handoff.load_prompt("coder_init.md"),
                    description=config.description,
                    handoff_file=handoff.coder_handoff_name(1),
                )
                coder_resume = False
            else:
                assert final_review is not None  # set by the prior round's review
                coder_prompt = _render(
                    handoff.load_prompt("coder_fix.md"),
                    round_no=str(round_no),
                    reviewer=config.reviewer,
                    acceptance_criteria=config.effective_acceptance_criteria,
                    findings=_render_findings(final_review.findings),
                    test_gate_note=_gate_note(gate),
                    review_file=handoff.reviewer_handoff_name(
                        round_no - 1, config.reviewer
                    ),
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
            )
            coder_cost += attempt_cost
            if coder_interrupted:
                failure_reason = (
                    f"round {round_no}: coder usage-limited; pause budget exhausted"
                )
                status = "interrupted"
                break
            done_present = (
                config.handoff_dir / handoff.coder_handoff_name(round_no)
            ).is_file()
            if not (coder_turn.succeeded and done_present):
                reasons = []
                if not coder_turn.succeeded:
                    reasons.append(f"coder turn failed (exit {coder_turn.exit_code})")
                if not done_present:
                    reasons.append("no coder handoff file")
                failure_reason = f"round {round_no}: " + "; ".join(reasons)
                status = "failed"
                break

            new_commit = git.commit_all(
                wt,
                f"story-develop r{round_no}: {config.description}",
                exclude=[HANDOFF_DIRNAME],
            )
            if round_no == 1 and new_commit is None:
                failure_reason = "round 1: coder produced no commit"
                status = "failed"
                break

            # --- test gate (only when there is a new commit to gate) --------
            if gate_command is not None and new_commit is not None:
                # Overwrite unconditionally: on a gate infra error this clears
                # to None rather than letting a PRIOR commit's result (e.g. a
                # stale RED under block_on_red) stand in for this commit. A
                # round with no new commit keeps the prior result — the tree is
                # unchanged, so it still describes HEAD.
                gate = _run_gate(config, wt, new_commit, round_no, gate_command)

            # --- reviewer turn --------------------------------------------
            if round_no == 1:
                review_prompt = _render(
                    handoff.load_prompt("reviewer_round.md"),
                    reviewer=config.reviewer,
                    acceptance_criteria=config.effective_acceptance_criteria,
                    coder_summary=_coder_summary(config, 1),
                    review_file=handoff.reviewer_handoff_name(1, config.reviewer),
                )
                review_resume = False
            else:
                review_prompt = _render(
                    handoff.load_prompt("reviewer_rereview.md"),
                    reviewer=config.reviewer,
                    round_no=str(round_no),
                    acceptance_criteria=config.effective_acceptance_criteria,
                    base_sha=base[:12],
                    coder_handoff_file=handoff.coder_handoff_name(round_no),
                    review_file=handoff.reviewer_handoff_name(
                        round_no, config.reviewer
                    ),
                )
                review_resume = True

            review, rev_failed = _review_turn(
                config,
                container=reviewer_name,
                session_id=reviewer_session,
                round_no=round_no,
                resume=review_resume,
                prompt=review_prompt,
                timeout=reviewer_timeout,
                tool=reviewer_tool_now,
            )
            review_cost += review.cost_usd

            # --- reviewer usage-limit reaction: switch first, pause last ----
            reviewer_interrupted = False
            while (
                rev_failed is not None
                and limits.classify_failure(rev_failed) == limits.USAGE_LIMITED
            ):
                nxt = limits.next_fallback_tool(reviewer_chain, reviewer_tool_now)
                while nxt is not None and not _tool_supported(nxt):
                    logger.warning(
                        "story-develop %s: fallback tool %r not supported yet; "
                        "skipping",
                        config.run_id,
                        nxt,
                    )
                    nxt = limits.next_fallback_tool(reviewer_chain, nxt)
                if nxt is not None:
                    # Replace ONLY the reviewer container; reseed a fresh
                    # session from the handoff history (PRD decision #4).
                    logger.info(
                        "story-develop %s: reviewer usage-limited; switching "
                        "tool %s -> %s",
                        config.run_id,
                        reviewer_tool_now,
                        nxt,
                    )
                    containers.stop_container(reviewer_name)
                    reviewer_tool_now = nxt
                    reviewer_session = str(uuid.uuid4())
                    containers.start_container(reviewer_cmd)
                    reseed_prompt = _render(
                        handoff.load_prompt("reviewer_reseed.md"),
                        reviewer=config.reviewer,
                        round_no=str(round_no),
                        acceptance_criteria=config.effective_acceptance_criteria,
                        base_sha=base[:12],
                        coder_handoff_file=handoff.coder_handoff_name(round_no),
                        prior_findings=_render_findings(
                            final_review.findings if final_review else []
                        ),
                        prior_review=_prior_review_text(config, round_no),
                        review_file=handoff.reviewer_handoff_name(
                            round_no, config.reviewer
                        ),
                    )
                    review, rev_failed = _review_turn(
                        config,
                        container=reviewer_name,
                        session_id=reviewer_session,
                        round_no=round_no,
                        resume=False,
                        prompt=reseed_prompt,
                        timeout=reviewer_timeout,
                        tool=reviewer_tool_now,
                    )
                    review_cost += review.cost_usd
                    continue
                # No alternate tool: pause-and-retry within the shared budget.
                plan = limits.pause_plan(
                    rev_failed,
                    poll_seconds=config.pause_poll_minutes * 60,
                    remaining_seconds=budget.remaining,
                )
                if plan is None:
                    reviewer_interrupted = True
                    break
                logger.info(
                    "story-develop %s: reviewer usage-limited; pausing %.0fs "
                    "(%s; %.0f min of pause budget left)",
                    config.run_id,
                    plan.wait_seconds,
                    plan.reason,
                    budget.remaining / 60,
                )
                _sleep(plan.wait_seconds)
                budget.remaining -= plan.wait_seconds
                if _session_transcript_exists(
                    config.reviewer_config_dir(config.reviewer), reviewer_session
                ):
                    retry_prompt, retry_resume = _CONTINUATION_PROMPT, True
                else:
                    retry_prompt, retry_resume = review_prompt, review_resume
                review, rev_failed = _review_turn(
                    config,
                    container=reviewer_name,
                    session_id=reviewer_session,
                    round_no=round_no,
                    resume=retry_resume,
                    prompt=retry_prompt,
                    timeout=reviewer_timeout,
                    tool=reviewer_tool_now,
                )
                review_cost += review.cost_usd

            if reviewer_interrupted:
                failure_reason = (
                    f"round {round_no}: reviewer usage-limited; pause budget exhausted"
                )
                status = "interrupted"
                final_review = review
                break
            final_review = review

            if review.status == "invalid":
                failure_reason = f"round {round_no}: reviewer handoff invalid"
                status = "failed"
                break
            if review.passed:
                if gate is not None and not gate.passed and config.block_on_red:
                    logger.info(
                        "story-develop %s: round %d review passed but test gate "
                        "is RED and --block-on-red is set; continuing",
                        config.run_id,
                        round_no,
                    )
                else:
                    status = "approved"
                    break
            # otherwise: loop to the next round (if any remain)
        else:
            # loop exhausted without an approval / failure break
            status = "max_rounds"
    finally:
        containers.stop_container(coder_name)
        containers.stop_container(reviewer_name)

    commits = git.commits_since(wt, base)
    handoff_present = (config.handoff_dir / handoff.coder_handoff_name(1)).is_file()

    log_path = config.run_dir / "conversation.md"
    log_path.write_text(
        handoff.conversation_log(config.handoff_dir, rounds_completed, config.reviewer),
        encoding="utf-8",
    )

    total = coder_cost + review_cost
    gate_part = f"; test gate {gate.verdict} (`{gate.command}`)" if gate else ""
    if status == "approved":
        assert final_review is not None
        sev = f" (max {final_review.max_severity})" if final_review.max_severity else ""
        message = (
            f"approved by [{final_review.reviewer}] in {rounds_completed} "
            f"round(s){sev}{gate_part}; {len(commits)} commit(s) on {branch}; "
            f"cost ${total:.4f}"
        )
    elif status == "max_rounds":
        assert final_review is not None
        sev = f" (max {final_review.max_severity})" if final_review.max_severity else ""
        message = (
            f"NOT approved after {rounds_completed} round(s) (max_rounds); "
            f"last review[{final_review.reviewer}]={final_review.status}{sev}"
            f"{gate_part}; {len(commits)} commit(s) on {branch}; cost ${total:.4f}"
        )
    elif status == "interrupted":
        message = (
            f"INTERRUPTED: {failure_reason}; {len(commits)} commit(s) on {branch}; "
            f"sessions + handoffs preserved in {config.run_dir} (re-run to retry); "
            f"cost ${total:.4f}"
        )
    else:  # failed
        message = f"{failure_reason}{gate_part}; {len(commits)} commit(s) on {branch}"

    # Durable run state (PRD decision #5: resume state is ~free — session ids
    # + handoffs are on disk). Written on every exit, primarily for
    # `interrupted` runs and the future daemon re-dispatch (T10).
    (config.run_dir / "state.json").write_text(
        json.dumps(
            {
                "status": status,
                "run_id": config.run_id,
                "branch": branch,
                "worktree": str(wt),
                "base_sha": base,
                "rounds": rounds_completed,
                "coder_session": coder_session,
                "reviewer_session": reviewer_session,
                "reviewer_tool": reviewer_tool_now,
                "pause_budget_remaining_s": round(budget.remaining, 1),
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
        review=final_review,
        test_gate=gate,
        conversation_log=log_path,
    )
