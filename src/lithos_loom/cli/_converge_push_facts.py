"""What ``develop converge-push`` reads before it decides: the run, the PR, the plan.

The read-only half of the command (:mod:`cli.converge_push` is the deciding
half), split out for the same reason ``develop deliver``'s preflight is: the
refusals are what make the command safe, and they are easier to read — and to
test — apart from the sequence they guard.

Three reads, in order, each of which can refuse on its own:

* **the run dir** — what an exhausted converge run left behind, and whether it
  is a run this command may act on at all;
* **the PR** — one live GitHub read, because the run dir's record of it is an
  intake-time snapshot and a branch name is not a PR;
* **the ref** — the worktree tip against the PR's live head, which is the
  verdict the operator authorises the push from.

…and how all three READ OUT: the operator's report, the stable ``--json``
object and the ``[ConvergePushed]`` summary are pure functions of the facts
and the plan, so they live here with them rather than in the sequence that
decides.
"""

from __future__ import annotations

import logging
import math
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lithos_loom.errors import LithosLoomError
from lithos_loom.plugins.story_develop import run_outcome
from lithos_loom.plugins.story_develop.github_access import (
    GitHubError,
    PullRequest,
    github_call,
)
from lithos_loom.plugins.story_develop.github_access import (
    repo_name_with_owner as origin_repo_name,
)
from lithos_loom.plugins.story_develop.pr_delivery import remote_head_sha
from lithos_loom.runner import git

logger = logging.getLogger(__name__)

__all__ = [
    "ALREADY_PUSHED",
    "CONVERGE_PUSHED",
    "FAST_FORWARD",
    "REFUSED",
    "UNCERTAIN",
    "ConvergePushRefused",
    "ConvergeRun",
    "NotAConvergeRun",
    "PrCheck",
    "PushPlan",
    "fetch_pull_request",
    "finding_summary",
    "json_record",
    "plan_push",
    "read_run",
    "report",
    "resolve_converge_run",
    "verify_pr",
]

FAST_FORWARD = "fast-forward"
ALREADY_PUSHED = "already pushed"
REFUSED = "refused"
# Only ever the verdict of a push whose outcome could not be READ back — never
# a plan's own verdict. It is kept apart from `refused` because the one thing
# it cannot assert is that nothing was written.
UNCERTAIN = "uncertain"

# The statuses a converge loop can stop at without pushing. `approved` is not
# among them: converge pushes on approval, so an approved run either delivered
# or failed its own push — and the ancestry check below reports that honestly.
_TERMINAL = frozenset(
    {
        "max_rounds",
        "disputed",
        "stalled",
        "cost_exceeded",
        "needs_decision",
        "infra_failed",
        "interrupted",
        "failed",
        run_outcome.APPROVED,
    }
)


class ConvergePushRefused(LithosLoomError):
    """A precondition failed; nothing was written."""


class NotAConvergeRun(LithosLoomError):
    """The key names a run that is not a converge run (or no run at all)."""


# ── the facts ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ConvergeRun:
    """What the run dir says about an exhausted converge run."""

    run_id: str
    run_dir: Path
    status: str
    failure_reason: str
    rounds: int | None
    cost_usd: float | None
    branch: str
    worktree: Path
    pr_url: str
    pr_number: int | None
    pr_head_branch: str
    intake_head_sha: str
    base_sha: str
    repo: str
    story_id: str
    test_gate: dict[str, Any] | None
    blocking_checks: tuple[dict[str, Any], ...]
    open_findings: tuple[dict[str, Any], ...]
    pushed_sha: str | None
    """The sha this run's rounds were pushed at, by either pusher — the
    offline ``already pushed`` answer, still proved against the remote before
    anything is done."""
    pushed_by: str = ""
    """WHICH pusher put it there — ``converge`` (its own approved push, whose
    epilogue is the converge command's own and is not this one's business) or
    ``converge-push`` (the operator's salvage, whose epilogue this command
    owes and resumes)."""
    intent_sha: str = ""
    """The tip a ``converge-push`` recorded it was ABOUT to push. With a
    remote that now holds it, this is what says the push landed even when the
    process died before recording it."""
    finding_posted: bool = False
    """Whether the ``[ConvergePushed]`` audit for that push has landed."""
    gate_completed: str = ""
    """The gate a previous ``--complete-gate`` completed, if any. With
    *finding_posted* this is what lets a re-run FINISH an epilogue a crash or
    a refusing transport left half-done, instead of reporting "already
    pushed" over work that is still owed."""


