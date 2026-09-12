"""Coder-side salvage + the handoff nudge (slice B, 5dbeb0c8; #114 / #298 twin).

Two small decisions :func:`rounds.coder_phase` makes after the coder's turn,
kept out of ``rounds`` for its line budget:

* :func:`nudge_for_handoff` — the #114 re-prompt (a clean turn left work but
  no handoff), now routed through the reaction wrapper like every other turn,
  so an infra death *during* the nudge gets the same retry / escalate
  treatment instead of a bare ``run_turn``;
* :func:`verdict` — what a finished attempt means for the round: proceed,
  proceed by **salvage** (the turn died on infra AFTER writing this round's
  handoff — the work product is authoritative, exactly as for reviewers in
  #298; provenance-guarded by a pre-turn content fingerprint so a stale file
  from an earlier attempt or dispatch is never accepted), ``infra_failed`` (a
  retry class was exhausted), or the plain ``failed`` exit.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from . import handoff, limits
from .turns import TurnAttempt, TurnResult

if TYPE_CHECKING:
    from .rounds import RoundContext

logger = logging.getLogger(__name__)


def nudge_for_handoff(ctx: RoundContext, round_no: int) -> TurnAttempt:
    """Re-prompt the coder once to write the missing handoff (#114)."""
    config = ctx.config
    logger.warning(
        "story-develop %s: round %d coder ended its turn with uncommitted "
        "changes but no handoff — re-prompting once to write it",
        config.run_id,
        round_no,
    )
    attempt = ctx.turn_with_reactions(
        config,
        ctx.budget,
        services=ctx.services,
        agent="coder",
        container=ctx.coder_container,
        config_dir=config.coder_config_dir,
        prompt=ctx.coder_handoff_nudge(round_no),
        session_id=ctx.coder_session,
        resume=True,
        round_no=round_no,
        timeout=ctx.coder_timeout,
        engine=ctx.coder_engine,
    )
    ctx.coder_cost += attempt.cost
    if attempt.turn.session_id:
        ctx.coder_session = attempt.turn.session_id
    return attempt


def written_by_dying_attempt(
    turn: TurnResult, done_path: Path, pre_turn: str | None
) -> bool:
    """True when a FAILED infra-class turn wrote (or rewrote) the handoff itself.

    Only a retry class (auth / transport / spawn — the reaction table's
    ``retry`` kind) qualifies: a crashed or timed-out coder that also wrote a
    handoff is the ordinary failure path, not infra. Usage-limited turns never
    reach here (the wrapper pauses them).
    """
    if turn.succeeded or not done_path.is_file():
        return False
    if limits.reaction_for(limits.classify_failure(turn)).kind != "retry":
        return False
    return handoff.file_fingerprint(done_path) != pre_turn


def verdict(
    run_id: str, attempt: TurnAttempt, done_path: Path, pre_turn: str | None
) -> tuple[str, str] | None:
    """``None`` to proceed with the round, else ``(status, reason)`` for the exit."""
    turn = attempt.turn
    if turn.succeeded and done_path.is_file():
        return None
    if written_by_dying_attempt(turn, done_path, pre_turn):
        logger.warning(
            "story-develop %s: coder turn failed (%s) after writing its handoff — "
            "salvaging the round's work product%s",
            run_id,
            limits.failure_summary(turn),
            f" (dropping: {attempt.escalation})" if attempt.escalation else "",
        )
        return None
    if attempt.escalation is not None:
        return "infra_failed", attempt.escalation
    reasons: list[str] = []
    if not turn.succeeded:
        reasons.append(f"coder turn failed (exit {turn.exit_code})")
    if not done_path.is_file():
        reasons.append("no coder handoff file")
    return "failed", "; ".join(reasons)
