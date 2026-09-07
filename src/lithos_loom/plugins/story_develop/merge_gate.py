"""Trial-merge a delivered PR into its current base and gate the result (PRD S3).

A delivered PR waits behind its ``pr`` gate while the base branch moves on.
GitHub answers one question about that — *does it still merge?* — and even
then without naming the conflicting paths. This module answers the one the
operator actually asks before pressing merge: **will the base break if I
merge this now?** It is zero tokens: a deterministic check-set on a different
tree.

In a throwaway worktree positioned at the PR head:

1. merge the base's **current tip** (``--no-ff``, so a base move yields a real
   merge commit whose parents are the head and the base — never a rewrite of
   which commit the head is);
2. a **conflict** returns the unmerged paths and stops — the only source of
   that list, and the input S5's resolver needs;
3. otherwise run the project's **current** check-set (PRD S3 decision: the
   base is defended by today's gate, not the one the story passed a week ago)
   on the merge result;
4. green **and behind** → push the merge commit onto the PR branch through the
   same leased, ancestry-proved push converge uses (ADR 0011 decision 2:
   additive is automatic). Red, errored, a vacuous check-set, or an
   up-to-date PR push nothing — an unverified or pointless update is never
   written to someone's PR.

The worktree and its branch are removed afterwards (the merge commit object
survives for the pushed ref). ``keep_worktree`` retains the merged tree for
inspection. Forks are refused before any git work: the sweep must never
fetch a third-party head into the operator's checkout, and the push could
not land under origin credentials anyway.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from ...runner import git, worktree
from .check_runner import build_check_set, run_check_set
from .check_set import Check, CheckSetResult
from .config import DevelopConfig
from .gate_findings import GateLedger
from .pr_delivery import ForkPushUnsupported, MergeRaceDetected, push_to_pr_ref
from .review_resolve import ResolvedChange

__all__ = [
    "MergeGateCheck",
    "MergeGateResult",
    "MergeGateStatus",
    "config_fingerprint",
    "run_merge_gate",
]

logger = logging.getLogger(__name__)

MergeGateStatus = Literal[
    "green",  # the check-set passed on the merge result
    "red",  # a blocking check failed on the merge result
    "errored",  # the check-set could not run (infra) — no verdict, nothing pushed
    "no_checks",  # the project's current check-set is empty — vacuous, nothing pushed
    "conflict",  # the base no longer merges; `conflicting_paths` names why
    "fork_unsupported",  # a fork PR: refused before any git work
]


@dataclass(frozen=True)
class MergeGateCheck:
    """One check's outcome on the merge result, flattened for the record."""

    name: str
    command: str
    state: str
    stage: str
    outcome: str
    passed: bool
    exit_code: int | None = None
    timed_out: bool = False
    output_tail: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": self.command,
            "state": self.state,
            "stage": self.stage,
            "outcome": self.outcome,
            "passed": self.passed,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "output_tail": self.output_tail,
        }


@dataclass(frozen=True)
class MergeGateResult:
    """The outcome of one trial merge + gate.

    ``status`` is the gate verdict; the push is reported beside it
    (``pushed`` / ``pushed_sha`` / ``push_error``) rather than folded into it,
    so a green gate whose update lost a race to a human push still reads as
    green. ``merge_sha`` is the gated tree: the merge commit when the PR was
    behind, the PR head itself when it was up to date, empty on a conflict.
    ``config_fingerprint`` identifies the check-set + image that produced the
    verdict — the third component of the sweep's re-run key.
    """

    status: MergeGateStatus
    change: ResolvedChange
    base_ref: str = ""
    base_sha: str = ""
    head_sha: str = ""
    merge_sha: str = ""
    behind: bool = False
    conflicting_paths: tuple[str, ...] = ()
    checks: tuple[MergeGateCheck, ...] = ()
    verdict: str | None = None
    config_fingerprint: str = ""
    pushed: bool = False
    pushed_sha: str = ""
    push_error: str = ""
    message: str = ""
    worktree: Path | None = field(default=None, compare=False)

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "head_ref": self.change.head_ref,
            "head_branch": self.change.head_branch,
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "merge_sha": self.merge_sha,
            "behind": self.behind,
            "conflicting_paths": list(self.conflicting_paths),
            "checks": [c.to_json() for c in self.checks],
            "verdict": self.verdict,
            "config_fingerprint": self.config_fingerprint,
            "pushed": self.pushed,
            "pushed_sha": self.pushed_sha,
            "push_error": self.push_error,
            "message": self.message,
        }