def read_run(run_dir: Path) -> ConvergeRun:
    """Read *run_dir* into :class:`ConvergeRun`, refusing what cannot be pushed.

    Two refusals live here because they are facts about the run, not about the
    remote: a run with no recorded outcome may be mid-round right now (pushing
    its tip would put commits the loop is still writing onto a live PR), and a
    run with no recorded PR has nothing to push onto — the intake record
    (:func:`run_outcome.record_converge_intake`) is what names it, and a run
    predating that record must be pushed by hand.
    """
    state = run_outcome.read_state(run_dir) or {}
    status = str(state.get("status") or "")
    if not status:
        raise ConvergePushRefused(
            f"run {run_dir.name} has recorded no outcome — there is no terminal "
            f"{run_outcome.STATE_FILE} in {run_dir}, which the loop writes only "
            "at run end, so this run may be mid-round right now. Watch it with "
            f"`lithos-loom develop attach {run_dir.name}`"
        )
    if status not in _TERMINAL:
        raise ConvergePushRefused(
            f"run {run_dir.name} is {status!r}, which is not a terminal converge "
            "outcome; nothing to push"
        )
    intake = run_outcome.converge_intake(run_dir) or {}
    pr_head_branch = str(intake.get("pr_head_branch") or "")
    if not pr_head_branch:
        raise ConvergePushRefused(
            f"run {run_dir.name} recorded no PR (its {run_outcome.STATE_FILE} has "
            f"no {run_outcome.CONVERGE_KEY!r} block — a run from before converge "
            "recorded one). There is no head branch to push onto: find the PR, "
            "check `git -C <worktree> log`, and push by hand"
        )
    worktree = Path(str(state.get("worktree") or (run_dir / "worktree")))
    push_record = run_outcome.converge_push_record(run_dir)
    return ConvergeRun(
        run_id=str(state.get("run_id") or run_dir.name),
        run_dir=run_dir,
        status=status,
        failure_reason=str(state.get("failure_reason") or ""),
        rounds=state.get("rounds") if isinstance(state.get("rounds"), int) else None,
        cost_usd=_whole_command_cost(state),
        branch=str(state.get("branch") or ""),
        worktree=worktree,
        pr_url=str(intake.get("pr_url") or ""),
        pr_number=(
            intake.get("pr_number")
            if isinstance(intake.get("pr_number"), int)
            else None
        ),
        pr_head_branch=pr_head_branch,
        intake_head_sha=str(intake.get("intake_head_sha") or ""),
        base_sha=str(intake.get("base_sha") or ""),
        repo=str(intake.get("repo") or ""),
        story_id=str(intake.get("story_id") or ""),
        test_gate=state.get("test_gate")
        if isinstance(state.get("test_gate"), dict)
        else None,
        blocking_checks=tuple(
            c for c in state.get("blocking_checks") or () if isinstance(c, dict)
        ),
        open_findings=tuple(
            f for f in state.get("open_findings") or () if isinstance(f, dict)
        ),
        pushed_sha=run_outcome.converge_pushed_sha(run_dir),
        pushed_by=str(push_record.get("by") or ""),
        intent_sha=str(push_record.get("intent_sha") or ""),
        finding_posted=bool(push_record.get("finding_posted")),
        gate_completed=str(push_record.get("gate_completed") or ""),
    )


def _whole_command_cost(state: Mapping[str, Any]) -> float | None:
    """The WHOLE command's spend: converge's intake / triage turn + the loop.

    Three sources, in order, because the two halves are written by different
    processes at different moments and the run can be read between them:

    1. ``total_cost_usd`` — converge's authoritative sum, written after the
       loop returns;
    2. ``cost_usd + intake_cost_usd`` — the loop's own figure (written by
       ``develop()`` alongside the terminal status every reader stops on) plus
       the pre-loop spend converge records BEFORE the loop starts. This is the
       window the sum exists for: a run read — or killed — after the terminal
       write but before the total lands still reports the whole spend, not the
       loop-only one;
    3. the loop's figure alone, for a run from before either key: the most
       that run can honestly claim.
    """
    total = _opt_cost(state.get("total_cost_usd"))
    if total is not None:
        return total
    loop = _opt_cost(state.get("cost_usd"))
    if loop is None:
        return None
    return loop + (_opt_cost(state.get("intake_cost_usd")) or 0.0)


