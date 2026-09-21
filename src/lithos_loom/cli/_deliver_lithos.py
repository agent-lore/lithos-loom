"""The Lithos half of ``lithos-loom develop deliver`` (see :mod:`cli.deliver`).

Everything the hand delivery reads from and writes to Lithos: the story and the
gates that hold it, the gate swap itself (raise the ``pr`` gate, then complete
the stop's loom ``human`` gate — in that order, which is the whole safety
property), the durable ``[ManualDelivery]`` marker, and the short-lived-client
seams the sync Typer command drives through ``asyncio.run``. Split out so the
command module stays the five steps and their flags; the ordering rationale
lives with the code that enforces it.

Four invariants live here rather than in the command:

* **Only the stopped run's own escalation is retired.** Loom raises ``human``
  gates from several subsystems, and the route says whose escalation a gate
  is. A **dispatch** route's gate says "this story's run stopped", and that is
  exactly what a delivery supersedes. The others say something else — and
  completing an ``external-remediation`` decision gate is the operator's
  CONSENT to spend another remediation budget on the delivered PR, which this
  command was never granted. So a gate raised by one of loom's own subsystems
  (:data:`~lithos_loom.gates.SUBSYSTEM_ROUTES`) is left OPEN and named in the
  finding; leaving it open is safe in every case, because the ``pr`` gate this
  delivery raised holds the story either way.
* **The gate must watch THIS PR.** An open ``pr`` gate is adopted only when
  its ``pr_url`` is the PR being delivered. A gate watching a *different* PR —
  even beside one that matches — means the story is already behind someone
  else's delivery: the command refuses rather than pointing the maintenance
  machine at the wrong PR while retiring the story's escalation.
* **The story write is repaired, not assumed.** ``record_delivery_on_story``
  runs whenever the LIVE story does not already say what a delivered story
  says — so a first pass that created the gate but lost the metadata write is
  finished by the next run, instead of being skipped because the gate exists.
* **The decision is made on a fresh read.** The command reads the story long
  before it pushes and opens the PR; the gate decision re-reads under the same
  client, so a gate that appeared in between is adopted rather than duplicated.
  (The cross-process guard is the story claim — :func:`claim_story`.)

:class:`DeliverRefused` lives here rather than in the command because both
halves raise it — the git half refuses a diverged branch, this half refuses an
unreadable story — and the command maps it onto one exit code.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from lithos_loom.errors import LithosClientError, LithosLoomError
from lithos_loom.gates import (
    STORY_GATE_ID_KEY,
    STORY_HUMAN_GATE_ID_KEY,
    WAITS_ON_GATE,
    create_pr_gate_best_effort,
    is_dispatch_route,
    is_loom_human_gate,
    is_pr_gate,
    parse_human_gate,
    parse_pr_gate,
)
from lithos_loom.lithos_client import LithosClient
from lithos_loom.subscriptions.delivery_gate import record_delivery_on_story
from lithos_loom.subscriptions.dispatch_guards import LAST_ATTEMPT_KEY_PREFIX

__all__ = [
    "DELIVERY_MARKER_KEY",
    "DELIVER_ASPECT",
    "DeliverRefused",
    "GateOutcome",
    "HumanGateRef",
    "PrGateRef",
    "StoryState",
    "DELIVER_CLAIM_TTL_MINUTES",
    "claim_story",
    "gate_delivery",
    "mark_delivery_finding",
    "post_finding",
    "read_story",
    "read_story_sync",
    "release_story",
    "renew_story",
    "run_gate_delivery",
]

DELIVERY_MARKER_KEY = "manual_delivery"
"""Story-metadata key recording that this delivery's ``[ManualDelivery]``
finding was posted: ``{"run_id": …, "pr_url": …}``.

On the **story**, not the gate: the story is the one object every mode reads
(``--no-gate`` raises no gate at all) and the one that outlives the gate — a
gate completed by the merge sweep would take a gate-side marker with it, and
the next run would re-post provenance for a delivery already recorded. Written
AFTER the finding (the finding-then-mark ordering the subscriptions use), so a
crash in between costs at most one duplicate finding rather than losing the
audit trail entirely — and only when the delivery **finished**, so a partial
pass stays re-postable and the run that completes it records the truth."""

DELIVER_ASPECT = "deliver"
"""Claim aspect serialising concurrent deliveries of the same story. Two
``deliver`` processes that both read "no pr gate" before either pushed would
otherwise each create one; the claim is the same cross-process primitive the
route-runner uses to win a dispatch race. Its own aspect, so it never contends
with a route's claim."""

