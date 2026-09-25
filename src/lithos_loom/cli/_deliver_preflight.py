"""What ``develop deliver`` resolves, and what it refuses, before it writes.

The preflight half of the command (beside :mod:`cli._deliver_facts`,
:mod:`cli._deliver_repo`, :mod:`cli._deliver_lithos` and
:mod:`cli._deliver_output`): turn the operator's key into :class:`RunFacts`,
find the checkout, name the routes whose gates a delivery may retire — and
refuse every state in which this run is not the operator's to deliver yet.

The refusals share one idea: **a stopped-looking run dir is not proof the run
is over.** The plugin writes its terminal ``state.json`` before the daemon
applies the result and raises the escalation, and neither the run's status nor
the story's claims settle that on their own — a claim is server-side state with
a TTL, and a Lithos outage longer than the TTL expires it under a producer that
is still running. So the guard is the durable handoff (the run's escalation on
the story) plus the local producer (the daemon's pidfile), and the explicit
``--branch``/``--story`` form stays the operator's own assertion.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from lithos_loom.cli._deliver_facts import RunFacts, run_facts
from lithos_loom.cli._deliver_lithos import (
    DeliverRefused,
    DeliverWrongCommand,
    StoryState,
)
from lithos_loom.config import LoomConfig
from lithos_loom.plugins.story_develop import run_outcome
from lithos_loom.runner import pidfile

__all__ = [
    "dispatch_routes",
    "refuse_if_the_run_is_still_the_daemons",
    "resolve_facts",
    "resolve_repo",
]


def resolve_facts(
    host: LoomConfig, *, run: str | None, branch: str | None, story_id: str | None
) -> RunFacts:
    """Resolve the run (or the explicit branch + story) into :class:`RunFacts`."""
    if run is None:
        if not (branch and story_id):
            raise DeliverRefused(
                "name a run (`develop deliver <run-id|task-id>`) or pass both "
                "--branch and --story"
            )
        return RunFacts(story_id=story_id, branch=branch)
    run_dir = run_outcome.resolve_run_dir(host.orchestrator.work_dir, run)
    if run_dir is None:
        if branch and story_id:
            # the work dir was reaped / never retained: the operator's own
            # branch + story stand in for it, minus the run's provenance
            return RunFacts(story_id=story_id, branch=branch)
        raise DeliverRefused(
            f"no run state for {run!r} under {host.orchestrator.work_dir} "
            "(`lithos-loom develop list` shows what is there). If the work dir "
            "is gone, pass --branch and --story"
        )
    if run_outcome.is_converge_run_dir(run_dir):
        # A converge run always HAS a PR — the one it was converging. Its
        # `state.json` names the run's own LOCAL branch and its parent dir is
        # `converge`, not a story, so delivering it would push that branch as a
        # NEW remote branch and open a SECOND PR against the story id
        # `converge`. The rounds belong on the PR they were written for.
        raise DeliverWrongCommand(
            f"{run!r} is a converge run ({run_dir}), not a stopped story run. "
            "Its commits belong on the PR it was converging, not on a second "
            f"PR: `lithos-loom develop converge-push {run}` reports what is "
            "unpushed and pushes it with --yes"
        )
    facts = run_facts(run_dir)
    if branch:
        facts = RunFacts(**{**asdict(facts), "branch": branch})
    if story_id:
        # A DIFFERENT story is a different set of acceptance criteria, and the
        # PR publishes those beside the run's verdict — so the swap is
        # recorded, and an approval the panel gave against the run's own story
        # is no longer published as one (`_deliver_facts.approval_unbound`).
        facts = RunFacts(
            **{
                **asdict(facts),
                "story_id": story_id,
                "story_overridden": story_id != facts.story_id,
            }
        )
    if facts.delivered_pr_url:
        raise DeliverRefused(
            f"run {facts.run_id} already delivered {facts.delivered_pr_url} — "
            "there is nothing to deliver by hand"
        )
    _refuse_if_run_may_be_live(run_dir, facts)
    if not facts.branch:
        raise DeliverRefused(
            f"run {facts.run_id} recorded no branch (it stopped before its "
            "worktree was cut); pass --branch if you know it"
        )
    return facts


def _refuse_if_run_may_be_live(run_dir: Path, facts: RunFacts) -> None:
    """Refuse a run that has not positively STOPPED — this command's whole
    domain is a run that is over.

    Two windows, both of which would put a hand delivery alongside a live
    process's own (two PRs, two ``pr`` gates — the claims are different
    aspects, so nothing stops them):

    * **No recorded outcome.** ``state.json`` lands only at run end
      (:func:`run_outcome.read_state`) while the run dir exists from the first
      seeded ``handoff/``, so a run dir without a status may be a run that is
      mid-round right now — and ``--branch`` would supply the very branch its
      state does not, delivering a live run's tip.
    * **Approved, delivery in flight.** ``develop()`` writes ``state.json`` the
      moment the dialogue approves, and the daemon's push / PR open /
      ``result.json`` all happen after it returns
      (:func:`run_outcome.delivery_complete` documents that window). The
      salvage this command exists for is a delivery that positively **failed**
      (#194) or whose recorded budget has **expired** (#189).

    The run-dir-less ``--branch`` + ``--story`` form stays the operator's own
    assertion for a reaped run: no on-disk state claims anything there.
    """
    if not facts.status:
        raise DeliverRefused(
            f"run {facts.run_id} has recorded no outcome — there is no "
            f"terminal {run_outcome.STATE_FILE} in {run_dir}, which the plugin "
            "writes only at run end, so this run may be mid-round right now. "
            "Delivering its branch would race the run's own delivery into a "
            "second PR and a second gate. Watch it with `lithos-loom develop "
            f"attach {facts.run_id}`. If the run is long gone and never wrote "
            "its state, deliver the branch explicitly instead: `develop "
            "deliver --branch <name> --story <id>` (no run id)"
        )
    if facts.status != run_outcome.APPROVED:
        return
    if run_outcome.delivery_failed(run_dir):
        return  # a recorded failure: exactly the salvage case
    if run_outcome.delivery_budget_expired(run_dir):
        return  # the automated delivery outlived its own budget
    raise DeliverRefused(
        f"run {facts.run_id} was APPROVED and its automated delivery has "
        "neither completed nor failed — the daemon may be pushing and opening "
        "its PR right now, and a hand delivery would race it into a second PR "
        "and a second gate. Watch it with `lithos-loom develop attach "
        f"{facts.run_id}`; deliver by hand only once it has failed or its "
        "delivery budget has expired (or name the branch explicitly with "
        "--branch/--story if you know the daemon is gone)"
    )


def _daemon_running(work_dir: Path) -> bool:
    """Whether a loom daemon is alive on this work dir (its pidfile's lock).

    The PROCESS, not its claim: a claim is server-side state with a TTL, and
    a Lithos outage longer than the TTL expires it while the run that owns it
    keeps going. The pidfile is the local, durable answer to "is there still a
    producer here", and it is the same one ``lithos-loom drain`` trusts.

    **Fails closed on a file it cannot read.** ``claim_pidfile`` truncates and
    rewrites the file *under its lock*, so a daemon booting right now shows a
    pidfile with no well-formed identity in it — and reading that as "nobody
    here" is exactly the wrong answer at exactly the wrong moment. So when the
    identity will not parse, the **lock** answers instead, and a file that
    exists while even the lock is unknowable counts as a daemon.
    """
    path = pidfile.pidfile_path(work_dir)
    identity = pidfile.read_pidfile(path)
    if identity is not None:
        return pidfile.daemon_alive(path, identity)
    held = pidfile.holder_alive(path)
    if held is not None:
        return held
    return path.exists()


def refuse_if_the_run_is_still_the_daemons(
    host: LoomConfig,
    *,
    facts: RunFacts,
    story: StoryState,
    routes: Sequence[str],
) -> None:
    """Refuse while the run's stop has not been handed over, and a daemon that
    could still be holding it is running here.

    The claim check above catches the common shape, but an absent claim is not
    proof the producer is gone: the route-runner's renew loop swallows every
    renewal failure and keeps going, so a Lithos outage longer than the claim
    TTL leaves the claim expired (and invisible) while the plugin writes its
    terminal ``state.json`` and the runner waits to apply the result. Winning
    that ordering would gate the story, complete nothing, post ``[ManualDelivery]``
    claiming the swap — and the runner would then raise its needs-human gate,
    leaving the story behind both.

    So the guard is the **durable handoff**, not the claim: this run's
    escalation must already be on the story (a gate this delivery would
    retire, or a failed-attempt marker naming the run). Three ways past it,
    each of which means there is no producer to race:

    * the story already carries a delivery of its own (an idempotent re-run,
      whose gates this delivery retired on an earlier pass);
    * **no daemon is running on this work dir** — the salvage this command
      exists for, and the common case for an operator cleaning up after a
      crash;
    * ``--branch``/``--story`` with no run id, which is the operator asserting
      the lifecycle is over in the first place.
    """
    if not facts.run_dir:
        return  # the operator's own explicit assertion
    if story.escalation_landed(run_id=facts.run_id, dispatch_routes=routes):
        return
    if story.delivery_visible(facts.run_id):
        return
    if not _daemon_running(host.orchestrator.work_dir):
        return
    raise DeliverRefused(
        f"run {facts.run_id} has stopped, but nothing on {story.story_id} "
        "records its escalation yet — no needs-human gate and no "
        "failed-attempt marker names it — and a loom daemon is running on "
        f"{host.orchestrator.work_dir}. A run writes its terminal state "
        "before the daemon applies its result, and the route's claim can "
        "expire under a run that is still going (an unreachable Lithos "
        "outlives the TTL), so an absent claim proves nothing. Delivering now "
        "would gate the story before the daemon raises its own gate and leave "
        "both standing. Wait for the [NeedsHuman] finding and re-run (or "
        "`lithos-loom drain` if the daemon is not coming) — or, if you know "
        "the run is over, assert it with `develop deliver --branch "
        f"{facts.branch} --story {story.story_id}`"
    )


def dispatch_routes(host: LoomConfig) -> tuple[str, ...]:
    """The host's configured ``[[routes]]`` names — the ALLOWLIST of routes
    whose ``human`` gate a delivery may retire.

    Named positively on purpose. The alternative (everything except the
    subsystem routes loom happens to ship today) admits the next subsystem
    that raises a gate, and the one it would admit first —
    ``external-remediation`` — is a *consent* gate whose completion re-arms a
    paid budget and cannot be undone by doing nothing. A host with no routes
    configured therefore retires nothing, and says so.
    """
    return tuple(route.name for route in getattr(host, "routes", ()) or ())


def resolve_repo(host: LoomConfig, story: StoryState) -> Path:
    """The project checkout holding the branch — ``[projects.<slug>].repo``."""
    slug = story.project
    if slug is None:
        raise DeliverRefused(
            f"story {story.story_id} names no project (`metadata.project`), so "
            "the checkout holding its branch is unknown"
        )
    project = host.projects.get(slug)
    if project is None:
        raise DeliverRefused(
            f"project {slug!r} is not mapped in this host's config — add a "
            f"[projects.{slug}] stanza with its `repo` path"
        )
    return project.repo


def refuse_bad_converge_flags(
    *,
    converge: bool,
    no_gate: bool,
    acceptance_file: Path | None,
    profile: str | None,
) -> None:
    """The ``--converge`` flag combinations that are refused before anything
    resolves, let alone writes."""
    if converge and no_gate:
        raise DeliverRefused(
            "--converge and --no-gate are exclusive: converge pushes review "
            "fixes onto a PR nothing is watching — no merge tracking, no "
            "external-review ingestion, no re-gate. Converging an unmonitored "
            "PR is `lithos-loom develop converge`'s own business; drop "
            "--no-gate to have this delivery gate the PR first"
        )
    if not converge and (acceptance_file is not None or profile is not None):
        # Silently ignoring them would have the operator believe a revised
        # acceptance file was read when nothing reviewed anything at all.
        flag = "--ac-file" if acceptance_file is not None else "--profile"
        raise DeliverRefused(
            f"{flag} only applies to the converge run --converge chains; "
            "this delivery reviews nothing. Add --converge, or drop the flag"
        )