def _opt_cost(*values: Any) -> float | None:
    """The first finite, non-negative number among *values*, else ``None``.

    ``json.loads`` accepts ``NaN`` / ``Infinity``, and a spend is published to
    the operator as the basis of a decision — "unknown" is the only honest
    rendering of a number that is not one.
    """
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        try:
            number = float(value)
        except (OverflowError, ValueError):
            continue  # an arbitrary-precision int is outside the float domain
        if math.isfinite(number) and number >= 0:
            return number
    return None


def resolve_converge_run(work_dir: Path, key: str) -> Path:
    """Resolve *key* — a converge run id, or a PR number (its newest run).

    The same operator-typed key shape as ``attach`` / ``dump`` / ``deliver``,
    narrowed to converge: a run id that names a story-develop run is refused
    here rather than acted on, because that run has no PR and ``deliver`` is
    its command.
    """
    converge_dir = work_dir / run_outcome.CONVERGE_DIR
    run_dir = run_outcome.resolve_run_dir(work_dir, key)
    if run_dir is not None:
        if not run_outcome.is_converge_run_dir(run_dir):
            raise NotAConvergeRun(
                f"run {key!r} is a story-develop run ({run_dir}), not a converge "
                "run: it has no PR to push onto. Deliver its branch with "
                f"`lithos-loom develop deliver {key}`"
            )
        return run_dir
    number = _pr_number(key)
    if number is not None and converge_dir.is_dir():
        candidates = [
            d
            for d in converge_dir.iterdir()
            if run_outcome.is_run_dir(d)
            and (run_outcome.converge_intake(d) or {}).get("pr_number") == number
        ]
        if candidates:
            # newest by its last on-disk activity, the same rule `resolve_run_dir`
            # uses when a task id names several runs
            return max(candidates, key=lambda p: p.stat().st_mtime)
    raise NotAConvergeRun(
        f"no converge run for {key!r} under {converge_dir} "
        "(`lithos-loom develop list` shows what is there)"
    )


def _pr_number(key: str) -> int | None:
    raw = key.strip().lstrip("#")
    return int(raw) if raw.isdigit() else None


# ── is the PR still the PR we recorded? (the live read) ────────────────


def fetch_pull_request(repo: str, number: int) -> PullRequest | None:
    """The PR as GitHub has it now (``None`` when it was deleted).

    A seam of its own so the verification below is one stubbable call, like
    every other ``gh`` read in this package.
    """
    return github_call(lambda c: c.get_pull_request(repo, number))


@dataclass(frozen=True)
class PrCheck:
    """Whether the recorded PR facts still describe reality."""

    ok: bool
    problem: str = ""
    head_ref: str = ""
    state: str = ""


