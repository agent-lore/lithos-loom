"""The per-round checkpoint: what a run had landed when it was still running.

The rest of the develop-run on-disk contract lives in :mod:`run_outcome`, which
names this marker in its inventory. It is here, one level above that leaf,
because ``run_outcome`` is imported by ``cli/develop.py`` purely to CLASSIFY a
run and must stay stdlib-light; the checkpoint has its own two writers/readers
(``develop()`` and the resume path) and adds a policy — *which* stops may resume
from it — that the classifier has no business carrying.

**Why a nested block, not a top-level ``status``.** The checkpoint records
``status="running"`` — the run is mid-loop when it is written — but it writes it
INSIDE ``state.json``'s ``checkpoint`` key, never at the top level:
:func:`run_outcome.run_phase` reads a top-level ``status`` as the run's terminal
VERDICT, so a top-level ``running`` would make ``develop attach`` report a live
run as finished the moment its first round landed. The loop's terminal write is
unchanged (it merges — :func:`run_outcome.write_state`), so a finished run's
state.json carries both: the verdict at the top, the last boundary's checkpoint
beside it.

What it is for (5dbeb0c8 slice C): every round of a develop run is already a
commit on a local branch in the run's worktree, but until this nothing on disk
said *round 4 of 8, on branch X, base Y* while the run was alive — so a run
whose host died mid-loop (a revoked OAuth token, a vanished coder container)
could only be re-developed from scratch, and the rounds it had paid for were
never used. The checkpoint is what a re-dispatch resumes from
(:mod:`.resume`), and what ``develop attach`` / ``develop list`` read the
current round from instead of guessing it from handoff filenames.
"""

from __future__ import annotations

import contextlib
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .run_outcome import read_state, write_state

logger = logging.getLogger(__name__)

__all__ = [
    "CHECKPOINT_KEY",
    "from_state",
    "RESUMABLE_ESCALATION_REASONS",
    "RoundCheckpoint",
    "record_round_checkpoint",
    "record_round_entered",
    "resumable_checkpoint",
    "round_checkpoint",
]

# ``state.json``'s own block for this marker (nested — see the module docstring).
CHECKPOINT_KEY = "checkpoint"

# The escalation reasons whose re-dispatch may CONTINUE the dead run's branch
# rather than start over (the 2026-09-13 sitting on task 5dbeb0c8). Both are
# verdicts on the HOST, not on the work: `infra` is an auth / transport / spawn
# failure that persisted through the reaction table's retries, and
# `resume_exhausted` is a usage-limited run that ran out of re-dispatches. Every
# other stop — `max_rounds`, `stalled`, `disputed`, `needs_decision` — is a
# verdict on the work itself, and whether "edit the acceptance criteria,
# complete the gate" should continue the branch rather than start over is a
# separate, bigger question that this deliberately does not answer.
RESUMABLE_ESCALATION_REASONS = frozenset({"infra", "resume_exhausted"})


@dataclass(frozen=True)
class RoundCheckpoint:
    """One round boundary of a run that was still going.

    ``round`` / ``cost_usd`` are THIS run's own (the numbers its handoff files
    are named after, and what its ``state.json`` will report); ``branch_rounds``
    / ``branch_cost_usd`` are the BRANCH's, carrying whatever an earlier run
    this one resumed had already spent. A resumed run's remaining budget is
    computed from the branch figures — otherwise each resume would hand the
    story a fresh ceiling and the whole point (money) would be lost — while the
    operator's surfaces show the run's own.
    """

    round: int
    branch: str
    head_sha: str  # the worktree's HEAD at the boundary — what a resume enters
    base_sha: str
    base_ref: str = ""
    commit: str = ""  # the round's OWN commit, when it made one
    repo: str = ""
    worktree: str = ""
    cost_usd: float = 0.0
    branch_rounds: int = 0
    branch_cost_usd: float = 0.0
    # The round the loop ACTUALLY entered after this boundary (always
    # ``round + 1``), or 0 while none has — the round that crashed (exit L), the
    # round that stopped the run, and the last round of an exhausted budget all
    # end a run at a boundary otherwise indistinguishable from a live one
    # between rounds (correctness/f-004). Written by the NEXT round's own first
    # act (:func:`record_round_entered`), never predicted at this boundary: a
    # death in between must leave "N", not a claim about a round that never
    # began. Only the loop knows; nobody may infer it.
    next_round: int = 0
    # The last round whose PANEL actually reviewed (0 = none did). The resume
    # reads its intake from this round, so the round is loom's own answer rather
    # than the agent-writable handoff dir's (security/f-003): a planted
    # `round_NN_review_<panel name>.md` for the round a run died in could
    # otherwise make that round the intake and drop the last real review.
    reviewed_round: int = 0
    at: str = ""
    status: str = "running"

    @property
    def has_committed_round(self) -> bool:
        """Whether this checkpoint has WORK to resume — a round that committed.

        A round that ended without a commit (a coder that never committed, a
        round-1 infra death) leaves the branch at its fork point, and resuming
        there is starting over with extra steps.
        """
        return (
            self.round >= 1 and bool(self.head_sha) and self.head_sha != self.base_sha
        )


