"""Triage-eval harness: build the tree, score one batch verdict, aggregate K.

Two rates, one denominator rule. A sample is one triage turn over the whole
batch (production shape). **Reject rate** = known-false findings rejected
*with a citation into their declared refutation* (file, and line range when
given) over every known-false opportunity in the valid samples;
**over-suppression rate** = must-proceed findings that were rejected over
every such opportunity. A sample whose triage turn degraded
(``TriageVerdicts.note`` set — failed turn, no verdict file) defaulted to
act on everything, which is the step's contract, not a verdict: it is
*errored* and excluded from both denominators, exactly as the review
harness excludes a crashed reviewer.

The batch reaches the step through the PRODUCTION intake
(``external_intake_reviews``): the eval builds ``ExternalFinding``s and lets
the ledger assign ids, the ``[author]`` prefix, the single anchor and the
severity — so what the triage agent reads is what ``converge --from-github``
would hand it. The live function is host-only (a read-only container turn);
it is injectable so the arithmetic stays hermetic.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ...plugins.story_develop.config import DevelopConfig
from ...plugins.story_develop.external_reviews import (
    ExternalFinding,
    ReviewStream,
    adapter_for,
    external_intake_reviews,
)
from ...plugins.story_develop.external_triage import (
    TriageVerdicts,
    cited_locations,
    triage_external_findings,
)
from ...plugins.story_develop.review_resolve import ResolvedChange
from ...runner import worktree
from ..review.patch import materialise_patched_head
from ..review.stats import wilson_interval
from .case import TriageCase, TriageFinding

__all__ = [
    "DEFAULT_BAR",
    "DEFAULT_K",
    "DEFAULT_TIMEOUT",
    "SampleScore",
    "TriageCaseResult",
    "TriageFn",
    "TriageSink",
    "aggregate_triage",
    "expected_fingerprint",
    "external_findings_for",
    "live_triage",
    "materialise_tree",
    "run_triage_case",
    "score_sample",
]

# (case, sha) → the step's verdicts. The sha is the materialised tree.
TriageFn = Callable[[TriageCase, str], TriageVerdicts]
# (case_id, sample index, payload) — one call per sample, for retention.
TriageSink = Callable[[str, int, dict], None]

DEFAULT_K = 5
DEFAULT_BAR = 0.8
# converge's reviewer_timeout default — the production step runs under it.
DEFAULT_TIMEOUT = 3600


def expected_fingerprint(case: TriageCase) -> str:
    """A stable hash of what the scorer consumes (cf. ``eval review``, #307).

    Recorded in ``summary.json`` so two report dirs can refuse a comparison
    across a reworded claim, a widened refutation or a flipped expectation.
    """
    payload = [
        {
            "id": f.finding_id,
            "author": f.author,
            "anchor": f.anchor,
            "body": f.body,
            "expected": f.expected,
            "ambiguous": f.ambiguous,
            "refutation": [r.spec for r in f.refutation],
        }
        for f in case.findings
    ]
    raw = json.dumps({"id": case.id, "findings": payload}, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def materialise_tree(case: TriageCase) -> tuple[str, Callable[[], None]]:
    """``(sha, cleanup)`` — identity for the sha form; ``base + head_patch`` as
    an ephemeral commit otherwise (the build worktree keeps it reachable until
    ``cleanup``). Call once per case so K samples share the tree."""
    if case.sha:
        return case.sha, lambda: None
    assert case.head_patch is not None and case.case_dir is not None
    repo = Path(case.repo).resolve()
    parent = Path(tempfile.mkdtemp(prefix="loom-eval-triage-patch-"))
    sha, wt = materialise_patched_head(
        repo, case.base, case.case_dir / case.head_patch, parent=parent
    )

    def cleanup() -> None:
        try:
            worktree.remove(wt, force=True)
        finally:
            shutil.rmtree(parent, ignore_errors=True)

    return sha, cleanup


@dataclass(frozen=True)
class SampleScore:
    correct: dict[str, bool]
    proceed: tuple[str, ...]
    rejections: dict[str, str]
    line_kinds: dict[str, str]
    verdict_text: str
    errored: bool
    note: str
    cost_usd: float
    rejected_known_false: int
    suppressed_known_true: int

    def payload(self) -> dict:
        return {
            "proceed": list(self.proceed),
            "rejections": dict(self.rejections),
            "line_kinds": dict(self.line_kinds),
            "verdict_text": self.verdict_text,
            "correct": dict(self.correct),
            "errored": self.errored,
            "note": self.note,
            "cost_usd": self.cost_usd,
            "rejected_known_false": self.rejected_known_false,
            "suppressed_known_true": self.suppressed_known_true,
        }


def _cites_refutation(evidence: str, finding: TriageFinding) -> bool:
    """True when a cited ``file:line`` in *evidence* lands in the finding's
    declared refutation (path equality; inside the range when one is given)."""
    return any(
        r.covers(path, line)
        for path, line in cited_locations(evidence)
        for r in finding.refutation
    )


def score_sample(case: TriageCase, verdicts: TriageVerdicts) -> SampleScore:
    """Score one batch verdict against the case's expected verdicts."""
    correct: dict[str, bool] = {}
    rejected_kf = 0
    suppressed_kt = 0
    for f in case.findings:
        if f.expected == "reject":
            ok = f.finding_id in verdicts.rejections and _cites_refutation(
                verdicts.rejections[f.finding_id], f
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
        line_kinds=dict(verdicts.line_kinds),
        verdict_text=verdicts.verdict_text,
        errored=bool(verdicts.note),
        note=verdicts.note,
        cost_usd=verdicts.cost_usd,
        rejected_known_false=rejected_kf,
        suppressed_known_true=suppressed_kt,
    )


@dataclass(frozen=True)
class TriageCaseResult:
    """Aggregated metrics for one case over K turns.

    Rates are per OPPORTUNITY (finding × valid sample), with Wilson intervals
    over those counts — which ignore that the opportunities in one sample come
    from one turn and that the same findings are re-asked every sample, so the
    bands are a lower bound on the uncertainty. ``samples_with_suppression``
    is the per-sample view of the gated rate (samples with ≥1 must-proceed
    finding rejected); ``per_finding_correct`` counts over the VALID samples,
    unlike the ``*_per_sample`` tuples which cover all ``n``.
    """

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
    samples_with_suppression: int
    errored_per_sample: tuple[bool, ...]
    rejected_known_false_per_sample: tuple[int, ...]
    suppressed_known_true_per_sample: tuple[int, ...]
    cost_usd_per_sample: tuple[float, ...]
    notes_per_sample: tuple[str, ...]
    per_finding_correct: dict[str, int]
    per_finding_expected: dict[str, str]
    per_finding_ambiguous: dict[str, bool]


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
        over_suppression_ci=(
            wilson_interval(suppressed, kt_opps) if kt_opps else (0.0, 0.0)
        ),
        passed=passed,
        rejected_known_false=rejected,
        known_false_opportunities=kf_opps,
        suppressed_known_true=suppressed,
        known_true_opportunities=kt_opps,
        samples_with_suppression=sum(1 for s in valid if s.suppressed_known_true),
        errored_per_sample=tuple(s.errored for s in scores),
        rejected_known_false_per_sample=tuple(s.rejected_known_false for s in scores),
        suppressed_known_true_per_sample=tuple(s.suppressed_known_true for s in scores),
        cost_usd_per_sample=tuple(s.cost_usd for s in scores),
        notes_per_sample=tuple(s.note for s in scores),
        per_finding_correct=per_finding,
        per_finding_expected={f.finding_id: f.expected for f in case.findings},
        per_finding_ambiguous={f.finding_id: f.ambiguous for f in case.findings},
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
    """Materialise the tree once, triage the batch *k* times, aggregate."""
    sha, cleanup = materialise_tree(case)
    try:
        scores: list[SampleScore] = []
        for i in range(k):
            score = score_sample(case, triage_fn(case, sha))
            if sink is not None:
                sink(case.id, i, {"sha": sha, **score.payload()})
            scores.append(score)
    finally:
        cleanup()
    return aggregate_triage(
        case.id,
        scores,
        case=case,
        k=k,
        bar=bar,
        max_over_suppression=max_over_suppression,
    )


def external_findings_for(case: TriageCase, sha: str) -> list[ExternalFinding]:
    """The batch as production would carry it into the intake.

    ``head_sha`` is the triaged tree, so the intake adds no re-anchor note;
    the stream is inline when the claim has a path (an inline comment is
    the only stream that carries one), conversation otherwise. Identity
    fields the reply epilogue needs are filled with eval placeholders — the
    eval never replies.
    """
    out: list[ExternalFinding] = []
    for i, f in enumerate(case.findings, start=1):
        stream = ReviewStream.INLINE if f.path else ReviewStream.CONVERSATION
        out.append(
            ExternalFinding(
                author=f.author,
                source="human",
                trusted=True,
                stream=stream,
                activity_id=i,
                reply_mode=adapter_for(stream).reply_mode,
                thread_url="",
                head_sha=sha,
                path=f.path,
                line=f.line,
                body=f.body,
            )
        )
    return out


def live_triage(
    case: TriageCase,
    sha: str,
    *,
    tool: str,
    model: str,
    effort: str | None,
    timeout: int = DEFAULT_TIMEOUT,
    default_models: dict[str, str] | None = None,
) -> TriageVerdicts:
    """Run the production triage step once over the case's batch at *sha*.

    Host-only — docker + the agent CLI. Same recipe as converge's external
    mode: ``external_intake_reviews`` → the seed outcome →
    :func:`triage_external_findings` under the reviewer timeout. The
    per-sample work dir is removed afterwards; the verdict file's text and
    per-line classes come back on the verdicts, so nothing is lost with it.
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
            base_sha=sha,
            head_sha=sha,
            head_ref=f"{case.id}@{sha[:12]}",
            body=case.acceptance_criteria,
        )
        seed, _ = external_intake_reviews(
            external_findings_for(case, sha), current_head_sha=sha
        )
        return triage_external_findings(config, change, seed[0], timeout=timeout)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
