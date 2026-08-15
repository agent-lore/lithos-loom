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

# A judge takes (mechanism, produced_findings) and returns the finding_ids that
# describe the SPECIFIC mechanism (empty = none). Injected so scoring stays
# unit-testable. Returning ids (not a bool) keeps severity-correctness accurate.
Judge = Callable[[str, list[dict]], list[str]]


@dataclass(frozen=True)
class MatchResult:
    """Whether one expected defect was surfaced, and how."""

    caught: bool
    severity_correct: bool
    method: str  # "structured" | "judge" | "none"
    finding_id: str = ""


# Reviewer statuses that mean the turn did NOT produce a verdict (always
# findings=[]): a crashed/malformed turn ("invalid") or a panel short-circuit
# ("not-run"). A report carrying either is incomplete — a "no findings" result
# from it is an absence of review, not a clean pass / genuine miss (#182 A3).
_INCOMPLETE_STATUSES = frozenset({"invalid", "not-run"})


@dataclass(frozen=True)
class RunScore:
    """Score for one review run against a case (all expecteds must match).

    ``n_findings`` / ``blocked`` are the run's **noise** instrumentation (#310):
    the catch answer is about the case's *expected* defect alone, so everything
    else a run reported — and whether it held approval over it — is invisible to
    it. On a known-good head that gap flatters exactly the changes most likely to
    be harmful (anything raising catch-rate by lowering the bar for what counts
    as a defect), so the raw observations are recorded alongside the verdict.
    """

    caught: bool
    severity_correct: bool
    matches: list[MatchResult] = field(default_factory=list)
    incomplete: bool = False
    n_findings: int = 0
    blocked: bool = False


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

    When a *judge* is given it is **authoritative**: it sees every produced
    finding and returns the ids that describe the *specific* mechanism — so it
    both **vetoes** a finding that only matches the topic (a structural keyword
    hit on a different defect) and **rescues** a correct finding worded without
    the keywords. Without a judge, the cheap structured match (file + ≥1 keyword)
    is used — deterministic, but topic-loose.
    """
    if judge is not None:
        matched_ids = set(judge(expected.mechanism, produced))
        matched = [f for f in produced if f.get("finding_id") in matched_ids]
        if matched:
            sev_ok = any(
                severity_at_or_above(f.get("severity", "minor"), expected.min_severity)
                for f in matched
            )
            return MatchResult(
                caught=True,
                severity_correct=sev_ok,
                method="judge",
                finding_id=str(matched[0].get("finding_id", "")),
            )
        return MatchResult(caught=False, severity_correct=False, method="judge")

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
    return MatchResult(caught=False, severity_correct=False, method="none")


def _all_produced(report_json: dict) -> list[dict]:
    """Flatten every reviewer's findings out of a ReviewReport JSON."""
    findings: list[dict] = []
    for reviewer in report_json.get("reviewers", []):
        findings.extend(reviewer.get("findings", []))
    return findings


def review_incomplete(report_json: dict) -> bool:
    """Whether any reviewer's turn did not produce a verdict (#182 A3).

    Keys on the *presence* of an error status (``invalid`` / ``not-run``), not
    the absence of a good one — so a reviewer dict without a ``status`` key (only
    test stubs; real reports always set it) is treated as complete.
    """
    return any(
        r.get("status") in _INCOMPLETE_STATUSES
        for r in report_json.get("reviewers", [])
    )


def finding_count(report_json: dict) -> int:
    """How many findings the run produced, across every reviewer (#310).

    Public because re-deriving the noise instrumentation from an **existing**
    report dir is free (pure counting — no judge, no tokens), so report dirs
    predating #310 stay analysable.
    """
    return len(_all_produced(report_json))


def review_blocked(report_json: dict) -> bool:
    """Whether the run **held approval** — the report's own blocking rule (#310).

    Read straight off the report so the eval can never disagree with what the
    review actually decided (``intake_blocks``: any reviewer not passing, an
    incomplete panel, or a blocking deterministic floor). A report without the
    key is treated as non-blocking; every real ``ReviewReport.to_json()`` sets
    it, so that default is reached by test stubs alone.
    """
    return bool(report_json.get("blocking", False))


def score_run(case: Case, report_json: dict, *, judge: Judge | None = None) -> RunScore:
    """Score one review run: the case is caught iff EVERY expected matches."""
    produced = _all_produced(report_json)
    matches = [match_expected(e, produced, judge=judge) for e in case.expected]
    caught = all(m.caught for m in matches)
    severity_correct = caught and all(m.severity_correct for m in matches)
    return RunScore(
        caught=caught,
        severity_correct=severity_correct,
        matches=matches,
        incomplete=review_incomplete(report_json),
        n_findings=len(produced),
        blocked=review_blocked(report_json),
    )


__all__ = [
    "Judge",
    "MatchResult",
    "RunScore",
    "finding_count",
    "match_expected",
    "review_blocked",
    "review_incomplete",
    "score_run",
]