def record_round_checkpoint(
    run_dir: Path,
    *,
    round_no: int,
    branch: str,
    head_sha: str,
    base_sha: str,
    base_ref: str = "",
    commit: str = "",
    repo: str = "",
    worktree: str = "",
    cost_usd: float = 0.0,
    branch_rounds: int | None = None,
    branch_cost_usd: float | None = None,
    reviewed_round: int = 0,
) -> None:
    """Record the round *round_no* boundary of a live run. Best-effort.

    Written by the loop after EVERY round, the terminal one included: the round
    that stops the run is also the last one a resume can build on, and the
    process may not survive to write anything else. Merged into ``state.json``
    like every other block there, so a converge intake record and the loop's own
    terminal verdict both survive it.

    ``next_round`` is deliberately NOT a parameter: a boundary never predicts
    the round after it (correctness/f-004) — the next round announces itself
    when it starts, through :func:`record_round_entered`.

    A write failure costs the resume, never the round — the run is mid-loop
    and a raise here would throw away work that is already committed.
    """
    with contextlib.suppress(OSError):
        write_state(
            run_dir,
            {
                CHECKPOINT_KEY: {
                    "status": "running",
                    "round": round_no,
                    "branch": branch,
                    "head_sha": head_sha,
                    "commit": commit,
                    "base_sha": base_sha,
                    "base_ref": base_ref,
                    "repo": repo,
                    "worktree": worktree,
                    "cost_usd": round(cost_usd, 4),
                    "branch_rounds": (
                        round_no if branch_rounds is None else branch_rounds
                    ),
                    "branch_cost_usd": round(
                        cost_usd if branch_cost_usd is None else branch_cost_usd, 4
                    ),
                    "next_round": 0,  # see record_round_entered
                    "reviewed_round": reviewed_round,
                    "at": datetime.now(UTC).isoformat(timespec="seconds"),
                    # For the operator reading the file out of context (every
                    # in-process reader already holds the path).
                    "run_dir": str(run_dir),
                }
            },
        )


def record_round_entered(run_dir: Path, round_no: int) -> None:
    """Record that the loop has ENTERED round *round_no*. Best-effort.

    The next round's own first act, so the claim is only ever made by the round
    making it (correctness/f-004): publishing ``next_round`` at the previous
    boundary would leave a permanent "round N+1 began" on a process killed in
    between, which is a number nobody can correct — the crashed run writes
    nothing else ever.

    A no-op when there is no boundary yet (round 1 of a run: nothing has been
    completed, so there is nothing to amend and the observers fall back to the
    handoff names) and when the recorded round is not this round's predecessor
    (belt for a future caller: the field's contract is ``round + 1``).
    """
    state = read_state(run_dir) or {}
    block = state.get(CHECKPOINT_KEY)
    if not isinstance(block, dict) or block.get("round") != round_no - 1:
        return
    with contextlib.suppress(OSError):
        write_state(run_dir, {CHECKPOINT_KEY: {**block, "next_round": round_no}})


def round_checkpoint(run_dir: Path) -> RoundCheckpoint | None:
    """The last round boundary *run_dir* recorded, or ``None``."""
    return from_state(read_state(run_dir))


