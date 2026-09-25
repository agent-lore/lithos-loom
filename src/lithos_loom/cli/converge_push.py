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
  thread replies **the push newly makes true** (the run answered the rest when
  it exited; :func:`replay_outcomes`), then ``[ConvergePushed]`` on the story —
  which names the findings left open, so that the decision is on the record
  rather than implied by a green PR.

Both halves rest on two live reads, not on the run dir alone: the PR itself
(:func:`verify_pr` — a record written at intake does not know that the PR has
since merged, closed, changed head branch or moved repository) and the head
ref. And a push that reports failure is **read back** before it is called one:
the lease proves atomicity, not observability (:func:`_classify_failed_push`).

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
import contextlib
import dataclasses
import json
import logging
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import typer

from lithos_loom.cli._converge_push_facts import (
    ALREADY_PUSHED,
    CONVERGE_PUSHED,
    REFUSED,
    UNCERTAIN,
    ConvergePushRefused,
    ConvergeRun,
    NotAConvergeRun,
    PushPlan,
    finding_summary,
    json_record,
    plan_push,
    read_run,
    report,
    resolve_converge_run,
    verify_pr,
)
from lithos_loom.cli._deliver_facts import sanitize_for_terminal
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
from lithos_loom.runner.orphans import pid_alive

logger = logging.getLogger(__name__)

__all__ = ["CONVERGE_PUSHED", "EXIT_CODES", "converge_push_command"]

# 0 reported / pushed / already pushed; 1 a refusal the operator must resolve
# (a moved PR head, a PR that is no longer the one recorded, a run still in
# flight, a run with no PR on record); 2 either bad input (no such run, not a
# converge run) or an UNCERTAIN push — "nothing was written" is the one thing
# that cannot be asserted there, so it never shares the refusal's code.
EXIT_CODES = {"ok": 0, "refused": 1, "bad_input": 2, "uncertain": 2}

# ── the external-thread epilogue ───────────────────────────────────────


