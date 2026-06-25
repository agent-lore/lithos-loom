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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ...plugins.story_develop.config import DevelopConfig
from ...plugins.story_develop.personas import canonical_personas
from ...plugins.story_develop.review_only import review_change
from ...plugins.story_develop.review_resolve import ResolvedChange
from .case import Case
from .match import Judge, score_run
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


ReportSink = Callable[[str, str, int, dict], None]


def run_case(
    case: Case,
    *,
    k: int = DEFAULT_K,
    bar: float = DEFAULT_BAR,
    judge: Judge | None = None,
    review_fn: ReviewFn | None = None,
    known_good_runs: int | None = None,
    report_sink: ReportSink | None = None,
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

        caught_samples: list[bool] = []
        severity_samples: list[bool] = []
        errored_samples: list[bool] = []
        for i in range(k):
            score = score_run(case, _review(case.head, "buggy", i), judge=judge)
            caught_samples.append(score.caught)
            severity_samples.append(score.severity_correct)
            # A crashed reviewer (incomplete report) that didn't catch is errored:
            # we can't tell a real miss from a crash-induced one, so exclude it. A
            # genuine catch is always trusted, even if a panel peer crashed.
            errored_samples.append((not score.caught) and score.incomplete)

        n_valid = k - sum(errored_samples)
        caught = sum(
            c for c, e in zip(caught_samples, errored_samples, strict=True) if not e
        )
        severity_ok = sum(
            s for s, e in zip(severity_samples, errored_samples, strict=True) if not e
        )
        catch_rate = caught / n_valid if n_valid else 0.0
        severity_correctness = severity_ok / caught if caught else 0.0

        fp_samples: list[bool] = []
        fp_errored_samples: list[bool] = []
        false_positive_rate = 0.0
        fp_ci = (0.0, 0.0)
        if case.known_good_head:
            j = known_good_runs if known_good_runs is not None else k
            for i in range(j):
                score = score_run(
                    case, _review(case.known_good_head, "known-good", i), judge=judge
                )
                fp_samples.append(score.caught)
                fp_errored_samples.append((not score.caught) and score.incomplete)
            fp_valid = j - sum(fp_errored_samples)
            flagged = sum(
                f for f, e in zip(fp_samples, fp_errored_samples, strict=True) if not e
            )
            false_positive_rate = flagged / fp_valid if fp_valid else 0.0
            fp_ci = wilson_interval(flagged, fp_valid)

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


def live_review(case: Case, head_sha: str) -> dict:
    """Run review-only mode against *head_sha* and return its report JSON.

    Host-only — needs docker + the agent CLIs. Resolves the case's personas to
    their canonical reviewer specs (engine / threshold / focus brief). The
    per-sample work dir (run state, handoffs, reviewer transcripts) is removed
    after the run so a K×cases×variants sweep does not leave state behind.
    """
    registry = canonical_personas()
    # load_case() validated every persona, so direct lookup can't KeyError and
    # an unknown name was never silently dropped.
    reviewers = tuple(registry[p] for p in case.personas)
    work_dir = Path(tempfile.mkdtemp(prefix="loom-eval-"))
    try:
        config = DevelopConfig(
            repo=Path(case.repo).resolve(),
            description=f"eval case {case.id}",
            work_dir=work_dir,
            acceptance_criteria=case.acceptance_criteria,
            review_profile=case.profile,
            reviewers=reviewers,
        )
        change = ResolvedChange(
            base_sha=_base_for(case, head_sha),
            head_sha=head_sha,
            head_ref=f"{case.id}@{head_sha[:12]}",
            body=case.acceptance_criteria,
        )
        return review_change(config, change).to_json()
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
