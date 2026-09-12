"""Triage-eval harness: score one batch verdict, aggregate K samples.

Two rates, one denominator rule. A sample is one triage turn over the whole
batch (production shape). **Reject rate** = known-false findings rejected
*with a citation into their refutation files* over every known-false
opportunity in the valid samples; **over-suppression rate** = must-proceed
findings that were rejected over every such opportunity. A sample whose
triage turn degraded (``TriageVerdicts.note`` set — failed turn, no verdict
file) defaulted to act on everything, which is the step's contract, not a
verdict: it is *errored* and excluded from both denominators, exactly as the
review harness excludes a crashed reviewer.

The live triage function is host-only (a read-only container turn); it is
injectable so the arithmetic stays hermetic.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ...plugins.story_develop.config import DevelopConfig
from ...plugins.story_develop.external_triage import (
    TriageVerdicts,
    triage_external_findings,
)
from ...plugins.story_develop.handoff import Finding
from ...plugins.story_develop.panel import ReviewOutcome
from ...plugins.story_develop.review_resolve import ResolvedChange
from ..review.stats import wilson_interval
from .case import TriageCase

__all__ = [
    "DEFAULT_BAR",
    "DEFAULT_K",
    "SampleScore",
    "TriageCaseResult",
    "TriageFn",
    "TriageSink",
    "aggregate_triage",
    "live_triage",
    "run_triage_case",
    "score_sample",
]

TriageFn = Callable[[TriageCase], TriageVerdicts]
# (case_id, sample index, payload) — one call per sample, for retention.
TriageSink = Callable[[str, int, dict], None]

DEFAULT_K = 5
DEFAULT_BAR = 0.8
_SEVERITY_RANK = {"minor": 0, "major": 1, "critical": 2}


@dataclass(frozen=True)
class SampleScore:
    correct: dict[str, bool]
    proceed: tuple[str, ...]
    rejections: dict[str, str]
    errored: bool
    note: str
    cost_usd: float
    rejected_known_false: int
    suppressed_known_true: int

    def payload(self) -> dict:
        return {
            "proceed": list(self.proceed),
            "rejections": dict(self.rejections),
            "correct": dict(self.correct),
            "errored": self.errored,
            "note": self.note,
            "cost_usd": self.cost_usd,
            "rejected_known_false": self.rejected_known_false,
            "suppressed_known_true": self.suppressed_known_true,
        }


def _cites(evidence: str, files: Sequence[str]) -> bool:
    """True when *evidence* carries a ``<file>:<line>`` citation into *files*.

    The ``:`` is required: it is the citation form the parser accepts, and it
    keeps ``a.py`` from matching inside ``za.py`` or a prose mention.
    """
    return any(f"{path}:" in evidence for path in files)


def score_sample(case: TriageCase, verdicts: TriageVerdicts) -> SampleScore:
    """Score one batch verdict against the case's expected verdicts."""
    correct: dict[str, bool] = {}
    rejected_kf = 0
    suppressed_kt = 0
    for f in case.findings:
        if f.expected == "reject":
            ok = f.finding_id in verdicts.rejections and _cites(
                verdicts.rejections[f.finding_id], f.refutation_files
            )
            rejected_kf += int(ok)
        else:
            ok = f.finding_id not in verdicts.rejections
            suppressed_kt += int(not ok)
        correct[f.finding_id] = ok
    return SampleScore(
        correct=correct,
        proceed=tuple(verdicts.proceed),
        rejections=dict(verdicts.rejections),
        errored=bool(verdicts.note),
        note=verdicts.note,
        cost_usd=verdicts.cost_usd,
        rejected_known_false=rejected_kf,
        suppressed_known_true=suppressed_kt,
    )


@dataclass(frozen=True)
class TriageCaseResult:
    case_id: str
    n: int
    n_valid: int
    reject_rate: float
    reject_rate_ci: tuple[float, float]
    over_suppression_rate: float
    over_suppression_ci: tuple[float, float]
    passed: bool
    rejected_known_false: int
    known_false_opportunities: int
    suppressed_known_true: int
    known_true_opportunities: int
    errored_per_sample: tuple[bool, ...]
    rejected_known_false_per_sample: tuple[int, ...]
    suppressed_known_true_per_sample: tuple[int, ...]
    cost_usd_per_sample: tuple[float, ...]
    notes_per_sample: tuple[str, ...]
    per_finding_correct: dict[str, int]
    per_finding_expected: dict[str, str]


