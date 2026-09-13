"""Resolve-eval harness: build the trees, validate the oracle, run S5 K times,
score each run against the oracle, aggregate.

One sample is one ``converge --resolve-conflicts`` run (:func:`converge_pr`
in resolve mode, ``no_push``) on the case's real conflicting merge: the
coder resolves the merge left in progress, the project's check-set and the
case's panel judge the composed tree, and the loop ends approved or not.
The eval then asks the case's **probes** — the executable oracle — two
questions of the run's trees:

- **coder-right** — did the round-1 merge commit (the coder's own resolution,
  before any panel feedback) satisfy every probe?
- **pipeline-right** — did the FINAL tree (after the panel's rounds)?

and reads the panel's verdict beside them. The cell that decides S5's posture
is **unsafe**: approved AND wrong — the merge commit S5 would have pushed
onto the PR branch, carrying a defect the oracle names. A correct
resolution the panel rejected is **wasted** (escalated to a human for
nothing) — a cost, not a hazard. A run with no merge commit (the coder
never got past the markers guard) is *not resolved*, and no probe runs on
it. An infra death (``infra_failed``, or a crash of the harness's own
plumbing) is *errored*: excluded from every denominator, like a crashed
reviewer in ``eval review``.

Fail-closed order: the oracle is validated on the known-good and known-bad
trees BEFORE the first paid sample (every probe passes the one, at least one
fails the other — an oracle that cannot discriminate would score every
resolution alike); a fixture whose merge is clean, unsupported or stale
aborts the case on its first sample (nothing was spent: the intake refuses
before any agent runs). The live pieces — the S5 run and the probe runner —
are injectable so the arithmetic stays hermetic.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ...plugins.story_develop.config import DevelopConfig, ReviewerSpec
from ...plugins.story_develop.converge import converge_pr
from ...plugins.story_develop.panel import findings_by_severity
from ...plugins.story_develop.review_resolve import ResolvedChange
from ...runner import git, worktree
from ..review.patch import materialise_patched_head
from ..review.stats import wilson_interval
from .case import Probe, ResolveCase

__all__ = [
    "DEFAULT_BAR",
    "DEFAULT_K",
    "DEFAULT_PROBE_TIMEOUT",
    "DEFAULT_TIMEOUT",
    "FIXTURE_STATUSES",
    "FixtureError",
    "OracleError",
    "ProbeResult",
    "ProbeRunner",
    "ResolveCaseResult",
    "ResolveFn",
    "ResolveOutcome",
    "ResolveSink",
    "SampleScore",
    "Trees",
    "aggregate_resolve",
    "expected_fingerprint",
    "live_resolve",
    "materialise_trees",
    "run_probe",
    "run_resolve_case",
    "score_sample",
]

DEFAULT_K = 5
DEFAULT_BAR = 0.8
# converge's coder / reviewer timeouts — the production run's bounds.
DEFAULT_TIMEOUT = 3600
DEFAULT_PROBE_TIMEOUT = 600
# A run that ended before any agent spent: the fixture, not the path, is
# what failed — the case aborts rather than paying K times for nothing.
FIXTURE_STATUSES = frozenset({"no_conflict", "conflict_unsupported", "base_moved"})
# A run that died under the harness or the host: excluded from every rate.
_ERRORED_STATUSES = frozenset({"infra_failed", "error"})
_OUTPUT_TAIL = 4000


class OracleError(ValueError):
    """The case's probes do not discriminate its own controls."""


class FixtureError(RuntimeError):
    """The case's merge is not one the S5 path can be measured on."""


@dataclass(frozen=True)
class Trees:
    """The three materialised commits a case needs: the PR head to resolve,
    and the two oracle controls."""

    head: str
    known_good: str
    known_bad: str

    def payload(self) -> dict:
        return {
            "head": self.head,
            "known_good": self.known_good,
            "known_bad": self.known_bad,
        }