def verify_pr(run: ConvergeRun) -> PrCheck:
    """Re-read the PR before the verdict is printed — the pin every other
    write path in this system carries.

    The run dir's `converge` block was written at intake, possibly days ago,
    and the only other live read (``ls-remote``) answers "what sha is on that
    branch NAME now" — not "is that branch still the head of PR #N, in this
    repository, and is that PR still open". Without this, an exhausted run's
    rounds can land on a **merged** PR's undeleted branch (``converge_pr``
    refuses a merged PR for exactly this reason), or on a branch deleted and
    recreated under the same deterministic name, while the thread replies and
    the ``[ConvergePushed]`` provenance assert a landing somewhere it did not
    happen.

    Fails **closed**: a read that does not answer is not a pass, because the
    same stale record addresses the replies and the audit finding. The caller
    turns a non-``ok`` check into a refusal the REPORT states too — the report
    is what the operator authorises the push from, so it may not look
    pushable when it is not.
    """
    if run.pr_number is None:
        return PrCheck(False, "the run recorded no PR number — nothing to verify")
    if not run.repo:
        # Written empty when the origin read failed at intake. Without it there
        # is nothing to compare the worktree's origin against and nothing to
        # address the replies to — "I do not know which repository" is not a
        # pass.
        return PrCheck(
            False,
            "the run recorded no repository (its origin could not be read at "
            "intake), so the PR it names cannot be confirmed — push by hand",
        )
    # The push goes to the worktree's `origin`; the replies and the finding
    # are addressed to the recorded repo. They must be the same place.
    try:
        origin = origin_repo_name(run.worktree)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        return PrCheck(False, f"could not read the worktree's origin ({exc})")
    if origin.lower() != run.repo.lower():
        return PrCheck(
            False,
            f"the worktree's origin is {origin!r}, not the {run.repo!r} this "
            "run recorded — the PR, the thread replies and the push would "
            "not be the same repository",
        )
    try:
        pr = fetch_pull_request(run.repo, run.pr_number)
    except (GitHubError, RuntimeError, OSError) as exc:
        return PrCheck(False, f"could not read {run.repo}#{run.pr_number} ({exc})")
    if pr is None:
        return PrCheck(False, f"{run.repo}#{run.pr_number} no longer exists")
    if pr.merged:
        return PrCheck(
            False,
            f"{run.repo}#{run.pr_number} has already MERGED — a fix commit "
            "pushed to its branch can never land; nothing to push",
            head_ref=pr.head_ref,
            state="merged",
        )
    if pr.state != "open":
        return PrCheck(
            False,
            f"{run.repo}#{run.pr_number} is {pr.state}, not open — pushing to "
            "its branch would land nothing a reviewer will read",
            head_ref=pr.head_ref,
            state=pr.state,
        )
    # Each check below refuses on a field it cannot READ, not only on one that
    # disagrees: `head_ref` is the single field binding "PR #N" to "the branch
    # we are about to push to", and an `x and x != y` shape would skip it on an
    # empty payload and degrade the guard to "the PR exists and is open".
    if not pr.head_ref or pr.head_ref != run.pr_head_branch:
        return PrCheck(
            False,
            f"{run.repo}#{run.pr_number} heads "
            f"{pr.head_ref or '(no head branch in the payload)'}, not the "
            f"{run.pr_head_branch!r} this run recorded — the branch under that "
            "name is not confirmed to be this PR's",
            head_ref=pr.head_ref,
            state=pr.state,
        )
    # GitHub returns `"head": {"repo": null}` for a PR whose head FORK was
    # deleted, which parses to an empty `head_repo`. Skipping the fork check
    # there is how an unreviewed third-party head lands on an origin branch of
    # the same name (this command leases against the LIVE head, so converge's
    # accidental protection — leasing against the fork's own sha — is gone).
    if not pr.head_repo or not pr.base_repo or pr.head_repo != pr.base_repo:
        return PrCheck(
            False,
            f"{run.repo}#{run.pr_number} is not confirmed to head this "
            f"repository (head {pr.head_repo or 'unknown — a deleted fork?'}, "
            f"base {pr.base_repo or 'unknown'}) — loom cannot push to a fork "
            "under origin credentials, and a head it cannot identify is not a "
            "head it may push to",
            head_ref=pr.head_ref,
            state=pr.state,
        )
    return PrCheck(True, head_ref=pr.head_ref, state=pr.state)


# ── the plan ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PushPlan:
    """What a push would do, decided against the PR's LIVE remote head."""

    verdict: str  # FAST_FORWARD | ALREADY_PUSHED | REFUSED
    detail: str
    tip: str
    remote_sha: str
    commits: tuple[str, ...] = ()
    log: str = ""
    diffstat: str = ""

    @property
    def pushable(self) -> bool:
        return self.verdict == FAST_FORWARD


