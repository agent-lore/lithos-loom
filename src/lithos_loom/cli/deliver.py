"""``lithos-loom develop deliver`` — deliver a stopped run's branch by hand.

A story-develop run that stops without delivering (`disputed`, `needs_decision`,
`stalled`, `max_rounds`, `cost_exceeded`, a coder / reviewer death) leaves a committable
branch behind and raises a loom ``human`` gate on the story. Every surface that
could carry that work forward wants a **PR**: ``develop converge`` refuses a
bare branch, the github-watcher's dispatchers key on a ``pr`` gate, and the one
automated way onward — ticking the human gate — re-dispatches the story *from
scratch* on a fresh branch, discarding the rounds that already landed. This
command is the third choice: turn the branch that exists into a monitored PR.

Five steps, each idempotent, in this order:

1. **Push, append-only.** Absent remote ref → ``git push -u``; already equal →
   nothing; remote an ancestor of the local branch → a plain fast-forward push;
   **diverged → refused**, naming both shas. Never a force: the branch may
   carry someone else's commits.
2. **Open or adopt the PR** through the same ``pr_delivery`` seam story-develop
   uses on approval — but only *this branch's own* PR: same repository, head at
   the sha just pushed (``gh pr list --head`` matches on the branch NAME, so a
   fork's PR is otherwise indistinguishable). A second invocation adopts and
   changes nothing; anything else refuses. Every ``gh`` call is pinned to the
   ``origin`` the branch was pushed to.
3. **Raise the ``pr`` gate** — or adopt the one already watching THIS PR — and
   record it on the story through
   :func:`~lithos_loom.subscriptions.delivery_gate.record_delivery_on_story` —
   the same write the daemon's delivering exit makes, so a hand-delivered story
   is indistinguishable from a daemon-delivered one to every later sweep, made
   whenever the live story does not already say it (a partial first pass is
   repaired, not skipped).
4. **Complete this run's own loom ``human`` gate(s)**, found from the story's
   incoming ``waits_on_gate`` edges (never from the ``needs_human_gate_id``
   provenance key, which can be stale) and narrowed to a gate raised by a
   route this host configures AND naming the run being delivered — another
   run's or another subsystem's escalation is a different decision (completing
   an ``external-remediation`` one is the operator's consent to re-arm a paid
   budget), so it is kept and named. **After** step 3, so the story is never
   momentarily on the ready frontier: the runner's readiness check then defers
   it, because a story behind a ``pr`` gate is absent from ``task_ready``.
5. **Post ``[ManualDelivery]``** on the story — the delivered sha, the PR, the
   gate that now holds the story, the gates retired and the gates kept —
   one-shot via a marker written **on the story** after the post (it must
   outlive the gate, and ``--no-gate`` raises none), so a lost finding is
   re-posted next run rather than computed away as "nothing changed". The
   marker records whether the PR ended up **gated**, so the run that gates a
   PR delivered ``--no-gate`` corrects the record instead of reading it as
   already said.

From there the PR is a first-class PR-maintenance object: landability, external
review ingestion, the base-move re-gate, the conflict resolver, merge → story
completed + dependents nudged, and the S6 admission count.

**Failure is classified by what is committed, not by which step raised.** The
record is built before the first external write and filled in as each step
lands. A refusal with nothing of this run's outside the host is exit 1; from
the first committed effect — the push — every later failure degrades into a
``[Friction]`` note and exits 2 saying what is owed, whether that is a PR that
could not be opened, a gate that did not land, a finding that would not post,
or a ``--json`` record that could not be written. The whole delivery runs under
a ``deliver`` claim on the story, renewed before the gate work, so two
invocations cannot interleave into two gates.

**The repo, not the worktree.** ``state.json`` names the branch, and the branch
ref lives in the project's own checkout whether or not the run's worktree still
exists (a salvage may well have removed it). So the run dir is used only to
*find* the branch and the story; everything else is ``git -C <repo>`` + ``gh`` +
Lithos. ``--branch`` / ``--story`` is the explicit fallback for a host with
``retain_failed_workdirs = false``.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import typer

from lithos_loom.cli._deliver_facts import (
    RunFacts,
    pr_body,
    sanitize_for_terminal,
)
from lithos_loom.cli._deliver_lithos import (
    DELIVER_ASPECT,
    DeliverRefused,
    DeliverUncertain,
    GateOutcome,
    StoryState,
    dispatch_hold_agent,
)
from lithos_loom.cli._deliver_output import (
    MANUAL_DELIVERY,
    delivery_finding,
    echo_plan,
    render,
)
from lithos_loom.cli._deliver_preflight import (
    dispatch_routes,
    refuse_if_the_run_is_still_the_daemons,
    resolve_facts,
    resolve_repo,
)
from lithos_loom.cli._deliver_repo import (
    PUSH_CREATE,
    PUSH_DIVERGED,
    PUSH_FAST_FORWARD,
    delivered_pr_head,
    open_or_adopt,
    origin_repo_name,
    pr_plan,
    push_branch,
    remote_state,
)
from lithos_loom.cli._deliver_session import (
    claim_story,
    post_finding,
    read_story_sync,
    release_story,
    renew_story,
    run_gate_delivery,
)
from lithos_loom.config import LoomConfig, load_config
from lithos_loom.errors import LithosLoomError
from lithos_loom.plugins.story_develop.pr_delivery import (
    pr_number_from_url,
    request_operator_review,
)

__all__ = ["EXIT_CODES", "MANUAL_DELIVERY", "deliver_command"]

# 0 delivered (or adopted, or a dry-run plan); 1 refused/failed with nothing
# written; 2 the PR is open but the gate half did not complete — a PARTIAL
# delivery the operator must finish, so it never shares an exit code with a
# refusal that wrote nothing.
EXIT_CODES = {"delivered": 0, "refused": 1, "ungated": 2}


# ── the command ─────────────────────────────────────────────────────────


def deliver_command(
    run: str | None = typer.Argument(
        None,
        help="The stopped run to deliver: a run id or a task id (its newest "
        "run). Omit only with --branch and --story.",
    ),
    branch: str | None = typer.Option(
        None,
        "--branch",
        help="Deliver this branch instead of resolving one from a run dir "
        "(with --story). The fallback for a host that does not retain failed "
        "work dirs.",
    ),
    story: str | None = typer.Option(
        None,
        "--story",
        help="Lithos task id of the story this branch implements (default: the "
        "run dir's task). Required with --branch.",
    ),
    base: str | None = typer.Option(
        None,
        "--base",
        help="Base branch for the PR (default: the repo's default branch).",
    ),
    no_gate: bool = typer.Option(
        False,
        "--no-gate",
        help="Open the PR only: no pr gate, and the needs-human gate is left "
        "open. The PR is then UNMONITORED — no merge tracking, no "
        "external-review ingestion, no base-move re-gate.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the plan with every fact resolved and WRITE nothing — no "
        "push, no PR, no Lithos write. It does make the two read-only gh "
        "calls step 2 makes (the default base, the open-PR list), so the "
        "adopt / open / refuse decision it shows is the real one.",
    ),
    json_out: Path | None = typer.Option(
        None, "--json", help="Write the structured JSON record to this path."
    ),
    config: Path | None = typer.Option(None, "--config", help="Host config path."),
) -> None:
    """Push a stopped run's branch, open its PR, and swap the needs-human gate
    for a pr gate."""
    try:
        record = _deliver(
            load_config(config),
            run=run,
            branch=branch,
            story_id=story,
            base=base,
            no_gate=no_gate,
            dry_run=dry_run,
            json_out=json_out,
        )
    except LithosLoomError as exc:
        # DeliverRefused (a precondition), or a config that would not load —
        # either way nothing was written. Stripped like every other line this
        # command prints: a refusal message carries `git` / `gh` stderr, which
        # relays remote- and server-supplied bytes, and an ANSI escape on the
        # REFUSAL path could erase the `error:` line and forge the success line
        # under this process's authority (CWE-117 / CWE-150).
        typer.secho(
            f"error: {sanitize_for_terminal(str(exc))}", err=True, fg=typer.colors.RED
        )
        raise typer.Exit(EXIT_CODES["refused"]) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        # git / gh could not be run at all (missing binary, timeout): nothing
        # was written by the step that raised, so this is a refusal too — and
        # the message carries the failed command line and its output.
        typer.secho(
            f"error: {sanitize_for_terminal(str(exc))}", err=True, fg=typer.colors.RED
        )
        raise typer.Exit(EXIT_CODES["refused"]) from exc
    if record is None:  # --dry-run: the plan was printed, nothing to record
        raise typer.Exit(EXIT_CODES["delivered"])
    for line in render(record):
        # `notes` carry git / gh / Lithos error text, which is agent- or
        # issue-authored often enough to matter (see `_echo_plan`).
        typer.echo(sanitize_for_terminal(line))
    # Anything committed but not finished — a push with no PR, a gate that did
    # not land, a finding that would not post, a `--json` record that could not
    # be written — is a PARTIAL delivery with its own exit code. Exit 1 is only
    # ever a refusal that wrote nothing.
    raise typer.Exit(
        EXIT_CODES["delivered"] if record["complete"] else EXIT_CODES["ungated"]
    )


def _deliver(
    host: LoomConfig,
    *,
    run: str | None,
    branch: str | None,
    story_id: str | None,
    base: str | None,
    no_gate: bool,
    dry_run: bool,
    json_out: Path | None,
) -> dict[str, Any] | None:
    """The command body. Returns the JSON record, or ``None`` for a dry run.

    **Phase-aware failure.** Everything up to and including the PR open may
    raise :class:`DeliverRefused` — nothing is written yet, so the command
    exits 1 with a plain message. From the moment ``create_pr`` returns, the
    PR exists: no later failure may lose its url or hide what is owed, so
    every step after it degrades into ``notes`` and the command exits 2 with
    the url printed and the `[Friction]` posted.
    """
    facts = resolve_facts(host, run=run, branch=branch, story_id=story_id)
    agent = host.orchestrator.agent_id
    url = host.orchestrator.lithos_url
    routes = dispatch_routes(host)
    story = read_story_sync(url, agent, facts.story_id)
    if story.route_claims:
        # A route dispatch holds the story's claim from before its run starts
        # until AFTER its escalation is raised — and the run writes its
        # terminal `state.json` long before the daemon applies the result. So
        # a stopped-looking run dir is not proof the lifecycle is over: inside
        # that window this command would gate a story whose needs-human gate
        # does not exist yet, complete nothing, and leave the runner to raise
        # it afterwards — the story ends up behind BOTH gates, with the
        # finding claiming the swap was made. The claim is the one signal that
        # says "not yours yet", and it is a different aspect from ours, so
        # nothing else would stop us.
        raise DeliverRefused(
            f"story {story.story_id} is claimed by a live dispatch "
            f"({', '.join(story.route_claims)}) — the run's result is still "
            "being applied, and the needs-human gate it will raise does not "
            "exist yet. Delivering now would gate the story before that gate "
            f"appears and leave both standing. Watch it with `lithos-loom "
            f"develop attach {facts.run_id or facts.branch}` and re-run once "
            "the dispatch has released the story"
        )
    refuse_if_the_run_is_still_the_daemons(
        host, facts=facts, story=story, routes=routes
    )
    if story.status != "open" and not no_gate:
        raise DeliverRefused(
            f"story {story.story_id} is {story.status}, not open — a terminal "
            "story takes no pr gate (the PR would be the operator's own). "
            "Re-run with --no-gate to open the PR alone"
        )
    repo = resolve_repo(host, story)
    repo_name = origin_repo_name(repo)
    # the same title rule story-develop's own delivery applies
    heading = story.title.strip()
    title = heading.splitlines()[0][:90] if heading else facts.branch
    if dry_run:
        state = remote_state(repo, facts.branch)
        # Every fact resolved, nothing written: the base and the adopt /
        # open / refuse decision come from the SAME reads step 2 makes
        # (`pr_plan`), so the screen the operator approves is the decision
        # the delivery takes — not a placeholder base and a generic "adopt or
        # open". Skipped only when the push above is refused, which is where
        # the real invocation stops too: a decision it would never reach is
        # not a fact about this delivery.
        plan = (
            None
            if state.action == PUSH_DIVERGED
            else pr_plan(
                repo,
                branch=facts.branch,
                repo_name=repo_name,
                base=base,
                head_sha=state.local_sha,
                # the preview runs BEFORE the push, and a PR's head is
                # whatever origin/<branch> points at: a fast-forward carries
                # an open PR at the current remote sha to the delivered one,
                # so the plan reads it as the adoption the real step 2 makes
                moves_to_ours=(
                    state.remote_sha if state.action == PUSH_FAST_FORWARD else ""
                ),
            )
        )
        echo_plan(
            facts=facts,
            story=story,
            repo=repo,
            repo_name=repo_name,
            plan=plan,
            state=state,
            title=title,
            no_gate=no_gate,
            retirement=story.retirement(run_id=facts.run_id, dispatch_routes=routes),
        )
        return None

    # A terminal story cannot be claimed, and with --no-gate on one there is
    # no gate work to serialise. Otherwise the claim is the cross-process
    # guard: two deliveries of one story must not interleave, or both read
    # "no pr gate" before either writes one and the story ends up with two.
    claim = _Claim(url=url, agent=agent, story_id=story.story_id)
    if story.status == "open" and not claim.take():
        raise DeliverRefused(
            f"another `develop deliver` holds the {DELIVER_ASPECT} claim on "
            f"{story.story_id} — it is delivering this story right now. Wait "
            "for it to finish (the claim's TTL is short) and re-run if needed"
        )
    # …and the DISPATCH HOLD: this story's route claims, under an identity of
    # our own, held until the `pr` gate exists. The preflight reads above are
    # point-in-time — a daemon can boot, bootstrap the story and pass its
    # readiness check a moment later, and the runner's own gap between
    # "ready" and "claimed" spans several awaits. Only a claim closes that:
    # whoever holds the route aspect owns the story, and the server decides
    # which of us got there first. Released in the `finally` — and that
    # release is itself the event that re-triggers the runner's readiness
    # check, which then defers the story behind the gate we raised.
    hold = _DispatchHold(
        url=url,
        agent=dispatch_hold_agent(agent),
        story_id=story.story_id,
        routes=routes if story.status == "open" else (),
    )
    try:
        refused = hold.take()
        if refused is not None:
            raise DeliverRefused(
                f"route {refused!r} is dispatching {story.story_id} right now "
                "(its claim was taken before this delivery could hold it) — "
                "the run it starts would develop the story from scratch over "
                "the branch being delivered. Watch it with `lithos-loom "
                f"develop attach {facts.run_id or facts.branch}` and re-run "
                "once it has finished"
            )
        return _deliver_claimed(
            host,
            facts=facts,
            story=story,
            repo=repo,
            repo_name=repo_name,
            title=title,
            base=base,
            no_gate=no_gate,
            json_out=json_out,
            claim=claim,
            routes=routes,
        )
    finally:
        hold.release()
        claim.release()


@dataclass
class _DispatchHold:
    """The story's route claims, held for the length of one delivery.

    The exclusion against a **dispatch**, as opposed to :class:`_Claim`'s
    exclusion against another delivery. Every configured dispatch route is
    claimed before the push and released after the gate work, so a daemon
    that boots (or bootstraps this story) mid-delivery cannot take the story
    into a fresh run behind our back: the route-runner claims the same aspect
    before it dispatches, finds it held by another agent, logs the lost race
    and defers. The identity is :func:`dispatch_hold_agent`'s, not the host's
    — a claim only excludes another *agent*.

    Taken route by route and released the same way, so a refusal leaves
    nothing behind (the caller releases in its ``finally``).
    """

    url: str
    agent: str
    story_id: str
    routes: Sequence[str]
    held: list[str] = field(default_factory=list)

    def take(self) -> str | None:
        """Claim every route. Returns the first route already held by someone
        else (nothing was written), or ``None`` when the story is ours."""
        for route in self.routes:
            if not claim_story(self.url, self.agent, self.story_id, aspect=route):
                return route
            self.held.append(route)
        return None

    def release(self) -> None:
        for route in self.held:
            release_story(self.url, self.agent, self.story_id, aspect=route)
        self.held.clear()


@dataclass
class _Claim:
    """The story's ``deliver`` lease for the length of one delivery.

    Exclusivity is only ever *asserted* while the lease is provably ours, so
    the handle remembers whether a renewal failed: a lease that would not renew
    may already belong to another delivery, and then this process must neither
    mutate gate state (:func:`_deliver_claimed` skips it) nor **release** —
    releasing would hand the other holder's own claim away, since deliveries on
    one host share the configured agent id.
    """

    url: str
    agent: str
    story_id: str
    held: bool = False
    lost: bool = False

    def take(self) -> bool:
        self.held = claim_story(self.url, self.agent, self.story_id)
        return self.held

    def renew(self) -> bool:
        if not self.held:
            return False
        if renew_story(self.url, self.agent, self.story_id):
            return True
        # Not ours to release either: another delivery may hold it now.
        self.lost = True
        return False

    def release(self) -> None:
        if self.held and not self.lost:
            release_story(self.url, self.agent, self.story_id)


def _deliver_claimed(
    host: LoomConfig,
    *,
    facts: RunFacts,
    story: StoryState,
    repo: Path,
    repo_name: str,
    title: str,
    base: str | None,
    no_gate: bool,
    json_out: Path | None,
    claim: _Claim,
    routes: Sequence[str],
) -> dict[str, Any]:
    """Steps 1-5, under the story's ``deliver`` claim.

    The record is built BEFORE the first external write and filled in as each
    step lands, so a failure is classified by what has already been committed
    rather than by which step raised: once the push is on ``origin`` the
    command owes the operator a report, not a "nothing written" refusal.
    """
    agent = host.orchestrator.agent_id
    url = host.orchestrator.lithos_url

    notes: list[str] = []
    record: dict[str, Any] = {
        "run_id": facts.run_id,
        "story_id": story.story_id,
        "branch": facts.branch,
        "pushed": False,
        "pushed_sha": "",
        "pr_url": None,
        "pr_number": None,
        "pr_head_sha": "",
        "pr_head_moved": False,
        "adopted": False,
        "push_uncertain": False,
        "pr_uncertain": False,
        "pr_gate_id": None,
        "human_gates_completed": [],
        "human_gates_retained": [],
        "gate_complete": True,
        "changed": True,
        "complete": True,
        "notes": notes,
    }

    # 1 — push, append-only. Classified HERE, under the claim and immediately
    # before the push, and the push sends that exact object: a local process
    # that moves the branch in between can no longer have us classify one
    # commit and deliver another.
    state = remote_state(repo, facts.branch)
    record["pushed_sha"] = state.local_sha
    try:
        upstream_note = push_branch(repo, facts.branch, state)
    except DeliverUncertain as exc:
        # The push may have landed and the remote could not be re-read to
        # settle it. Anything but "nothing was written" — the one claim that
        # cannot be made — so it is a partial the operator re-runs.
        notes.append(str(exc))
        record["push_uncertain"] = True
        record["gate_complete"] = False
        record["complete"] = False
        _file_record(json_out, record, notes)
        return record
    record["pushed"] = state.action in (PUSH_CREATE, PUSH_FAST_FORWARD)
    if upstream_note:
        # The push landed; only the local tracking config did not. Reported,
        # never fatal — and never allowed to unwind the pushed state.
        notes.append(upstream_note)

    # 2 — adopt this branch's own open PR, or open one.
    try:
        pr_url, adopted = open_or_adopt(
            repo,
            branch=facts.branch,
            repo_name=repo_name,
            base=base,
            head_sha=state.local_sha,
            title=title,
            body=lambda: pr_body(
                facts=facts,
                story=story,
                repo_name=repo_name,
                # the revision being delivered: a recorded approval is
                # published as one only if it was given on THIS head
                head_sha=state.local_sha,
            ),
        )
    except (
        DeliverUncertain,
        DeliverRefused,
        OSError,
        subprocess.SubprocessError,
    ) as exc:
        if not record["pushed"] and not isinstance(exc, DeliverUncertain):
            raise  # nothing of ours is on the remote — a plain refusal
        if isinstance(exc, DeliverUncertain):
            # A PR may exist. Every line about it must stay in the subjunctive
            # — an operator told "no PR was opened" opens a second one by hand.
            record["pr_uncertain"] = True
            notes.append(
                f"{exc} The story is NOT gated either way; re-run to settle it"
            )
        else:
            # The branch IS on origin and there is positively no PR of ours.
            notes.append(
                f"no PR was opened or adopted ({exc}); the story is NOT gated. "
                "Re-run to finish — the push is append-only and an existing PR "
                "at this head is adopted, so a second run duplicates nothing"
            )
        record["gate_complete"] = False
        record["complete"] = False
        _file_record(json_out, record, notes)
        return record
    record["pr_url"] = pr_url
    record["adopted"] = adopted
    try:
        record["pr_number"] = pr_number_from_url(pr_url)
    except RuntimeError as exc:
        notes.append(f"could not read the PR number from {pr_url} ({exc})")

    # 2b — read back the revision GitHub actually put behind the PR. `gh pr
    # create` opens from a head BRANCH (the API takes no sha), so an actor who
    # advances origin/<branch> between the push and the create gets their
    # commit into a PR whose body describes ours; adoption has the same shape
    # around its list. The window cannot be closed, so it is OBSERVED: every
    # later claim about the delivered revision — the story's audit copy above
    # all — is made against this head, not against the sha we chose to push.
    record["pr_head_sha"] = delivered_pr_head(
        repo, branch=facts.branch, repo_name=repo_name, pr_url=pr_url
    )
    if not record["pr_head_sha"]:
        # "Could not ask" is not "it matches": nothing establishes that an open
        # PR still stands at the revision this delivery pushed, so the delivery
        # is PARTIAL (exit 2) and stays unmarked — the run that does read the
        # head posts the record, corrected if the head turns out to have moved.
        record["gate_complete"] = False
        record["complete"] = False
        notes.append(
            f"the head behind {pr_url} could not be read back, so the revision "
            f"it delivers is UNVERIFIED — this delivery pushed "
            f"{state.local_sha[:12]}, and nothing here confirms the PR is still "
            "open at it. Re-run to settle it; check the PR before merging"
        )
    elif record["pr_head_sha"] != state.local_sha:
        # Not a refusal: the PR exists and must be gated and reported. But it
        # is not the revision this delivery pushed, so nothing said about it
        # may rest on that — including a recorded panel approval.
        record["pr_head_moved"] = True
        # unmarked as well as partial: the story must stay re-postable, so a
        # later run that finds the PR back at the delivered revision (or at
        # another one) records that rather than being silenced by this pass
        record["gate_complete"] = False
        record["complete"] = False
        notes.append(
            f"{pr_url} is at {record['pr_head_sha'][:12]}, NOT the "
            f"{state.local_sha[:12]} this delivery pushed — the branch moved "
            "under the delivery, so the PR carries commits this run neither "
            "pushed nor described, and the body's review provenance (any "
            "recorded approval included) covers the pushed revision only. "
            "Review the PR head before merging"
        )

    # Best-effort notify (#113). Never fatal, and never before the PR exists.
    section = getattr(host, "story_develop", None)
    login = getattr(section, "operator_github_login", None) if section else None
    pr_number = record["pr_number"]
    notify = bool(login) and not adopted and isinstance(pr_number, int)
    if notify and request_operator_review(repo_name, int(pr_number), str(login)) == (
        "failed"
    ):
        notes.append(f"could not notify @{login} of the PR")

    # 3 + 4 — the gate swap. A transport failure here is NOT a refusal: the PR
    # is open, so it degrades to a note and the partial exit code.
    outcome: GateOutcome | None = None
    if not no_gate and claim.held and not claim.renew():
        # Exclusivity could not be proved, so the gate work does not run: a
        # lease that would not renew may already belong to another delivery,
        # and creating a gate under it is exactly the duplicate the claim
        # exists to prevent. The PR stands and the human gate stays — a safe
        # partial that a later invocation (holding a real claim) finishes.
        notes.append(
            "could not renew the deliver claim, so the gate work was SKIPPED — "
            "another delivery may hold the story. The PR is open and the "
            "needs-human gate still holds the story; re-run to finish"
        )
        record["gate_complete"] = False
        record["complete"] = False
    elif not no_gate:
        try:
            outcome = run_gate_delivery(
                url,
                agent,
                story=story,
                pr_url=pr_url,
                run_id=facts.run_id,
                dispatch_routes=routes,
            )
        except DeliverRefused as exc:
            notes.append(
                f"the PR is open but the gate could not be raised ({exc}) — the "
                "story is NOT gated: nothing tracks this PR's merge and the "
                "needs-human gate still holds the story. Re-run to finish"
            )
            record["gate_complete"] = False
            record["complete"] = False
        else:
            record["pr_gate_id"] = outcome.pr_gate_id
            record["human_gates_completed"] = list(outcome.human_gates_completed)
            record["human_gates_retained"] = list(outcome.human_gates_retained)
            # AND-ed, never assigned: an earlier step may already have found
            # this delivery unfinished (a PR head that could not be read, or
            # one that is not the revision pushed), and a clean gate phase
            # does not settle that — the marker must not be written over it.
            record["gate_complete"] = record["gate_complete"] and not outcome.problems
            record["complete"] = record["complete"] and record["gate_complete"]
            notes.extend(outcome.problems)
    # --no-gate needs no note here: it is a choice, not friction, and
    # `delivery_finding` says UNMONITORED in the finding's own body. Keeping
    # `notes` to real problems is what lets a repeat invocation stay silent.

    # 5 — the provenance finding, when this run changed something OR the story
    # does not yet record that a finding was posted for this delivery. The
    # marker (written on the STORY, after the post) is what makes a crash
    # between steps 4 and 5 recoverable: the next run sees it missing and
    # posts, rather than computing "nothing changed" and losing the audit.
    # ONE rule for the provenance finding: post it unless the STORY already
    # records a COMPLETE delivery of this (run, PR). The marker is written only
    # when the delivery finished (below), so a partial first pass leaves none
    # and the run that completes it posts the corrected record — while a re-run
    # that finds nothing left to do posts nothing, whatever changed in between.
    # Deliberately NOT `changed or …`: a repair pass that fixes gate state must
    # not manufacture a duplicate of a finding the story already carries.
    # The state this delivery LEFT BEHIND — not what this invocation did. A
    # `--no-gate` pass (or one whose gate phase was skipped) over a PR that is
    # already behind its own `pr` gate must neither call that PR UNMONITORED
    # nor write a marker saying so: the story would then durably contradict
    # its own open gate. So the live story's gates count too, matched on the
    # PR this delivery is about.
    live_gate_id = next(
        (gate.gate_id for gate in story.pr_gates if gate.pr_url == pr_url), None
    )
    gate_id = (outcome.pr_gate_id if outcome is not None else None) or live_gate_id
    gated = gate_id is not None
    if record["pr_gate_id"] is None:
        record["pr_gate_id"] = live_gate_id
    # …and whether the SWAP is finished, which is the other half of what step 5
    # promises to name. A pass that leaves an open gate that could be this
    # run's escalation must not mark the delivery as told — whether it left it
    # deliberately (`--no-gate`), because its gate phase failed, or because
    # this host's config gave it no authority to retire it: the run that
    # finally retires that gate is the one whose record says so.
    swapped = (
        outcome.swap_complete
        if outcome is not None
        else not story.unretired_run_gates(facts.run_id)
    )
    marked = (
        outcome.finding_marked
        if outcome is not None
        else story.delivery_marked(
            pr_url=pr_url, run_id=facts.run_id, gated=gated, swapped=swapped
        )
    )
    record["changed"] = bool(
        record["pushed"]
        or not adopted
        or (
            outcome is not None
            and (
                outcome.gate_created
                or outcome.story_recorded
                or outcome.human_gates_completed
            )
        )
    )
    if not marked:
        summary = delivery_finding(
            facts=facts,
            record=record,
            outcome=outcome,
            notes=notes,
            no_gate=no_gate,
            live_gate_id=live_gate_id,
        )
        try:
            post_finding(
                url,
                agent,
                story.story_id,
                summary,
                pr_url=pr_url,
                run_id=facts.run_id,
                gated=gated,
                swapped=swapped,
                # mark only a delivery that finished: a partial one must stay
                # re-postable, so the run that completes it records the truth
                mark=bool(record["gate_complete"]),
            )
        except DeliverRefused as exc:
            notes.append(
                f"could not post {MANUAL_DELIVERY} on the story ({exc}); re-run "
                "to leave the provenance"
            )
            record["gate_complete"] = False
            record["complete"] = False
    _file_record(json_out, record, notes)
    return record


def _file_record(
    json_out: Path | None, record: dict[str, Any], notes: list[str]
) -> None:
    """Write the ``--json`` record, if one was asked for.

    A record the operator asked for and did not get is a partial result, not a
    footnote: the delivery stands, but the output they will script against is
    missing, so it lowers ``complete`` (exit 2) as well as leaving a note.
    """
    if json_out is None:
        return
    try:
        _write_json(json_out, record)
    except OSError as exc:
        notes.append(f"could not write the JSON record to {json_out} ({exc})")
        record["complete"] = False


def _write_json(json_out: Path | None, record: Mapping[str, Any]) -> None:
    if json_out is None:
        return
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(dict(record), indent=2), encoding="utf-8")
