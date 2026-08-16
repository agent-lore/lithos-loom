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

Two invariants shape the module. **A judge error is never a verdict**: a timeout
and a veto both produce no matched ids, so a site is only called *stable* when
every repeat actually answered — otherwise an all-failed measurement would read
as 100% stable, which is the exact false confidence this command exists to
remove. And **everything that can fail the scorer fails at load**: the retained
reports are already parsed before the first judge call, so a malformed one is a
usage error rather than an ``AttributeError`` after half the sweep is paid for.

Typer lives in ``cli_rescore``; everything here is importable and testable with a
scripted judge.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .case import SEVERITIES, Case, expected_fingerprint
from .harness import DEFAULT_BAR, CaseResult, aggregate_case, count_valid
from .match import Judge, JudgeVerdict, RunScore, produced_findings, score_run

_VARIANTS = ("buggy", "known-good")

# This command's own output. Named here because it is also the ONE extra file a
# case dir may hold: writing anything else into one makes the dir unloadable on
# the next run, so `--out` is checked against this too.
RESCORE_FILENAME = "rescore.json"
# Files a report dir may hold besides `<variant>-<i>.json`. `rescore.json` is in
# the list so running this command twice against one dir does not trip its own
# strict-filename check on its own output.
_ALLOWED_FILES = frozenset({"summary.json", RESCORE_FILENAME})


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
    summary_path: Path | None = None
    case_dir: Path | None = None

    def samples(self) -> tuple[SampleReport, ...]:
        return self.buggy + self.known_good

    def input_paths(self) -> tuple[Path, ...]:
        """Every retained file this case was read from — what ``--out`` must not
        clobber: each one is the product of a paid reviewer run."""
        paths = [s.path for s in self.samples()]
        if self.summary_path is not None:
            paths.append(self.summary_path)
        return tuple(paths)


@dataclass(frozen=True)
class JudgeSite:
    """One (sample × expected) decision point, and its verdict per repeat.

    The full :class:`~.match.JudgeVerdict` is kept, not just its matched ids:
    an ``ok`` veto and a timed-out call both carry no ids, so collapsing them
    would let a site the judge never answered be reported as a stable veto.
    """

    variant: str
    sample: int
    expected: int
    produced_ids: tuple[str, ...]
    verdicts: tuple[JudgeVerdict, ...] = ()

    @property
    def judged(self) -> bool:
        """Whether the judge was actually consulted (it skips empty findings)."""
        return bool(self.produced_ids)

    @property
    def answers(self) -> tuple[frozenset[str], ...]:
        """The matched-id sets of the repeats that produced an answer at all."""
        return tuple(frozenset(v.matched_ids) for v in self.verdicts if v.usable)

    @property
    def errored(self) -> bool:
        """Any repeat gave no usable verdict (a timeout, an unreadable reply)."""
        return any(not v.usable for v in self.verdicts)

    @property
    def flipped(self) -> bool:
        """Two repeats that both answered disagreed — observed instability.

        Counted even when other repeats errored: a disagreement *seen* is a
        fact about the judge, and dropping the site for an unrelated timeout
        would hide the very thing being measured.
        """
        return len(set(self.answers)) > 1

    @property
    def stable(self) -> bool:
        """Every repeat answered, and they all agreed — the only honest 'stable'."""
        return self.judged and not self.errored and len(set(self.answers)) <= 1

    @property
    def measured(self) -> bool:
        """In the stability denominator: an observed flip, or a clean sweep.

        A judged site that neither flipped nor answered every time is
        *unmeasured* — reported separately rather than folded into either
        column, because it is an absence of data, not a result.
        """
        return self.judged and (self.stable or self.flipped)


@dataclass(frozen=True)
class CaseRescore:
    """A case re-scored: the authoritative result plus what varied around it."""

    case_id: str
    judged: CaseResult
    structured: CaseResult
    sites: tuple[JudgeSite, ...] = ()
    catch_per_repeat: tuple[int, ...] = ()
    valid_per_repeat: tuple[int, ...] = ()
    bar: float = DEFAULT_BAR
    bar_source: str = "default"  # flag | summary | default
    fingerprint: str = ""
    identity: str = "unverifiable"  # verified | unverifiable | changed
    drift: dict = field(default_factory=dict)

    @property
    def judged_sites(self) -> int:
        return sum(1 for s in self.sites if s.judged)

    @property
    def measured_sites(self) -> int:
        return sum(1 for s in self.sites if s.measured)

    @property
    def flipped_sites(self) -> int:
        return sum(1 for s in self.sites if s.judged and s.flipped)

    @property
    def unmeasured_sites(self) -> int:
        return sum(1 for s in self.sites if s.judged and not s.measured)

    @property
    def errored_sites(self) -> int:
        """Judged sites where **any** repeat failed to answer.

        Tracked apart from ``unmeasured_sites`` because a site can both flip and
        error: the flip keeps it in the stability denominator (a disagreement
        observed is real), which would otherwise make the failed repeat vanish
        from every aggregate and leave it findable only by reading the JSON.
        """
        return sum(1 for s in self.sites if s.judged and s.errored)


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


