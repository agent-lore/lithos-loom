"""Eval harness: run a case K times and aggregate the rates (#183).

``run_case`` drives review-only mode (#154 — :func:`review_change`) K times
against a case's buggy head, scores each run, and reports **catch-rate**,
**severity-correctness** (among caught runs), and **false-positive rate** (on the
paired known-good head). The live run is host-only (docker + agent CLIs); the
review function is injectable so the aggregation logic stays hermetic.

This is an **on-demand** eval target — never part of ``make check``.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ...plugins.story_develop.config import DevelopConfig, ReviewerSpec
from ...plugins.story_develop.personas import canonical_personas
from ...plugins.story_develop.review_only import review_change
from ...plugins.story_develop.review_resolve import ResolvedChange
from .artifacts import seed_case_artifacts
from .case import Case
from .match import Judge, RunScore, judge_status_errored, score_run
from .patch import materialise_patch_heads
from .stats import wilson_interval

# A review function takes (case, head_sha) and returns the ReviewReport JSON.
ReviewFn = Callable[[Case, str], dict]

DEFAULT_K = 5
DEFAULT_BAR = 0.8


@dataclass(frozen=True)
class CaseResult:
    """Aggregated metrics for one case over K runs.

    The per-sample boolean tuples + Wilson 95% CIs (#182) let a catch-rate be
    read with its sampling error, not as a bare point estimate — the basis for
    measuring review-panel variance. ``false_positive_per_sample`` is empty when
    the case has no known-good head.

    A sample whose reviewer turn crashed (incomplete report) and did not catch is
    **errored** (#182 A3): excluded from the rate denominators and flagged in
    ``errored_per_sample`` — ``catch_rate`` / ``false_positive_rate`` and their
    CIs are over the *valid* (non-errored) samples only, so agent flakiness never
    masquerades as a review miss.

    The **noise** fields (#310) answer what the defect-specific rates cannot:
    ``false_positive_rate`` counts only known-good runs that reported *the case's
    expected defect*, so a panel filing four unrelated findings on a clean head —
    and **blocking** — still scores `0/3`. ``noise_rate`` is the fraction of
    *valid* known-good runs that reported **anything**, over the same denominator
    so the two read side by side; the per-sample counts and block flags are kept
    for both arms. The buggy arm is instrumented but deliberately **not** rated:
    an extra finding there may be a real second defect, whereas on the known-good
    head it is at best a distraction.

    The **judge** fields (#307) close the other half of that gap. Scoring has two
    halves — the reviewer produces findings, the judge rules on them — and either
    can fail. ``errored_per_sample`` keeps its exact meaning (a crashed
    *reviewer*); ``judge_status_per_sample`` records the *judge's* answer, whose
    error statuses mean it gave none. Both are excluded from every denominator,
    via ``excluded_per_sample``. ``structured_caught_per_sample`` is the free
    judge-free counterfactual, so a judge/structured divergence is visible in the
    result rather than only findable by re-scoring a report dir by hand.
    """

    case_id: str
    n: int
    catch_rate: float
    severity_correctness: float
    false_positive_rate: float
    passed: bool
    caught_per_sample: tuple[bool, ...] = ()
    severity_per_sample: tuple[bool, ...] = ()
    catch_rate_ci: tuple[float, float] = (0.0, 0.0)
    false_positive_per_sample: tuple[bool, ...] = ()
    false_positive_rate_ci: tuple[float, float] = (0.0, 0.0)
    errored_per_sample: tuple[bool, ...] = ()
    false_positive_errored_per_sample: tuple[bool, ...] = ()
    findings_per_sample: tuple[int, ...] = ()
    blocked_per_sample: tuple[bool, ...] = ()
    known_good_findings_per_sample: tuple[int, ...] = ()
    known_good_blocked_per_sample: tuple[bool, ...] = ()
    noise_rate: float = 0.0
    noise_rate_ci: tuple[float, float] = (0.0, 0.0)
    judge_status_per_sample: tuple[str, ...] = ()
    false_positive_judge_status_per_sample: tuple[str, ...] = ()
    structured_caught_per_sample: tuple[bool, ...] = ()
    false_positive_structured_per_sample: tuple[bool, ...] = ()

    @property
    def judge_errored_per_sample(self) -> tuple[bool, ...]:
        return tuple(judge_status_errored(s) for s in self.judge_status_per_sample)

    @property
    def false_positive_judge_errored_per_sample(self) -> tuple[bool, ...]:
        return tuple(
            judge_status_errored(s) for s in self.false_positive_judge_status_per_sample
        )

    @property
    def excluded_per_sample(self) -> tuple[bool, ...]:
        """Reviewer-errored OR judge-errored — the denominator rule (#182 A3)."""
        return _union(self.n, self.errored_per_sample, self.judge_errored_per_sample)

    @property
    def false_positive_excluded_per_sample(self) -> tuple[bool, ...]:
        return _union(
            len(self.false_positive_per_sample),
            self.false_positive_errored_per_sample,
            self.false_positive_judge_errored_per_sample,
        )


def _union(n: int, *flags: Sequence[bool]) -> tuple[bool, ...]:
    """Elementwise OR over *flags*, padding short/absent tuples with ``False``.

    Length-tolerant on purpose: a ``CaseResult`` built before these fields
    existed (or by a test stub) carries empty tuples, and must not raise.
    """
    return tuple(any(f[i] for f in flags if i < len(f)) for i in range(n))


ReportSink = Callable[[str, str, int, dict], None]
# Receives every scored sample as (case, variant, index, score) so the judge's
# verdicts can be persisted for audit. Takes the Case, not just its id: a verdict
# is only meaningful next to the `expected` that prompted it (#307).
JudgeSink = Callable[[Case, str, int, RunScore], None]


def count_valid(flags: Sequence[bool], errored: Sequence[bool]) -> tuple[int, int]:
    """``(true count, number of valid samples)`` ignoring the errored ones.

    Every rate in this module shares one denominator rule — the samples whose
    reviewer turn actually produced a verdict (#182 A3) — so catch, false
    positives and noise stay directly comparable. An *empty* ``errored`` (an
    older ``CaseResult``, or a caller that tracks no errors) means none errored.
    """
    if not errored:
        errored = (False,) * len(flags)
    valid = [f for f, e in zip(flags, errored, strict=True) if not e]
    return sum(valid), len(valid)


def run_case(
    case: Case,
    *,
    k: int = DEFAULT_K,
    bar: float = DEFAULT_BAR,
    judge: Judge | None = None,
    review_fn: ReviewFn | None = None,
    known_good_runs: int | None = None,
    report_sink: ReportSink | None = None,
    judge_sink: JudgeSink | None = None,
) -> CaseResult:
    """Run *case* *k* times and aggregate the catch / severity / FP rates.

    *review_fn* defaults to the live review-only run; tests inject a stub.
    *report_sink*, when given, receives every run's report JSON as
    ``(case_id, variant, index, report)`` so per-run findings can be retained
    for inspection (``variant`` is ``"buggy"`` / ``"known-good"``).
    """
    review = review_fn or live_review

    # #193: resolve any patch-defined head(s) to ephemeral commits ONCE, up front,
    # so the rest of the flow sees only shas (and they're reused across all K
    # samples). A sha-based case is identity + a no-op cleanup.
    case, cleanup = materialise_patch_heads(case)
    try:

        def _review(head: str, variant: str, i: int) -> dict:
            report = review(case, head)
            if report_sink is not None:
                report_sink(case.id, variant, i, report)
            return report

        def _score(head: str, variant: str, i: int) -> RunScore:
            score = score_run(case, _review(head, variant, i), judge=judge)
            if judge_sink is not None:
                judge_sink(case, variant, i, score)
            return score

        caught_samples: list[bool] = []
        severity_samples: list[bool] = []
        errored_samples: list[bool] = []
        findings_samples: list[int] = []
        blocked_samples: list[bool] = []
        judge_status_samples: list[str] = []
        structured_samples: list[bool] = []
        for i in range(k):
            score = _score(case.head, "buggy", i)
            caught_samples.append(score.caught)
            severity_samples.append(score.severity_correct)
            # A crashed reviewer (incomplete report) that didn't catch is errored:
            # we can't tell a real miss from a crash-induced one, so exclude it. A
            # genuine catch is always trusted, even if a panel peer crashed.
            errored_samples.append((not score.caught) and score.incomplete)
            findings_samples.append(score.n_findings)
            blocked_samples.append(score.blocked)
            judge_status_samples.append(score.judge_status)
            structured_samples.append(score.structured_caught)

        # The judge is the other half of the instrument, and it fails too (#307):
        # a timeout or an unreadable reply is an ABSENCE of a verdict, so it is
        # excluded exactly like a crashed reviewer rather than scoring as a miss.
        judge_err_samples = [judge_status_errored(s) for s in judge_status_samples]
        excluded = _union(k, errored_samples, judge_err_samples)

        caught, n_valid = count_valid(caught_samples, excluded)
        severity_ok, _ = count_valid(severity_samples, excluded)
        catch_rate = caught / n_valid if n_valid else 0.0
        severity_correctness = severity_ok / caught if caught else 0.0

        fp_samples: list[bool] = []
        fp_errored_samples: list[bool] = []
        kg_findings_samples: list[int] = []
        kg_blocked_samples: list[bool] = []
        false_positive_rate = 0.0
        fp_ci = (0.0, 0.0)
        noise_rate = 0.0
        noise_ci = (0.0, 0.0)
        kg_judge_status_samples: list[str] = []
        kg_structured_samples: list[bool] = []
        fp_excluded: tuple[bool, ...] = ()
        if case.known_good_head:
            j = known_good_runs if known_good_runs is not None else k
            for i in range(j):
                score = _score(case.known_good_head, "known-good", i)
                fp_samples.append(score.caught)
                fp_errored_samples.append((not score.caught) and score.incomplete)
                kg_findings_samples.append(score.n_findings)
                kg_blocked_samples.append(score.blocked)
                kg_judge_status_samples.append(score.judge_status)
                kg_structured_samples.append(score.structured_caught)
            fp_excluded = _union(
                j,
                fp_errored_samples,
                [judge_status_errored(s) for s in kg_judge_status_samples],
            )
            flagged, fp_valid = count_valid(fp_samples, fp_excluded)
            false_positive_rate = flagged / fp_valid if fp_valid else 0.0
            fp_ci = wilson_interval(flagged, fp_valid)
            # #310: "reported anything at all", over the same valid denominator
            # as the FP rate — an incomplete panel reports nothing, so counting
            # it as quiet would flatter the arm. The denominator stays shared
            # when the JUDGE fails too, so `fp` and `noise` never describe
            # different sets of runs (#307).
            noisy, _ = count_valid([n > 0 for n in kg_findings_samples], fp_excluded)
            noise_rate = noisy / fp_valid if fp_valid else 0.0
            noise_ci = wilson_interval(noisy, fp_valid)

        return CaseResult(
            case_id=case.id,
            n=k,
            catch_rate=catch_rate,
            severity_correctness=severity_correctness,
            false_positive_rate=false_positive_rate,
            passed=n_valid > 0 and catch_rate >= bar,
            caught_per_sample=tuple(caught_samples),
            severity_per_sample=tuple(severity_samples),
            catch_rate_ci=wilson_interval(caught, n_valid),
            false_positive_per_sample=tuple(fp_samples),
            false_positive_rate_ci=fp_ci,
            errored_per_sample=tuple(errored_samples),
            false_positive_errored_per_sample=tuple(fp_errored_samples),
            findings_per_sample=tuple(findings_samples),
            blocked_per_sample=tuple(blocked_samples),
            known_good_findings_per_sample=tuple(kg_findings_samples),
            known_good_blocked_per_sample=tuple(kg_blocked_samples),
            noise_rate=noise_rate,
            noise_rate_ci=noise_ci,
            judge_status_per_sample=tuple(judge_status_samples),
            false_positive_judge_status_per_sample=tuple(kg_judge_status_samples),
            structured_caught_per_sample=tuple(structured_samples),
            false_positive_structured_per_sample=tuple(kg_structured_samples),
        )
    finally:
        cleanup()


def _base_for(case: Case, head_sha: str) -> str:
    """The base to diff *head_sha* against — the defect base for the buggy head,
    the (optional) known-good base otherwise. Lets a case pair an independent
    defect diff and clean diff."""
    if head_sha == case.head:
        return case.base
    return case.known_good_base or case.base


def live_review(
    case: Case,
    head_sha: str,
    *,
    reviewers: tuple[ReviewerSpec, ...] | None = None,
    profile: str | None = None,
    default_models: dict[str, str] | None = None,
) -> dict:
    """Run review-only mode against *head_sha* and return its report JSON.

    Host-only — needs docker + the agent CLIs. Resolves the case's personas to
    their canonical reviewer specs (engine / threshold / focus brief) unless an
    already-resolved *reviewers* panel / *profile* override is supplied (the
    RH-7 seam — the eval CLI resolves overrides once and closes over them). The
    per-sample work dir (run state, handoffs, reviewer transcripts) is removed
    after the run so a K×cases×variants sweep does not leave state behind.
    """
    if reviewers is None:
        registry = canonical_personas()
        # load_case() validated every persona, so direct lookup can't KeyError
        # and an unknown name was never silently dropped.
        reviewers = tuple(registry[p] for p in case.personas)
    work_dir = Path(tempfile.mkdtemp(prefix="loom-eval-"))
    try:
        config = DevelopConfig(
            repo=Path(case.repo).resolve(),
            description=f"eval case {case.id}",
            work_dir=work_dir,
            acceptance_criteria=case.acceptance_criteria,
            review_profile=profile or case.profile,
            reviewers=reviewers,
            # #305: the switch-time model source should a reviewer's engine
            # ever fall back mid-run (canonical personas carry no chains today)
            default_models=default_models or {},
        )
        change = ResolvedChange(
            base_sha=_base_for(case, head_sha),
            head_sha=head_sha,
            head_ref=f"{case.id}@{head_sha[:12]}",
            body=case.acceptance_criteria,
        )
        # RH-3: an artifact case seeds its checked-in captures into the run's
        # artifacts dir and measures the approval-hold artifact-review pass
        # instead of the diff panel — one surface per case, so a catch is
        # attributable (findings carry no pass provenance). The seeder picks
        # the captures matching *this* head (RH-1), so the known-good run
        # reviews the fixed render.
        if case.artifacts_dir is not None:
            seed_case_artifacts(case, config, head_sha=head_sha)
            return review_change(config, change, artifact_only=True).to_json()
        return review_change(config, change).to_json()
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
