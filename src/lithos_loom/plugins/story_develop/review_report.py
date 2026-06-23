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

    def to_json(self) -> dict:
        return {
            "reviewer": self.reviewer,
            "severity": self.severity,
            "files": list(self.files),
            "rationale": self.rationale,
            "finding_id": self.finding_id,
        }


@dataclass(frozen=True)
class ReviewerReport:
    """One reviewer's verdict on the change."""

    name: str
    status: str  # LGTM | FINDINGS | invalid
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
                lines.append(f"- {fid}**[{f.severity}]** ({where}) {f.rationale}")
            lines.append("")
        if self.gate:
            lines.append("## Gate")
            lines.append("")
            for g in self.gate:
                state = "BLOCK" if g.blocked else g.outcome
                lines.append(f"- `{g.name}`: {state}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"
