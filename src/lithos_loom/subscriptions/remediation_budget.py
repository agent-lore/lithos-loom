"""The S5b external-remediation budget marker on a ``pr`` gate.

``metadata.external_remediation`` — ``{pr_url, rounds_used,
last_loom_pushed_sha, last_seen_head_sha, needs_human_gate_id}``, url-scoped
— is the on-disk contract :mod:`.external_remediation` reads and writes and
:mod:`.remediation_escalation` records its gate in. A separate key from
``external_review_seen`` and the merge marker — no marker may trip another's
skip logic. Kept apart from the dispatcher so both halves stay under the
module budget and the marker's shape has one home.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from lithos_loom.notifications import NeedsHumanNotice

__all__ = [
    "PENDING_KEY",
    "REMEDIATION_KEY",
    "RemediationBudget",
    "RemediationNotifier",
    "RemediationSettings",
    "read_budget",
]

# Gate-metadata key holding the S5b budget state.
REMEDIATION_KEY = "external_remediation"

# Gate-metadata key parking a batch deferred behind the busy single-flight
# slot (PR #346 review F1): ingestion's high-water marks consume the batch,
# so without a durable trigger a deferred dispatch would never happen if the
# PR then went quiet. Url-scoped; consumed atomically with the budget
# reservation on dispatch; re-parked by a structured CLI refusal; survives
# restarts.
PENDING_KEY = "external_remediation_pending"


class RemediationNotifier(Protocol):
    async def needs_human(self, notice: NeedsHumanNotice) -> list[str]: ...


@dataclass(frozen=True)
class RemediationBudget:
    """The gate's parsed S5b budget state (fresh when absent / foreign-url)."""

    pr_url: str
    rounds_used: int = 0
    last_loom_pushed_sha: str = ""
    last_seen_head_sha: str = ""
    # The needs-human gate raised when this budget ran out (PRD S5b:
    # exhaustion → human gate); empty until then. Lives on the budget so a
    # human-push reset — a fresh budget — clears it, and the NEXT exhaustion
    # escalates again; within one budget the gate is raised once.
    needs_human_gate_id: str = ""

    def as_marker(self) -> dict[str, Any]:
        return {
            "pr_url": self.pr_url,
            "rounds_used": self.rounds_used,
            "last_loom_pushed_sha": self.last_loom_pushed_sha,
            "last_seen_head_sha": self.last_seen_head_sha,
            "needs_human_gate_id": self.needs_human_gate_id,
        }


def read_budget(gate: Any, pr_url: str) -> RemediationBudget:
    """Parse the gate's budget marker; fresh state for a foreign / absent url."""
    raw = gate.metadata.get(REMEDIATION_KEY)
    if not isinstance(raw, dict) or raw.get("pr_url") != pr_url:
        return RemediationBudget(pr_url=pr_url)
    rounds = raw.get("rounds_used")
    loom_sha = raw.get("last_loom_pushed_sha")
    seen_sha = raw.get("last_seen_head_sha")
    gate_id = raw.get("needs_human_gate_id")
    return RemediationBudget(
        pr_url=pr_url,
        rounds_used=rounds if isinstance(rounds, int) and rounds >= 0 else 0,
        last_loom_pushed_sha=loom_sha if isinstance(loom_sha, str) else "",
        last_seen_head_sha=seen_sha if isinstance(seen_sha, str) else "",
        needs_human_gate_id=gate_id if isinstance(gate_id, str) else "",
    )


@dataclass(frozen=True)
class RemediationSettings:
    """Host-side knobs the watcher child threads in from its config."""

    trusted_bots: tuple[str, ...]
    budget: int
    projects: Mapping[str, Path] = field(default_factory=dict)
    work_dir: Path = Path(".")
    # Forwarded to the subprocess as `--config` so it loads the same host
    # config (`develop converge` has no `-c` short flag — the daemon commands do);
    # None lets it fall back to env/CWD discovery (the child's own mode).
    config_path: Path | None = None
    # The push sinks for the needs-human gate an exhausted budget raises
    # (PRD S5b); None = gate + finding only.
    notifier: RemediationNotifier | None = None
