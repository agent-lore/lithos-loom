"""``lithos-loom develop converge-push`` — push an exhausted converge run's rounds.

``develop converge`` pushes only when its loop approves, which is right: an
unapproved push would put unreviewed commits on a delivered PR. But a run that
stops exhausted (``not_converged`` on ``max_rounds`` / ``disputed`` /
``stalled`` / ``cost_exceeded``) leaves those rounds **committed on a local
branch** in ``<work_dir>/converge/<run>/worktree``, and nothing in the CLI
showed the operator what they produced or let them decide. Recovering run
26f8ecc5 on 2026-09-24 — max_rounds 5, gate green, the operator's Medium fixed
in round 1 — took three hand steps: find the run dir, prove the PR's remote
head is an ancestor of the worktree tip, ``git push origin HEAD:<pr-branch>``.

This is that decision as a command, in two halves:

* **Report** (no ``--yes``) — the PR and its live remote head, the stop, the
  rounds and spend, the last round's gate verdict, the findings that review
  left open, the fixer commits with a diffstat, and the push verdict
  (``fast-forward`` / ``already pushed`` / ``refused: PR head moved``). It
  writes NOTHING: no push, no PR write, no Lithos write.
* **Push** (``--yes``) — the same append-only, ancestry-proved seam ``converge``
  itself uses (:func:`~.pr_delivery.push_to_pr_ref`: exact-ref ``ls-remote``,
  ``--is-ancestor``, an atomic lease, never a force), then the per-finding
  thread replies the run would have posted had it converged, then
  ``[ConvergePushed]`` on the story — which names the findings left open, so
  that the decision is on the record rather than implied by a green PR.

**The push is the OPERATOR's, not loom's.** It is deliberately not recorded on
the S5b remediation budget as loom's own push: the watcher reads the new head
as a human push and the budget re-arms exactly as it does after a hand
``git push`` — which is what the operator just did, through a command that
knows the branch. ``--complete-gate`` additionally completes the run's own
``remediation_exhausted`` gate (matched by the gate's ``run_id``, as ``deliver``
does); leaving it to close on merge is equally valid, so it is opt-in. No gate
is ever cancelled.

Not this command's business: rebasing or merging the base (the merge-gate owns
that), re-running the panel on the pushed tip (a later ``converge <pr>`` does),
or a run that is still in flight (refused — ``develop list`` shows it).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from lithos_loom.cli._deliver_lithos import read_story
from lithos_loom.config import load_config
from lithos_loom.errors import LithosClientError, LithosLoomError
from lithos_loom.lithos_client import LithosClient
from lithos_loom.plugins.story_develop import run_outcome
from lithos_loom.plugins.story_develop.external_record import read_external_intake
from lithos_loom.plugins.story_develop.external_reviews import (
    ExternalOutcome,
    final_round_outcomes,
)
from lithos_loom.plugins.story_develop.pr_delivery import (
    ForkPushUnsupported,
    MergeRaceDetected,
    push_to_pr_ref,
    remote_head_sha,
)
from lithos_loom.runner import git

logger = logging.getLogger(__name__)

__all__ = ["CONVERGE_PUSHED", "EXIT_CODES", "converge_push_command"]

CONVERGE_PUSHED = "[ConvergePushed]"
"""Finding prefix: an exhausted converge run's rounds were pushed onto the PR
by the operator's decision (``develop converge-push``), naming the sha, the
rounds and the findings the run left open."""

# 0 reported / pushed / already pushed; 1 a refusal the operator must resolve
# (a moved PR head, a run still in flight, a run with no PR on record); 2 bad
# input (no such run, not a converge run). Never a partial: the push is one
# atomic leased update, and everything after it degrades into a note.
EXIT_CODES = {"ok": 0, "refused": 1, "bad_input": 2}

FAST_FORWARD = "fast-forward"
ALREADY_PUSHED = "already pushed"
REFUSED = "refused"

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
    """A previous ``converge-push`` of this run — the offline ``already
    pushed`` answer, still proved against the remote before anything is done."""


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
    return ConvergeRun(
        run_id=str(state.get("run_id") or run_dir.name),
        run_dir=run_dir,
        status=status,
        failure_reason=str(state.get("failure_reason") or ""),
        rounds=state.get("rounds") if isinstance(state.get("rounds"), int) else None,
        cost_usd=(
            float(state["cost_usd"])
            if isinstance(state.get("cost_usd"), int | float)
            else None
        ),
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
    )


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
        # The ancestry question is only answerable about commits this clone
        # HAS: a head someone else pushed while the run was working is not in
        # the worktree's object store, and `--is-ancestor` on it errors rather
        # than answering "no". Fetch the ref first (read-only), so the verdict
        # below is git's real answer about the real commits.
        problem = git.fetch_refspecs(run.worktree, [f"refs/heads/{run.pr_head_branch}"])
        if problem:
            logger.info(
                "converge-push %s: could not fetch %s (%s); judging on what "
                "this clone already has",
                run.run_id,
                run.pr_head_branch,
                problem,
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
    since = run.intake_head_sha or remote_sha
    commits = tuple(git.commits_since(run.worktree, since))
    log = git.log_between(run.worktree, since)
    diffstat = git.diff_stat(run.worktree, since)
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


# ── the external-thread epilogue ───────────────────────────────────────


def replay_outcomes(run: ConvergeRun) -> tuple[ExternalOutcome, ...]:
    """The dispositions this run's threads are owed, or ``()``.

    The epilogue a converged run would have run, replayed from what the run
    recorded at intake plus the coder handoffs on disk — the same
    acknowledgement rules (#387 / #399), read by the same function. It is run
    with ``loop_approved=True`` because the operator's ``--yes`` IS the
    approval the loop never gave: they have read the open findings above and
    decided the rounds should land. Everything else is unchanged, so a
    ``Fixed in <sha>`` still needs the coder's own ``FIXED`` acknowledgement
    for that id in its final handoff.

    ``()`` for a local-panel run (no external material was injected) and for
    one whose record is missing or unreadable — there is then no thread this
    command can honestly answer.
    """
    intake = read_external_intake(run.run_dir)
    if intake is None or not intake.id_map:
        return ()
    return final_round_outcomes(
        handoff_dir=run.run_dir / "handoff",
        run_id=run.run_id,
        rounds=run.rounds or 1,
        loop_approved=True,
        worktree=run.worktree,
        head_sha=run.intake_head_sha,
        generated_paths=intake.generated_paths,
        id_map=intake.id_map,
        rejections=intake.rejections,
        nothing_to_remediate=intake.nothing_to_remediate,
        surviving_ids=intake.surviving_ids,
    )


# ── the Lithos epilogue ────────────────────────────────────────────────

REMEDIATION_EXHAUSTED = "remediation_exhausted"


async def _lithos_coro(
    url: str,
    agent: str,
    *,
    story_id: str,
    summary: str,
    run_id: str,
    complete_gate: bool,
) -> tuple[str, list[str]]:
    """Post the finding, then (opt-in) complete THIS run's exhaustion gate.

    Returns ``(completed_gate_id, problems)``. The gate is matched on both
    keys — ``escalation_reason`` *and* the gate's own ``run_id`` — because
    completing an ``external-remediation`` gate is the operator's consent to
    spend another budget: another run's gate, or one raised for another
    reason, is a decision nobody made here and is left open.
    """
    problems: list[str] = []
    completed = ""
    async with LithosClient(url, agent_id=agent) as client:
        try:
            await client.finding_post(task_id=story_id, summary=summary, agent=agent)
        except (LithosClientError, OSError) as exc:
            problems.append(f"could not post {CONVERGE_PUSHED} on {story_id} ({exc})")
        if not complete_gate:
            return completed, problems
        story = await read_story(client, story_id)
        targets = [
            g
            for g in story.human_gates
            if g.reason == REMEDIATION_EXHAUSTED and g.run_id == run_id
        ]
        if not targets:
            problems.append(
                f"--complete-gate: no open loom {REMEDIATION_EXHAUSTED} gate on "
                f"{story_id} names run {run_id}; no gate was touched"
            )
            return completed, problems
        for gate in targets:
            try:
                await client.task_complete(task_id=gate.gate_id, agent=agent)
            except (LithosClientError, OSError) as exc:
                problems.append(f"could not complete gate {gate.gate_id} ({exc})")
            else:
                completed = gate.gate_id
    return completed, problems


# ── the command ────────────────────────────────────────────────────────


def converge_push_command(
    run: str = typer.Argument(
        ...,
        help="The converge run to push: a run id, or a PR number (its newest "
        "converge run).",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Push. Without it the command only reports and writes nothing.",
    ),
    complete_gate: bool = typer.Option(
        False,
        "--complete-gate",
        help="After the push, complete this run's own remediation_exhausted "
        "needs-human gate (matched by the gate's run_id). Leaving it open is "
        "equally valid — it closes when the PR merges.",
    ),
    story: str | None = typer.Option(
        None,
        "--story",
        help="Lithos task id of the story behind the PR (default: the one the "
        "converge run recorded). Without either, nothing is posted to Lithos.",
    ),
    json_out: Path | None = typer.Option(
        None, "--json", help="Write the structured record to this path."
    ),
    config: Path | None = typer.Option(None, "--config", help="Host config path."),
) -> None:
    """Report an exhausted converge run's unpushed rounds — and push them."""
    try:
        cfg = load_config(config)
        run_dir = resolve_converge_run(cfg.orchestrator.work_dir, run)
        facts = read_run(run_dir)
        if story:
            facts = dataclasses.replace(facts, story_id=story)
        plan = plan_push(facts)
    except NotAConvergeRun as exc:
        typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(EXIT_CODES["bad_input"]) from exc
    except (ConvergePushRefused, LithosLoomError) as exc:
        typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(EXIT_CODES["refused"]) from exc

    notes: list[str] = []
    pushed_sha = ""
    if yes and plan.pushable:
        try:
            pushed_sha = push_to_pr_ref(
                facts.worktree,
                facts.branch,
                facts.pr_head_branch,
                expected_remote_sha=plan.remote_sha,
            )
        except (MergeRaceDetected, ForkPushUnsupported, RuntimeError, OSError) as exc:
            # Nothing landed (the lease is atomic), so this is a refusal like
            # any other — the report above already told the operator what the
            # run holds.
            typer.secho(f"error: push refused: {exc}", err=True, fg=typer.colors.RED)
            raise typer.Exit(EXIT_CODES["refused"]) from exc
        try:
            run_outcome.record_converge_push(
                facts.run_dir, pushed_sha=pushed_sha, pr_url=facts.pr_url
            )
        except OSError as exc:
            # The remote is the authority; this record is only the offline
            # fast path a re-run and `develop list` read.
            notes.append(f"could not record the push in the run dir ({exc})")
        notes += _post_epilogue(
            cfg, facts, pushed_sha=pushed_sha, complete_gate=complete_gate
        )

    for line in report(facts, plan):
        typer.echo(line)
    if pushed_sha:
        typer.echo(f"  pushed {pushed_sha[:12]} → {facts.pr_head_branch}")
    for note in notes:
        typer.echo(f"  note: {note}")
    if json_out is not None:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(
            json.dumps(
                json_record(facts, plan, pushed_sha=pushed_sha, notes=notes), indent=2
            )
            + "\n",
            encoding="utf-8",
        )
    if plan.verdict == REFUSED:
        raise typer.Exit(EXIT_CODES["refused"])
    raise typer.Exit(EXIT_CODES["ok"])


def _post_epilogue(
    cfg: Any, facts: ConvergeRun, *, pushed_sha: str, complete_gate: bool
) -> list[str]:
    """Everything owed AFTER the push: the reviewers' threads, then the story.

    Best-effort by construction — the commits are on the PR and no later
    failure may be reported as a failure to push. Each problem becomes a note
    on the command's own output (and in ``--json``), so what is owed is said
    rather than lost.
    """
    notes: list[str] = []
    try:
        outcomes = replay_outcomes(facts)
    except (RuntimeError, OSError, ValueError) as exc:
        notes.append(f"could not replay the external dispositions ({exc})")
        outcomes = ()
    if outcomes and facts.repo and facts.pr_number:
        # imported here: the reply transports live with the converge command,
        # and importing them at module scope would make this module depend on
        # the whole converge CLI just to report.
        from lithos_loom.cli.converge import post_external_replies

        try:
            post_external_replies(
                outcomes,
                repo=facts.repo,
                pr_number=facts.pr_number,
                pushed=True,
                pushed_sha=pushed_sha,
            )
        except (RuntimeError, OSError) as exc:
            notes.append(f"external thread replies failed ({exc})")
    elif outcomes:
        notes.append(
            "the run injected external findings but recorded no repo / PR "
            "number — no thread reply was posted"
        )
    if not facts.story_id:
        notes.append(
            f"no story recorded for this run — {CONVERGE_PUSHED} was not "
            "posted (pass --story to record it)"
        )
        return notes
    try:
        _, problems = asyncio.run(
            _lithos_coro(
                cfg.orchestrator.lithos_url,
                cfg.orchestrator.agent_id,
                story_id=facts.story_id,
                summary=finding_summary(facts, pushed_sha=pushed_sha),
                run_id=facts.run_id,
                complete_gate=complete_gate,
            )
        )
    except (LithosClientError, LithosLoomError, OSError, ExceptionGroup) as exc:
        # LithosLoomError covers the story read's own refusal (a story that is
        # gone). Nothing here may turn a landed push into a crash.
        return [*notes, f"Lithos: {exc}; the finding was not posted"]
    return notes + problems
