"""S5a — triage external claims before a coder acts on them (PRD S2/S5a).

A trusted bot can be confidently wrong, and an injected finding drives a
coder that then pushes. Triage is a **separate cheap step**, not a sceptical
clause in the fixer prompt — a fixer handed a job is pulled toward doing it,
so decide-if-real and fix are different agents. One read-only container turn
checks every claim in a batch (each is a *closed* question: a named file,
line and mechanism) and writes per-id verdicts.

The load-bearing rule is **default-to-act**: a claim is dropped only by an
explicit ``REJECT`` carrying cited evidence. A bare reject, an unmentioned
id, a garbled line, a missing verdict file, or a failed turn all PROCEED —
actioning a false positive is recoverable (the loop's own panel + check-set
still gate the result, and the push is append-only), while suppressing a
true positive is the failure this whole arc exists to fix. The lens34
over-suppression result is the risk to watch; the evidence requirement is
its guard.
"""

from __future__ import annotations

import logging
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ...runner import worktree
from . import containers, engines, handoff, turns
from .agent_session import build_run_cmd
from .config import HANDOFF_MOUNT_NAME, DevelopConfig
from .sandbox_facts import for_prompt as _sandbox_section

if TYPE_CHECKING:
    from .panel import ReviewOutcome
    from .review_resolve import ResolvedChange

logger = logging.getLogger(__name__)

__all__ = [
    "TRIAGE_HANDOFF_NAME",
    "TriageVerdicts",
    "parse_triage_verdicts",
    "triage_external_findings",
]

# The verdict file the triage agent writes into the handoff mount.
TRIAGE_HANDOFF_NAME = "round_00_triage.md"