DELIVER_CLAIM_TTL_MINUTES = 60
"""Claim lifetime. It must **exceed every external operation the claim
covers**, or a second invocation inherits an expired claim while the first is
still working and both decide "no gate yet". The command's own subprocess
budget alone is ~14 minutes (push 300s + PR list 120s + default branch 120s +
PR create 300s), before GitHub and Lithos latency, so the TTL is set well
above it and :func:`renew_story` re-ups the lease before the gate work — the
window that actually needs exclusivity — so the gate phase never runs on a
lease the git/gh phases spent."""


class DeliverRefused(LithosLoomError):
    """A precondition failed and nothing was written. Exits ``1``."""


@dataclass(frozen=True)
class PrGateRef:
    """An open ``pr`` gate holding the story, and what it watches."""

    gate_id: str
    pr_url: str
    """The PR this gate watches (``""`` when its metadata is unparseable — a
    malformed gate is never treated as watching ours)."""


@dataclass(frozen=True)
class HumanGateRef:
    """An open loom ``human`` gate holding the story, and whose escalation it
    is.

    *route* is the discriminator (:func:`is_dispatch_route`): a dispatch
    route's gate is the stopped run's own escalation, which this delivery
    supersedes; any other is another subsystem's and is left alone.
    """

    gate_id: str
    route: str | None
    reason: str = ""

    @property
    def supersedable(self) -> bool:
        """Whether delivering this branch retires this gate."""
        return is_dispatch_route(self.route)

    def describe(self) -> str:
        """``<id> (route <route>)`` — how the finding names a gate it kept."""
        return f"{self.gate_id} (route {self.route or 'unrecorded'})"


@dataclass(frozen=True)
class StoryState:
    """The live story, and the gates that hold it."""

    story_id: str
    title: str
    description: str
    status: str
    metadata: Mapping[str, Any]
    human_gates: tuple[HumanGateRef, ...]
    pr_gates: tuple[PrGateRef, ...] = ()
    """Open ``pr`` gates already blocking the story — a previous ``deliver``,
    or the daemon's own delivery. Their ``pr_url`` is what decides whether one
    is ours to adopt."""

    @property
    def superseded_human_gates(self) -> tuple[HumanGateRef, ...]:
        """The stopped run's own escalations — this delivery's to complete."""
        return tuple(gate for gate in self.human_gates if gate.supersedable)

    @property
    def retained_human_gates(self) -> tuple[HumanGateRef, ...]:
        """Another subsystem's escalations: left OPEN, named in the finding.

        Chief among them the ``external-remediation`` decision gate, whose
        completion re-arms autonomous (paid) remediation on the delivered PR —
        a decision that is the operator's alone, and one nothing in this
        command was asked to make.
        """
        return tuple(gate for gate in self.human_gates if not gate.supersedable)

    @property
    def project(self) -> str | None:
        slug = self.metadata.get("project")
        return slug if isinstance(slug, str) and slug else None

    @property
    def acceptance_criteria(self) -> str | None:
        ac = self.metadata.get("acceptance_criteria")
        return ac if isinstance(ac, str) and ac.strip() else None

    @property
    def github_issue_url(self) -> str | None:
        url = self.metadata.get("github_issue_url")
        return url if isinstance(url, str) and url else None

    @property
    def attempt_routes(self) -> tuple[str, ...]:
        """Routes whose failed-attempt marker this delivery supersedes — read
        off the story's own keys, since a stopped run records no route name on
        disk."""
        return tuple(
            sorted(
                key[len(LAST_ATTEMPT_KEY_PREFIX) :]
                for key in self.metadata
                if key.startswith(LAST_ATTEMPT_KEY_PREFIX)
            )
        )

    @property
    def task_text(self) -> str:
        """Title + body, the shape story-develop hands the coder (and the PR)."""
        body = self.description.strip()
        return f"{self.title}\n\n{body}" if body else self.title

    def delivery_marked(self, *, pr_url: str, run_id: str) -> bool:
        """Whether the story already records a ``[ManualDelivery]`` finding for
        this exact delivery (:data:`DELIVERY_MARKER_KEY`)."""
        marker = self.metadata.get(DELIVERY_MARKER_KEY)
        if not isinstance(marker, Mapping):
            return False
        return (
            marker.get("pr_url") == pr_url and str(marker.get("run_id") or "") == run_id
        )

    def delivery_recorded(self, gate_id: str) -> bool:
        """Whether the story already carries everything a delivered story
        carries: this gate's id, and neither retirement left behind."""
        return (
            self.metadata.get(STORY_GATE_ID_KEY) == gate_id
            and self.metadata.get(STORY_HUMAN_GATE_ID_KEY) is None
            and not self.attempt_routes
        )


