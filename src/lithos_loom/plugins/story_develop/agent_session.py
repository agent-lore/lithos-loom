"""Per-agent container + turn plumbing for story-develop (ARCH-1.S4).

Moved out of ``develop.py`` behind their public names:

* :func:`build_run_cmd` — ``(container_name, docker-run argv)`` for an agent
  container, with all per-tool provisioning read off the :class:`Engine`
  (ARCH-2.E3);
* :class:`PauseBudget` — the run's shared usage-limit pause budget;
* :func:`turn_with_reactions` — run a turn and apply the failure-class
  reaction table (:mod:`limits`, slice B): pause-and-retry through provider
  usage limits (T5), retry-with-backoff through transient infrastructure
  failures, escalate when a retry class is exhausted; the subtle rebind /
  budget / resume-vs-fresh reaction. It calls its side-effecting seams
  (``run_turn`` / ``sleep``) through the injected :class:`~.rounds.Services`,
  and the per-tool transcript layout it consults to decide resume-vs-fresh
  lives on the :class:`Engine` (``engine.session_transcript_exists``), not here;
* :func:`resume_after_from` — when an interrupted run should be retried (T10).

``develop.py`` keeps ``_`` -prefixed aliases (deleted in S8) so its own call
sites, ``review_only`` / ``pr_delivery`` importers, and the tests' monkeypatch
targets keep resolving through ``develop``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ...runner import worktree
from . import containers, engines, limits
from .config import DevelopConfig
from .rounds import Services
from .turns import TurnAttempt, TurnResult

logger = logging.getLogger(__name__)


def build_run_cmd(
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
    # #283: the artifacts dir must exist at container-create time for the RO
    # bind mount; contents appear as the host collector publishes snapshots.
    config.artifacts_dir.mkdir(parents=True, exist_ok=True)
    cmd = containers.build_run_command(
        name=name,
        image=config.image,
        worktree=wt,
        config_dir=config_dir,
        handoff_dir=config.handoff_dir,
        artifacts_dir=config.artifacts_dir,
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


# --- usage-limit reaction (T5) ----------------------------------------------

_CONTINUATION_PROMPT = (
    "You were interrupted by a provider usage limit, which has now lifted. "
    "Continue the task from where you left off. If you had already finished, "
    "just write the handoff file as previously instructed."
)


class PauseBudget:
    """The run's shared usage-limit pause budget, in seconds."""

    def __init__(self, seconds: float) -> None:
        self.remaining = seconds


INFRA_CONTINUATION_PROMPT = (
    "Your previous turn was cut short by an infrastructure failure (the agent "
    "process died before it could finish; nothing you did was lost). Continue "
    "the task from where you left off. If you had already finished, just write "
    "the handoff file as previously instructed."
)


# When a usage-limited run checkpoints WITHOUT a parseable reset hint, suggest
# retrying after this long. Provider windows are typically 1-5h; an hourly
# re-dispatch is bounded and cheap, where re-trying at the pause-poll cadence
# (minutes) would burn a full container spin-up per attempt.
_RESUME_FALLBACK_MINUTES = 60


def resume_after_from(turn: TurnResult | None) -> datetime:
    """When an interrupted run should be retried (PRD decision #5, T10).

    The provider's parsed reset time when available, else now + a fixed
    fallback delay. Always returns a value — an ``interrupted`` status is by
    definition retryable, so the daemon contract gets a concrete timestamp.
    """
    hint = limits.reset_hint(turn) if turn is not None else None
    return hint or (datetime.now(UTC) + timedelta(minutes=_RESUME_FALLBACK_MINUTES))