def replay_outcomes(run: ConvergeRun) -> tuple[ExternalOutcome, ...]:
    """The threads this push owes an answer — never one already answered.

    The dispositions are the recorded batch (the intake record plus the coder
    handoffs on disk) read through the same ``final_round_outcomes`` and the
    same #387 / #399 acknowledgement rules, with ``loop_approved=True``: the
    operator's ``--yes`` IS the approval the loop never gave (they have read
    the open findings and decided the rounds should land), and that is what
    turns an acknowledged ``FIXED`` from an unassertable claim into
    ``Fixed in <sha>``.

    What is subtracted is the run's record of the threads it **actually
    posted** to (``external.json``'s ``replied``, written by whichever process
    posted each one — see :func:`~.external_record.record_replied`). Not what
    the run was *eligible* to answer: its terminal status is written by the
    loop *before* ``converge_pr`` returns and before the CLI reaches its reply
    epilogue, so a SIGTERM in that window — or a transport that simply
    returned ``False`` — leaves a rejection or a dispute unanswered on a run
    that looks, from its status alone, as though it had answered. Reconstructed
    eligibility would suppress exactly those.

    ``()`` for a local-panel run (no external material was injected) and for
    one whose record is missing or unreadable — there is then no thread this
    command can honestly answer.
    """
    intake = read_external_intake(run.run_dir)
    if intake is None or not intake.id_map:
        return ()
    owed = final_round_outcomes(
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
    return tuple(o for o in owed if o.finding_id not in intake.replied)


# ── the Lithos epilogue ────────────────────────────────────────────────

REMEDIATION_EXHAUSTED = "remediation_exhausted"


async def _lithos_coro(
    url: str,
    agent: str,
    *,
    story_id: str,
    summary: str,
    run_id: str,
    post_finding: bool,
    complete_gate: bool,
) -> tuple[bool, str, list[str]]:
    """Post the finding, then (opt-in) complete THIS run's exhaustion gate.

    Returns ``(finding_posted, completed_gate_id, problems)`` — what actually
    LANDED, so the caller can record it and a re-run finishes only what did
    not. Each step is skipped when a previous invocation already did it
    (*post_finding* / *complete_gate*), so resuming never duplicates.

    The gate is matched on both keys — ``escalation_reason`` *and* the gate's
    own ``run_id`` — because completing an ``external-remediation`` gate is
    the operator's consent to spend another budget: another run's gate, or one
    raised for another reason, is a decision nobody made here and is left open.
    """
    problems: list[str] = []
    completed = ""
    posted = False
    async with LithosClient(url, agent_id=agent) as client:
        if post_finding:
            try:
                await client.finding_post(
                    task_id=story_id, summary=summary, agent=agent
                )
            except (LithosClientError, OSError) as exc:
                problems.append(
                    f"could not post {CONVERGE_PUSHED} on {story_id} ({exc})"
                )
            else:
                posted = True
        if not complete_gate:
            return posted, completed, problems
        # The gate half never raises past here: the finding may have just
        # LANDED, and losing that answer would have the caller record nothing
        # and the next run post the audit a second time.
        try:
            story = await read_story(client, story_id)
        except (LithosClientError, LithosLoomError, OSError) as exc:
            problems.append(f"--complete-gate: could not read {story_id} ({exc})")
            return posted, completed, problems
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
            return posted, completed, problems
        for gate in targets:
            try:
                await client.task_complete(task_id=gate.gate_id, agent=agent)
            except (LithosClientError, OSError) as exc:
                problems.append(f"could not complete gate {gate.gate_id} ({exc})")
            else:
                completed = gate.gate_id
    return posted, completed, problems


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
        _error(str(exc))
        raise typer.Exit(EXIT_CODES["bad_input"]) from exc
    except (ConvergePushRefused, LithosLoomError) as exc:
        _error(str(exc))
        raise typer.Exit(EXIT_CODES["refused"]) from exc

    # The live read, BEFORE the verdict is printed: the report is what the
    # operator authorises the push from, so a PR that has merged, closed,
    # changed head branch or moved repository must read as refused there too,
    # not only under --yes.
    check = verify_pr(facts)
    if not check.ok:
        plan = dataclasses.replace(plan, verdict=REFUSED, detail=check.problem)

    notes: list[str] = []
    pushed_sha = ""
    exit_code = EXIT_CODES["refused"] if plan.verdict == REFUSED else EXIT_CODES["ok"]
    if yes:
        # Everything that WRITES runs under one per-run lock: the push, its
        # record, and the epilogue that resumes are a read-then-act sequence,
        # and two invocations interleaving in it would post the same replies
        # and the same `[ConvergePushed]` twice.
        try:
            with _run_lock(facts.run_dir):
                pushed_sha, plan, exit_code = _write_phase(
                    cfg,
                    facts,
                    plan,
                    complete_gate=complete_gate,
                    notes=notes,
                    exit_code=exit_code,
                )
        except ConvergePushRefused as exc:
            _error(str(exc))
            raise typer.Exit(EXIT_CODES["refused"]) from exc

    # Everything printed below carries text loom did not author — the agent's
    # `failure_reason`, commit subjects (a converge run's are built from the
    # PR title), git / gh stderr, Lithos error text. An ANSI escape in any of
    # them could erase or forge the two lines the operator decides from
    # (CWE-117 / CWE-150), so every line goes through the same stripper
    # `develop deliver` uses.
    for line in report(facts, plan):
        typer.echo(sanitize_for_terminal(line))
    if pushed_sha:
        typer.echo(
            sanitize_for_terminal(
                f"  pushed {pushed_sha[:12]} → {facts.pr_head_branch}"
            )
        )
    for note in notes:
        typer.echo(sanitize_for_terminal(f"  note: {note}"))
    if json_out is not None:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(
            json.dumps(
                json_record(facts, plan, pushed_sha=pushed_sha, notes=notes), indent=2
            )
            + "\n",
            encoding="utf-8",
        )
    raise typer.Exit(exit_code)


def _write_phase(
    cfg: Any,
    facts: ConvergeRun,
    plan: PushPlan,
    *,
    complete_gate: bool,
    notes: list[str],
    exit_code: int,
) -> tuple[str, PushPlan, int]:
    """The push and everything owed after it. Returns ``(pushed_sha, plan,
    exit_code)``; *notes* is appended to in place.

    Called under :func:`_run_lock`, so the "is this epilogue still owed?" read
    and the writes that answer it cannot interleave with another invocation's
    — which is also why the run's record is re-read HERE: the copy the report
    was built from predates the lock, and another invocation may have finished
    the very epilogue this one is about to decide is owed.
    """
    try:
        facts = dataclasses.replace(read_run(facts.run_dir), story_id=facts.story_id)
    except ConvergePushRefused as exc:  # pragma: no cover - the read just worked
        notes.append(f"could not re-read the run under the lock ({exc})")
    pushed_sha = ""
    if plan.pushable:
        # The intent, BEFORE the push: killed between `git push` returning and
        # the record below, the run would otherwise carry no trace that its
        # rounds are on the PR, and the next invocation would report "already
        # pushed" over an audit that was never written.
        try:
            run_outcome.record_converge_push_intent(
                facts.run_dir, tip=plan.tip, pr_url=facts.pr_url
            )
        except OSError as exc:
            notes.append(f"could not record the push intent ({exc})")
        try:
            pushed_sha = push_to_pr_ref(
                facts.worktree,
                facts.branch,
                facts.pr_head_branch,
                expected_remote_sha=plan.remote_sha,
            )
        except (MergeRaceDetected, ForkPushUnsupported) as exc:
            # PROVEN non-landings, and the only ones: the push seam raises
            # these from its pre-push reads (the ref is absent from origin, or
            # it no longer holds the head the lease names) and from a push the
            # server itself REJECTED — a rejection is the server's own
            # report-status, so the update was not applied. Reading the ref
            # back here would see whatever the other actor left and report
            # "uncertain" about an invocation that is known to have written
            # nothing. Re-planned rather than exited on, so this refusal is
            # the SAME report and the same `--json` object as the head-moved
            # refusal the plan would have printed a moment earlier.
            plan = _refused_after_race(facts, plan, exc)
            exit_code = (
                EXIT_CODES["ok"]
                if plan.verdict == ALREADY_PUSHED
                else EXIT_CODES["refused"]
            )
        except (RuntimeError, OSError) as exc:
            # Everything else is ambiguous: the server can accept the update
            # and the connection drop before the client sees the answer. The
            # lease proves atomicity, not observability — so read the ref back
            # and classify.
            pushed_sha, problem, code = _classify_failed_push(facts, plan, exc)
            notes.append(problem)
            if not pushed_sha:
                plan = dataclasses.replace(
                    plan,
                    verdict=REFUSED if code == EXIT_CODES["refused"] else UNCERTAIN,
                    detail=problem,
                )
                exit_code = code
    if pushed_sha:
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
    elif _epilogue_owed(facts, plan, complete_gate=complete_gate):
        # The push landed on an earlier invocation but the work that FOLLOWS it
        # did not finish — a refusing transport, a Lithos outage, a SIGTERM
        # between the two. That work is required, and "already pushed" must not
        # report it as done: the recorded push is the resume point, and a run
        # whose epilogue is complete still writes nothing here.
        notes.append(
            "the push is already on the PR; finishing the epilogue it left owed"
        )
        if not facts.pushed_sha:
            # a push whose own record never landed (killed right after it):
            # write it now, from the intent the remote has confirmed
            with contextlib.suppress(OSError):
                run_outcome.record_converge_push(
                    facts.run_dir, pushed_sha=plan.tip, pr_url=facts.pr_url
                )
        notes += _post_epilogue(
            cfg, facts, pushed_sha=plan.tip, complete_gate=complete_gate
        )
    return pushed_sha, plan, exit_code


LOCK_FILE = "converge-push.lock"


@contextlib.contextmanager
def _run_lock(run_dir: Path) -> Iterator[None]:
    """Hold this run for the duration of the write phase, or refuse.

    Two ``converge-push --yes`` invocations on one run would each read "the
    epilogue is owed" before either wrote, and both would post the replies and
    the `[ConvergePushed]` finding. There is no Lithos task to claim here (a
    converge run has none — the PR is its subject), and the contention is
    local to one work dir, so the lock is a file created ``O_EXCL`` in the run
    dir holding the owner's pid.

    A lock left by a process that is **gone** (the SIGTERM this command's
    whole resume path exists for) is taken over rather than left to block
    forever — the same liveness rule ``develop prune`` applies to ``owner``.
    An unreadable or live owner refuses.
    """
    path = run_dir / LOCK_FILE
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            owner = _lock_owner(path)
            if owner is not None and pid_alive(owner):
                raise ConvergePushRefused(
                    f"another `develop converge-push` (pid {owner}) is writing "
                    f"for run {run_dir.name}. Wait for it to finish and re-run "
                    "— everything it does is recorded, so a second run picks up "
                    "exactly what is left"
                ) from None
            # stale (or unparseable, which a live process never writes): the
            # owner is provably gone, so break the lock and take it
            with contextlib.suppress(OSError):
                path.unlink()
            continue
        except OSError as exc:
            raise ConvergePushRefused(
                f"could not take the write lock for run {run_dir.name} ({exc})"
            ) from exc
        break
    try:
        with contextlib.suppress(OSError):
            os.write(fd, f"{os.getpid()}\n".encode())
        os.close(fd)
        yield
    finally:
        with contextlib.suppress(OSError):
            path.unlink()


def _lock_owner(path: Path) -> int | None:
    """The pid in the lock file, or ``None`` when it cannot be read."""
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(raw) if raw.isdigit() else None


def _refused_after_race(run: ConvergeRun, plan: PushPlan, exc: Exception) -> PushPlan:
    """Turn a proven non-landing into a REFUSED plan the renderer can print.

    Re-plans first, so the report names the head as it is NOW and measures its
    ranges against it — the operator asked for a report and a `--json` object,
    and a race is exactly when they need the fresh one.

    The fresh verdict is **kept when it is ``already pushed``**: the head that
    moved under the lease is this run's own tip, put there by a concurrent
    ``converge-push`` (or by the operator's own hand push) — the outcome the
    acceptance calls idempotent, exit 0 with nothing written, not a failure.
    Every other fresh verdict is forced to ``refused``: THIS invocation pushed
    nothing, and a re-plan that has become pushable again (the other actor
    moved the branch back) must not read as success beside a non-zero exit.
    """
    try:
        fresh = plan_push(run)
    except ConvergePushRefused as refusal:
        fresh = dataclasses.replace(plan, detail=str(refusal))
    if fresh.verdict == ALREADY_PUSHED:
        return dataclasses.replace(
            fresh,
            detail=(
                f"{fresh.detail} (this invocation's push was refused: {exc} — "
                "the tip was already there)"
            ),
        )
    detail = f"push refused: {exc}"
    if fresh.verdict == REFUSED and fresh.detail:
        detail = f"{detail} — {fresh.detail}"
    return dataclasses.replace(fresh, verdict=REFUSED, detail=detail)


def _epilogue_owed(run: ConvergeRun, plan: PushPlan, *, complete_gate: bool) -> bool:
    """Whether an EARLIER push of this run left required work unfinished.

    Only for a push **this command made**, whose tip is the one now on the PR —
    the push itself is done, so what remains is the audit the operator is
    owed: the ``[ConvergePushed]`` finding, the reviewers' threads, and (when
    asked for) the gate. Each is recorded as it lands, so a fully finished
    epilogue answers ``False`` here and ``--yes`` on an already-pushed run
    stays the no-op it is documented to be.

    Two ways a push is proved to be this command's: its completed record (with
    ``by: converge-push``), or the **intent** it writes before pushing — the
    only evidence left by a process killed between ``git push`` and its
    record, and conclusive once the remote holds that exact tip.

    An ordinary ``converge`` delivery (``by: converge``) is deliberately NOT
    resumable here: its own epilogue is the converge command's, and posting
    `[ConvergePushed]` over it would claim the operator salvaged an exhausted
    run that in fact converged.
    """
    if plan.verdict != ALREADY_PUSHED:
        return False
    ours = (
        run.pushed_by == run_outcome.PUSHED_BY_CONVERGE_PUSH
        and run.pushed_sha == plan.tip
    ) or run.intent_sha == plan.tip
    if not ours:
        return False
    if run.story_id and not run.finding_posted:
        return True
    if complete_gate and run.story_id and not run.gate_completed:
        return True
    return bool(replay_outcomes(run))


def _error(message: str) -> None:
    """One refusal line on stderr, stripped: these messages carry `git` / `gh`
    stderr (remote- and server-supplied bytes) and agent text, and an escape
    on the REFUSAL path could erase the `error:` line and forge a success line
    under this process's authority."""
    typer.secho(
        f"error: {sanitize_for_terminal(message)}", err=True, fg=typer.colors.RED
    )


def _classify_failed_push(
    run: ConvergeRun, plan: PushPlan, exc: Exception
) -> tuple[str, str, int]:
    """Read the ref back after a failed push: ``(pushed_sha, message, exit)``.

    A nonzero ``git push`` means the client did not see a success — not that
    the server did not apply the update. The three answers are kept apart, as
    ``develop deliver``'s own push does:

    * the ref is **at our tip** — the push landed and only the acknowledgement
      was lost; the epilogue (the record, the replies, the finding) is owed
      exactly as on a clean push, with a note saying what happened;
    * the ref is **exactly where it was** — a proven non-landing, and the
      refusal is the truth (exit 1, nothing written);
    * **anything else** — a third sha, or a ref that cannot be read. If that
      sha contains our tip, our commits ARE on the PR (someone appended after
      us) and the epilogue is owed; otherwise "nothing was written" is the one
      thing that cannot be asserted, so it is an UNCERTAIN exit (2) naming
      what to check, never a clean refusal.
    """
    try:
        observed = remote_head_sha(run.worktree, run.pr_head_branch)
    except (RuntimeError, OSError, subprocess.SubprocessError) as read_exc:
        return (
            "",
            f"git push reported failure ({exc}) and {run.pr_head_branch} could "
            f"not then be read ({read_exc}), so it is not known whether "
            f"{plan.tip[:12]} reached the PR. Nothing else was attempted; "
            "re-run when the remote answers — the push is append-only, so a "
            "second run is safe either way",
            EXIT_CODES["uncertain"],
        )
    if observed == plan.tip:
        return (
            plan.tip,
            f"git push reported failure ({exc}) but {run.pr_head_branch} is at "
            f"{plan.tip[:12]}: the update LANDED and only its acknowledgement "
            "was lost, so the replies and the finding below were posted",
            EXIT_CODES["ok"],
        )
    if observed == plan.remote_sha:
        return ("", f"push refused: {exc}", EXIT_CODES["refused"])
    if _remote_contains(run, tip=plan.tip, observed=observed):
        return (
            plan.tip,
            f"git push reported failure ({exc}) and {run.pr_head_branch} is now "
            f"at {observed[:12]}, which contains {plan.tip[:12]}: the update "
            "landed and another actor appended to the branch after it",
            EXIT_CODES["ok"],
        )
    return (
        "",
        f"git push reported failure ({exc}) and {run.pr_head_branch} is now at "
        f"{observed[:12] or '(absent)'} — neither the "
        f"{plan.remote_sha[:12]} it held before nor the {plan.tip[:12]} this "
        "push sent, so another actor moved the branch and it is not known "
        "whether the push landed first. Nothing else was attempted; reconcile "
        "the branch and re-run — the push is append-only, so a second run is "
        "safe either way",
        EXIT_CODES["uncertain"],
    )


def _remote_contains(run: ConvergeRun, *, tip: str, observed: str) -> bool:
    """Whether the tip the read-back actually SAW has our commit in its
    history. ``False`` for anything that could not be established — "I could
    not look" must never be reported as "your commit is there"."""
    git.fetch_refspecs(run.worktree, [f"refs/heads/{run.pr_head_branch}"])
    try:
        # asked of the OBSERVED object, never of a ref name: another fetch
        # landing meanwhile would answer this push's question about a tip
        # nobody here read.
        return git.is_ancestor(run.worktree, tip, observed)
    except (RuntimeError, OSError):
        return False


def _post_epilogue(
    cfg: Any, facts: ConvergeRun, *, pushed_sha: str, complete_gate: bool
) -> list[str]:
    """Everything owed AFTER the push: the reviewers' threads, then the story.

    Best-effort by construction — the commits are on the PR and no later
    failure may be reported as a failure to push. Each problem becomes a note
    on the command's own output (and in ``--json``), so what is owed is said
    rather than lost.

    And each step that LANDS is recorded (the replies per id in
    ``external.json``, the finding and the gate on the push record), so a step
    that did not — a transport that refused, a Lithos outage, a SIGTERM — is
    resumed by the next ``--yes`` instead of being lost behind "already
    pushed" (:func:`_epilogue_owed`).
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
                # every reply that lands is recorded, so a re-run after a lost
                # push acknowledgement answers nobody twice
                run_dir=facts.run_dir,
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
    # Skipped when a previous invocation already posted it: the finding is the
    # audit of ONE push, not of each attempt to finish its epilogue.
    post_finding = not facts.finding_posted
    want_gate = complete_gate and not facts.gate_completed
    try:
        posted, gate_id, problems = asyncio.run(
            _lithos_coro(
                cfg.orchestrator.lithos_url,
                cfg.orchestrator.agent_id,
                story_id=facts.story_id,
                summary=finding_summary(facts, pushed_sha=pushed_sha),
                run_id=facts.run_id,
                post_finding=post_finding,
                complete_gate=want_gate,
            )
        )
    except (LithosClientError, LithosLoomError, OSError, ExceptionGroup) as exc:
        # LithosLoomError covers the story read's own refusal (a story that is
        # gone). Nothing here may turn a landed push into a crash.
        return [*notes, f"Lithos: {exc}; the finding was not posted"]
    if posted or gate_id:
        with contextlib.suppress(OSError):
            run_outcome.record_converge_push(
                facts.run_dir,
                pushed_sha=pushed_sha,
                pr_url=facts.pr_url,
                finding_posted=True if posted else None,
                gate_completed=gate_id or None,
            )
    return notes + problems