async def read_story(client: Any, story_id: str) -> StoryState:
    """Read the story plus the open gates blocking it.

    The gates come from the story's incoming ``waits_on_gate`` **edges**, never
    from ``needs_human_gate_id``: that key is provenance only — stale after a
    partial write, and it names one gate where a story may carry several. Each
    human gate keeps its ``route``, which is what decides whether this delivery
    supersedes it (:class:`HumanGateRef`).
    """
    story = await client.task_get(task_id=story_id)
    if story is None:
        raise DeliverRefused(f"Lithos task {story_id!r} not found")
    raw = getattr(story, "metadata", None)
    metadata: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    human_gates: list[HumanGateRef] = []
    pr_gates: list[PrGateRef] = []
    edges = await client.task_edge_list(
        task_id=story_id, direction="incoming", types=[WAITS_ON_GATE]
    )
    for edge in edges:
        gate = await client.task_get(task_id=edge.from_task_id)
        if gate is None or gate.status != "open":
            continue
        if is_loom_human_gate(gate):
            spec = parse_human_gate(gate)
            human_gates.append(
                HumanGateRef(
                    gate_id=gate.id,
                    route=spec.route if spec is not None else None,
                    reason=spec.reason if spec is not None else "",
                )
            )
        elif is_pr_gate(gate):
            spec = parse_pr_gate(gate)
            pr_gates.append(
                PrGateRef(
                    gate_id=gate.id,
                    pr_url=spec.pr_url if spec is not None else "",
                )
            )
    return StoryState(
        story_id=story_id,
        title=str(getattr(story, "title", "") or story_id),
        description=str(getattr(story, "description", "") or ""),
        status=str(getattr(story, "status", "") or ""),
        metadata=metadata,
        human_gates=tuple(human_gates),
        pr_gates=tuple(pr_gates),
    )


@dataclass
class GateOutcome:
    """What the Lithos half of the delivery managed to do."""

    pr_gate_id: str | None = None
    gate_created: bool = False
    """False when the story already had an open ``pr`` gate for THIS PR — a
    re-run adopts it rather than stacking a second blocker."""
    story_recorded: bool = False
    """This run wrote ``pr_gate_id`` + the retirements (a first delivery, or a
    repair of a partial one). False when the live story already said it."""
    finding_marked: bool = False
    """The adopted gate already records this delivery's ``[ManualDelivery]``
    finding, so it must not be posted twice."""
    human_gates_completed: list[str] = field(default_factory=list)
    human_gates_retained: list[str] = field(default_factory=list)
    """Open loom ``human`` gates another subsystem raised (described with their
    route). Left alone deliberately — not friction, so never a problem."""
    problems: list[str] = field(default_factory=list)


