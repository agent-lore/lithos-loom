"""Re-score retained eval reports offline — no reviewer ever runs again (#307).

Scoring is a pure function of ``(case.expected, stored report JSON, judge)``:
no git, no worktree, no container. So a report dir written by a costly K-sample
sweep can be scored again for **judge calls alone** — 69 for the whole pinned
baseline, against hours of container reviewer runs. That is what makes judge
variance measurable at all, and what turns a judge-prompt change from "invalidates
a paid baseline" into "costs a few dollars to re-score".

``--judge-repeats N`` is the measurement #307 was filed for: ask the judge the
*same question* N times over identical stored findings and see whether it answers
the same way. Repeat 0 is authoritative — this command must **measure** variance,
not silently change the estimator while measuring it (majority-of-N is the
issue's suggestion 3, conditional on what this reports).

Typer lives in ``cli_rescore``; everything here is importable and testable with a
scripted judge.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .case import Case, expected_fingerprint
from .harness import CaseResult, aggregate_case
from .match import Judge, JudgeVerdict, RunScore, produced_findings, score_run

_VARIANTS = ("buggy", "known-good")
# Files a report dir may hold besides `<variant>-<i>.json`. `rescore.json` is in
# the list so running this command twice against one dir does not trip its own
# strict-filename check on its own output.
_ALLOWED_FILES = frozenset({"summary.json", "rescore.json"})


class RescoreError(Exception):
    """A report dir cannot be scored as asked — always raised before paid work."""


@dataclass(frozen=True)
class SampleReport:
    """One retained ``<variant>-<i>.json``."""

    variant: str
    index: int
    path: Path
    report: dict


@dataclass(frozen=True)
class CaseReports:
    """Every retained artefact for one case in a report dir."""

    case_id: str
    buggy: tuple[SampleReport, ...]
    known_good: tuple[SampleReport, ...] = ()
    summary: dict | None = None

    def samples(self) -> tuple[SampleReport, ...]:
        return self.buggy + self.known_good


@dataclass(frozen=True)
class JudgeSite:
    """One (sample × expected) decision point, and its verdict per repeat."""

    variant: str
    sample: int
    expected: int
    produced_ids: tuple[str, ...]
    verdicts: tuple[frozenset[str], ...] = ()

    @property
    def judged(self) -> bool:
        """Whether the judge was actually consulted (it skips empty findings)."""
        return bool(self.produced_ids)

    @property
    def stable(self) -> bool:
        return len(set(self.verdicts)) <= 1


@dataclass(frozen=True)
class CaseRescore:
    """A case re-scored: the authoritative result plus what varied around it."""

    case_id: str
    judged: CaseResult
    structured: CaseResult
    sites: tuple[JudgeSite, ...] = ()
    catch_per_repeat: tuple[int, ...] = ()
    fingerprint: str = ""
    identity: str = "unverifiable"  # verified | unverifiable | changed
    drift: dict = field(default_factory=dict)

    @property
    def judged_sites(self) -> int:
        return sum(1 for s in self.sites if s.judged)

    @property
    def flipped_sites(self) -> int:
        return sum(1 for s in self.sites if s.judged and not s.stable)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RescoreError(f"{path}: not valid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise RescoreError(f"{path}: not a JSON object")
    return data


def _parse_name(name: str) -> tuple[str, int] | None:
    for variant in _VARIANTS:
        prefix = f"{variant}-"
        if name.startswith(prefix) and name.endswith(".json"):
            digits = name[len(prefix) : -len(".json")]
            if digits.isdigit():
                return variant, int(digits)
    return None


def _load_case_reports(case_dir: Path) -> CaseReports:
    by_variant: dict[str, list[SampleReport]] = {v: [] for v in _VARIANTS}
    summary: dict | None = None
    for path in sorted(case_dir.iterdir()):
        if path.is_dir():
            continue  # `judge/` sidecars (#307) — not scoring inputs
        if path.name in _ALLOWED_FILES:
            if path.name == "summary.json":
                summary = _read_json(path)
            continue
        parsed = _parse_name(path.name)
        if parsed is None:
            raise RescoreError(
                f"{case_dir.name}: unexpected file {path.name!r} — expected "
                "summary.json, rescore.json, or <variant>-<i>.json"
            )
        variant, index = parsed
        report = _read_json(path)
        if "reviewers" not in report:
            raise RescoreError(f"{path}: not a ReviewReport (no 'reviewers' key)")
        by_variant[variant].append(SampleReport(variant, index, path, report))

    for variant, samples in by_variant.items():
        samples.sort(key=lambda s: s.index)
        indices = [s.index for s in samples]
        if indices and indices != list(range(len(indices))):
            raise RescoreError(
                f"{case_dir.name}: {variant} sample indices are not contiguous "
                f"({indices}) — the recorded per-sample tuples are positional, so "
                "a drift comparison would misalign"
            )
    if not by_variant["buggy"]:
        raise RescoreError(
            f"{case_dir.name}: no buggy-N.json reports — refusing to rescore a "
            "case with no defect arm"
        )
    return CaseReports(
        case_id=case_dir.name,
        buggy=tuple(by_variant["buggy"]),
        known_good=tuple(by_variant["known-good"]),
        summary=summary,
    )


def load_report_dir(
    report_dir: Path, *, case_id: str | None = None
) -> list[CaseReports]:
    """Every case in *report_dir*, parsed up front so failures precede paid work."""
    if not report_dir.is_dir():
        raise RescoreError(f"no report directory at {report_dir}")
    case_dirs = sorted(d for d in report_dir.iterdir() if d.is_dir())
    if case_id is not None:
        case_dirs = [d for d in case_dirs if d.name == case_id]
        if not case_dirs:
            raise RescoreError(f"no case {case_id!r} under {report_dir}")
    loaded = [_load_case_reports(d) for d in case_dirs]
    if not loaded:
        raise RescoreError(
            f"no eval reports under {report_dir} "
            "(expected <dir>/<case-id>/buggy-0.json)"
        )
    return loaded


def judge_call_count(
    reports: Sequence[CaseReports], cases: Mapping[str, Case], repeats: int
) -> int:
    """Exactly how many judge calls a rescore will make — known before paying.

    Not an estimate: the reports are already parsed, and the judge short-circuits
    a sample with no findings without calling an agent, so samples that produced
    nothing cost nothing here either.
    """
    total = 0
    for case_reports in reports:
        n_expected = len(cases[case_reports.case_id].expected)
        for sample in case_reports.samples():
            if produced_findings(sample.report):
                total += n_expected * repeats
    return total


def _record_sites(
    case: Case, reports: CaseReports, *, judge: Judge, repeats: int
) -> dict[tuple[str, int], list[list[JudgeVerdict]]]:
    """Ask the judge every question ``repeats`` times; keep each answer.

    Keyed by (variant, sample) then expected index, in ``score_run``'s own call
    order — so replaying a universe needs no correlation on mechanism strings.
    """
    recorded: dict[tuple[str, int], list[list[JudgeVerdict]]] = {}
    for sample in reports.samples():
        produced = produced_findings(sample.report)
        per_expected: list[list[JudgeVerdict]] = []
        for expected in case.expected:
            if not produced:
                # Mirror the judge's own short-circuit exactly, so the printed
                # call count is the count actually paid.
                per_expected.append([JudgeVerdict() for _ in range(repeats)])
                continue
            per_expected.append(
                [judge(expected.mechanism, produced) for _ in range(repeats)]
            )
        recorded[(sample.variant, sample.index)] = per_expected
    return recorded


def _replay_judge(verdicts: Sequence[JudgeVerdict]) -> Judge:
    """A judge that answers from a recording, in ``score_run``'s call order."""
    calls = {"n": 0}

    def judge(mechanism: str, findings: list[dict]) -> JudgeVerdict:
        i = calls["n"]
        calls["n"] += 1
        return verdicts[i] if i < len(verdicts) else JudgeVerdict()

    return judge


