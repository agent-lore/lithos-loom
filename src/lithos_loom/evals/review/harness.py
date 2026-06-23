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

# A review function takes (case, head_sha) and returns the ReviewReport JSON.
ReviewFn = Callable[[Case, str], dict]

DEFAULT_K = 5
DEFAULT_BAR = 0.8


@dataclass(frozen=True)
class CaseResult:
    """Aggregated metrics for one case over K runs."""

    case_id: str
    n: int
    catch_rate: float
    severity_correctness: float
    false_positive_rate: float
    passed: bool


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

    def _review(head: str, variant: str, i: int) -> dict:
        report = review(case, head)
        if report_sink is not None:
            report_sink(case.id, variant, i, report)
        return report

    caught = 0
    severity_ok = 0
    for i in range(k):
        score = score_run(case, _review(case.head, "buggy", i), judge=judge)
        caught += int(score.caught)
        severity_ok += int(score.severity_correct)

    catch_rate = caught / k if k else 0.0
    severity_correctness = severity_ok / caught if caught else 0.0

    false_positive_rate = 0.0
    if case.known_good_head:
        j = known_good_runs if known_good_runs is not None else k
        flagged = sum(
            int(
                score_run(
                    case, _review(case.known_good_head, "known-good", i), judge=judge
                ).caught
            )
            for i in range(j)
        )
        false_positive_rate = flagged / j if j else 0.0

    return CaseResult(
        case_id=case.id,
        n=k,
        catch_rate=catch_rate,
        severity_correctness=severity_correctness,
        false_positive_rate=false_positive_rate,
        passed=catch_rate >= bar,
    )


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