@dataclass(frozen=True)
class ProbeResult:
    name: str
    passed: bool
    exit_code: int | None
    output: str
    error: str = ""

    def payload(self) -> dict:
        return {
            "passed": self.passed,
            "exit_code": self.exit_code,
            "output": self.output,
            "error": self.error,
        }


@dataclass(frozen=True)
class ResolveOutcome:
    """What one S5 run produced — the harness's view of a
    :class:`~..converge.ConvergeResult`, plus the trees to probe.

    ``merge_sha`` is the round-1 merge commit (empty when the coder never
    committed one); ``final_sha`` the tree the run ended on. ``cleanup``
    releases the run's worktree + branch — called by the harness AFTER the
    probes and the sink, never before (the probes need the commits
    reachable). ``retained`` is per-run material for the report dir (the
    merge commit's combined diff, the final diff, the conversation log).
    """

    status: str
    message: str
    rounds: int
    cost_usd: float
    conflict_paths: tuple[str, ...]
    merge_sha: str
    final_sha: str
    gate_green: bool
    findings_by_severity: Mapping[str, int]
    retained: Mapping[str, str] = field(default_factory=dict)
    cleanup: Callable[[], None] = lambda: None


# (case, trees) → one S5 run's outcome.
ResolveFn = Callable[[ResolveCase, Trees], ResolveOutcome]
# (case, sha, probe) → whether the probe holds on the tree at sha.
ProbeRunner = Callable[[ResolveCase, str, Probe], ProbeResult]
# (case_id, sample index, payload) — one call per sample, for retention.
ResolveSink = Callable[[str, int, dict], None]


def expected_fingerprint(case: ResolveCase) -> str:
    """A stable hash of what the SCORER consumes: the case id and its probes.

    Recorded in ``summary.json`` so two report dirs can refuse a comparison
    across a reworded or added probe (cf. ``eval review``, #307). The trees
    are out — they change what the CODER saw, which no re-score revisits.
    """
    payload = {
        "id": case.id,
        "probes": [{"name": p.name, "command": p.command} for p in case.probes],
    }
    raw = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# --- trees ------------------------------------------------------------------


def materialise_trees(case: ResolveCase) -> tuple[Trees, Callable[[], None]]:
    """``(trees, cleanup)`` — each tree is its sha, or ``anchor + patch`` as an
    ephemeral commit (the head on ``merge_base``, the controls on ``base``);
    the build worktrees keep the commits reachable until ``cleanup``. Called
    once per case so K samples share the trees."""
    repo = Path(case.repo).resolve()
    parent = Path(tempfile.mkdtemp(prefix="loom-eval-resolve-trees-"))
    worktrees: list[Path] = []

    def build(sha: str, patch: str | None, anchor: str) -> str:
        if sha:
            return sha
        assert patch is not None and case.case_dir is not None
        # Absolute: `git apply` runs with cwd=build-worktree (review/patch.py).
        patch_path = (case.case_dir / patch).resolve()
        built, wt = materialise_patched_head(repo, anchor, patch_path, parent=parent)
        worktrees.append(wt)
        return built

    def cleanup() -> None:
        try:
            for wt in worktrees:
                with contextlib.suppress(Exception):  # cleanup is best-effort
                    worktree.remove(wt, force=True)
        finally:
            shutil.rmtree(parent, ignore_errors=True)

    try:
        trees = Trees(
            head=build(case.head, case.head_patch, case.merge_base),
            known_good=build(case.known_good, case.known_good_patch, case.base),
            known_bad=build(case.known_bad, case.known_bad_patch, case.base),
        )
    except Exception:
        cleanup()
        raise
    return trees, cleanup


# --- scoring ----------------------------------------------------------------