def from_state(state: Mapping[str, object] | None) -> RoundCheckpoint | None:
    """The checkpoint inside an already-read ``state.json``, or ``None``.

    The state-dict form exists for ``develop attach``, which reads the file once
    per poll and classifies the run from it: the round it displays must come
    from the same snapshot as the phase it displays, not from a second read that
    could straddle the boundary being reported.

    ``None`` for a run with no checkpoint at all (one that predates this, or one
    that died before its first round boundary), for a block missing any of the
    four fields every consumer needs — the round, the branch, the head it left
    the branch at and the fork point it measured from — and for one whose fields
    CONTRADICT each other. A partial or inconsistent block is not a checkpoint:
    every consumer would have to guess exactly what it exists to record, and the
    two guesses that matter are money and the review range (correctness/f-005) —
    a `branch_rounds` below the round, or a `branch_cost_usd` below the run's
    own, hands a resume budget it has already spent; a missing fork point makes
    the resumed panel review an empty range. Type-valid is not valid: these are
    invariants the WRITER guarantees, so a record that breaks one is corrupt (or
    not loom's), and falling back to a fresh run is the safe direction.
    """
    block = (state or {}).get(CHECKPOINT_KEY)
    if not isinstance(block, dict):
        return None
    raw_round = block.get("round")
    if not isinstance(raw_round, int) or isinstance(raw_round, bool) or raw_round < 1:
        return None
    branch = block.get("branch")
    head_sha = block.get("head_sha")
    base_sha = block.get("base_sha")
    if not isinstance(branch, str) or not branch:
        return None
    if not isinstance(head_sha, str) or not head_sha:
        return None
    if not isinstance(base_sha, str) or not base_sha:
        return None

    def _text(key: str) -> str:
        value = block.get(key)
        return value if isinstance(value, str) else ""

    def _money(key: str, default: float) -> float:
        """A recorded spend: finite and not negative, or the default.

        The resumed run's ceiling is computed from these (security/f-004), and
        ``json.loads`` accepts the ``NaN`` / ``Infinity`` literals — a NaN spend
        would pass every ``<= 0`` budget guard (NaN compares False against
        everything) and install an effectively unlimited ceiling, while a
        negative one would GRANT more budget than the project ever allowed. A
        value loom itself wrote is neither; a corrupt, truncated or planted
        file is exactly where this is read. Mirrors ``_count``'s ``>= 0``.
        """
        value = block.get(key)
        if (
            isinstance(value, int | float)
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value >= 0
        ):
            return float(value)
        return default

    def _count(key: str, default: int) -> int:
        value = block.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        return default

    cost = _money("cost_usd", 0.0)
    branch_rounds = _count("branch_rounds", raw_round)
    branch_cost = _money("branch_cost_usd", cost)
    next_round = _count("next_round", 0)
    reviewed_round = _count("reviewed_round", 0)
    # The writer's own invariants, checked rather than assumed: the branch
    # totals include this run's, the entered round is this round's successor (or
    # nothing yet), and a reviewed round is one that happened.
    for what, ok in (
        (
            f"branch_rounds {branch_rounds} < round {raw_round}",
            branch_rounds >= raw_round,
        ),
        (f"branch_cost_usd {branch_cost} < cost_usd {cost}", branch_cost >= cost),
        (
            f"next_round {next_round} is not 0 or {raw_round + 1}",
            next_round in (0, raw_round + 1),
        ),
        (
            f"reviewed_round {reviewed_round} > round {raw_round}",
            reviewed_round <= raw_round,
        ),
    ):
        if not ok:
            logger.warning("checkpoint rejected — %s", what)
            return None
    return RoundCheckpoint(
        round=raw_round,
        branch=branch,
        head_sha=head_sha,
        base_sha=base_sha,
        base_ref=_text("base_ref"),
        commit=_text("commit"),
        repo=_text("repo"),
        worktree=_text("worktree"),
        cost_usd=cost,
        branch_rounds=branch_rounds,
        branch_cost_usd=branch_cost,
        next_round=next_round,
        reviewed_round=reviewed_round,
        at=_text("at"),
        status=_text("status") or "running",
    )


def resumable_checkpoint(run_dir: Path) -> RoundCheckpoint | None:
    """*run_dir*'s checkpoint iff there is a committed round to resume from.

    The cheap half of the resume decision — no git, no config, no agent
    machinery — so the route-runner can make it on the dispatch path and the
    plugin can re-make it before it spends anything. The expensive half (the
    commit still being in the repo, the budget having anything left) is
    :func:`.resume.prepare_resume`'s.
    """
    checkpoint = round_checkpoint(run_dir)
    if checkpoint is None or not checkpoint.has_committed_round:
        return None
    return checkpoint
