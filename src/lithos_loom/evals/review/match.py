"""Expected->produced matching + per-run scoring (#183).

The cheapest match that does not reward vague findings: a produced finding
matches an expected defect when it touches the expected **file** AND mentions at
least one expected **keyword** (the structured match). On a structural miss an
optional **LLM-judge** is consulted with the expected *mechanism* prose. Scoring
is over a single review run; the harness aggregates rates across K runs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ...plugins.story_develop.handoff import severity_at_or_above
from .case import Case, Expected

# A judge takes (mechanism, produced_findings) and returns whether any finding
# describes the mechanism. Injected so scoring stays unit-testable.
Judge = Callable[[str, list[dict]], bool]


@dataclass(frozen=True)
class MatchResult:
    """Whether one expected defect was surfaced, and how."""

    caught: bool
    severity_correct: bool
    method: str  # "structured" | "judge" | "none"
    finding_id: str = ""


@dataclass(frozen=True)
class RunScore:
    """Score for one review run against a case (all expecteds must match)."""

    caught: bool
    severity_correct: bool
    matches: list[MatchResult] = field(default_factory=list)


def _haystack(finding: dict) -> str:
    parts = [finding.get("rationale", ""), *finding.get("files", [])]
    return " ".join(parts).lower()


def _structured_match(expected: Expected, finding: dict) -> bool:
    text = _haystack(finding)
    file_hit = expected.file.lower() in text
    keyword_hit = any(kw.lower() in text for kw in expected.keywords)
    return file_hit and keyword_hit


def match_expected(
    expected: Expected, produced: list[dict], *, judge: Judge | None = None
) -> MatchResult:
    """Match one *expected* defect against the *produced* findings.

    Structured match first (file + ≥1 keyword); on a miss, fall back to the
    *judge* (if provided) on the expected mechanism.
    """
    for finding in produced:
        if _structured_match(expected, finding):
            return MatchResult(
                caught=True,
                severity_correct=severity_at_or_above(
                    finding.get("severity", "minor"), expected.min_severity
                ),
                method="structured",
                finding_id=finding.get("finding_id", ""),
            )

    if judge is not None and produced and judge(expected.mechanism, produced):
        # The judge affirms the mechanism is described; it does not adjudicate
        # severity, so severity-correctness is credited when any produced finding
        # reaches the band (best-effort — the structured path is authoritative).
        sev_ok = any(
            severity_at_or_above(f.get("severity", "minor"), expected.min_severity)
            for f in produced
        )
        return MatchResult(
            caught=True, severity_correct=sev_ok, method="judge", finding_id=""
        )

    return MatchResult(caught=False, severity_correct=False, method="none")


def _all_produced(report_json: dict) -> list[dict]:
    """Flatten every reviewer's findings out of a ReviewReport JSON."""
    findings: list[dict] = []
    for reviewer in report_json.get("reviewers", []):
        findings.extend(reviewer.get("findings", []))
    return findings


def score_run(case: Case, report_json: dict, *, judge: Judge | None = None) -> RunScore:
    """Score one review run: the case is caught iff EVERY expected matches."""
    produced = _all_produced(report_json)
    matches = [match_expected(e, produced, judge=judge) for e in case.expected]
    caught = all(m.caught for m in matches)
    severity_correct = caught and all(m.severity_correct for m in matches)
    return RunScore(caught=caught, severity_correct=severity_correct, matches=matches)