def plan_push(run: ConvergeRun) -> PushPlan:
    """Read the worktree tip + the PR's live head and decide the verdict.

    Read-only, and the decision is git's: the remote head must be an ancestor
    of the tip (the update can then only ADD commits). Anything else — the
    head moved under the run, the branch is gone — is refused, here and in
    the push seam, which re-checks under a lease.
    """
    if not run.worktree.is_dir():
        raise ConvergePushRefused(
            f"run {run.run_id}'s worktree {run.worktree} is gone — its commits "
            "are not on this host any more; nothing can be pushed"
        )
    try:
        tip = git.commit_sha(run.worktree)
        remote_sha = remote_head_sha(run.worktree, run.pr_head_branch)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        raise ConvergePushRefused(f"could not read the PR's head: {exc}") from exc
    if remote_sha:
        # The ancestry question — and every range below it — is only answerable
        # about commits this clone HAS: a head someone else pushed while the
        # run was working is not in the worktree's object store, and `git
        # rev-list` / `--is-ancestor` on it ERROR rather than answering. Fetch
        # the ref (read-only); if the object is still not here afterwards there
        # is no honest verdict to give, so refuse rather than raise past the
        # report.
        problem = git.fetch_refspecs(run.worktree, [f"refs/heads/{run.pr_head_branch}"])
        try:
            git.commit_sha(run.worktree, remote_sha)
        except (RuntimeError, OSError) as exc:
            logger.info(
                "converge-push %s: the PR head %s is not in this clone after "
                "fetching %s (%s): %s",
                run.run_id,
                remote_sha[:12],
                run.pr_head_branch,
                problem or "the fetch reported success",
                exc,
            )
            return PushPlan(
                verdict=REFUSED,
                detail=(
                    f"the PR head is {remote_sha[:12]}, which this clone does "
                    f"not have and could not fetch ({problem or exc}) — the "
                    "push can only be proved append-only against an object "
                    "that is here. Re-run when the remote answers"
                ),
                tip=tip,
                remote_sha=remote_sha,
            )

    if not remote_sha:
        return PushPlan(
            verdict=REFUSED,
            detail=(
                f"the PR head branch {run.pr_head_branch!r} is not on origin "
                "(deleted, or a fork PR) — nothing to push onto"
            ),
            tip=tip,
            remote_sha="",
        )
    # The base the push is LEASED against is the live remote head, so that is
    # what the report must measure — never the intake head. They differ
    # whenever the PR head moved: a rewind to an ancestor (the standard way to
    # take an accidentally-committed secret back off a branch) still passes the
    # ancestry guard, and measuring from the intake head would show the
    # operator "2 commits" while the push silently restores the removed ones.
    # `intake_head_sha` stays a reported fact of its own.
    try:
        commits = tuple(git.commits_since(run.worktree, remote_sha))
        log = git.log_between(run.worktree, remote_sha)
        diffstat = git.diff_stat(run.worktree, remote_sha)
    except (RuntimeError, OSError) as exc:
        # Defensive beside the presence check above: a range this clone cannot
        # build is a report it cannot make, and the operator gets a refusal
        # rather than a traceback where the verdict should be.
        return PushPlan(
            verdict=REFUSED,
            detail=(
                f"the commits between the PR head {remote_sha[:12]} and this "
                f"run's tip {tip[:12]} could not be read ({exc}); nothing can "
                "be reported or pushed"
            ),
            tip=tip,
            remote_sha=remote_sha,
        )
    if remote_sha == tip:
        return PushPlan(
            verdict=ALREADY_PUSHED,
            detail=(
                f"the PR head is already this run's tip {tip[:12]} — nothing to push"
            ),
            tip=tip,
            remote_sha=remote_sha,
            commits=commits,
            log=log,
            diffstat=diffstat,
        )
    try:
        descends = git.is_ancestor(run.worktree, remote_sha, tip)
    except (RuntimeError, OSError) as exc:
        # The head is a commit this clone does not have even after the fetch:
        # unprovable is REFUSED, never assumed safe.
        logger.info("converge-push %s: ancestry check failed: %s", run.run_id, exc)
        descends = False
    if not descends:
        return PushPlan(
            verdict=REFUSED,
            detail=(
                f"PR head moved to {remote_sha[:12]}, which is not an ancestor "
                f"of this run's tip {tip[:12]} — pushing would drop whoever "
                "moved it. Re-converge the PR at its current head"
            ),
            tip=tip,
            remote_sha=remote_sha,
            commits=commits,
            log=log,
            diffstat=diffstat,
        )
    return PushPlan(
        verdict=FAST_FORWARD,
        detail=(
            f"{len(commits)} commit(s) would fast-forward {run.pr_head_branch} "
            f"from {remote_sha[:12]} to {tip[:12]}"
        ),
        tip=tip,
        remote_sha=remote_sha,
        commits=commits,
        log=log,
        diffstat=diffstat,
    )


CONVERGE_PUSHED = "[ConvergePushed]"
"""Finding prefix: an exhausted converge run's rounds were pushed onto the PR
by the operator's decision (``develop converge-push``), naming the sha, the
rounds and the findings the run left open."""


# ── rendering ──────────────────────────────────────────────────────────


def _gate_line(run: ConvergeRun) -> str:
    bits = []
    if run.test_gate:
        bits.append(f"test {run.test_gate.get('verdict', '?')}")
    for check in run.blocking_checks:
        bits.append(f"{check.get('name', '?')} {check.get('verdict', 'RED')}")
    return ", ".join(bits) if bits else "no gate verdict recorded"