def turn_with_reactions(
    config: DevelopConfig,
    budget: PauseBudget,
    *,
    services: Services,
    agent: str,
    container: str,
    config_dir: Path,
    prompt: str,
    session_id: str,
    resume: bool,
    round_no: int,
    timeout: int,
    engine: engines.Engine,
) -> TurnAttempt:
    """Run a turn, applying the failure-class reaction table until it settles.

    A ``usage_limited`` turn pauses within the shared budget and retries (T5);
    ``interrupted`` is set when the budget runs out — the caller checkpoints
    rather than treating it as an agent failure. A ``retry`` class (auth,
    transient infra, a killed process) sleeps its per-attempt backoff — which
    never spends the pause budget — and retries, resuming the session when its
    transcript survived; when its retries are exhausted the attempt carries an
    ``escalation`` line and the caller ends the run ``infra_failed``. Every
    other failure returns at once (the existing failure paths own those). Retry
    counts are kept per class, so a disconnect followed by an auth failure
    spends each class's own budget. Every failed turn is recorded as a
    classification fixture (G4 capture harness).

    The turn and the sleeps run through *services* (ARCH-1.S4) so the loop is
    testable with fakes; the resume-vs-fresh transcript check runs through
    *engine* (per-tool layout lives on the Engine, ARCH-2.E1/E2).
    """
    attempt_prompt, attempt_resume = prompt, resume
    total_cost = 0.0
    attempts: dict[limits.FailureClass, int] = {}
    while True:
        turn = services.run_turn(
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
        # a pause / backoff resumes the SAME session (and the transcript check
        # below globs the right id) rather than the stale pre-mint uuid. No-op
        # for claude (echoes the supplied uuid) — mirrors the reviewer path's
        # `cur_session` rebind in `_review_turn`.
        if turn.session_id:
            session_id = turn.session_id
        if turn.succeeded:
            return TurnAttempt(turn, False, total_cost, session_id=session_id)
        limits.record_failure_fixture(
            config.failures_dir, agent=agent, round_no=round_no, turn=turn
        )
        cls = limits.classify_failure(turn)
        reaction = limits.reaction_for(cls)
        if reaction.kind == "pause":
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
                return TurnAttempt(turn, True, total_cost, session_id=session_id)
            logger.info(
                "story-develop %s: %s usage-limited; pausing %.0fs (%s; %.0f min "
                "of pause budget left)",
                config.run_id,
                agent,
                plan.wait_seconds,
                plan.reason,
                budget.remaining / 60,
            )
            services.sleep(plan.wait_seconds)
            budget.remaining -= plan.wait_seconds
            continuation = _CONTINUATION_PROMPT
        elif reaction.kind == "retry":
            used = attempts.get(cls, 0)
            if used >= reaction.retries:
                summary = limits.failure_summary(turn)
                n = used + 1
                escalation = (
                    (
                        f"{agent} {cls.value} persisted after {n} attempt"
                        f"{'s' if n != 1 else ''}: {summary}"
                    )
                    if reaction.escalate
                    else None
                )
                logger.warning(
                    "story-develop %s: %s %s after %d attempt(s): %s",
                    config.run_id,
                    agent,
                    cls.value,
                    used + 1,
                    summary,
                )
                return TurnAttempt(
                    turn,
                    False,
                    total_cost,
                    escalation,
                    host_action=reaction.host_action if escalation else "",
                    session_id=session_id,
                )
            wait = reaction.backoff_seconds[used]
            attempts[cls] = used + 1
            logger.warning(
                "story-develop %s: %s %s (%s); retrying in %.0fs (attempt %d of %d)",
                config.run_id,
                agent,
                cls.value,
                limits.failure_summary(turn),
                wait,
                used + 2,
                reaction.retries + 1,
            )
            services.sleep(wait)
            continuation = INFRA_CONTINUATION_PROMPT
        else:
            return TurnAttempt(turn, False, total_cost, session_id=session_id)
        # Resume the SAME session when its transcript survived the interruption
        # (the in-session context is the thing we are protecting); otherwise
        # re-issue the original prompt fresh.
        if reaction.resume and engine.session_transcript_exists(config_dir, session_id):
            attempt_prompt, attempt_resume = continuation, True
        else:
            attempt_prompt, attempt_resume = prompt, resume
