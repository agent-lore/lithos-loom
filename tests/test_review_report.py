"""Tests for the review-only report model + renderers (#154)."""

from __future__ import annotations

from lithos_loom.plugins.story_develop.review_report import (
    GateCheckReport,
    ReviewerReport,
    ReviewFinding,
    ReviewReport,
)


def _report(*, blocking: bool, reviewers=None, gate=None) -> ReviewReport:
    return ReviewReport(
        head_ref="#142 (feature)",
        base_sha="b" * 40,
        head_sha="h" * 40,
        profile="standard",
        reviewers=reviewers if reviewers is not None else [],
        gate=gate if gate is not None else [],
        blocking=blocking,
    )


def test_to_json_has_stable_keys() -> None:
    report = _report(
        blocking=True,
        reviewers=[
            ReviewerReport(
                name="correctness",
                status="FINDINGS",
                passed=False,
                findings=[
                    ReviewFinding(
                        reviewer="correctness",
                        severity="critical",
                        files=["cli/develop.py"],
                        rationale="exits before delivery",
                        finding_id="f-001",
                    )
                ],
            )
        ],
        gate=[GateCheckReport(name="lint", outcome="ran", blocked=False)],
    )

    data = report.to_json()
    assert set(data) >= {
        "head_ref",
        "base_sha",
        "head_sha",
        "profile",
        "blocking",
        "reviewers",
        "gate",
    }
    assert data["blocking"] is True
    finding = data["reviewers"][0]["findings"][0]
    assert set(finding) == {"reviewer", "severity", "files", "rationale", "finding_id"}
    assert finding["severity"] == "critical"
    assert data["gate"][0] == {"name": "lint", "outcome": "ran", "blocked": False}


def test_to_markdown_groups_by_reviewer_and_shows_gate() -> None:
    report = _report(
        blocking=True,
        reviewers=[
            ReviewerReport(
                name="correctness",
                status="FINDINGS",
                passed=False,
                findings=[
                    ReviewFinding(
                        reviewer="correctness",
                        severity="critical",
                        files=["cli/develop.py"],
                        rationale="exits before delivery",
                    )
                ],
            ),
            ReviewerReport(name="security", status="LGTM", passed=True, findings=[]),
        ],
        gate=[
            GateCheckReport(name="lint", outcome="ran", blocked=False),
            GateCheckReport(name="typecheck", outcome="ran", blocked=True),
        ],
    )

    md = report.to_markdown()
    assert "correctness" in md and "security" in md
    assert "critical" in md
    assert "cli/develop.py" in md
    # the gate checks are surfaced by name
    assert "lint" in md and "typecheck" in md
    # a blocking report says so prominently
    assert "BLOCK" in md.upper()


def test_to_markdown_clean_report_reads_as_passed() -> None:
    report = _report(
        blocking=False,
        reviewers=[
            ReviewerReport(name="correctness", status="LGTM", passed=True, findings=[])
        ],
        gate=[GateCheckReport(name="lint", outcome="ran", blocked=False)],
    )
    md = report.to_markdown()
    assert "BLOCK" not in md.upper()
    # a non-blocking review communicates the all-clear
    assert "LGTM" in md or "PASS" in md.upper() or "APPROV" in md.upper()