def report(run: ConvergeRun, plan: PushPlan) -> list[str]:
    """The operator-facing report — every fact the decision rests on."""
    stop = f"{run.status}" + (f" — {run.failure_reason}" if run.failure_reason else "")
    lines = [
        f"converge-push {run.run_id}: {stop}",
        f"  PR:           {run.pr_url or '(url unknown)'}"
        + (f" (#{run.pr_number})" if run.pr_number else ""),
        f"  head branch:  {run.pr_head_branch}",
        f"  intake head:  {run.intake_head_sha[:12] or '(unknown)'}"
        f"   PR head now: {plan.remote_sha[:12] or '(absent)'}",
        f"  worktree tip: {plan.tip[:12]}  ({run.worktree})",
        f"  rounds:       {run.rounds if run.rounds is not None else 'unknown'}"
        + (f"   cost: ${run.cost_usd:.2f}" if run.cost_usd is not None else ""),
        f"  gate:         {_gate_line(run)}",
    ]
    if run.open_findings:
        lines.append(f"  open findings ({len(run.open_findings)}):")
        lines += [
            f"    - [{f.get('reviewer', '?')}/{f.get('finding_id', '?')}] "
            f"{f.get('severity', '?')} — {f.get('title', '')}"
            for f in run.open_findings
        ]
    else:
        lines.append("  open findings: none recorded")
    lines.append(f"  fixer commits ({len(plan.commits)}):")
    lines += [f"    {line}" for line in plan.log.splitlines() if line.strip()]
    lines += [f"    {line}" for line in plan.diffstat.splitlines() if line.strip()]
    if run.pushed_sha:
        lines.append(f"  recorded push: {run.pushed_sha[:12]} (converge-push)")
    lines.append(f"  verdict:      {plan.verdict} — {plan.detail}")
    if plan.pushable:
        lines.append(
            "  re-run with --yes to push (the watcher reads it as a HUMAN "
            "push: the remediation budget re-arms)"
        )
    return lines


def json_record(
    run: ConvergeRun, plan: PushPlan, *, pushed_sha: str = "", notes: list[str]
) -> dict[str, Any]:
    """The same facts as a stable object."""
    return {
        "run_id": run.run_id,
        "run_dir": str(run.run_dir),
        "status": run.status,
        "failure_reason": run.failure_reason,
        "rounds": run.rounds,
        "total_cost_usd": run.cost_usd,
        "pr_url": run.pr_url,
        "pr_number": run.pr_number,
        "pr_head_branch": run.pr_head_branch,
        "story_id": run.story_id,
        "repo": run.repo,
        "intake_head_sha": run.intake_head_sha,
        "remote_head_sha": plan.remote_sha,
        "worktree_tip": plan.tip,
        "verdict": plan.verdict,
        "detail": plan.detail,
        "fixer_commits": list(plan.commits),
        "diffstat": plan.diffstat,
        "gate": {
            "test_gate": run.test_gate,
            "blocking_checks": list(run.blocking_checks),
        },
        "open_findings": list(run.open_findings),
        "pushed": bool(pushed_sha),
        "pushed_sha": pushed_sha or None,
        "notes": notes,
    }


def finding_summary(run: ConvergeRun, *, pushed_sha: str) -> str:
    """``[ConvergePushed]`` — what landed, and what it landed WITH.

    The open findings are named, not counted: the operator overrode an
    unapproved loop, and the record of that decision is what makes the next
    reader of this PR able to tell a converged head from a pushed one.
    """
    lines = [
        f"{CONVERGE_PUSHED} converge run {run.run_id} stopped {run.status} "
        f"without pushing; its rounds were pushed to "
        f"{run.pr_url or run.pr_head_branch} by "
        "`lithos-loom develop converge-push` (the operator's decision).",
        f"- pushed {pushed_sha[:12]} onto {run.pr_head_branch}",
        f"- rounds: {run.rounds if run.rounds is not None else 'unknown'}"
        + (f", cost ${run.cost_usd:.2f}" if run.cost_usd is not None else ""),
        f"- gate at the last round: {_gate_line(run)}",
    ]
    if run.open_findings:
        lines.append("- pushed WITH these findings still open:")
        lines += [
            f"  - [{f.get('reviewer', '?')}/{f.get('finding_id', '?')}] "
            f"{f.get('severity', '?')} — {f.get('title', '')}"
            for f in run.open_findings
        ]
    else:
        lines.append("- no review findings were left open")
    lines.append(
        "- the push is the operator's, not loom's: the watcher reads it as a "
        "human push and the external-remediation budget re-arms."
    )
    return "\n".join(lines)