def config_fingerprint(config: DevelopConfig, checks: tuple[Check, ...]) -> str:
    """A short stable digest of *what gated*: the resolved checks + the image.

    The PRD's re-run key is ``(head_sha, base_sha, config fingerprint)`` —
    without the third component a tightened check-set would never re-gate
    an already-observed PR, which is precisely the case re-resolving the
    project's current config exists to catch. Commands, states and stages
    are part of it; run-local paths and names are not.
    """
    payload = {
        "image": config.image,
        "checks": [[c.name, c.command, c.state, c.stage, c.raw_exit] for c in checks],
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8"))
    return digest.hexdigest()[:16]


def _flatten(result: CheckSetResult) -> tuple[MergeGateCheck, ...]:
    rows = []
    for r in result.results:
        gate = r.gate
        rows.append(
            MergeGateCheck(
                name=r.check.name,
                command=r.check.command,
                state=r.check.state,
                stage=r.check.stage,
                outcome=r.execution_outcome,
                passed=r.passed,
                exit_code=gate.exit_code if gate is not None else None,
                timed_out=gate.timed_out if gate is not None else False,
                output_tail=gate.output_tail if gate is not None else "",
            )
        )
    return tuple(rows)


def _cleanup(repo: Path, wt: Path, branch: str) -> None:
    """Best-effort removal of the throwaway worktree + branch; never raises."""
    try:
        worktree.remove(wt, force=True)
    except Exception as exc:  # noqa: BLE001 — hygiene must not mask the verdict
        logger.warning("merge-gate: could not remove worktree %s: %s", wt, exc)
        return
    try:
        git.delete_branch(repo, branch)
    except RuntimeError as exc:
        logger.warning("merge-gate: could not delete branch %s: %s", branch, exc)


def run_merge_gate(
    config: DevelopConfig,
    change: ResolvedChange,
    *,
    push: bool = True,
    keep_worktree: bool = False,
) -> MergeGateResult:
    """Trial-merge *change*'s current base into its head and gate the result.

    *change* is a PR resolution (``head_branch`` set; ``base_ref`` the live
    base — ``origin/<base>`` from :func:`review_resolve.resolve_change`, which
    has already fetched it). Returns a :class:`MergeGateResult`; never
    raises for a gate outcome, only for a broken precondition (an
    unresolvable ref, a repo that is not a repo).
    """
    if change.is_fork:
        return MergeGateResult(
            status="fork_unsupported",
            change=change,
            head_sha=change.head_sha,
            message=(
                f"{change.head_ref} is a fork PR: not trial-merged (the head "
                "would have to be fetched into the operator's checkout and the "
                "update could not be pushed under origin credentials)"
            ),
        )
    base_ref = change.base_ref or f"origin/{config.base_branch}"
    base_sha = git.commit_sha(config.repo, base_ref)
    head_sha = change.head_sha
    behind = not git.is_ancestor(config.repo, base_sha, head_sha)

    config.worktree_parent.mkdir(parents=True, exist_ok=True)
    wt = worktree.create_on_branch(
        config.repo,
        head_sha,
        f"merge-gate {change.head_ref}",
        parent=config.worktree_parent,
    )
    branch = wt.name
    logger.info(
        "merge-gate %s: worktree %s at %s (base %s @ %s, %s)",
        config.run_id,
        wt,
        head_sha[:12],
        base_ref,
        base_sha[:12],
        "behind" if behind else "up to date",
    )
    keep = keep_worktree
    kept = wt if keep else None
    try:
        merge_sha = head_sha
        if behind:
            conflicts = git.merge(
                wt, base_sha, message=f"Merge {base_ref} into {change.head_branch}"
            )
            if conflicts:
                return MergeGateResult(
                    status="conflict",
                    change=change,
                    base_ref=base_ref,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    behind=True,
                    conflicting_paths=tuple(conflicts),
                    worktree=kept,
                    message=(
                        f"{change.head_ref} conflicts with {base_ref} @ "
                        f"{base_sha[:12]} in {len(conflicts)} path(s): "
                        + ", ".join(conflicts)
                    ),
                )
            merge_sha = git.commit_sha(wt)

        checks = build_check_set(config, wt)
        fingerprint = config_fingerprint(config, checks)
        # every verdict below is this record with its status + details filled in
        gated = MergeGateResult(
            status="errored",
            change=change,
            base_ref=base_ref,
            base_sha=base_sha,
            head_sha=head_sha,
            merge_sha=merge_sha,
            behind=behind,
            config_fingerprint=fingerprint,
            worktree=kept,
        )
        if not checks:
            return replace(
                gated,
                status="no_checks",
                message=(
                    f"{change.head_ref}: the project's current check-set is "
                    "empty — nothing gated the merge result, so it was not pushed"
                ),
            )
        outcome = run_check_set(config, wt, merge_sha, 1, checks, GateLedger())
        if outcome is None:
            return replace(
                gated,
                message=(
                    f"{change.head_ref}: the check-set could not run on the merge "
                    "result (infrastructure) — no verdict, nothing pushed"
                ),
            )
        rows = _flatten(outcome)
        verdict = outcome.aggregate_verdict
        if not outcome.blocking_passed:
            failing = [r.name for r in rows if not r.passed]
            return replace(
                gated,
                status="red",
                checks=rows,
                verdict=verdict,
                message=(
                    f"{change.head_ref} merged with {base_ref} @ {base_sha[:12]} "
                    f"fails the current check-set: {', '.join(failing)}"
                ),
            )

        pushed = False
        pushed_sha = ""
        push_error = ""
        if push and behind:
            try:
                pushed_sha = push_to_pr_ref(
                    wt, branch, change.head_branch, expected_remote_sha=head_sha
                )
                pushed = True
            except (MergeRaceDetected, ForkPushUnsupported, RuntimeError) as exc:
                push_error = str(exc)
                logger.warning(
                    "merge-gate %s: update of %s not pushed: %s",
                    config.run_id,
                    change.head_branch,
                    exc,
                )
        if pushed:
            message = (
                f"{change.head_ref} is green on {base_ref} @ {base_sha[:12]}; "
                f"pushed merge commit {pushed_sha[:12]} to {change.head_branch}"
            )
        elif behind:
            message = (
                f"{change.head_ref} is green on {base_ref} @ {base_sha[:12]} "
                "(behind; update not pushed"
                + (f": {push_error}" if push_error else ", push disabled")
                + ")"
            )
        else:
            message = f"{change.head_ref} is up to date with {base_ref} and green"
        return replace(
            gated,
            status="green",
            checks=rows,
            verdict=verdict,
            pushed=pushed,
            pushed_sha=pushed_sha,
            push_error=push_error,
            message=message,
        )
    finally:
        if keep:
            logger.info("merge-gate %s: worktree kept at %s", config.run_id, wt)
        else:
            _cleanup(config.repo, wt, branch)
