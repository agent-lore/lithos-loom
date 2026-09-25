"""Re-entering a develop run that died mid-loop, on its own branch (5dbeb0c8 slice C).

An infra death (a revoked OAuth token, a vanished coder container) leaves every
round it paid for committed on a local branch in the run's worktree, and
:mod:`.checkpoint` records where that branch stood at the last round boundary.
This module turns that into the loop's existing "enter on an existing branch"
shape — the :class:`~.loop_entry.LoopEntry` converge has used since ADR 0003 §9
— so the re-dispatch the operator's gate tick asks for continues the work
instead of starting over:

- a fresh committable worktree positioned at the dead run's HEAD, so the new
  run's commits land on top of the old rounds (a NEW branch: the dead worktree
  still has the old one checked out, and git allows one checkout per branch);
- the dead run's LAST reviewer handoffs as the intake, which is the same
  cold-start the converge entry proves works — the agent sessions are gone
  (that is what an expired token or a dead container means) but the branch and
  the handoffs are the durable truth;
- the REMAINDER of the branch's round and cost budgets, never a fresh one: the
  whole point of resuming is that a long run's spend is not paid twice.

The decision to resume at all belongs to the caller — the route-runner reads it
off the story's failed-attempt marker (only the host-verdict reasons in
:data:`~.checkpoint.RESUMABLE_ESCALATION_REASONS`), and
``lithos-loom develop resume`` is the operator saying it directly.
:func:`prepare_resume` is the shared refusal surface: it answers with a
:class:`Resumption` or with the one sentence saying why this run cannot be
continued, and a caller that is refused runs (or reports) as it would have
before.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from ...runner import git, worktree
from . import handoff
from .checkpoint import RoundCheckpoint, resumable_checkpoint
from .config import DevelopConfig
from .handoff import HandoffError
from .loop_entry import LoopEntry
from .panel import ReviewOutcome
from .run_outcome import write_state

logger = logging.getLogger(__name__)

__all__ = [
    "RESUMED_FROM_KEY",
    "ResumePlan",
    "Resumption",
    "prepare_resume",
    "record_resumed_from",
]

# ``state.json``'s provenance block on the RESUMED run: which run's branch it
# picked up, and what that branch had already spent. The operator's surfaces
# read the run's own numbers; this is what says they are not the whole story.
RESUMED_FROM_KEY = "resumed_from"

# The reviewer handoffs of the run being resumed: round_NN_review_<name>.md
# (``handoff.reviewer_handoff_name``). The coder's own handoff is not intake —
# the new coder reconstructs what it did from the branch and the findings.
_REVIEW_HANDOFF_RE = re.compile(r"^round_(\d+)_review_(.+)\.md$")

# The handoff dir is bind-mounted RW into every agent container, so both the
# names in it and their contents are the agents' to choose, and the live loop
# never enumerates it — it builds the filenames it expects from the configured
# panel. The intake is the one reader that must cope with a dir it did not
# write, so it bounds what it takes from one (security/f-002, f-003):
#
# * a reviewer name must be a plain token — it is rendered to the model AS a
#   reviewer's identity and as the qualifying prefix on every finding id;
# * the run's CONFIGURED panel is preferred over whatever is on disk, so a
#   planted file cannot displace the real review (or, by making an unreviewed
#   round look reviewed, suppress the last real one);
# * at most this many reviewers are read per round, and at most this much
#   finding text is carried into the prompt — ``read_handoff`` bounds each file
#   at 1 MiB, which without a COUNT bound is 1 MiB × N for an attacker-chosen N,
#   the same OOM/billing exposure that per-file cap exists to close. The
#   remainder is elided in the rendered text, as ``check_artifacts`` does.
_REVIEWER_TOKEN_RE = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
MAX_INTAKE_REVIEWERS = 6  # the largest canonical panel is 5
MAX_INTAKE_FINDING_CHARS = 20_000

# The prompt the resumed run's round 1 renders instead of ``coder_init.md``:
# the work is already on the branch and the coder must continue it, which is
# neither a cold start (``coder_init.md``) nor picking up a stranger's pull
# request (``converge_coder_init.md``).
RESUME_CODER_INIT = "resume_coder_init.md"


@dataclass(frozen=True)
class ResumePlan:
    """What is being continued: the dead run, its checkpoint, its last review."""

    prior_run_dir: Path
    checkpoint: RoundCheckpoint
    intake_round: int  # the round whose reviewer handoffs seed the new coder
    intake_reviews: list[ReviewOutcome]


@dataclass(frozen=True)
class Resumption:
    """A resume ready to run: the remainder-budget config, the loop entry, the plan."""

    config: DevelopConfig
    entry: LoopEntry
    plan: ResumePlan
    note: str  # one operator line naming what this run continues


def _elision(count: int, what: str) -> handoff.Finding:
    """One finding standing in for what the intake bound left out."""
    return handoff.Finding(
        finding_id="(elided)",
        severity="minor",
        status="accepted",
        rationale=(
            f"{count} further {what} from this round were left out to bound the "
            "resumed run's intake; read the branch and the run's handoff dir for "
            "the full record."
        ),
    )


def _bounded(outcomes: list[ReviewOutcome], dropped_files: int) -> list[ReviewOutcome]:
    """Trim *outcomes* to the intake's text budget, naming what was left out.

    Findings are kept in order until :data:`MAX_INTAKE_FINDING_CHARS` of text
    has been carried; the rest — and any reviewer file the per-round cap
    dropped — become one ``(elided)`` finding on the last outcome, so the coder
    is told the intake is partial instead of silently shown less.
    """
    kept: list[ReviewOutcome] = []
    budget = MAX_INTAKE_FINDING_CHARS
    dropped_findings = 0
    for outcome in outcomes:
        findings: list[handoff.Finding] = []
        for finding in outcome.findings:
            size = len(finding.rationale) + len(finding.coder_response)
            if budget - size < 0:
                dropped_findings += 1
                continue
            budget -= size
            findings.append(finding)
        kept.append(dataclasses.replace(outcome, findings=findings))
    if dropped_findings:
        kept[-1].findings.append(_elision(dropped_findings, "finding(s)"))
    if dropped_files:
        kept[-1].findings.append(_elision(dropped_files, "reviewer handoff(s)"))
    return kept


def _intake_reviews(
    prior_run_dir: Path, checkpoint: RoundCheckpoint, panel: Sequence[str]
) -> tuple[list[ReviewOutcome], int]:
    """The dead run's last reviewer verdicts, as loop-entry intake.

    The newest round at or below the checkpoint's whose reviewer handoffs
    actually PARSE — a run that died in its coder turn has no review for the
    round it was in, and the round before it is the live state of the dialogue.
    Each handoff is read through the same bounded, adversarial-input reader
    every other consumer uses; a round whose handoffs are all unreadable falls
    back to the round before it rather than failing the resume (the branch is
    the work; a handoff is a breadcrumb).

    *panel* is the run's configured reviewer names, and they are what is read
    when the round has them: the files are the dead agents' to name, so
    discovery is the fallback, not the rule (see the module's bound constants).

    The findings ride along as the round-1 coder prompt's input only. The new
    panel re-reviews the branch from scratch and mints its OWN finding ids, so
    these ids are history the coder is shown, not a ledger it is held to —
    exactly as with a converge intake.
    """
    handoff_dir = prior_run_dir / "handoff"
    try:
        names = sorted(p.name for p in handoff_dir.iterdir())
    except OSError:
        names = []
    configured = set(panel)
    by_round: dict[int, list[tuple[str, str]]] = {}
    for name in names:
        match = _REVIEW_HANDOFF_RE.match(name)
        if match is None:
            continue
        round_no = int(match.group(1))
        if round_no > checkpoint.round:
            continue  # a handoff past the last boundary we can vouch for
        reviewer = match.group(2)
        if not _REVIEWER_TOKEN_RE.match(reviewer):
            logger.warning(
                "resume: ignoring handoff %s — %r is not a reviewer name",
                handoff_dir / name,
                reviewer[:80],
            )
            continue
        by_round.setdefault(round_no, []).append((reviewer, name))
    # The CONFIGURED panel decides which ROUND is the intake as well as which
    # files are read within it (security/f-003): a coder that plants a clean
    # review for the round it died in would otherwise make that round the
    # intake and drop the last genuinely reviewed round — findings and all —
    # telling the resumed coder there is nothing to fix. Discovery is the
    # fallback for a run whose panel has since been reconfigured, not the rule.
    ordered = sorted(by_round, reverse=True)
    by_panel = [r for r in ordered if any(n in configured for n, _ in by_round[r])]
    for intake_round, panel_only in [(r, True) for r in by_panel] + [
        (r, False) for r in ordered
    ]:
        entries = sorted(by_round[intake_round])
        candidates = (
            [e for e in entries if e[0] in configured] if panel_only else entries
        )
        chosen = candidates[:MAX_INTAKE_REVIEWERS]
        dropped = len(candidates) - len(chosen)
        outcomes: list[ReviewOutcome] = []
        for reviewer, name in chosen:
            try:
                parsed = handoff.parse_review_handoff(
                    handoff.read_handoff(handoff_dir / name)
                )
            except (HandoffError, OSError) as exc:
                logger.warning(
                    "resume: skipping unreadable handoff %s (%s)",
                    handoff_dir / name,
                    exc,
                )
                continue
            open_findings = [f for f in parsed.findings if f.is_open]
            outcomes.append(
                ReviewOutcome(
                    reviewer=reviewer,
                    status=parsed.status,
                    passed=not open_findings,
                    max_severity=handoff.max_severity(
                        [f.severity for f in open_findings]
                    ),
                    findings=list(parsed.findings),
                )
            )
        if outcomes:
            return _bounded(outcomes, dropped), intake_round
    # Nothing was reviewed (or nothing survived parsing) before the death. ONE
    # empty outcome, rather than none, so the round-1 prompt's findings slot
    # renders the "no structured findings" line instead of a blank.
    return [
        ReviewOutcome(
            reviewer="(no review recorded)",
            status="LGTM",
            passed=True,
            max_severity=None,
        )
    ], 0


def _resume_brief(plan: ResumePlan, *, rounds_left: int) -> str:
    """The `{resume_brief}` slot: what this run is picking up, for the coder."""
    cp = plan.checkpoint
    spent = f" and spent ${cp.branch_cost_usd:.2f}" if cp.branch_cost_usd else ""
    reviewed = (
        f"The findings below are round {plan.intake_round}'s review of that work."
        if plan.intake_round
        else "No review of that work was recorded before the run died."
    )
    return (
        f"A previous session of this same task ran {cp.branch_rounds} round(s)"
        f"{spent}, then died for an infrastructure reason — an expired credential "
        "or a container that vanished — not because the work was wrong or "
        f"finished. Its commits are already on this branch (HEAD "
        f"{cp.head_sha[:12]}); that session itself is gone, so reconstruct what it "
        f"did from the branch and the commit history below. {reviewed} You have "
        f"{rounds_left} round(s) left for this task."
    )


def prepare_resume(
    config: DevelopConfig, prior_run_dir: Path
) -> tuple[Resumption | None, str]:
    """Plan a resume of *prior_run_dir* under *config*, or say why not.

    Returns ``(resumption, "")`` or ``(None, reason)``. The refusals are all
    "continuing this run buys nothing", never "this run is a problem": a caller
    that is refused does whatever it would have done without a checkpoint — the
    daemon develops the story from scratch, the CLI reports and exits.

    *config* is the run the caller has already resolved (a fresh dispatch's
    route + project + task layering, or the CLI's): the resumed run develops
    the task's CURRENT text with the CURRENT settings and only takes the
    branch, the intake and the remaining budget from the checkpoint.
    """
    checkpoint = resumable_checkpoint(prior_run_dir)
    if checkpoint is None:
        return None, (
            f"{prior_run_dir.name} recorded no round boundary with a commit on "
            "its branch (it died before its first round finished, or it predates "
            "per-round checkpointing)"
        )
    try:
        git.commit_sha(config.repo, checkpoint.head_sha)
    except (RuntimeError, OSError) as exc:
        return None, (
            f"{prior_run_dir.name}'s branch head {checkpoint.head_sha[:12]} is no "
            f"longer in {config.repo} ({exc})"
        )
    rounds_left = config.max_rounds - checkpoint.branch_rounds
    if rounds_left < 1:
        return None, (
            f"{prior_run_dir.name}'s branch already ran {checkpoint.branch_rounds} "
            f"round(s), meeting the max_rounds ceiling of {config.max_rounds}"
        )
    cost_left: float | None = None
    if config.max_cost_usd is not None:
        cost_left = round(config.max_cost_usd - checkpoint.branch_cost_usd, 4)
        # `<= 0` alone is not a budget guard: NaN compares False against
        # everything, so a non-finite remainder would install a ceiling
        # `cost_ceiling_phase` can never reach (security/f-004). The recorded
        # spend is already floored and finite (:func:`checkpoint.from_state`);
        # this is the second half of the same rule, on the arithmetic.
        if not math.isfinite(cost_left) or cost_left <= 0:
            return None, (
                f"{prior_run_dir.name}'s branch already spent "
                f"${checkpoint.branch_cost_usd:.2f} of the ${config.max_cost_usd:.2f} "
                "ceiling"
            )

    intake_reviews, intake_round = _intake_reviews(
        prior_run_dir,
        checkpoint,
        [spec.name for spec in config.effective_reviewers],
    )
    plan = ResumePlan(
        prior_run_dir=prior_run_dir,
        checkpoint=checkpoint,
        intake_round=intake_round,
        intake_reviews=intake_reviews,
    )
    resumed = replace(config, max_rounds=rounds_left, max_cost_usd=cost_left)
    # The live base ref the range is resolved against each round (S5c). The
    # checkpoint's own is authoritative — it is the base the dead run measured
    # from; naming it afresh is only for a checkpoint that predates the field.
    base_ref = checkpoint.base_ref or git.base_ref_for(config.repo, config.base_branch)
    entry = LoopEntry(
        # A fresh branch AT the dead run's head: its own branch is still checked
        # out in its worktree (which the operator may still want to read), and
        # `create_on_branch` is the same committable-entry factory converge uses.
        worktree_factory=lambda cfg: worktree.create_on_branch(
            cfg.repo, checkpoint.head_sha, cfg.description, parent=cfg.worktree_parent
        ),
        base_override=git.RangeBase(
            checkpoint.base_sha or checkpoint.head_sha, base_ref
        ),
        intake_reviews=intake_reviews,
        intake_check_set=None,
        coder_init_template=RESUME_CODER_INIT,
        coder_init_extra={
            "description": config.description,
            "resume_brief": _resume_brief(plan, rounds_left=rounds_left),
        },
        # This IS a story-develop run continuing, so the operator gate behind a
        # `needs-decision` mark still exists — unlike a converge entry, whose
        # question has nobody to answer it (see LoopEntry.decisions_enabled).
        decisions_enabled=True,
        carried_rounds=checkpoint.branch_rounds,
        carried_cost_usd=checkpoint.branch_cost_usd,
    )
    ceiling = (
        f", ${cost_left:.2f} of ${config.max_cost_usd:.2f} left" if cost_left else ""
    )
    note = (
        f"resuming run {prior_run_dir.name} at round {checkpoint.branch_rounds} "
        f"(branch {checkpoint.branch} @ {checkpoint.head_sha[:12]}, "
        f"${checkpoint.branch_cost_usd:.2f} spent): {rounds_left} round(s) left"
        f"{ceiling}; intake is round {intake_round or 'n/a'}'s review"
    )
    return Resumption(config=resumed, entry=entry, plan=plan, note=note), ""


def record_resumed_from(run_dir: Path, plan: ResumePlan) -> None:
    """Record on the RESUMED run whose branch it continues (provenance).

    Written before the loop starts, so a run that dies again still says where
    its head came from — and merged into ``state.json`` like every other block,
    so the loop's own terminal write keeps it.
    """
    cp = plan.checkpoint
    write_state(
        run_dir,
        {
            RESUMED_FROM_KEY: {
                "run_id": plan.prior_run_dir.name,
                "run_dir": str(plan.prior_run_dir),
                "rounds": cp.branch_rounds,
                "cost_usd": cp.branch_cost_usd,
                "branch": cp.branch,
                "head_sha": cp.head_sha,
                "intake_round": plan.intake_round,
            }
        },
    )