def _validate_finding(where: str, finding: object) -> None:
    """Reject a stored finding the scorer would crash on or mis-score.

    Only the fields scoring reads are checked, and each for the reason it is
    read: ``rationale`` / ``files`` are concatenated into the structured
    matcher's haystack (a non-string raises), ``severity`` is looked up in an
    ordered table (an unknown value raises ``KeyError``), and ``finding_id`` is
    what the judge answers with — a non-string id compares unequal to its own
    stringified form and would silently score every verdict as a veto.
    """
    if not isinstance(finding, dict):
        raise RescoreError(f"{where} is not an object")
    if not isinstance(finding.get("rationale", ""), str):
        raise RescoreError(f"{where}.rationale is not a string")
    files = finding.get("files", [])
    if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
        raise RescoreError(f"{where}.files is not a list of strings")
    if not isinstance(finding.get("finding_id", ""), str):
        raise RescoreError(f"{where}.finding_id is not a string")
    severity = finding.get("severity", "minor")
    if not isinstance(severity, str) or severity.lower() not in SEVERITIES:
        raise RescoreError(
            f"{where}.severity must be one of {', '.join(SEVERITIES)} "
            f"(got {severity!r})"
        )


def _validate_report(path: Path, report: dict) -> None:
    """Structurally check one retained report — **before** any judge call.

    Every field ``score_run`` reads is covered, each because of how it is read:
    ``status`` is tested for membership in a frozenset (an unhashable value
    raises ``TypeError``), and ``blocking`` is passed through ``bool()``, which
    never raises but turns the string ``"false"`` into a block — silently
    corrupting the noise instrumentation instead of failing. Both stay
    **optional**: reports predating #310 have no ``blocking`` key at all, and
    refusing them would make the retained corpus unrescorable.
    """
    if "reviewers" not in report:
        raise RescoreError(f"{path}: not a ReviewReport (no 'reviewers' key)")
    reviewers = report["reviewers"]
    if not isinstance(reviewers, list):
        raise RescoreError(f"{path}: not a ReviewReport ('reviewers' is not a list)")
    if "blocking" in report and not isinstance(report["blocking"], bool):
        raise RescoreError(
            f"{path}: blocking is not a boolean (got {report['blocking']!r})"
        )
    for i, reviewer in enumerate(reviewers):
        if not isinstance(reviewer, dict):
            raise RescoreError(f"{path}: reviewers[{i}] is not an object")
        if "status" in reviewer and not isinstance(reviewer["status"], str):
            raise RescoreError(
                f"{path}: reviewers[{i}].status is not a string "
                f"(got {reviewer['status']!r})"
            )
        findings = reviewer.get("findings", [])
        if not isinstance(findings, list):
            raise RescoreError(f"{path}: reviewers[{i}].findings is not a list")
        for j, finding in enumerate(findings):
            _validate_finding(f"{path}: reviewers[{i}].findings[{j}]", finding)


def _is_rate(value: object) -> bool:
    # bool is an int subclass, and `bar = true` would silently score at 1.0.
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0.0 <= value <= 1.0
    )


def _validate_summary(path: Path, summary: dict) -> None:
    """Check the recorded fields a rescore consumes, at load rather than at use.

    ``drift_vs_summary`` runs *after* the whole measurement is paid for, so a
    malformed per-sample array there would throw away the run it was comparing.
    """
    for key, element in _DRIFT_KEYS.items():
        if key not in summary:
            continue
        values = summary[key]
        if not isinstance(values, list):
            raise RescoreError(f"{path}: {key} is not a list")
        # Element types matter to the COMPARISON, not just to reading it:
        # `[1] == [True]` in Python, so a recorded int array would compare equal
        # to a rescored boolean one and report no drift where the corpus differs.
        if any(not isinstance(v, element) for v in values):
            raise RescoreError(
                f"{path}: {key} must be a list of {element.__name__} (got {values!r})"
            )
    fingerprint = summary.get("expected_fingerprint")
    if fingerprint is not None and not isinstance(fingerprint, str):
        raise RescoreError(f"{path}: expected_fingerprint is not a string")
    bar = summary.get("bar")
    if bar is not None and not _is_rate(bar):
        raise RescoreError(
            f"{path}: bar must be a finite rate between 0 and 1 (got {bar!r})"
        )


def _load_case_reports(case_dir: Path) -> CaseReports:
    by_variant: dict[str, list[SampleReport]] = {v: [] for v in _VARIANTS}
    summary: dict | None = None
    summary_path: Path | None = None
    for path in sorted(case_dir.iterdir()):
        if path.is_dir():
            continue  # `judge/` sidecars (#307) — not scoring inputs
        if path.name in _ALLOWED_FILES:
            if path.name == "summary.json":
                summary = _read_json(path)
                _validate_summary(path, summary)
                summary_path = path
            continue
        parsed = _parse_name(path.name)
        if parsed is None:
            raise RescoreError(
                f"{case_dir.name}: unexpected file {path.name!r} — expected "
                "summary.json, rescore.json, or <variant>-<i>.json"
            )
        variant, index = parsed
        report = _read_json(path)
        _validate_report(path, report)
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
        summary_path=summary_path,
        case_dir=case_dir,
    )


