"""The converge verdict — :class:`ConvergeResult`, :class:`ConflictSummary`
and the closed :data:`ConvergeStatus` set — as a leaf module.

Split out of :mod:`.converge` (the mode entry points + the shared fix-loop
tail, at the module budget) so the CLI, the resolve eval harness and the
watcher's result reader import the types without the modes; ``converge``
re-exports them, so ``from .converge import ConvergeResult`` still works.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .develop import DevelopResult
from .external_reviews import ExternalOutcome
from .findings import DeferredFinding
from .review_resolve import ResolvedChange

__all__ = ["ConflictSummary", "ConvergeResult", "ConvergeStatus"]

# The converge verdict. A closed set so a new status can't be added in one place
# (render / exit code / tests) and silently missed in another (finding #5).
ConvergeStatus = Literal[
    "already_clean",
    "converged",
    "triage_rejected",
    "not_converged",
    "infra_failed",
    "fork_unsupported",
    "merged",
    "merge_race",
    "failed",
    "no_conflict",
    "conflict_unsupported",
    "base_moved",
]


@dataclass(frozen=True)
class ConflictSummary:
    """Resolve mode (PRD S5): what the run set out to resolve."""

    paths: tuple[str, ...]
    base_ref: str
    base_sha: str

    def to_json(self) -> dict:
        return {
            "paths": list(self.paths),
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
        }


@dataclass(frozen=True)
class ConvergeResult:
    """Outcome of a :func:`converge_pr` run.

    ``status`` is the operator-facing verdict and drives the CLI exit code:

    * ``already_clean`` — the intake did not block; no coder ran, nothing pushed.
      In external mode (#380): every injected finding was refuted by triage or
      dispositioned ``no_change_needed`` by the round-1 coder, which committed
      nothing — reported, not remediated.
      Reports on the PR **snapshot resolved before intake** (not a live re-check).
    * ``converged`` — the loop approved; the fixed branch was pushed (unless
      ``no_push``).
    * ``not_converged`` — the loop stopped without approval (``max_rounds`` /
      ``disputed`` / ``stalled`` / ``cost_exceeded``); the fixes are left in the
      local worktree, nothing pushed.
    * ``infra_failed`` — the loop died on the host (an auth / transport / spawn
      failure persisted through the reaction table's retries, slice B): NOT a
      verdict on the change, so the watcher refunds the S5b round and keeps the
      conflict-resolve pair armed (#377); ``host_action`` rides the JSON.
    * ``fork_unsupported`` — the PR head is on a fork loom cannot push to.
    * ``merged`` — the PR has already landed; there is nothing to converge and
      any fix commit would be unlandable on it.
    * ``merge_race`` — the PR head advanced remotely mid-run; converge refuses to
      ``--force`` over the contributor's history. Re-run to pick up the new tip.
    * ``failed`` — the intake review was **incomplete** (interrupted / invalid /
      absent panel), or the pre-loop spend (intake review, or external-mode
      triage) already exhausted ``--max-cost`` — there was no trustworthy
      review to seed the fix loop from.
    * ``triage_rejected`` — external mode only: every injected finding was
      rejected by triage with cited evidence; no coder ran, nothing pushed.
      The rejections ride on ``external_outcomes`` for the caller's replies.

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
    # 819370e5 (PR #342 review): out-of-scope findings the INTAKE panel filed.
    # Converge has no Lithos source task, so nothing spawns — these must be
    # surfaced or they are lost. Loop-phase deferrals live on
    # ``develop_result.deferred_findings``; see :attr:`deferred_findings`.
    intake_deferred: tuple[DeferredFinding, ...] = ()
    # External mode (PRD S2): per-injected-finding dispositions — triage
    # rejections (with evidence) + the coder's round-1 claims — for the
    # caller's thread-reply epilogue. Empty on the local-panel path.
    external_outcomes: tuple[ExternalOutcome, ...] = ()
    # Resolve mode (PRD S5): the conflict this run addressed; None on the
    # other modes and on a `no_conflict` exit.
    conflict: ConflictSummary | None = None
    # ``infra_failed`` only (#377): what to fix on the host — the loop's own
    # when it ran, the intake panel's when the panel died before any loop
    host_action: str = ""

    @property
    def deferred_findings(self) -> tuple[DeferredFinding, ...]:
        """Every out-of-scope deferral this command produced (intake + loop).

        Both halves are needed: intake outcomes never enter the fix loop's
        ledgers, and an ``already_clean`` exit has no ``develop_result`` at
        all. May contain near-duplicates when the loop's fresh panel re-defers
        an intake finding — operator-visible, deliberately un-deduplicated.
        """
        loop = (
            self.develop_result.deferred_findings
            if self.develop_result is not None
            else ()
        )
        return self.intake_deferred + loop

    @property
    def undecided_external(self) -> tuple[ExternalOutcome, ...]:
        """External findings the run could not settle: a fix the loop made
        and then undid (``reverted``, #387) — the reviewer and the story's
        acceptance criteria disagree, and that is the operator's call."""
        return tuple(o for o in self.external_outcomes if o.disposition == "reverted")

    @property
    def succeeded(self) -> bool:
        """True when the PR is ready for the human merge gate (nothing left to
        do) — a converged tree with an undecided external finding is not."""
        return (
            self.status in ("already_clean", "converged", "triage_rejected")
            and not self.undecided_external
        )

    @property
    def total_cost_usd(self) -> float:
        """Whole-command agent spend: the intake review plus the fix loop."""
        loop = self.develop_result.total_cost_usd if self.develop_result else 0.0
        return self.intake_cost_usd + loop

    def to_json(self) -> dict:
        """Structured summary for ``--json`` / machine consumption."""
        dev = self.develop_result
        deferred = [
            {
                "reviewer": f.reviewer,
                "finding_id": f.finding_id,
                "severity": f.severity,
                "rationale": f.rationale,
                "deferral_reason": f.deferral_reason,
            }
            for f in self.deferred_findings
        ]
        external = [
            {
                "finding_id": o.finding_id,
                "author": o.finding.author,
                "source": o.finding.source,
                "stream": o.finding.stream.value,
                "activity_id": o.finding.activity_id,
                "reply_mode": o.finding.reply_mode.value,
                "thread_url": o.finding.thread_url,
                "disposition": o.disposition,
                "detail": o.detail,
            }
            for o in self.external_outcomes
        ]
        return {
            "deferred_findings": deferred,
            "external_outcomes": external,
            "status": self.status,
            "conflict": self.conflict.to_json() if self.conflict else None,
            # The one success verdict (PR #361 review): a consumer that
            # judged by `status == "converged"` read `triage_rejected` —
            # every external claim refuted with evidence — as a failure.
            "succeeded": self.succeeded,
            "head_ref": self.change.head_ref,
            "head_branch": self.change.head_branch,
            "base_sha": self.change.base_sha,
            "head_sha": self.change.head_sha,
            "rounds": dev.rounds if dev is not None else 0,
            "develop_status": dev.status if dev is not None else None,
            # #377: what to fix on the host when the run ended `infra_failed`
            "host_action": self.host_action,
            "fixer_commits": len(self.fixer_commits),
            "pushed": self.pushed,
            "pushed_sha": self.pushed_sha or None,
            "intake_cost_usd": round(self.intake_cost_usd, 4),
            "total_cost_usd": round(self.total_cost_usd, 4),
            "message": self.message,
        }