# One verdict per LINE: `- f-001: PROCEED` or `- f-001: REJECT — <evidence>`.
# Anchored ^..$ with MULTILINE and no newline-crossing whitespace — an
# unanchored `\s*` would let one verdict's optional evidence group swallow the
# next line entirely (a PROCEED line eating the REJECT after it).
_VERDICT_RE = re.compile(
    r"^[ \t]*-[ \t]*(?P<fid>f-\d+)[ \t]*:[ \t]*(?P<verdict>PROCEED|REJECT)"
    r"[ \t]*(?:[—–-]+[ \t]*(?P<evidence>.*\S))?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


# What counts as CITED evidence (PR #345 reviews F2 + re-reviews 2 + 3): the
# rejection must name a checkable source location — a ``file:line`` token
# whose file part contains a letter (``src/util.py:42``, ``Makefile:12``) —
# and, when a tracked-file snapshot is supplied, at least one cited path
# must RESOLVE to a real file in the repo at the reviewed commit. That
# referent check is what keeps citation-shaped prose (``HTTP:404``,
# ``timeout:30``, ``RFC:7231``) from counting; a dotted token alone
# (``v1.2``, a bare ``README.md``) or an all-digit ``12:30`` never matches
# the shape at all. The rule lives in the parser, not just the prompt, so a
# vague model rejection can never silently discard a true defect.
#
# The validation boundary is deliberate and ENDS at the referent: no
# line-existence check, no content check. A model can trivially cite a valid
# line of a real file and still be wrong about what it does — no shape or
# referent test can tell a true claim from a false one, so tightening past
# this point is an unbounded chase. Semantic triage quality (known-false
# rejected / known-true proceeds) is measured by the S8 eval fixtures.
_CITATION_RE = re.compile(r"(?=[\w./-]*[A-Za-z])(?P<path>[\w./-]+):\d+")


def _tracked_files(wt: Path) -> frozenset[str]:
    """Snapshot the tracked paths at the reviewed commit, for the citation
    referent check.

    Failure degrades to an **empty set** — every rejection then lacks a
    resolving citation and PROCEEDS. That is the module's default-to-act
    direction (over-acting is recoverable; suppression is not), at the cost
    of a git hiccup turning that run's rejections into proceeds.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(wt), "ls-files", "-z"],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
    except Exception:  # noqa: BLE001 — degrade to act, never raise
        logger.warning("triage: could not snapshot tracked files in %s", wt)
        return frozenset()
    return frozenset(p for p in out.stdout.split("\0") if p)


def _resolves_in_repo(evidence: str, repo_files: frozenset[str]) -> bool:
    """True when any cited ``file:line`` path names a tracked file.

    The triage agent reads the tree at ``/workspace``, so container-rooted
    and ``./``-relative spellings normalise to the repo-relative path.
    """
    for match in _CITATION_RE.finditer(evidence):
        path = match.group("path").removeprefix("./").lstrip("/")
        if path.removeprefix("workspace/") in repo_files:
            return True
    return False


@dataclass(frozen=True)
class TriageVerdicts:
    """Parsed per-finding verdicts (also the container step's result shape)."""

    proceed: tuple[str, ...]
    rejections: dict[str, str] = field(default_factory=dict)
    cost_usd: float = 0.0
    note: str = ""  # non-empty when triage degraded and defaulted to act


def parse_triage_verdicts(
    text: str,
    finding_ids: list[str],
    *,
    repo_files: frozenset[str] | None = None,
) -> TriageVerdicts:
    """Parse the verdict file, applying default-to-act per finding.

    A finding is rejected only by an explicit ``REJECT`` line whose evidence
    cites a ``file:line`` location (``_CITATION_RE``) — and, when
    *repo_files* is supplied (the production step always passes the
    worktree's tracked-file snapshot), one that resolves to a real tracked
    file. Everything else — PROCEED, bare REJECT, uncited prose,
    citation-shaped prose naming no repo file, unmentioned, garbled —
    proceeds. Ids the output invents are ignored. ``repo_files=None`` is the
    pure/unit-test mode: shape-only, no filesystem coupling.
    """
    known = set(finding_ids)
    rejections: dict[str, str] = {}
    for match in _VERDICT_RE.finditer(text):
        fid = match.group("fid")
        if fid not in known:
            continue
        evidence = (match.group("evidence") or "").strip()
        cited = bool(evidence) and _CITATION_RE.search(evidence) is not None
        if cited and repo_files is not None:
            cited = _resolves_in_repo(evidence, repo_files)
        if match.group("verdict").upper() == "REJECT" and cited:
            rejections[fid] = evidence
    proceed = tuple(fid for fid in finding_ids if fid not in rejections)
    return TriageVerdicts(proceed=proceed, rejections=rejections)


def triage_external_findings(
    config: DevelopConfig,
    change: ResolvedChange,
    outcome: ReviewOutcome,
    *,
    timeout: int = 1800,
) -> TriageVerdicts:
    """Run the one-turn read-only triage pass over *outcome*'s findings.

    Builds a throwaway worktree at the PR head (the tree the claims are
    about), runs a single fresh coder-engine turn against it with the repo
    mounted read-only, and parses the verdict file. Every degraded path —
    turn failure, missing/unreadable verdict file — returns all-proceed with
    a ``note`` explaining the degradation; this function never raises for
    those, and never blocks the remediation it guards.
    """
    finding_ids = [f.finding_id for f in outcome.findings]

    config.worktree_parent.mkdir(parents=True, exist_ok=True)
    config.coder_config_dir.mkdir(parents=True, exist_ok=True)
    handoff.seed_handoff_dir(config.handoff_dir)
    wt = worktree.create_at(
        config.repo, change.head_sha, config.description, parent=config.worktree_parent
    )
    # Read-only container: the handoff bind-mountpoint must pre-exist in the
    # worktree (docker cannot create it inside an RO /workspace) — the same
    # rule review-only applies for its RO reviewers.
    (wt / HANDOFF_MOUNT_NAME).mkdir(parents=True, exist_ok=True)
    # Snapshot now — the worktree is torn down in the finally below, before
    # the verdict file is parsed, and the RO mount means the set can't change.
    repo_files = _tracked_files(wt)

    prompt = handoff.render_prompt(
        handoff.load_prompt("external_triage.md"),
        acceptance_criteria=config.effective_acceptance_criteria,
        findings=handoff.render_findings(outcome.findings),
        handoff_file=TRIAGE_HANDOFF_NAME,
        sandbox_facts=_sandbox_section(config.image, for_coder=False),
    )
    engine = engines.get_engine(config.coder)
    name, run_cmd = build_run_cmd(
        config,
        agent="triage",
        engine=engine,
        config_dir=config.coder_config_dir,
        wt=wt,
        read_only=True,
    )
    try:
        containers.start_container(run_cmd)
        turn = turns.run_turn(
            container=name,
            prompt=prompt,
            engine=engine,
            session_id=str(uuid.uuid4()),
            resume=False,
            timeout=timeout,
            model=config.coder_model,
            effort=config.coder_effort,
        )
    finally:
        containers.stop_container(name)
        try:
            worktree.remove(wt, force=True)
        except Exception:  # noqa: BLE001 — cleanup only
            logger.warning("triage: failed to remove worktree %s", wt)

    cost = turn.cost_usd or 0.0
    if not turn.succeeded:
        note = "triage turn failed — defaulting to act on every finding"
        logger.warning("triage %s: %s", config.run_id, note)
        return TriageVerdicts(proceed=tuple(finding_ids), cost_usd=cost, note=note)

    verdict_path = config.handoff_dir / TRIAGE_HANDOFF_NAME
    try:
        text = verdict_path.read_text(encoding="utf-8")
    except OSError:
        note = "triage wrote no verdict file — defaulting to act on every finding"
        logger.warning("triage %s: %s", config.run_id, note)
        return TriageVerdicts(proceed=tuple(finding_ids), cost_usd=cost, note=note)

    verdicts = parse_triage_verdicts(text, finding_ids, repo_files=repo_files)
    logger.info(
        "triage %s: %d proceed / %d rejected (cost $%.2f)",
        config.run_id,
        len(verdicts.proceed),
        len(verdicts.rejections),
        cost,
    )
    return TriageVerdicts(
        proceed=verdicts.proceed,
        rejections=verdicts.rejections,
        cost_usd=cost,
    )