@dataclass(frozen=True)
class SampleScore:
    status: str
    message: str
    errored: bool
    resolved: bool
    gate_green: bool
    approved: bool
    correct_first: bool
    correct_final: bool
    rounds: int
    cost_usd: float
    conflict_paths: tuple[str, ...]
    merge_sha: str
    final_sha: str
    probes_first: Mapping[str, bool]
    probes_final: Mapping[str, bool]
    probe_results_first: tuple[ProbeResult, ...]
    probe_results_final: tuple[ProbeResult, ...]
    findings_by_severity: Mapping[str, int]

    @property
    def unsafe(self) -> bool:
        """Approved AND wrong — the merge S5 would have pushed."""
        return self.approved and not self.correct_final

    @property
    def wasted(self) -> bool:
        """Right AND not approved — escalated to a human for nothing."""
        return self.correct_final and not self.approved

    def payload(self) -> dict:
        return {
            "status": self.status,
            "message": self.message,
            "errored": self.errored,
            "resolved": self.resolved,
            "gate_green": self.gate_green,
            "approved": self.approved,
            "correct_first": self.correct_first,
            "correct_final": self.correct_final,
            "unsafe": self.unsafe,
            "wasted": self.wasted,
            "rounds": self.rounds,
            "cost_usd": self.cost_usd,
            "conflict_paths": list(self.conflict_paths),
            "merge_sha": self.merge_sha,
            "final_sha": self.final_sha,
            "probes_first": dict(self.probes_first),
            "probes_final": dict(self.probes_final),
            "probe_results_first": {
                r.name: r.payload() for r in self.probe_results_first
            },
            "probe_results_final": {
                r.name: r.payload() for r in self.probe_results_final
            },
            "findings_by_severity": dict(self.findings_by_severity),
        }


def _probe_tree(
    case: ResolveCase, sha: str, probe_runner: ProbeRunner
) -> tuple[ProbeResult, ...]:
    return tuple(probe_runner(case, sha, probe) for probe in case.probes)


def _held(results: Sequence[ProbeResult]) -> bool:
    return bool(results) and all(r.passed for r in results)


def score_sample(
    case: ResolveCase, outcome: ResolveOutcome, probe_runner: ProbeRunner
) -> SampleScore:
    """Score one S5 run: the panel's verdict beside the oracle's, on the
    round-1 merge commit and on the final tree. No probe runs on a run that
    produced no merge commit, nor on an errored one."""
    errored = outcome.status in _ERRORED_STATUSES
    resolved = bool(outcome.merge_sha) and not errored
    first: tuple[ProbeResult, ...] = ()
    final: tuple[ProbeResult, ...] = ()
    if resolved:
        first = _probe_tree(case, outcome.merge_sha, probe_runner)
        final = (
            first
            if outcome.final_sha == outcome.merge_sha
            else _probe_tree(case, outcome.final_sha, probe_runner)
        )
    return SampleScore(
        status=outcome.status,
        message=outcome.message,
        errored=errored,
        resolved=resolved,
        gate_green=resolved and outcome.gate_green,
        approved=outcome.status == "converged",
        correct_first=_held(first),
        correct_final=_held(final),
        rounds=outcome.rounds,
        cost_usd=outcome.cost_usd,
        conflict_paths=tuple(outcome.conflict_paths),
        merge_sha=outcome.merge_sha,
        final_sha=outcome.final_sha,
        probes_first={r.name: r.passed for r in first},
        probes_final={r.name: r.passed for r in final},
        probe_results_first=first,
        probe_results_final=final,
        findings_by_severity=dict(outcome.findings_by_severity),
    )


@dataclass(frozen=True)
class ResolveCaseResult:
    """Aggregated metrics for one case over K runs.

    Counts and rates are over the VALID (non-errored) samples; the
    ``*_per_sample`` tuples cover all ``n``. ``passed`` needs valid samples,
    NO unsafe approval, and the pipeline-right rate at the bar — an approved
    wrong merge is the expensive direction, so one fails the case.
    """

    case_id: str
    n: int
    n_valid: int
    resolved: int
    gate_green: int
    approved: int
    correct_first: int
    correct_final: int
    unsafe: int
    wasted: int
    resolved_rate: float
    correct_first_rate: float
    correct_final_rate: float
    correct_final_ci: tuple[float, float]
    approved_rate: float
    unsafe_rate: float
    unsafe_ci: tuple[float, float]
    passed: bool
    status_per_sample: tuple[str, ...]
    message_per_sample: tuple[str, ...]
    errored_per_sample: tuple[bool, ...]
    resolved_per_sample: tuple[bool, ...]
    approved_per_sample: tuple[bool, ...]
    correct_first_per_sample: tuple[bool, ...]
    correct_final_per_sample: tuple[bool, ...]
    unsafe_per_sample: tuple[bool, ...]
    rounds_per_sample: tuple[int, ...]
    cost_usd_per_sample: tuple[float, ...]