async def gate_delivery(
    client: Any,
    *,
    story: StoryState,
    pr_url: str,
    run_id: str,
    agent: str,
) -> GateOutcome:
    """Steps 3 + 4: raise (or adopt) the ``pr`` gate, then retire the stop's
    human gates.

    Ordering is load-bearing: the ``pr`` gate must hold the story **before**
    any human gate is completed, or the story is momentarily on the ready
    frontier and a live runner could claim it into a second, duplicate run.
    So every path that ends without a gate for *pr_url* leaves every human
    gate open — the story stays blocked by the gate it already had, which is
    the safe direction.

    Which gates are retired is decided by their **route**: the stopped run's
    own escalation (a dispatch route) and nothing else. A gate one of loom's
    subsystems raised is a different decision — an ``external-remediation``
    gate is the operator's consent to spend another remediation budget on the
    delivered PR — so it is reported and left open.
    """
    outcome = GateOutcome()
    # Re-read under THIS client: the caller's snapshot predates the push and
    # the PR open, so a gate raised in between (a concurrent deliver, the
    # daemon) must be seen — adopting beats duplicating.
    live = await read_story(client, story.story_id)
    if live.status != "open":
        # The story went terminal while this delivery pushed and opened its PR
        # (the operator completed it, or the issue mirror did). A terminal
        # story takes no gate — #372's shape: gating a done story strands an
        # open blocker nothing will resolve, and counts against admission.
        outcome.problems.append(
            f"the story became {live.status} while this delivery ran — no pr "
            "gate was created and no needs-human gate was completed. The PR "
            "stands and is now the operator's own (nothing tracks its merge)"
        )
        return outcome
    ours = [gate for gate in live.pr_gates if gate.pr_url == pr_url]
    foreign = [gate for gate in live.pr_gates if gate.pr_url != pr_url]

    outcome.finding_marked = live.delivery_marked(pr_url=pr_url, run_id=run_id)

    if foreign:
        # The story is (also) behind a delivery of a DIFFERENT PR. Checked
        # BEFORE the adopt branch, not as its `else`: a mixed state — one gate
        # for this PR, one for another — is not an idempotent re-run, and
        # completing the human gates there would leave the story's merge
        # semantics hostage to a PR that does not contain this branch. Refuse
        # and let the operator resolve the older delivery.
        watched = ", ".join(
            f"{g.gate_id} → {g.pr_url or '(unparseable)'}" for g in ours + foreign
        )
        outcome.problems.append(
            f"an open pr gate already holds this story and watches a different "
            f"PR ({watched}) — {pr_url} was NOT gated and no needs-human gate "
            "was completed. Resolve the existing delivery first (merge or "
            "close its PR, or complete its gate)"
        )
        return outcome
    if ours:
        outcome.pr_gate_id = ours[0].gate_id
        if len(ours) > 1:
            outcome.problems.append(
                "the story carries more than one open pr gate for this PR "
                f"({', '.join(g.gate_id for g in ours)}); complete the stale one"
            )
    else:
        gate_id, problem = await create_pr_gate_best_effort(
            client,
            story_id=live.story_id,
            story_title=live.title,
            pr_url=pr_url,
            project=live.project,
            agent=agent,
        )
        if problem is not None:
            outcome.problems.append(problem)
        if gate_id is None:
            # Nothing structurally holds the story, so its needs-human gate
            # stays: completing it now would put the story back on the ready
            # frontier and buy a duplicate PR.
            return outcome
        outcome.pr_gate_id = gate_id
        outcome.gate_created = True

    # The one story write — made whenever the LIVE story does not already say
    # what a delivered story says. An adopted gate whose first pass lost this
    # write is repaired here; a story that already carries it is untouched.
    if not live.delivery_recorded(outcome.pr_gate_id):
        if await record_delivery_on_story(
            client,
            task_id=live.story_id,
            agent=agent,
            gate_id=outcome.pr_gate_id,
            routes=live.attempt_routes,
        ):
            outcome.story_recorded = True
        else:
            outcome.problems.append(
                "could not record pr_gate_id on the story, but the pr gate "
                "already blocks re-dispatch"
            )

    outcome.human_gates_retained = [
        gate.describe() for gate in live.retained_human_gates
    ]
    for human_gate_id in (gate.gate_id for gate in live.superseded_human_gates):
        try:
            await client.task_complete(task_id=human_gate_id, agent=agent)
        except (LithosClientError, OSError) as exc:
            outcome.problems.append(
                f"could not complete the needs-human gate {human_gate_id} "
                f"({exc}); the story now carries two blockers — complete it by "
                "hand (the pr gate holds the story, so this will not re-dispatch)"
            )
        else:
            outcome.human_gates_completed.append(human_gate_id)
    return outcome


async def mark_delivery_finding(
    client: Any, *, story_id: str, pr_url: str, run_id: str, agent: str
) -> None:
    """Record on the **story** that this delivery's finding was posted.

    Finding-then-mark: the marker is what makes ``[ManualDelivery]`` one-shot,
    and writing it only after the post means a crash in between re-posts next
    run rather than losing the provenance. It lives on the story (not the
    gate) so ``--no-gate`` gets the same guarantee and so a gate the merge
    sweep completes cannot take the record with it.
    """
    await client.task_update(
        task_id=story_id,
        agent=agent,
        metadata={DELIVERY_MARKER_KEY: {"run_id": run_id, "pr_url": pr_url}},
    )


