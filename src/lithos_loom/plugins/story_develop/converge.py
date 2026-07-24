"""On-demand PR review-convergence loop (converge / ADR 0003 §9 "Shape 1").

``converge_pr`` automates the operator's manual review chore — run the reviewer
panel + deterministic gate on an existing PR, feed the findings to a coder that
fixes the PR branch, re-review, and loop until the panel LGTMs and the gate
floor is clean — then fast-forward-push the fixed branch back to the PR head,
ready for the human merge gate.

It is a **thin orchestrator** over three already-tested pieces, deliberately
adding no new loop of its own (ADR 0004 §1 — the fix loop is single-sourced):

1. **Intake** — :func:`review_only.review_head` runs the panel + gate once at the
   PR head and returns the raw pieces; :attr:`~review_only.IntakeResult.blocking`
   (the same rule review-only's report applies) decides whether it blocks. A
   non-blocking intake short-circuits to ``already_clean`` before any coder
   container is built (the cheapest path for the common re-check).
2. **Fix loop** — :func:`develop` entered via a :class:`~.rounds.LoopEntry` that
   positions a committable worktree at the PR head, diffs against the PR
   merge-base, and seeds round 1's cold-start coder from the intake review
   (converge PR 2). The loop's own ``approved`` / ``disputed`` / ``stalled`` /
   ``cost_exceeded`` / ``max_rounds`` termination is reused verbatim.
3. **Push epilogue** — on approval, :func:`push_to_pr_ref` fast-forwards the
   reviewed HEAD onto the PR head ref under an atomic lease (never a blind
   ``--force`` / history rewrite); a fork PR is refused *pre-loop* and a mid-run
   remote advance surfaces as ``merge_race``.

v1 is local-panel-only: it converges against loom's in-container codex/claude
panel + check-floor, not the GitHub review bots (a deferred slice).
"""

from __future__ import annotations

import dataclasses
import logging
import math
from dataclasses import dataclass
from typing import Literal

from ...runner import git, worktree
from . import review_only
from .config import DevelopConfig
from .develop import DevelopResult, develop
from .pr_delivery import ForkPushUnsupported, MergeRaceDetected, push_to_pr_ref
from .review_resolve import ResolvedChange
from .rounds import LoopEntry

logger = logging.getLogger(__name__)

# The converge verdict. A closed set so a new status can't be added in one place
# (render / exit code / tests) and silently missed in another (finding #5).
ConvergeStatus = Literal[
    "already_clean",
    "converged",
    "not_converged",
    "fork_unsupported",
    "merge_race",
    "failed",
]


@dataclass(frozen=True)
class ConvergeResult:
    """Outcome of a :func:`converge_pr` run.

    ``status`` is the operator-facing verdict and drives the CLI exit code:

    * ``already_clean`` — the intake did not block; no coder ran, nothing pushed.
      Reports on the PR **snapshot resolved before intake** (not a live re-check).
    * ``converged`` — the loop approved; the fixed branch was pushed (unless
      ``no_push``).
    * ``not_converged`` — the loop stopped without approval (``max_rounds`` /
      ``disputed`` / ``stalled`` / ``cost_exceeded``); the fixes are left in the
      local worktree, nothing pushed.
    * ``fork_unsupported`` — the PR head is on a fork loom cannot push to.
    * ``merge_race`` — the PR head advanced remotely mid-run; converge refuses to
      ``--force`` over the contributor's history. Re-run to pick up the new tip.
    * ``failed`` — the intake review was **incomplete** (interrupted / invalid /
      absent panel), or the intake spend already exhausted ``--max-cost`` — there
      was no trustworthy review to seed the fix loop from.

    ``fixer_commits`` counts only the coder's commits (PR head → HEAD), NOT
    ``develop_result.commits`` — converge enters at the PR head with the base set
    to the merge-base, so the loop's own commit span includes the PR's original
    commits (the PR-3 reporting gotcha).
    """

    status: ConvergeStatus
    change: ResolvedChange
    develop_result: DevelopResult | None = None
    fixer_commits: tuple[str, ...] = ()
    pushed: bool = False
    pushed_sha: str = ""
    intake_cost_usd: float = 0.0
    message: str = ""

    @property
    def succeeded(self) -> bool:
        """True when the PR is ready for the human merge gate (nothing left to do)."""
        return self.status in ("already_clean", "converged")

    @property
    def total_cost_usd(self) -> float:
        """Whole-command agent spend: the intake review plus the fix loop."""
        loop = self.develop_result.total_cost_usd if self.develop_result else 0.0
        return self.intake_cost_usd + loop

    def to_json(self) -> dict:
        """Structured summary for ``--json`` / machine consumption."""
        dev = self.develop_result
        return {
            "status": self.status,
            "head_ref": self.change.head_ref,
            "head_branch": self.change.head_branch,
            "base_sha": self.change.base_sha,
            "head_sha": self.change.head_sha,
            "rounds": dev.rounds if dev is not None else 0,
            "develop_status": dev.status if dev is not None else None,
            "fixer_commits": len(self.fixer_commits),
            "pushed": self.pushed,
            "pushed_sha": self.pushed_sha or None,
            "intake_cost_usd": round(self.intake_cost_usd, 4),
            "total_cost_usd": round(self.total_cost_usd, 4),
            "message": self.message,
        }


