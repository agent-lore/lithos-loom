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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import islice
from pathlib import Path

from ...runner import git, worktree
from . import handoff
from .checkpoint import RoundCheckpoint, resumable_checkpoint, round_checkpoint
from .config import DevelopConfig
from .handoff import HandoffError
from .loop_entry import LoopEntry
from .panel import ReviewOutcome
from .run_outcome import read_state, write_state

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
# * the intake ROUND is loom's own (the checkpoint's ``reviewed_round``), so
#   the directory never chooses it; discovery only picks files within a round
#   loom vouched for, and only when the configured names are absent;
# * at most this many reviewers are read per round, and at most this much
#   RENDERED text is carried into the prompt — ``read_handoff`` bounds each file
#   at 1 MiB, which without a COUNT bound is 1 MiB × N for an attacker-chosen N,
#   the same OOM/billing exposure that per-file cap exists to close. The budget
#   is measured through ``handoff.render_findings`` itself, so it cannot drift
#   from what is actually rendered (a finding's ``files`` /
#   ``deferral_reason`` / ``decision_contest`` are rendered too, security/f-002),
#   and the remainder is elided in the text, as ``check_artifacts`` does;
# * the directory listing itself is bounded, so a dir full of planted names
#   costs a bounded index rather than a few hundred MB of transient strings.
_REVIEWER_TOKEN_RE = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
# The largest canonical panel is 5, and a round that ran an artifact pass files
# a second handoff per reviewer under its own `_artifacts` token (#283 / #291) —
# both are vouched for, so a fully-reviewed round is up to 2x the panel.
MAX_INTAKE_REVIEWERS = 12
MAX_INTAKE_FINDING_CHARS = 20_000
MAX_INTAKE_SCAN_ENTRIES = 4096  # a real run's dir holds a handful per round
# How far back the intake follows the ``resumed_from`` chain for a review. Back-
# to-back infra deaths are exactly this feature's target condition, so the chain
# is real; it is also loom-written and one link per re-dispatch, so anything
# beyond a handful means a link points at itself or at a cycle. Bounded rather
# than trusted, like everything else the intake reads off disk.
MAX_RESUME_CHAIN = 8

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
    # WHICH run that round belongs to. Usually ``prior_run_dir``; an earlier
    # link of the resume chain when this one died before its own panel ran
    # (:func:`_intake_reviews`), in which case the round number is that run's.
    intake_run_dir: Path


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

    Findings are kept in order until :data:`MAX_INTAKE_FINDING_CHARS` of
    RENDERED text has been carried; the rest — and any reviewer file the
    per-round cap dropped — become one ``(elided)`` finding on the last outcome,
    so the coder is told the intake is partial instead of silently shown less.

    Each finding is measured by rendering it through
    :func:`handoff.render_findings` — the very function the prompt is built
    with (security/f-002). Counting a hand-picked subset of fields is what the
    budget did before, and it was wrong in both directions: ``files``,
    ``deferral_reason`` and ``decision_contest`` are rendered and were
    uncounted (one planted handoff rendered 800 kB inside a 20 kB "budget"),
    while ``coder_response`` was counted and is not rendered at all. Measuring
    per finding rather than per accumulated block keeps this linear in the text
    — the handoffs are agent-written, so a quadratic re-render would be its own
    denial of service.
    """
    kept: list[ReviewOutcome] = []
    budget = MAX_INTAKE_FINDING_CHARS
    dropped_findings = 0
    for outcome in outcomes:
        findings: list[handoff.Finding] = []
        for finding in outcome.findings:
            size = len(handoff.render_findings([finding]))
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


def _scan_rounds(
    handoff_dir: Path, up_to_round: int
) -> dict[int, list[tuple[str, str]]]:
    """``{round: [(reviewer, filename), …]}`` for the reviewer handoffs on disk.

    The listing is bounded (:data:`MAX_INTAKE_SCAN_ENTRIES`) and every reviewer
    token is validated: this dir is an RW bind mount in the dead run's agent
    containers, so both the count and the names are theirs to choose, and an
    index built one entry per matching file is itself a cost.
    """
    try:
        entries = sorted(
            islice((p.name for p in handoff_dir.iterdir()), MAX_INTAKE_SCAN_ENTRIES)
        )
    except OSError:
        return {}
    if len(entries) == MAX_INTAKE_SCAN_ENTRIES:
        logger.warning(
            "resume: %s holds more than %d entries; reading the first page only",
            handoff_dir,
            MAX_INTAKE_SCAN_ENTRIES,
        )
    by_round: dict[int, list[tuple[str, str]]] = {}
    for name in entries:
        match = _REVIEW_HANDOFF_RE.match(name)
        if match is None:
            continue
        round_no = int(match.group(1))
        if round_no > up_to_round:
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
    return by_round


def _read_handoffs(
    handoff_dir: Path, entries: Sequence[tuple[str, str]]
) -> list[ReviewOutcome]:
    """Parse *entries* (``(reviewer, filename)``) into intake outcomes.

    Each file goes through the same bounded, adversarial-input reader every other
    consumer uses; one that will not parse is skipped rather than failing the
    resume (the branch is the work; a handoff is a breadcrumb).
    """
    outcomes: list[ReviewOutcome] = []
    for reviewer, name in entries:
        try:
            parsed = handoff.parse_review_handoff(
                handoff.read_handoff(handoff_dir / name)
            )
        except (HandoffError, OSError) as exc:
            logger.warning(
                "resume: skipping unreadable handoff %s (%s)", handoff_dir / name, exc
            )
            continue
        open_findings = [f for f in parsed.findings if f.is_open]
        outcomes.append(
            ReviewOutcome(
                reviewer=reviewer,
                status=parsed.status,
                passed=not open_findings,
                max_severity=handoff.max_severity([f.severity for f in open_findings]),
                findings=list(parsed.findings),
            )
        )
    return outcomes


def _vouched_intake(
    handoff_dir: Path, round_no: int, digests: Mapping[str, str]
) -> list[ReviewOutcome]:
    """One round's intake, read ONLY as the panel left it (security/f-005).

    The handoff dir is one flat mount shared RW by every round's agents, so a
    later round's coder can overwrite an earlier round's review with an "LGTM"
    and suppress its open findings. The checkpoint records what the panel left —
    a content fingerprint per reviewer — so a file that no longer matches is not
    a review: it is skipped, and the caller falls to the round below exactly as
    it does for an unreadable one. The reviewers read are the recorded ones, so
    a file planted under any other name is not eligible at all.
    """
    entries: list[tuple[str, str]] = []
    for name in sorted(digests)[:MAX_INTAKE_REVIEWERS]:
        path = handoff_dir / handoff.reviewer_handoff_name(round_no, name)
        if handoff.file_fingerprint(path) != digests[name]:
            logger.warning(
                "resume: distrusting %s — it does not match what the panel left",
                path,
            )
            continue
        entries.append((name, path.name))
    return _read_handoffs(handoff_dir, entries)


def _round_intake(
    handoff_dir: Path,
    round_no: int,
    panel: Sequence[str],
    by_round: dict[int, list[tuple[str, str]]] | None = None,
) -> tuple[list[ReviewOutcome], int]:
    """One round's intake WITHOUT recorded digests: the legacy path.

    Only for a checkpoint that predates ``reviewed_digests`` — a verified round
    goes through :func:`_vouched_intake`. The CONFIGURED panel's own filenames
    first (no enumeration: the loop names them deterministically); discovery
    within the round is the fallback for a run whose panel has since been
    reconfigured, and it is capped, sorted and token-validated.
    """
    configured = [
        (name, handoff.reviewer_handoff_name(round_no, name))
        for name in sorted(set(panel))
    ][:MAX_INTAKE_REVIEWERS]
    present = [e for e in configured if (handoff_dir / e[1]).is_file()]
    if present:
        return _read_handoffs(handoff_dir, present), 0
    found = sorted((by_round or _scan_rounds(handoff_dir, round_no)).get(round_no, []))
    chosen = found[:MAX_INTAKE_REVIEWERS]
    return _read_handoffs(handoff_dir, chosen), len(found) - len(chosen)


def _no_review() -> list[ReviewOutcome]:
    """The empty intake: ONE outcome, rather than none, so the round-1 prompt's
    findings slot renders the "no structured findings" line instead of a blank
    (and ``intake_reviews is not None`` still selects the cold-start entry)."""
    return [
        ReviewOutcome(
            reviewer="(no review recorded)",
            status="LGTM",
            passed=True,
            max_severity=None,
        )
    ]


def _recorded_intake(
    prior_run_dir: Path, checkpoint: RoundCheckpoint, panel: Sequence[str]
) -> tuple[list[ReviewOutcome], int]:
    """ONE run's last reviewer verdicts, as loop-entry intake.

    **Which round, AND what it said, are loom's own answers — not the
    directory's** (security/f-003, f-005). The handoff dir is one flat RW mount
    shared by every round's agents, so a coder can both plant a review for the
    round it died in (making an unreviewed round look reviewed) and overwrite an
    earlier round's real review with an "LGTM" (suppressing its findings). The
    checkpoint therefore records the last round whose panel actually reviewed
    AND a content fingerprint per reviewer per round: only recorded rounds at or
    below the vouched one are read, only recorded reviewers within them, and only
    files that still match. Anything else falls to the round below. A checkpoint
    that predates the digests keeps the round but takes the directory's content;
    one that predates both falls back to bounded discovery.

    *panel* is the run's configured reviewer names — the files read within the
    chosen round, with bounded discovery as the fallback (see the module's
    bound constants).

    The findings ride along as the round-1 coder prompt's input only. The new
    panel re-reviews the branch from scratch and mints its OWN finding ids, so
    these ids are history the coder is shown, not a ledger it is held to —
    exactly as with a converge intake.
    """
    handoff_dir = prior_run_dir / "handoff"
    if checkpoint.reviewed_digests:
        # Both halves are loom's: WHICH rounds were reviewed, and WHAT each
        # reviewer left in them. A round with no recorded digests is not read at
        # all, a file that no longer matches its digest is not a review, and a
        # round above the vouched one — where a plant for the round the run died
        # in would sit — is never considered.
        for intake_round in range(checkpoint.reviewed_round, 0, -1):
            digests = checkpoint.reviewed_digests.get(str(intake_round))
            if not digests:
                continue
            outcomes = _vouched_intake(handoff_dir, intake_round, digests)
            if outcomes:
                return _bounded(outcomes, 0), intake_round
        return _no_review(), 0
    if checkpoint.reviewed_round:
        # A checkpoint with the round but no digests (written before them):
        # loom's round, the directory's content. Never a round ABOVE it; BELOW it
        # is the real dialogue, so a recorded round whose handoff is corrupt or
        # gone degrades to the round before it rather than to no findings at all.
        for intake_round in range(checkpoint.reviewed_round, 0, -1):
            outcomes, dropped = _round_intake(handoff_dir, intake_round, panel)
            if outcomes:
                return _bounded(outcomes, dropped), intake_round
        return _no_review(), 0
    # A checkpoint that predates `reviewed_round`: discovery, newest round
    # first, preferring one that carries a CONFIGURED reviewer's handoff — the
    # weaker version of the same rule, which is all this path can do.
    by_round = _scan_rounds(handoff_dir, checkpoint.round)
    configured = set(panel)
    ordered = sorted(by_round, reverse=True)
    by_panel = [r for r in ordered if any(n in configured for n, _ in by_round[r])]
    for intake_round in by_panel + [r for r in ordered if r not in by_panel]:
        outcomes, dropped = _round_intake(handoff_dir, intake_round, panel, by_round)
        if outcomes:
            return _bounded(outcomes, dropped), intake_round
    return _no_review(), 0


def _prior_link(run_dir: Path) -> Path | None:
    """The run *run_dir* itself resumed, or ``None``.

    Read from the ``resumed_from`` provenance loom writes on every resumed run
    (:func:`record_resumed_from`) — never from the handoff dir, which the agents
    can write. The link is resolved as a SIBLING: ``run_id`` joined onto the same
    per-task dir, and then re-checked to BE a direct child of it, which is the
    check ``daemon_io.read_resume_run_dir`` applies to the pointer that got us
    here. Containment is the whole rule — a traversal, an absolute value (a join
    discards the left side of one entirely) and a symlink out all fail it — so
    the walk can only ever reach runs of the task being resumed, whatever the
    recorded path says. Nothing is expanded: the recorded ``run_dir`` is
    provenance for the operator, not a path this follows.
    """
    state = read_state(run_dir) or {}
    block = state.get(RESUMED_FROM_KEY)
    if not isinstance(block, dict):
        return None
    run_id = block.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        return None
    prior = run_dir.parent / run_id
    try:
        contained = prior.resolve().parent == run_dir.parent.resolve()
    except OSError:
        return None
    if not contained or not prior.is_dir():
        logger.warning(
            "resume: ignoring chain link %r — not a run beside %s",
            run_id[:80],
            run_dir.parent,
        )
        return None
    return prior


def _intake_reviews(
    prior_run_dir: Path, checkpoint: RoundCheckpoint, panel: Sequence[str]
) -> tuple[list[ReviewOutcome], int, Path]:
    """The last review recorded on this BRANCH, as loop-entry intake.

    Usually *prior_run_dir*'s own (:func:`_recorded_intake`). But a resumed run
    starts with an empty review record — it vouches for a round only once its
    own panel has run — so a second infra death before that point would leave a
    checkpoint with no reviewed round, and the next resume would hand the coder
    "no review recorded" while the first run's still-open findings sat one link
    back. Back-to-back infra failures are this feature's own target condition,
    and a blind coder can spend the remaining paid rounds rediscovering work the
    branch was already told about — so when a link vouches for nothing, the
    ``resumed_from`` provenance is followed to the one before it.

    Only loom's own records are walked and only loom's own rounds are read
    within them (:func:`_recorded_intake`), so the chain inherits the intake's
    trust rules whole: a link whose checkpoint is missing or corrupt ends the
    walk exactly as a rejected checkpoint ends a resume. Returns the intake, the
    round it came from (0 = none anywhere on the chain) and the run dir that
    round belongs to.
    """
    run_dir, cp = prior_run_dir, checkpoint
    seen = {run_dir}
    for _ in range(MAX_RESUME_CHAIN):
        outcomes, intake_round = _recorded_intake(run_dir, cp, panel)
        if intake_round:
            return outcomes, intake_round, run_dir
        prior = _prior_link(run_dir)
        if prior is None or prior in seen:
            break
        prior_cp = round_checkpoint(prior)
        if prior_cp is None:
            logger.info(
                "resume: %s records no usable checkpoint; the chain's intake "
                "ends at %s",
                prior,
                run_dir.name,
            )
            break
        logger.info(
            "resume: %s vouched for no review; taking the intake from the run "
            "it continued (%s)",
            run_dir.name,
            prior.name,
        )
        seen.add(prior)
        run_dir, cp = prior, prior_cp
    return _no_review(), 0, prior_run_dir


def _resume_brief(plan: ResumePlan, *, rounds_left: int) -> str:
    """The `{resume_brief}` slot: what this run is picking up, for the coder."""
    cp = plan.checkpoint
    spent = f" and spent ${cp.branch_cost_usd:.2f}" if cp.branch_cost_usd else ""
    carried = plan.intake_run_dir != plan.prior_run_dir
    reviewed = (
        (
            f"The findings below are round {plan.intake_round}'s review of this "
            "branch — the last one recorded before it was interrupted, carried "
            "forward because the session after it died before its own panel ran, "
            "so treat them as still open unless the branch already answers them."
            if carried
            else f"The findings below are round {plan.intake_round}'s review of "
            "that work."
        )
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
    # Both commits the entry is built from must be IN the repo, and the run
    # enters the RESOLVED object (correctness/f-005). `from_state` has already
    # refused anything but an object name, so this is presence plus
    # canonicalisation rather than ref resolution — which is the point: a
    # symbolic value would resolve here and AGAIN at worktree creation, and a
    # base move in between would resume at code the run never checkpointed.
    try:
        head_sha = git.commit_sha(config.repo, checkpoint.head_sha)
    except (RuntimeError, OSError) as exc:
        return None, (
            f"{prior_run_dir.name}'s branch head {checkpoint.head_sha[:12]} is no "
            f"longer in {config.repo} ({exc})"
        )
    try:
        base_sha = git.commit_sha(config.repo, checkpoint.base_sha)
    except (RuntimeError, OSError) as exc:
        return None, (
            f"{prior_run_dir.name}'s fork point {checkpoint.base_sha[:12]} is no "
            f"longer in {config.repo} ({exc})"
        )
    # …and the fork point must actually be BEHIND the head it was recorded for.
    # Existing and well-formed is not enough (correctness/f-005): a sha from a
    # sibling or later commit would be handed to `RangeBase`, whose `fork_point`
    # can then select it, and the resumed panel would review a range the dead run
    # never recorded — a diff against unrelated code, or nothing at all.
    try:
        descends = git.is_ancestor(config.repo, base_sha, head_sha)
    except (RuntimeError, OSError) as exc:
        return None, (
            f"{prior_run_dir.name}'s fork point {base_sha[:12]} could not be "
            f"related to its head {head_sha[:12]} in {config.repo} ({exc})"
        )
    if not descends:
        return None, (
            f"{prior_run_dir.name}'s recorded fork point {base_sha[:12]} is not an "
            f"ancestor of its head {head_sha[:12]}, so the range it recorded is "
            "not this branch's"
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

    intake_reviews, intake_round, intake_run_dir = _intake_reviews(
        prior_run_dir,
        checkpoint,
        [spec.name for spec in config.effective_reviewers],
    )
    plan = ResumePlan(
        prior_run_dir=prior_run_dir,
        checkpoint=checkpoint,
        intake_round=intake_round,
        intake_reviews=intake_reviews,
        intake_run_dir=intake_run_dir,
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
            cfg.repo, head_sha, cfg.description, parent=cfg.worktree_parent
        ),
        # The fork point the dead run measured from — never a fallback to its
        # HEAD, which would review an empty range (correctness/f-005); a
        # checkpoint without one is refused by `from_state` before we get here.
        base_override=git.RangeBase(base_sha, base_ref),
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
        + (
            f" (carried from run {intake_run_dir.name})"
            if intake_round and intake_run_dir != prior_run_dir
            else ""
        )
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
                # Which run that round belongs to — ``run_id`` above unless the
                # intake was carried from further back down the chain.
                "intake_run_id": plan.intake_run_dir.name,
            }
        },
    )