def _rate(count: int, n: int) -> float:
    return count / n if n else 0.0


def aggregate_resolve(
    case_id: str, scores: Sequence[SampleScore], *, k: int, bar: float
) -> ResolveCaseResult:
    valid = [s for s in scores if not s.errored]
    n_valid = len(valid)
    resolved = sum(s.resolved for s in valid)
    gate_green = sum(s.gate_green for s in valid)
    approved = sum(s.approved for s in valid)
    first = sum(s.correct_first for s in valid)
    final = sum(s.correct_final for s in valid)
    unsafe = sum(s.unsafe for s in valid)
    wasted = sum(s.wasted for s in valid)
    final_rate = _rate(final, n_valid)
    return ResolveCaseResult(
        case_id=case_id,
        n=k,
        n_valid=n_valid,
        resolved=resolved,
        gate_green=gate_green,
        approved=approved,
        correct_first=first,
        correct_final=final,
        unsafe=unsafe,
        wasted=wasted,
        resolved_rate=_rate(resolved, n_valid),
        correct_first_rate=_rate(first, n_valid),
        correct_final_rate=final_rate,
        correct_final_ci=wilson_interval(final, n_valid) if n_valid else (0.0, 0.0),
        approved_rate=_rate(approved, n_valid),
        unsafe_rate=_rate(unsafe, n_valid),
        unsafe_ci=wilson_interval(unsafe, n_valid) if n_valid else (0.0, 0.0),
        passed=n_valid > 0 and unsafe == 0 and final_rate >= bar,
        status_per_sample=tuple(s.status for s in scores),
        message_per_sample=tuple(s.message for s in scores),
        errored_per_sample=tuple(s.errored for s in scores),
        resolved_per_sample=tuple(s.resolved for s in scores),
        approved_per_sample=tuple(s.approved for s in scores),
        correct_first_per_sample=tuple(s.correct_first for s in scores),
        correct_final_per_sample=tuple(s.correct_final for s in scores),
        unsafe_per_sample=tuple(s.unsafe for s in scores),
        rounds_per_sample=tuple(s.rounds for s in scores),
        cost_usd_per_sample=tuple(s.cost_usd for s in scores),
    )


# --- the run ----------------------------------------------------------------


def _validate_oracle(
    case: ResolveCase, trees: Trees, probe_runner: ProbeRunner
) -> None:
    good = _probe_tree(case, trees.known_good, probe_runner)
    failed = [r.name for r in good if not r.passed]
    if failed:
        raise OracleError(
            f"case {case.id}: probe(s) {failed} fail on the known-good tree "
            f"{trees.known_good[:12]} — the oracle does not hold on the "
            "control it must hold on"
        )
    bad = _probe_tree(case, trees.known_bad, probe_runner)
    if _held(bad):
        raise OracleError(
            f"case {case.id}: every probe passes on the known-bad tree "
            f"{trees.known_bad[:12]} — the oracle cannot tell a wrong resolution "
            "from a right one"
        )


def _errored_score(exc: BaseException) -> SampleScore:
    return SampleScore(
        status="error",
        message=f"{type(exc).__name__}: {exc}",
        errored=True,
        resolved=False,
        gate_green=False,
        approved=False,
        correct_first=False,
        correct_final=False,
        rounds=0,
        cost_usd=0.0,
        conflict_paths=(),
        merge_sha="",
        final_sha="",
        probes_first={},
        probes_final={},
        probe_results_first=(),
        probe_results_final=(),
        findings_by_severity={},
    )


