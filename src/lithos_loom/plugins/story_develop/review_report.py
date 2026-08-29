"""Review-only report model + renderers (#154).

A :class:`ReviewReport` is the consolidated output of running the panel + gate
against an existing change. ``to_json`` is the stable, machine-readable contract
the review-correctness eval harness (#183) consumes; ``to_markdown`` is the
operator-facing summary. The dataclasses are deliberately decoupled from the
orchestrator's internal types — :mod:`review_only` assembles them — so this
module stays pure and trivially testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ReviewFinding:
    """One finding a reviewer raised against the change."""

    reviewer: str
    severity: str  # critical | major | minor
    files: list[str] = field(default_factory=list)
    rationale: str = ""
    finding_id: str = ""
    # Lifecycle status at report time (819370e5). Additive to the contract:
    # reports predating it carry no key, and consumers default to "open".
    # `out-of-scope` findings are excluded from eval catch-matching
    # (match.actionable_findings) — a deferral is an escape, not a catch.
    status: str = "open"

    def to_json(self) -> dict:
        return {
            "reviewer": self.reviewer,
            "severity": self.severity,
            "files": list(self.files),
            "rationale": self.rationale,
            "finding_id": self.finding_id,
            "status": self.status,
        }


# Every status a serialised reviewer may carry. Public because the on-disk
# report is a stable contract that outlives the run: `eval rescore` reads
# retained reports back months later and must be able to reject one that is not
# a real ReviewerReport before spending anything on it.
REVIEWER_STATUSES = ("LGTM", "FINDINGS", "invalid", "not-run")


@dataclass(frozen=True)
class ReviewerReport:
    """One reviewer's verdict on the change."""

    name: str
    status: str  # one of REVIEWER_STATUSES
    passed: bool  # by this reviewer's own block threshold
    findings: list[ReviewFinding] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "passed": self.passed,
            "findings": [f.to_json() for f in self.findings],
        }


@dataclass(frozen=True)
class GateCheckReport:
    """One deterministic check's outcome on the change's head tree."""

    name: str
    outcome: str  # ran | absent | errored | timed_out | n_a
    blocked: bool  # a required check whose verdict holds approval

    def to_json(self) -> dict:
        return {"name": self.name, "outcome": self.outcome, "blocked": self.blocked}


@dataclass(frozen=True)
class ReviewReport:
    """Consolidated panel + gate result for a single existing change."""

    head_ref: str
    base_sha: str
    head_sha: str
    profile: str
    reviewers: list[ReviewerReport] = field(default_factory=list)
    gate: list[GateCheckReport] = field(default_factory=list)
    blocking: bool = False

    def to_json(self) -> dict:
        return {
            "head_ref": self.head_ref,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "profile": self.profile,
            "blocking": self.blocking,
            "reviewers": [r.to_json() for r in self.reviewers],
            "gate": [g.to_json() for g in self.gate],
        }

    def to_markdown(self) -> str:
        verdict = "🛑 BLOCKING" if self.blocking else "✅ clean (approved)"
        lines = [
            f"# Review of {self.head_ref}",
            "",
            f"`{self.base_sha[:12]}..{self.head_sha[:12]}` · "
            f"profile **{self.profile}** · **{verdict}**",
            "",
        ]
        deferred = [
            f for r in self.reviewers for f in r.findings if f.status == "out-of-scope"
        ]
        if deferred:
            # 819370e5 (PR #342 review): a deferral is non-blocking, so
            # without this a "clean" verdict would hide it — and this surface
            # spawns no follow-up task, so an unnoticed deferral is LOST.
            lines.append(
                f"⚠ {len(deferred)} finding(s) deferred as **out-of-scope** — "
                "real, but judged not this change's to fix. No follow-up task "
                "is spawned on this surface: file them manually or they are "
                "lost. Marked `[deferred]` below."
            )
            lines.append("")
        for reviewer in self.reviewers:
            mark = "PASS" if reviewer.passed else "FAIL"
            lines.append(f"## {reviewer.name} — {reviewer.status} ({mark})")
            if not reviewer.findings:
                lines.append("")
                lines.append("_no findings_")
                lines.append("")
                continue
            lines.append("")
            for f in reviewer.findings:
                where = ", ".join(f.files) if f.files else "—"
                fid = f"{f.finding_id} " if f.finding_id else ""
                mark = "" if f.status == "open" else f" `[{f.status}]`"
                lines.append(f"- {fid}**[{f.severity}]**{mark} ({where}) {f.rationale}")
            lines.append("")
        if self.gate:
            lines.append("## Gate")
            lines.append("")
            for g in self.gate:
                state = "BLOCK" if g.blocked else g.outcome
                lines.append(f"- `{g.name}`: {state}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"
