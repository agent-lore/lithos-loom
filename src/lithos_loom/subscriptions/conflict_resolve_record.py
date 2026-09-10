"""The gate's ``conflict_resolve`` record and the dispatcher's constants (PRD S5).

``metadata.conflict_resolve`` on a ``pr`` gate — url-scoped like every other
gate marker — is the once-per-sha-pair bound :mod:`.conflict_resolve_dispatch`
reserves before a spawn and :mod:`.conflict_resolve_outcome` writes the
outcome into. A separate key from the merge-gate, landability and remediation
markers (ADR 0011). Kept apart so the shape has one home and the dispatcher
stays under the module budget.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "CONFLICT_ACTIONS",
    "CONFLICT_RESOLVED",
    "CONFLICT_RESOLVE_KEY",
    "CONFLICT_RESOLVE_SETTING",
    "PUSHED_BREADCRUMB_KEY",
    "STRICT_WRITE_DELAYS",
    "ConflictResolveRecord",
    "Debt",
    "read_record",
]

# Stable, machine-parseable finding prefix (see AGENTS.md): loom resolved a
# delivered PR's conflict with its base and pushed the merge commit.
CONFLICT_RESOLVED = "[ConflictResolved]"

# Gate-metadata key holding the last resolution record (the sha pair + outcome).
CONFLICT_RESOLVE_KEY = "conflict_resolve"

# Per-project opt-out on the context doc's metadata (default on).
CONFLICT_RESOLVE_SETTING = "develop_conflict_resolve"

# Story-metadata key breadcrumbing a pushed resolution BEFORE the combined
# record + budget write on the gate is attempted: a gate write outage does
# not take a story write down with it, so a restarted daemon can recover a
# held debt from it (see ConflictResolveDispatch.recover_debt) instead of
# letting the next sweep read the merge commit as a human push.
PUSHED_BREADCRUMB_KEY = "conflict_resolve_pushed"

# The writes that BOUND this dispatcher — the pre-spawn reservation, the
# crash record, the post-push outcome + budget — are strict (PR #366 review
# F1 + F2): retried with backoff, and a reservation that does not land
# spawns nothing, while a post-push write that does not land becomes a
# held debt (see ConflictResolveDispatch.busy_on) rather than a lost sha.
STRICT_WRITE_DELAYS: tuple[float, ...] = (0.5, 2.0, 5.0)


CONFLICT_ACTIONS = (
    "the story stays behind its pr gate; resolve the conflict by merging the "
    "base into the PR branch by hand (never rebase a delivered branch) — a "
    "human push re-keys every sweep — or re-run `develop converge <pr> "
    "--resolve-conflicts --story <id>` with a higher --max-rounds; complete "
    "this gate once decided"
)
"""What the operator can do about an unresolved conflict — none of it is a
re-dispatch of the story, so the runner's two actions would mislead here."""


@dataclass(frozen=True)
class ConflictResolveRecord:
    """The gate's ``conflict_resolve`` marker: the sha pair + the outcome."""

    pr_url: str
    head_sha: str
    base_sha: str
    status: str = ""
    attempts: int = 0
    pushed_sha: str = ""
    needs_human_gate_id: str = ""
    boot_id: str = ""
    message: str = ""
    # for a repo_mismatch refusal: what the sweep observed when it ran — the
    # settle key; the same shas re-arm when either moves (PR #366 review F4)
    repo_path: str = ""
    origin_seen: str = ""

    def as_marker(self) -> dict[str, Any]:
        return {
            "pr_url": self.pr_url,
            "head_sha": self.head_sha,
            "base_sha": self.base_sha,
            "status": self.status,
            "attempts": self.attempts,
            "pushed_sha": self.pushed_sha,
            "needs_human_gate_id": self.needs_human_gate_id,
            "boot_id": self.boot_id,
            "message": self.message,
            "repo_path": self.repo_path,
            "origin_seen": self.origin_seen,
        }


def read_record(gate: Any, pr_url: str) -> ConflictResolveRecord | None:
    """The gate's record; ``None`` for an absent / foreign-url marker."""
    raw = gate.metadata.get(CONFLICT_RESOLVE_KEY)
    if not isinstance(raw, dict) or raw.get("pr_url") != pr_url:
        return None

    def _s(key: str) -> str:
        value = raw.get(key)
        return value if isinstance(value, str) else ""

    attempts = raw.get("attempts")
    return ConflictResolveRecord(
        pr_url=pr_url,
        head_sha=_s("head_sha"),
        base_sha=_s("base_sha"),
        status=_s("status"),
        attempts=attempts if isinstance(attempts, int) and attempts >= 0 else 0,
        pushed_sha=_s("pushed_sha"),
        needs_human_gate_id=_s("needs_human_gate_id"),
        boot_id=_s("boot_id"),
        message=_s("message"),
        repo_path=_s("repo_path"),
        origin_seen=_s("origin_seen"),
    )


@dataclass(frozen=True)
class Debt:
    """A pushed resolution whose record + budget write has not landed yet:
    the marker to flush and the finding to post once it does. Held in the
    dispatcher's memory; recoverable from the story breadcrumb after a
    restart."""

    gate_id: str
    story_id: str
    marker: Mapping[str, Any]
    summary: str