def run_resolve_case(
    case: ResolveCase,
    *,
    k: int = DEFAULT_K,
    bar: float = DEFAULT_BAR,
    resolve_fn: ResolveFn,
    probe_runner: ProbeRunner,
    sink: ResolveSink | None = None,
    materialise: Callable[[ResolveCase], tuple[Trees, Callable[[], None]]] = (
        materialise_trees
    ),
) -> ResolveCaseResult:
    """Materialise the trees once, validate the oracle, run S5 *k* times,
    score each run against the oracle, aggregate.

    Raises :class:`OracleError` before any paid sample when the probes do not
    discriminate the controls, and :class:`FixtureError` on the first sample
    whose intake refused the merge (clean / unsupported / stale — nothing
    spent). A run that raises is one errored sample, not a crashed case.
    """
    trees, cleanup = materialise(case)
    try:
        _validate_oracle(case, trees, probe_runner)
        scores: list[SampleScore] = []
        for i in range(k):
            try:
                outcome = resolve_fn(case, trees)
            except Exception as exc:  # noqa: BLE001 — one errored sample
                score = _errored_score(exc)
                if sink is not None:
                    sink(case.id, i, {"trees": trees.payload(), **score.payload()})
                scores.append(score)
                continue
            try:
                if outcome.status in FIXTURE_STATUSES:
                    raise FixtureError(
                        f"case {case.id}: the intake refused the merge "
                        f"({outcome.status}): {outcome.message}"
                    )
                score = score_sample(case, outcome, probe_runner)
                if sink is not None:
                    sink(
                        case.id,
                        i,
                        {
                            "trees": trees.payload(),
                            **score.payload(),
                            "retained": dict(outcome.retained),
                        },
                    )
                scores.append(score)
            finally:
                outcome.cleanup()
    finally:
        cleanup()
    return aggregate_resolve(case.id, scores, k=k, bar=bar)


# --- live pieces (host-only: docker + the agent CLIs + the project's toolchain) --


def run_probe(
    case: ResolveCase,
    sha: str,
    probe: Probe,
    *,
    timeout: int = DEFAULT_PROBE_TIMEOUT,
) -> ProbeResult:
    """Run one probe on a detached worktree at *sha*: exit 0 = holds.

    The command is the case's (repo-controlled data, run as an argv — no
    shell), with ``{case_dir}`` / ``{worktree}`` rendered shell-quoted. A
    timeout or an unlaunchable command is a failed probe with ``error`` set,
    never an exception — the sample records it and scores as not holding.
    """
    assert case.case_dir is not None
    repo = Path(case.repo).resolve()
    parent = Path(tempfile.mkdtemp(prefix="loom-eval-resolve-probe-"))
    try:
        wt = worktree.create_at(repo, sha, f"probe-{probe.name}", parent=parent)
        try:
            argv = shlex.split(
                probe.render(case_dir=case.case_dir.resolve(), worktree=wt)
            )
            try:
                proc = subprocess.run(
                    argv, cwd=wt, capture_output=True, text=True, timeout=timeout
                )
            except subprocess.TimeoutExpired:
                return ProbeResult(
                    name=probe.name,
                    passed=False,
                    exit_code=None,
                    output="",
                    error=f"timed out after {timeout}s",
                )
            except OSError as exc:
                return ProbeResult(
                    name=probe.name,
                    passed=False,
                    exit_code=None,
                    output="",
                    error=str(exc),
                )
            output = (proc.stdout + proc.stderr)[-_OUTPUT_TAIL:]
            return ProbeResult(
                name=probe.name,
                passed=proc.returncode == 0,
                exit_code=proc.returncode,
                output=output,
            )
        finally:
            with contextlib.suppress(Exception):  # cleanup is best-effort
                worktree.remove(wt, force=True)
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def _git_text(wt: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=wt, capture_output=True, text=True)
    return (
        proc.stdout
        if proc.returncode == 0
        else f"(git {' '.join(args)} failed: {proc.stderr.strip()})"
    )


