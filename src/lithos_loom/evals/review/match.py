"""Expected->produced matching + per-run scoring (#183).

The cheapest match that does not reward vague findings: a produced finding
matches an expected defect when it touches the expected **file** AND mentions at
least one expected **keyword** (the structured match). On a structural miss an
optional **LLM-judge** is consulted with the expected *mechanism* prose. Scoring
is over a single review run; the harness aggregates rates across K runs.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Literal

from ...plugins.story_develop.handoff import severity_at_or_above
from .case import Case, Expected

JudgeStatus = Literal["ok", "unparsed", "failed"]

# Ordered worst-last: a case's status is the worst across its expecteds.
_STATUS_RANK: dict[str, int] = {"ok": 0, "unparsed": 1, "failed": 2}
_JUDGE_ERROR_STATUSES = frozenset({"unparsed", "failed"})


@dataclass(frozen=True)
class JudgeVerdict:
    """One judge answer about one expected defect — the answer, and why (#307).

    ``status`` separates the three things an empty ``matched_ids`` used to mean:
    a genuine **veto** (``ok``), a reply whose verdict line could not be read
    (``unparsed``), and a call that never produced one (``failed`` — a timeout,
    a missing CLI). Only ``ok`` is a measurement; the other two are an ABSENCE
    of one and must not score as a review miss. ``reply`` is retained so a veto
    is auditable after the fact instead of invisible.
    """

    matched_ids: tuple[str, ...] = ()
    status: JudgeStatus = "ok"
    reply: str = ""
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.status == "ok"


# A judge takes (mechanism, produced_findings) and answers which finding_ids
# describe the SPECIFIC mechanism. Injected so scoring stays unit-testable.
# Answering with ids (not a bool) keeps severity-correctness accurate.
Judge = Callable[[str, list[dict]], JudgeVerdict]


def judge_status_errored(status: str) -> bool:
    """Whether a recorded judge status means **no verdict was produced**.

    ``unparsed`` / ``failed`` are an absence of an answer, so a sample carrying
    either is excluded from every rate denominator (#182 A3, #307) — unlike a
    veto, which is an answer.
    """
    return status in _JUDGE_ERROR_STATUSES


def worst_judge_status(statuses: Iterable[str]) -> str:
    """The worst status across a run's expecteds (``""`` when no judge ran)."""
    worst = ""
    for status in statuses:
        if not worst or _STATUS_RANK.get(status, 0) > _STATUS_RANK.get(worst, 0):
            worst = status
    return worst


@dataclass(frozen=True)
class MatchResult:
    """Whether one expected defect was surfaced, and how.

    ``structured_caught`` is the judge-free answer, computed on **every** match
    (#307). The structured matcher is pure over the already-in-memory findings,
    so carrying its answer alongside the judge's costs no agent call and no
    tokens — and a judge/structured divergence is exactly the signal that used
    to be findable only by re-scoring a report dir by hand.
    """

    caught: bool
    severity_correct: bool
    method: str  # "structured" | "judge" | "none"
    finding_id: str = ""
    structured_caught: bool = False
    structured_finding_id: str = ""
    judge: JudgeVerdict | None = None  # None iff no judge ran


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
    judge_status: str = ""  # worst across the expecteds; "" when no judge ran
    structured_caught: bool = False

    @property
    def judge_errored(self) -> bool:
        """The judge gave no usable answer, so this sample cannot be scored.

        Kept **distinct** from ``incomplete`` (a crashed *reviewer*): conflating
        them would hide which half of the instrument broke. Both are excluded
        from rate denominators the same way (#182 A3).
        """
        return judge_status_errored(self.judge_status)


def _haystack(finding: dict) -> str:
    parts = [finding.get("rationale", ""), *finding.get("files", [])]
    return " ".join(parts).lower()


def _structured_match(expected: Expected, finding: dict) -> bool:
    text = _haystack(finding)
    file_hit = expected.file.lower() in text
    keyword_hit = any(kw.lower() in text for kw in expected.keywords)
    return file_hit and keyword_hit


def _structured_hit(expected: Expected, produced: list[dict]) -> dict | None:
    """The first finding matching *expected* structurally (file + ≥1 keyword)."""
    for finding in produced:
        if _structured_match(expected, finding):
            return finding
    return None


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

    The structured answer is computed either way and carried on the result: it
    is a pure function of *produced*, so recording what the judge-free matcher
    would have said is free (#307).
    """
    hit = _structured_hit(expected, produced)
    structured_id = str(hit.get("finding_id", "")) if hit is not None else ""

    if judge is not None:
        verdict = judge(expected.mechanism, produced)
        wanted = set(verdict.matched_ids)
        matched = [f for f in produced if f.get("finding_id") in wanted]
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
                structured_caught=hit is not None,
                structured_finding_id=structured_id,
                judge=verdict,
            )
        return MatchResult(
            caught=False,
            severity_correct=False,
            method="judge",
            structured_caught=hit is not None,
            structured_finding_id=structured_id,
            judge=verdict,
        )

    if hit is not None:
        return MatchResult(
            caught=True,
            severity_correct=severity_at_or_above(
                hit.get("severity", "minor"), expected.min_severity
            ),
            method="structured",
            finding_id=structured_id,
            structured_caught=True,
            structured_finding_id=structured_id,
        )
    return MatchResult(caught=False, severity_correct=False, method="none")


def produced_findings(report_json: dict) -> list[dict]:
    """Flatten every reviewer's findings out of a ReviewReport JSON.

    Public because offline re-scoring (``eval rescore``) asks the judge the same
    question the live run did, and that needs the findings the run produced —
    read straight off the retained report, no re-run.
    """
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
    return len(produced_findings(report_json))


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
    produced = produced_findings(report_json)
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
        judge_status=worst_judge_status(
            m.judge.status for m in matches if m.judge is not None
        ),
        structured_caught=all(m.structured_caught for m in matches),
    )


__all__ = [
    "Judge",
    "JudgeStatus",
    "JudgeVerdict",
    "MatchResult",
    "RunScore",
    "finding_count",
    "judge_status_errored",
    "match_expected",
    "produced_findings",
    "review_blocked",
    "review_incomplete",
    "score_run",
    "worst_judge_status",
]