def converge_pr(
    config: DevelopConfig,
    change: ResolvedChange,
    *,
    no_push: bool = False,
    coder_timeout: int = 3600,
    reviewer_timeout: int = 3600,
) -> ConvergeResult:
    """Run the review-convergence loop against an existing PR *change*.

    See the module docstring for the three-stage flow. Returns a
    :class:`ConvergeResult`; never raises for the expected terminal states
    (fork / merge-race / unapproved) — they are reported via ``status``. Raises
    :class:`ValueError` only for a caller error: an invalid numeric config
    (non-finite / non-positive ``max_cost_usd``, ``max_rounds < 1``) or a
    *change* with no pushable head branch (not a PR).
    """
    # Validate the numeric bounds at this reusable-API boundary, not only in the
    # CLI: a future daemon caller that passes max_cost_usd <= 0 or max_rounds < 1
    # must fail fast here rather than spend on intake and surface the error deep in
    # develop() (or, worse, run an unbounded loop).
    # NaN compares False against everything — `<= 0` here AND every later budget
    # comparison — so a NaN ceiling would silently behave as unlimited; reject
    # non-finite values outright.
    if config.max_cost_usd is not None and (
        not math.isfinite(config.max_cost_usd) or config.max_cost_usd <= 0
    ):
        raise ValueError(
            f"max_cost_usd must be finite and > 0, got {config.max_cost_usd}"
        )
    if config.max_rounds < 1:
        raise ValueError(f"max_rounds must be >= 1, got {config.max_rounds}")
    # Same fail-fast rationale for the change itself: converge delivers to a PR
    # head branch. A range/branch-resolved change (no pushable branch) would
    # spend the whole intake + loop and then die in the push epilogue with a
    # misleading fork error from an ls-remote on an empty ref.
    if not change.head_branch:
        raise ValueError(
            f"change {change.head_ref!r} has no pushable head branch "
            "(not a PR?); converge requires a PR"
        )

    # Fork guard, pre-loop: loom pushes fixes under origin credentials, so a PR
    # whose head lives on a fork can never be pushed back. Refuse before spending
    # any reviewer/coder containers on a run we could not deliver.
    if change.is_fork:
        return ConvergeResult(
            status="fork_unsupported",
            change=change,
            message=(
                f"PR {change.head_ref} head is on a fork; converge cannot push "
                "fixes back under origin credentials"
            ),
        )

    # --- intake: one panel + gate pass at the PR head ---
    # Run intake under a DISTINCT run_id so its round-1 artifacts (handoff dir,
    # gate export at gate_dir/round_01/tree, container names — all run_id-derived)
    # never collide with the fix loop's own round 1. `export_tree` overlays and
    # `seed_handoff_dir` doesn't clear, so a shared run_id would let intake's head
    # export / stale reviewer handoff bleed into the fixed-tree gate + panel
    # (finding #1). The in-memory intake seed (reviews + check-set) carries over
    # regardless of run_id.
    intake_config = dataclasses.replace(config, run_id=f"{config.run_id}-intake")
    intake = review_only.review_head(
        intake_config, change, reviewer_timeout=reviewer_timeout
    )
    intake_cost = intake.panel.cost if intake.panel is not None else 0.0

    if intake.incomplete:
        # The panel produced no usable review (interrupted / invalid / absent).
        # There is nothing trustworthy to seed the fix loop from — surface it as a
        # failure rather than fixing against a partial/absent review (finding #2).
        logger.info(
            "converge %s: %s intake did not complete", config.run_id, change.head_ref
        )
        return ConvergeResult(
            status="failed",
            change=change,
            intake_cost_usd=intake_cost,
            message="intake review did not complete (interrupted / invalid panel) "
            "— cannot seed the fix loop",
        )
    # Whole-command budget: the intake spend alone must not meet the ceiling. If
    # it does, stop with `failed` REGARDLESS of whether the intake was clean or
    # blocking — checked BEFORE the already-clean return so a clean intake can't
    # bypass the budget contract (finding #2). (The intake is one atomic review
    # pass and can't be sub-bounded; --max-cost then bounds only the fix loop.)
    if config.max_cost_usd is not None and intake_cost >= config.max_cost_usd:
        logger.info(
            "converge %s: intake spend $%.2f exhausted --max-cost $%.2f",
            config.run_id,
            intake_cost,
            config.max_cost_usd,
        )
        return ConvergeResult(
            status="failed",
            change=change,
            intake_cost_usd=intake_cost,
            message=f"intake review spent ${intake_cost:.2f}, meeting the --max-cost "
            f"${config.max_cost_usd:.2f} ceiling before the fix loop",
        )

    if not intake.blocking:
        logger.info(
            "converge %s: %s intake already clean", config.run_id, change.head_ref
        )
        return ConvergeResult(
            status="already_clean",
            change=change,
            intake_cost_usd=intake_cost,
            message="intake review is already clean — nothing to converge",
        )

    # Carry the intake spend into the loop budget so --max-cost bounds the WHOLE
    # command, not just the loop. The exhaustion check above guarantees the
    # remainder is > 0 here.
    loop_config = config
    if config.max_cost_usd is not None:
        loop_config = dataclasses.replace(
            config, max_cost_usd=config.max_cost_usd - intake_cost
        )

    # --- fix loop: enter develop() on the PR branch, seeded from the intake ---
    logger.info(
        "converge %s: %s intake blocks — entering fix loop",
        config.run_id,
        change.head_ref,
    )
    assert intake.panel is not None  # narrowed by the `intake.incomplete` guard
    entry = LoopEntry(
        worktree_factory=lambda cfg: worktree.create_on_branch(
            cfg.repo, change.head_sha, cfg.description, parent=cfg.worktree_parent
        ),
        base_override=change.base_sha,
        intake_reviews=intake.panel.round_reviews,
        intake_check_set=intake.check_set,
    )
    result = develop(
        loop_config,
        coder_timeout=coder_timeout,
        reviewer_timeout=reviewer_timeout,
        entry=entry,
    )

    # Only the fixer's commits (PR head → HEAD), never develop()'s own span
    # (merge-base → HEAD includes the PR's original commits — the reporting gotcha).
    fixer_commits = tuple(git.commits_since(result.worktree, change.head_sha))

    if not result.approved:
        return ConvergeResult(
            status="not_converged",
            change=change,
            develop_result=result,
            fixer_commits=fixer_commits,
            intake_cost_usd=intake_cost,
            message=result.message,
        )

    # --- push epilogue: fast-forward the fixed branch onto the PR head ref ---
    if no_push:
        return ConvergeResult(
            status="converged",
            change=change,
            develop_result=result,
            fixer_commits=fixer_commits,
            intake_cost_usd=intake_cost,
            message="converged — push skipped (--no-push)",
        )
    try:
        pushed_sha = push_to_pr_ref(
            result.worktree,
            result.branch,
            change.head_branch,
            expected_remote_sha=change.head_sha,
        )
    except MergeRaceDetected as exc:
        return ConvergeResult(
            status="merge_race",
            change=change,
            develop_result=result,
            fixer_commits=fixer_commits,
            intake_cost_usd=intake_cost,
            message=str(exc),
        )
    except ForkPushUnsupported as exc:  # defensive — forks are guarded pre-loop
        return ConvergeResult(
            status="fork_unsupported",
            change=change,
            develop_result=result,
            fixer_commits=fixer_commits,
            intake_cost_usd=intake_cost,
            message=str(exc),
        )
    logger.info(
        "converge %s: pushed %s -> %s",
        config.run_id,
        pushed_sha[:12],
        change.head_branch,
    )
    return ConvergeResult(
        status="converged",
        change=change,
        develop_result=result,
        fixer_commits=fixer_commits,
        pushed=True,
        pushed_sha=pushed_sha,
        intake_cost_usd=intake_cost,
        message=f"converged and pushed to {change.head_branch}",
    )
