"""The gate's ``merge_gate`` record (PRD S3): the re-run key + the outcome.

``metadata.merge_gate`` on a ``pr`` gate — url-scoped like every other gate
marker — is the on-disk contract :mod:`.merge_gate_dispatch` reads and
:mod:`.merge_gate_outcome` writes. A separate key from the landability,
review-seen and remediation markers: no marker may trip another's skip
logic (ADR 0011). Kept apart from the dispatcher so the shape has one home
and both halves stay under the module budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "MAX_ATTEMPTS_PER_KEY",
    "MERGE_GATE_FAILED",
    "MERGE_GATE_KEY",
    "MergeGateRecord",
    "read_record",
]

# Stable, machine-parseable finding prefix (see AGENTS.md): the project's
# current check-set went red (or could not verify) on the trial merge of a
# delivered PR into its base's current tip — merging it now would break the
# base.
MERGE_GATE_FAILED = "[MergeGateFailed]"

# Gate-metadata key holding the last re-gate record (the re-run key + outcome).
MERGE_GATE_KEY = "merge_gate"

# A run that produced no verdict to stand on — a crash (no record), a failed
# push beside a green verdict, a repo mismatch — is retried this many times
# on the SAME key, then waits: a host or config problem must not become an
# hourly loop. A crash / mismatch then waits for the key to move; a failed
# push falls back to the settings probe (its verdict is settings-dependent).
MAX_ATTEMPTS_PER_KEY = 2


@dataclass(frozen=True)
class MergeGateRecord:
    """The gate's parsed ``merge_gate`` marker: the re-run key + the outcome.

    ``head_sha`` / ``base_sha`` are the shas the SWEEP observed when it
    dispatched (the key it compares next pass), not the run's own view.
    ``attempts`` counts runs on this key (a crash, a failed push or a repo
    mismatch retries a bounded number of times); a fresh key starts at 1.
    ``behind`` with an empty ``pushed_sha`` means the merge commit was made
    and its push FAILED — ``push_error`` says why.
    """

    pr_url: str
    head_sha: str
    base_sha: str
    settings_fingerprint: str = ""
    status: str = ""
    verdict: str | None = None
    merge_sha: str = ""
    pushed_sha: str = ""
    config_fingerprint: str = ""
    attempts: int = 0
    behind: bool = False
    push_error: str = ""
    # the checkout the run was mapped to, and what its origin turned out to
    # be on a repo_mismatch — so fixing the mapping (a new path, or an origin
    # that now matches) is a fresh key for the same shas
    repo_path: str = ""
    actual_repo: str = ""
    # the sweep's OWN origin read (lower-cased owner/name, "" when unreadable)
    # at the time of the record — with repo_path, the settle key for a repo
    # mismatch of either kind: the CLI's verdict and the cheap read can
    # disagree, so a mismatch re-arms only when what the sweep observes moves
    origin_seen: str = ""
    # for a checkout_unresolved record: why the sweep's read could not answer
    origin_reason: str = ""

    def as_marker(self) -> dict[str, Any]:
        return {
            "pr_url": self.pr_url,
            "head_sha": self.head_sha,
            "base_sha": self.base_sha,
            "settings_fingerprint": self.settings_fingerprint,
            "status": self.status,
            "verdict": self.verdict,
            "merge_sha": self.merge_sha,
            "pushed_sha": self.pushed_sha,
            "config_fingerprint": self.config_fingerprint,
            "attempts": self.attempts,
            "behind": self.behind,
            "push_error": self.push_error,
            "repo_path": self.repo_path,
            "actual_repo": self.actual_repo,
            "origin_seen": self.origin_seen,
            "origin_reason": self.origin_reason,
        }


def read_record(gate: Any, pr_url: str) -> MergeGateRecord | None:
    """Parse the gate's record; ``None`` for an absent / foreign-url marker
    (a replacement PR re-evaluates from scratch). Tolerant of a malformed
    field — it reads as unset, never raises."""
    raw = gate.metadata.get(MERGE_GATE_KEY)
    if not isinstance(raw, dict) or raw.get("pr_url") != pr_url:
        return None

    def _s(key: str) -> str:
        value = raw.get(key)
        return value if isinstance(value, str) else ""

    attempts = raw.get("attempts")
    verdict = raw.get("verdict")
    return MergeGateRecord(
        pr_url=pr_url,
        head_sha=_s("head_sha"),
        base_sha=_s("base_sha"),
        settings_fingerprint=_s("settings_fingerprint"),
        status=_s("status"),
        verdict=verdict if isinstance(verdict, str) else None,
        merge_sha=_s("merge_sha"),
        pushed_sha=_s("pushed_sha"),
        config_fingerprint=_s("config_fingerprint"),
        attempts=attempts if isinstance(attempts, int) and attempts >= 0 else 0,
        behind=raw.get("behind") is True,
        push_error=_s("push_error"),
        repo_path=_s("repo_path"),
        actual_repo=_s("actual_repo"),
        origin_seen=_s("origin_seen"),
        origin_reason=_s("origin_reason"),
    )