def aggregate_triage(
    case_id: str,
    scores: Sequence[SampleScore],
    *,
    case: TriageCase,
    k: int,
    bar: float,
    max_over_suppression: float,
) -> TriageCaseResult:
    """Turn per-sample scores into a :class:`TriageCaseResult`.

    ``passed`` needs valid samples, the reject rate at the bar (vacuously
    true when the batch has no known-false), and over-suppression at or
    under *max_over_suppression* — default 0: one wrongly rejected true
    finding fails the case, because that is the expensive direction.
    """
    valid = [s for s in scores if not s.errored]
    n_kf = len(case.known_false)
    n_kt = len(case.known_true)
    rejected = sum(s.rejected_known_false for s in valid)
    kf_opps = n_kf * len(valid)
    suppressed = sum(s.suppressed_known_true for s in valid)
    kt_opps = n_kt * len(valid)
    reject_rate = rejected / kf_opps if kf_opps else 0.0
    over = suppressed / kt_opps if kt_opps else 0.0
    per_finding = {
        f.finding_id: sum(int(s.correct.get(f.finding_id, False)) for s in valid)
        for f in case.findings
    }
    passed = (
        bool(valid)
        and (kf_opps == 0 or reject_rate >= bar)
        and over <= max_over_suppression
    )
    return TriageCaseResult(
        case_id=case_id,
        n=k,
        n_valid=len(valid),
        reject_rate=reject_rate,
        reject_rate_ci=wilson_interval(rejected, kf_opps) if kf_opps else (0.0, 0.0),
        over_suppression_rate=over,
        over_suppression_ci=wilson_interval(suppressed, kt_opps)
        if kt_opps
        else (0.0, 0.0),
        passed=passed,
        rejected_known_false=rejected,
        known_false_opportunities=kf_opps,
        suppressed_known_true=suppressed,
        known_true_opportunities=kt_opps,
        errored_per_sample=tuple(s.errored for s in scores),
        rejected_known_false_per_sample=tuple(s.rejected_known_false for s in scores),
        suppressed_known_true_per_sample=tuple(s.suppressed_known_true for s in scores),
        cost_usd_per_sample=tuple(s.cost_usd for s in scores),
        notes_per_sample=tuple(s.note for s in scores),
        per_finding_correct=per_finding,
        per_finding_expected={f.finding_id: f.expected for f in case.findings},
    )


def run_triage_case(
    case: TriageCase,
    *,
    k: int = DEFAULT_K,
    bar: float = DEFAULT_BAR,
    max_over_suppression: float = 0.0,
    triage_fn: TriageFn,
    sink: TriageSink | None = None,
) -> TriageCaseResult:
    """Triage the batch *k* times and aggregate."""
    scores: list[SampleScore] = []
    for i in range(k):
        score = score_sample(case, triage_fn(case))
        if sink is not None:
            sink(case.id, i, score.payload())
        scores.append(score)
    return aggregate_triage(
        case.id,
        scores,
        case=case,
        k=k,
        bar=bar,
        max_over_suppression=max_over_suppression,
    )


def _outcome_for(case: TriageCase) -> ReviewOutcome:
    findings = [
        Finding(
            finding_id=f.finding_id,
            severity=f.severity,
            status="open",
            files=list(f.files),
            rationale=f.rationale,
        )
        for f in case.findings
    ]
    top = max(case.findings, key=lambda f: _SEVERITY_RANK[f.severity]).severity
    return ReviewOutcome(
        reviewer="external",
        status="FINDINGS",
        passed=False,
        max_severity=top,
        findings=findings,
    )


def live_triage(
    case: TriageCase,
    *,
    tool: str,
    model: str,
    effort: str | None,
    default_models: dict[str, str] | None = None,
) -> TriageVerdicts:
    """Run the production triage step once over the case's batch.

    Host-only — docker + the agent CLI. Builds the same ``DevelopConfig`` /
    ``ResolvedChange`` / ``ReviewOutcome`` the remediation path hands
    :func:`triage_external_findings`, positioned at the case's sha, and
    returns its verdicts. The per-sample work dir is removed afterwards.
    """
    work_dir = Path(tempfile.mkdtemp(prefix="loom-eval-triage-"))
    try:
        extra: dict = {"image": case.image} if case.image else {}
        config = DevelopConfig(
            repo=Path(case.repo).resolve(),
            description=f"eval triage {case.id}",
            work_dir=work_dir,
            acceptance_criteria=case.acceptance_criteria,
            coder=tool,
            coder_model=model,
            coder_effort=effort,
            default_models=default_models or {},
            **extra,
        )
        change = ResolvedChange(
            base_sha=case.sha,
            head_sha=case.sha,
            head_ref=f"{case.id}@{case.sha[:12]}",
            body=case.acceptance_criteria,
        )
        return triage_external_findings(config, change, _outcome_for(case))
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