def live_resolve(
    case: ResolveCase,
    trees: Trees,
    *,
    tool: str,
    model: str,
    effort: str | None,
    reviewers: tuple[ReviewerSpec, ...],
    profile: str,
    max_rounds: int,
    coder_timeout: int = DEFAULT_TIMEOUT,
    reviewer_timeout: int = DEFAULT_TIMEOUT,
    default_models: dict[str, str] | None = None,
) -> ResolveOutcome:
    """Run the production S5 path once on the case's merge.

    Host-only. The same recipe as ``converge --resolve-conflicts`` with
    ``--no-push``: :func:`converge_pr` in resolve mode on a change whose
    ``base_ref`` is the case's base tip and whose ``base_sha`` is the PR's
    merge-base (so the fork point moves to the base tip once the merge commit
    exists, as in production). The run's worktree + branch stay until the
    outcome's ``cleanup`` — the harness probes them first.
    """
    repo = Path(case.repo).resolve()
    work_dir = Path(tempfile.mkdtemp(prefix="loom-eval-resolve-"))
    extra: dict = {"image": case.image} if case.image else {}
    config = DevelopConfig(
        repo=repo,
        description=case.title,
        work_dir=work_dir,
        acceptance_criteria=case.acceptance_criteria,
        review_profile=profile,
        reviewers=reviewers,
        coder=tool,
        coder_model=model,
        coder_effort=effort,
        max_rounds=max_rounds,
        default_models=default_models or {},
        **extra,
    )
    change = ResolvedChange(
        base_sha=case.merge_base,
        head_sha=trees.head,
        head_ref=f"{case.id}@{trees.head[:12]}",
        base_ref=case.base,
        title=case.title,
        body=case.acceptance_criteria,
        # converge requires a pushable branch name; --no-push never uses it
        head_branch=f"eval-resolve/{case.id}",
    )
    run_wt: Path | None = None
    branch = ""

    def cleanup() -> None:
        try:
            if run_wt is not None:
                with contextlib.suppress(Exception):  # cleanup is best-effort
                    worktree.remove(run_wt, force=True)
            if branch:
                with contextlib.suppress(Exception):
                    git.delete_branch(repo, branch)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    try:
        result = converge_pr(
            config,
            change,
            no_push=True,
            resolve_conflicts=True,
            coder_timeout=coder_timeout,
            reviewer_timeout=reviewer_timeout,
        )
    except Exception:
        cleanup()
        raise
    dr = result.develop_result
    merge_sha = ""
    final_sha = ""
    gate_green = False
    retained: dict[str, str] = {}
    severities: dict[str, int] = {}
    if dr is not None:
        run_wt = dr.worktree
        branch = dr.branch
        final_sha = git.commit_sha(run_wt)
        # the merge commit is the first first-parent commit past the PR head
        # that contains the base — the round-1 resolution
        for sha in result.fixer_commits:
            if git.is_ancestor(run_wt, case.base, sha):
                merge_sha = sha
                break
        gate_green = bool(dr.test_gate is not None and dr.test_gate.passed)
        severities = findings_by_severity(dr.reviews)
        if merge_sha:
            # HOW the coder resolved (the merge's combined diff), and what the
            # final tree puts on the base; without a merge commit the final
            # tree is the unmerged head and neither diff says anything
            retained["merge.diff"] = _git_text(run_wt, "show", "--cc", merge_sha)
            retained["final.diff"] = _git_text(run_wt, "diff", case.base, final_sha)
        if dr.conversation_log is not None and dr.conversation_log.is_file():
            retained["conversation.md"] = dr.conversation_log.read_text(
                encoding="utf-8", errors="replace"
            )
    conflict = result.conflict
    return ResolveOutcome(
        status=result.status,
        message=result.message,
        rounds=dr.rounds if dr is not None else 0,
        cost_usd=result.total_cost_usd,
        conflict_paths=tuple(conflict.paths) if conflict is not None else (),
        merge_sha=merge_sha,
        final_sha=final_sha,
        gate_green=gate_green,
        findings_by_severity=severities,
        retained=retained,
        cleanup=cleanup,
    )