def run_lithos(coro: Any) -> Any:
    """Run one Lithos phase, mapping transport failures onto the refusal."""
    try:
        return asyncio.run(coro)
    except (LithosClientError, OSError, ExceptionGroup) as exc:
        # LithosClient.__aenter__ surfaces a connect failure as a plain OSError
        # or, inside a task group, an ExceptionGroup wrapping it (the `gates`
        # command's rationale). ExceptionGroup, not BaseExceptionGroup, so
        # KeyboardInterrupt / SystemExit still propagate.
        raise DeliverRefused(f"Lithos call failed: {exc}") from exc


async def _read_story_coro(url: str, agent: str, story_id: str) -> StoryState:
    async with LithosClient(url, agent_id=agent) as client:
        return await read_story(client, story_id)


async def _gate_coro(
    url: str, agent: str, *, story: StoryState, pr_url: str, run_id: str
) -> GateOutcome:
    async with LithosClient(url, agent_id=agent) as client:
        return await gate_delivery(
            client, story=story, pr_url=pr_url, run_id=run_id, agent=agent
        )


async def _post_coro(
    url: str,
    agent: str,
    story_id: str,
    summary: str,
    *,
    pr_url: str,
    run_id: str,
    mark: bool,
) -> None:
    async with LithosClient(url, agent_id=agent) as client:
        await client.finding_post(task_id=story_id, summary=summary, agent=agent)
        if mark:
            await mark_delivery_finding(
                client, story_id=story_id, pr_url=pr_url, run_id=run_id, agent=agent
            )


async def _claim_coro(url: str, agent: str, story_id: str) -> bool:
    async with LithosClient(url, agent_id=agent) as client:
        try:
            await client.task_claim(
                task_id=story_id,
                aspect=DELIVER_ASPECT,
                agent=agent,
                ttl_minutes=DELIVER_CLAIM_TTL_MINUTES,
            )
        except LithosClientError as exc:
            if exc.code == "claim_failed":
                return False
            raise
    return True


async def _renew_coro(url: str, agent: str, story_id: str) -> None:
    async with LithosClient(url, agent_id=agent) as client:
        await client.task_renew(
            task_id=story_id,
            aspect=DELIVER_ASPECT,
            agent=agent,
            ttl_minutes=DELIVER_CLAIM_TTL_MINUTES,
        )


async def _release_coro(url: str, agent: str, story_id: str) -> None:
    async with LithosClient(url, agent_id=agent) as client:
        await client.task_release(task_id=story_id, aspect=DELIVER_ASPECT, agent=agent)


def read_story_sync(url: str, agent: str, story_id: str) -> StoryState:
    """Step 0: the live story + the gates holding it."""
    return run_lithos(_read_story_coro(url, agent, story_id))


def run_gate_delivery(
    url: str, agent: str, *, story: StoryState, pr_url: str, run_id: str
) -> GateOutcome:
    """Steps 3 + 4, in one client session."""
    return run_lithos(_gate_coro(url, agent, story=story, pr_url=pr_url, run_id=run_id))


def post_finding(
    url: str,
    agent: str,
    story_id: str,
    summary: str,
    *,
    pr_url: str = "",
    run_id: str = "",
    mark: bool = True,
) -> None:
    """Step 5: post ``[ManualDelivery]``, then mark the story (in that order).

    *mark* is False for a delivery that did NOT finish: the marker is what
    silences later runs, so a partial pass must not write one — the run that
    completes the delivery posts the corrected record and marks it then.
    """
    run_lithos(
        _post_coro(
            url, agent, story_id, summary, pr_url=pr_url, run_id=run_id, mark=mark
        )
    )


def claim_story(url: str, agent: str, story_id: str) -> bool:
    """Take the ``deliver`` claim on the story; ``False`` when another process
    holds it (two deliveries of one story must not interleave)."""
    return run_lithos(_claim_coro(url, agent, story_id))


def renew_story(url: str, agent: str, story_id: str) -> bool:
    """Re-up the ``deliver`` lease before the gate work — the phase that must
    be exclusive — so it never runs on a lease the git / gh phases spent.
    ``False`` when the renewal did not land (the caller notes it; the gate
    work still runs, since refusing there would strand an open PR)."""
    try:
        run_lithos(_renew_coro(url, agent, story_id))
    except DeliverRefused:
        return False
    return True


def release_story(url: str, agent: str, story_id: str) -> None:
    """Release the ``deliver`` claim. Best-effort: a lingering claim only
    expires with its short TTL."""
    with contextlib.suppress(DeliverRefused):
        run_lithos(_release_coro(url, agent, story_id))
