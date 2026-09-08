"""Trial-merge a delivered PR into its current base and gate the result (PRD S3).

A delivered PR waits behind its ``pr`` gate while the base branch moves on.
GitHub answers one question about that — *does it still merge?* — and even
then without naming the conflicting paths. This module answers the one the
operator actually asks before pressing merge: **will the PR's current base
break if I merge this now?** It is zero tokens: a deterministic check-set on a different
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
from .check_runner import (
    build_check_set,
    check_result_blocks,
    gate_floor_blocks,
    run_check_set,
)
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
    "pr_closed",  # the PR is merged / closed: its branch is not a live target
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
    ``verdict`` is the gate's own decision — ``RED`` when the ledger-aware
    floor blocks, ``GREEN`` when it does not, ``None`` when no verdict was
    produced (errored / no checks / conflict) — never the process-exit
    aggregate. ``config_fingerprint`` identifies the check-set, image, timeout
    and blocking threshold that produced it — the third component of the
    sweep's re-run key.
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
    # The resolved gate SETTINGS alone (no worktree needed) — the sweep's
    # re-run key component, probed each pass with `--resolve-only`.
    settings_fingerprint: str = ""
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
            "settings_fingerprint": self.settings_fingerprint,
            "pushed": self.pushed,
            "pushed_sha": self.pushed_sha,
            "push_error": self.push_error,
            "message": self.message,
        }


def settings_fingerprint(config: DevelopConfig) -> str:
    """A short stable digest of the resolved gate SETTINGS — everything that
    decides which checks run and how they block, and nothing that needs a
    tree or is run-local.

    :func:`config_fingerprint` (the check rows) needs a worktree — ecosystem
    detection reads the tree — so the watcher sweep cannot compute it
    without a fetch and a merge. This one it can: ``merge-gate --resolve-only``
    prints it from the story's current config alone, and the sweep re-gates
    an already-observed ``(head_sha, base_sha)`` when it changes — the
    "tightened check-set" case re-resolving the current config exists for.
    A tree-dependent change (a lockfile appears) moves the head sha anyway.
    """
    payload = {
        "image": config.image,
        "test_timeout": config.test_timeout,
        "block_threshold": config.block_threshold,
        "review_profile": config.review_profile,
        "test_command": config.test_command,
        "test_gate": config.test_gate,
        "check_commands": dict(sorted(config.check_commands.items())),
        "check_states": dict(sorted(config.check_states.items())),
        "parity_command": config.parity_command,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8"))
    return digest.hexdigest()[:16]


def config_fingerprint(config: DevelopConfig, checks: tuple[Check, ...]) -> str:
    """A short stable digest of *what gated*: the resolved checks, the image,
    the per-check timeout and the blocking threshold.

    The PRD's re-run key is ``(head_sha, base_sha, config fingerprint)`` —
    without the third component a tightened check-set would never re-gate
    an already-observed PR, which is precisely the case re-resolving the
    project's current config exists to catch. Commands, states and stages
    are part of it; run-local paths and names are not.
    """
    payload = {
        "image": config.image,
        # verdict-affecting knobs beyond the rows (PR #360 review F4): a
        # longer timeout can turn a timeout into a pass, a lower threshold a
        # pass into a block — either must invalidate the sweep's key.
        "test_timeout": config.test_timeout,
        "block_threshold": config.block_threshold,
        "checks": [[c.name, c.command, c.state, c.stage, c.raw_exit] for c in checks],
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8"))
    return digest.hexdigest()[:16]


def _flatten(
    result: CheckSetResult, ledger: GateLedger, threshold: str
) -> tuple[MergeGateCheck, ...]:
    """One row per check, ``passed`` from the SAME ledger-aware predicate the
    develop floor uses (:func:`check_result_blocks`) — an adapter-backed
    required check (ruff / bandit, ``--exit-zero``) blocks through its
    findings at *threshold*, never through the exit code (PR #360 review F1)."""
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
                # merge-gate's OWN decision: the ledger-aware floor, plus a
                # required check that never executed is not a pass here
                # (PR #360 re-review F2 — the record is the watcher's contract)
                passed=not check_result_blocks(r, ledger, threshold)
                and not (
                    r.check.state == "required" and r.execution_outcome == "errored"
                ),
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
    if change.is_merged or change.is_closed:
        return MergeGateResult(
            status="pr_closed",
            change=change,
            head_sha=change.head_sha,
            message=(
                f"{change.head_ref} is {'merged' if change.is_merged else 'closed'}: "
                "its branch is not a live target — nothing trial-merged, nothing "
                "pushed"
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
        ledger = GateLedger()
        threshold = config.block_threshold
        outcome = run_check_set(config, wt, merge_sha, 1, checks, ledger)
        if outcome is None:
            return replace(
                gated,
                message=(
                    f"{change.head_ref}: the check-set could not run on the merge "
                    "result (infrastructure) — no verdict, nothing pushed"
                ),
            )
        rows = _flatten(outcome, ledger, threshold)
        # The record's verdict is WHOLLY the gate's own decision (PR #360
        # re-review 2): RED when the ledger-aware floor blocks, GREEN when it
        # does not — never the process-exit aggregate, which reads GREEN for a
        # ruff --exit-zero finding and RED for an informational check that
        # exited non-zero (which, correctly, does not block).
        blocked = gate_floor_blocks(outcome, ledger, threshold)
        verdict = "RED" if blocked else "GREEN"
        # A required check that never EXECUTED is "not blocking" for the
        # agent loop (a reviewer compensates); here nobody does, and the
        # contract is that an unverified merge is never pushed (PR #360
        # review F2): no infrastructure verdict on a required check → errored.
        unverified = [
            r.check.name
            for r in outcome.results
            if r.check.state == "required" and r.execution_outcome == "errored"
        ]
        if unverified:
            return replace(
                gated,
                checks=rows,
                verdict=None,  # no verdict was produced for the set as a whole
                message=(
                    f"{change.head_ref}: required check(s) could not run on the "
                    f"merge result (infrastructure): {', '.join(unverified)} — no "
                    "verdict, nothing pushed"
                ),
            )
        if blocked:
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