def load_report_dir(
    report_dir: Path, *, case_id: str | None = None
) -> list[CaseReports]:
    """Every case in *report_dir*, parsed **and structurally validated** up front.

    Loading the whole corpus before returning is the point: a malformed report
    in the last case must not surface after the first case has already been
    judged and paid for.
    """
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
    """How many **verdict requests** a rescore will make — known before paying.

    Exact as a count of questions asked: the reports are already parsed, and the
    judge short-circuits a sample with no findings without calling an agent, so
    samples that produced nothing cost nothing here either. It is *not* the
    agent-invocation count — a failed call retries once, so the ceiling is
    ``judge.MAX_ATTEMPTS_PER_REQUEST`` times this.
    """
    total = 0
    for case_reports in reports:
        n_expected = len(cases[case_reports.case_id].expected)
        for sample in case_reports.samples():
            if produced_findings(sample.report):
                total += n_expected * repeats
    return total


def resolve_bar(summary: Mapping | None, override: float | None) -> tuple[float, str]:
    """The bar to score at, and where it came from — flag, run, or default.

    Silently re-scoring at the module default a run that was scored at ``--bar
    0.6`` would report ``REGRESSED`` for a case whose numbers never moved: the
    rescore would be measuring its own flag rather than the judge.
    """
    if override is not None:
        return override, "flag"
    if summary is not None and _is_rate(summary.get("bar")):
        return float(summary["bar"]), "summary"
    return DEFAULT_BAR, "default"


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

    if judge is None:
        structured = universe(None)
        return CaseRescore(
            case_id=case.id,
            judged=structured,
            structured=structured,
            bar=bar,
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
                    verdicts=tuple(verdicts),
                )
            )

    universes = [universe(r) for r in range(repeats)]
    # Counted over each universe's OWN valid denominator: a repeat where the
    # judge errored has fewer scorable samples, and holding K fixed would read
    # that as a drop in catches (#315 review).
    tallies = [
        count_valid(u.caught_per_sample, u.excluded_per_sample) for u in universes
    ]

    return CaseRescore(
        case_id=case.id,
        # repeat 0 is authoritative — measure, don't re-estimate
        judged=universes[0],
        structured=universe(None),
        sites=tuple(sites),
        catch_per_repeat=tuple(caught for caught, _ in tallies),
        valid_per_repeat=tuple(valid for _, valid in tallies),
        bar=bar,
        fingerprint=expected_fingerprint(case),
    )


# Per-sample arrays a rescore can compare against what the run recorded, with
# the element type each one must hold. Both error arms are here deliberately:
# an original ok-veto and a rescore timeout both leave `caught` False, so
# comparing verdicts alone would call a case unchanged while its valid
# denominator silently dropped to zero.
_DRIFT_KEYS: dict[str, type] = {
    "caught_per_sample": bool,
    "severity_per_sample": bool,
    "false_positive_per_sample": bool,
    "errored_per_sample": bool,
    "false_positive_errored_per_sample": bool,
    "judge_status_per_sample": str,
    "false_positive_judge_status_per_sample": str,
}

_JUDGE_DRIFT_KEYS = frozenset(
    {"judge_status_per_sample", "false_positive_judge_status_per_sample"}
)

# Distinct from any recorded value, so a sample present on one side only always
# compares unequal instead of matching a falsy neighbour.
_ABSENT = object()


def _at(values: Sequence, i: int) -> object:
    return values[i] if i < len(values) else _ABSENT


def drift_vs_summary(
    judged: CaseResult, summary: dict | None, *, compare_judge_status: bool = True
) -> dict:
    """Per-key comparison against what the run recorded, if anything.

    A key the recorded summary never had reports ``None`` rather than a false
    match: report dirs predate most of these fields, and "absent" must not read
    as "agreed". The judge-status arms report ``None`` under ``--no-judge`` for
    the same reason — a structured-only rescore has no judge answer to compare,
    and flagging every case would bury the comparisons that do mean something.

    Sample counts are compared too: a report dir that gained or lost a trailing
    sample is a different corpus, and a positional zip would skip the difference
    entirely.
    """
    if summary is None:
        return {}
    out: dict = {}
    for key in _DRIFT_KEYS:
        skip_judge = key in _JUDGE_DRIFT_KEYS and not compare_judge_status
        if key not in summary or skip_judge:
            out[key] = None
            continue
        recorded = list(summary[key])
        now = list(getattr(judged, key))
        flipped = [
            i
            for i in range(max(len(recorded), len(now)))
            if _at(recorded, i) != _at(now, i)
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
