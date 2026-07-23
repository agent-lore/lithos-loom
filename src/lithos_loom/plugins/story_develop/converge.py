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
   fixed branch onto the PR head ref (never ``--force``); a fork PR is refused
   *pre-loop* and a mid-run remote advance surfaces as ``merge_race``.

v1 is local-panel-only: it converges against loom's in-container codex/claude
panel + check-floor, not the GitHub review bots (a deferred slice).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ...runner import git, worktree
from . import review_only
from .config import DevelopConfig
from .develop import DevelopResult, develop
from .pr_delivery import ForkPushUnsupported, MergeRaceDetected, push_to_pr_ref
from .review_resolve import ResolvedChange
from .rounds import LoopEntry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConvergeResult:
    """Outcome of a :func:`converge_pr` run.

    ``status`` is the operator-facing verdict and drives the CLI exit code:

    * ``already_clean`` — the intake did not block; no coder ran, nothing pushed.
    * ``converged`` — the loop approved; the fixed branch was pushed (unless
      ``no_push``).
    * ``not_converged`` — the loop stopped without approval (``max_rounds`` /
      ``disputed`` / ``stalled`` / ``cost_exceeded`` / ``failed``); the fixes are
      left in the local worktree, nothing pushed.
    * ``fork_unsupported`` — the PR head is on a fork loom cannot push to.
    * ``merge_race`` — the PR head advanced remotely mid-run; converge refuses to
      ``--force`` over the contributor's history. Re-run to pick up the new tip.
    * ``failed`` — the intake review could not be produced (panel crash), so
      there was nothing to seed the loop from.

    ``fixer_commits`` counts only the coder's commits (PR head → HEAD), NOT
    ``develop_result.commits`` — converge enters at the PR head with the base set
    to the merge-base, so the loop's own commit span includes the PR's original
    commits (the PR-3 reporting gotcha).
    """

    status: str
    change: ResolvedChange
    develop_result: DevelopResult | None = None
    fixer_commits: tuple[str, ...] = ()
    pushed: bool = False
    pushed_sha: str = ""
    message: str = ""

    @property
    def succeeded(self) -> bool:
        """True when the PR is ready for the human merge gate (nothing left to do)."""
        return self.status in ("already_clean", "converged")

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
    (fork / merge-race / unapproved) — they are reported via ``status``.
    """
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
    intake = review_only.review_head(config, change, reviewer_timeout=reviewer_timeout)
    if not intake.blocking:
        logger.info(
            "converge %s: %s intake already clean", config.run_id, change.head_ref
        )
        return ConvergeResult(
            status="already_clean",
            change=change,
            message="intake review is already clean — nothing to converge",
        )
    if intake.panel is None:
        # Blocking because the intake panel produced no review at all (a crash /
        # interrupt before any reviewer ran): there is nothing to seed the
        # cold-start coder from. Surface it rather than crash on round_reviews.
        return ConvergeResult(
            status="failed",
            change=change,
            message="intake review did not complete — cannot seed the fix loop",
        )

    # --- fix loop: enter develop() on the PR branch, seeded from the intake ---
    logger.info(
        "converge %s: %s intake blocks — entering fix loop",
        config.run_id,
        change.head_ref,
    )
    entry = LoopEntry(
        worktree_factory=lambda cfg: worktree.create_on_branch(
            cfg.repo, change.head_sha, cfg.description, parent=cfg.worktree_parent
        ),
        base_override=change.base_sha,
        intake_reviews=intake.panel.round_reviews,
        intake_check_set=intake.check_set,
    )
    result = develop(
        config,
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
            message=result.message,
        )

    # --- push epilogue: fast-forward the fixed branch onto the PR head ref ---
    if no_push:
        return ConvergeResult(
            status="converged",
            change=change,
            develop_result=result,
            fixer_commits=fixer_commits,
            pushed=False,
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
            message=str(exc),
        )
    except ForkPushUnsupported as exc:  # defensive — forks are guarded pre-loop
        return ConvergeResult(
            status="fork_unsupported",
            change=change,
            develop_result=result,
            fixer_commits=fixer_commits,
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
        message=f"converged and pushed to {change.head_branch}",
    )