def rescore_case(
    case: Case,
    reports: CaseReports,
    *,
    bar: float,
    judge: Judge | None,
    repeats: int = 1,
) -> CaseRescore:
    """Score *reports* against *case*, plus the free structured counterfactual."""
    k = len(reports.buggy)

    def universe(pick: int | None) -> CaseResult:
        """Score every sample. ``pick=None`` means the judge-free matcher."""

        def scores(samples: Sequence[SampleReport]) -> list[RunScore]:
            out = []
            for s in samples:
                if pick is None:
                    out.append(score_run(case, s.report, judge=None))
                else:
                    per_expected = recorded[(s.variant, s.index)]
                    replay = _replay_judge([v[pick] for v in per_expected])
                    out.append(score_run(case, s.report, judge=replay))
            return out

        return aggregate_case(
            case.id, scores(reports.buggy), scores(reports.known_good), k=k, bar=bar
        )

    recorded: dict[tuple[str, int], list[list[JudgeVerdict]]] = {}
    sites: list[JudgeSite] = []
    catch_per_repeat: list[int] = []

    if judge is None:
        structured = universe(None)
        return CaseRescore(
            case_id=case.id,
            judged=structured,
            structured=structured,
            fingerprint=expected_fingerprint(case),
        )

    recorded = _record_sites(case, reports, judge=judge, repeats=repeats)
    for sample in reports.samples():
        per_expected = recorded[(sample.variant, sample.index)]
        produced = produced_findings(sample.report)
        ids = tuple(str(f.get("finding_id", "")) for f in produced)
        for idx, verdicts in enumerate(per_expected):
            sites.append(
                JudgeSite(
                    variant=sample.variant,
                    sample=sample.index,
                    expected=idx,
                    produced_ids=ids,
                    verdicts=tuple(frozenset(v.matched_ids) for v in verdicts),
                )
            )

    universes = [universe(r) for r in range(repeats)]
    catch_per_repeat = [sum(u.caught_per_sample) for u in universes]

    return CaseRescore(
        case_id=case.id,
        judged=universes[0],  # repeat 0 is authoritative — measure, don't re-estimate
        structured=universe(None),
        sites=tuple(sites),
        catch_per_repeat=tuple(catch_per_repeat),
        fingerprint=expected_fingerprint(case),
    )


_DRIFT_KEYS = (
    "caught_per_sample",
    "severity_per_sample",
    "false_positive_per_sample",
    "errored_per_sample",
)


def drift_vs_summary(judged: CaseResult, summary: dict | None) -> dict:
    """Per-key comparison against what the run recorded, if anything.

    A key the recorded summary never had reports ``None`` rather than a false
    match: report dirs predate most of these fields, and "absent" must not read
    as "agreed".
    """
    if summary is None:
        return {}
    out: dict = {}
    for key in _DRIFT_KEYS:
        if key not in summary:
            out[key] = None
            continue
        recorded = list(summary[key])
        now = list(getattr(judged, key))
        flipped = [
            i for i, (a, b) in enumerate(zip(recorded, now, strict=False)) if a != b
        ]
        out[key] = {"recorded": recorded, "rescored": now, "flipped": flipped}
    return out


def identity_of(case: Case, summary: dict | None) -> str:
    """``verified`` | ``changed`` | ``unverifiable`` for a case's scoring inputs."""
    if summary is None or "expected_fingerprint" not in summary:
        return "unverifiable"
    return (
        "verified"
        if summary["expected_fingerprint"] == expected_fingerprint(case)
        else "changed"
    )
