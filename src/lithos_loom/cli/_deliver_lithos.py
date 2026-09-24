"""The Lithos half of ``lithos-loom develop deliver`` (see :mod:`cli.deliver`).

Everything the hand delivery reads from and writes to Lithos: the story and the
gates that hold it, the gate swap itself (raise the ``pr`` gate, then complete
the stop's loom ``human`` gate — in that order, which is the whole safety
property), the durable ``[ManualDelivery]`` marker, and the short-lived-client
seams the sync Typer command drives through ``asyncio.run``. Split out so the
command module stays the five steps and their flags; the ordering rationale
lives with the code that enforces it.

Five invariants live here rather than in the command:

* **Only THIS run's own escalation is retired** (:meth:`StoryState
  .retirement`): a gate whose route is one this host actually configures —
  an allowlist, because a route nobody configured is never a stopped run's and
  the first gate a denylist would wrongly admit is ``external-remediation``,
  whose completion is the operator's CONSENT to spend another paid budget —
  *and* which names the run being delivered, because a story may match two
  dispatch routes, each with its own stopped run and its own open decision.
  Everything else is left OPEN and named in the finding; leaving a gate open
  is safe in every case, because the ``pr`` gate this delivery raised holds
  the story either way.
* **A live dispatch owns the story, not us.** A run writes its terminal
  ``state.json`` long before the daemon applies its result and raises the
  escalation, and the route holds its claim across that whole window — so the
  story's live claims are read with it (``task_status``) and a claimed story
  is refused before anything is written. Inside that window a delivery would
  gate a story whose needs-human gate does not exist yet, and the runner would
  raise it afterwards: both gates standing, and a finding claiming the swap.
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

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from lithos_loom.errors import LithosClientError, LithosLoomError
from lithos_loom.gates import (
    STORY_GATE_ID_KEY,
    STORY_HUMAN_GATE_ID_KEY,
    WAITS_ON_GATE,
    create_pr_gate_best_effort,
    is_loom_human_gate,
    is_pr_gate,
    parse_human_gate,
    parse_pr_gate,
)
from lithos_loom.subscriptions.delivery_gate import record_delivery_on_story
from lithos_loom.subscriptions.dispatch_guards import LAST_ATTEMPT_KEY_PREFIX

__all__ = [
    "DELIVERY_MARKER_KEY",
    "DELIVER_ASPECT",
    "dispatch_hold_agent",
    "DELIVER_CLAIM_TTL_MINUTES",
    "DeliverRefused",
    "DeliverUncertain",
    "GateOutcome",
    "GateRetirement",
    "HumanGateRef",
    "PrGateRef",
    "StoryState",
    "gate_delivery",
    "mark_delivery_finding",
    "read_story",
]

DELIVERY_MARKER_KEY = "manual_delivery"
"""Story-metadata key recording that this delivery's ``[ManualDelivery]``
finding was posted:
``{"run_id": …, "pr_url": …, "gated": bool, "swapped": bool}``.

Every field is contractual. ``gated`` says whether a ``pr`` gate was holding
this PR when the finding was written; ``swapped`` says whether the gate SWAP
had finished — no human gate this delivery would retire was left open. Both
are read as **floors** by :meth:`StoryState.delivery_marked`: a later run that
gates the PR, or that completes the human gate an earlier pass could not,
posts the corrected record, while a pass that achieves less than the record
already says stays silent (it corrects nothing). A marker without a key reads
as ``False``, the direction that re-posts.

On the **story**, not the gate: the story is the one object every mode reads
(``--no-gate`` raises no gate at all) and the one that outlives the gate — a
gate completed by the merge sweep would take a gate-side marker with it, and
the next run would re-post provenance for a delivery already recorded. Written
AFTER the finding (the finding-then-mark ordering the subscriptions use), so a
crash in between costs at most one duplicate finding rather than losing the
audit trail entirely — and only when the delivery **finished**, so a partial
pass stays re-postable and the run that completes it records the truth."""


def dispatch_hold_agent(agent: str) -> str:
    """The identity the **dispatch hold** is taken under — deliberately NOT
    the host's own agent id.

    A claim only excludes *another agent*: ``LithosClient.task_claim`` reports
    ``claim_failed`` when the aspect is held by someone else, and a same-agent
    claim is a re-claim that succeeds (its own docstring recommends
    process-unique ids for exactly this reason). The daemon runs under the
    configured id, so a hold taken under that id would exclude nothing — the
    route would claim straight through it and dispatch the story this delivery
    is gating.
    """
    return f"{agent}-deliver"


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
lease the git/gh phases spent. A chained ``--converge`` is the one operation
this cannot cover: see :data:`DELIVER_CHAIN_CLAIM_TTL_MINUTES`."""

DELIVER_CHAIN_CLAIM_TTL_MINUTES = 480
"""Claim lifetime for a delivery that chains ``--converge`` (PR #427 review).
The chain is a paid multi-round loop whose coder alone has an hour per turn
— a four-round run measured 1h52 — and Lithos refuses to renew a claim that
has already expired, so under the short lease every such chain ended with the
gate work skipped as a partial. Renewing on a timer under a blocking
subprocess is a second protocol; taking the lease for the longest a claim can
live (Lithos's ``claim_max_ttl_minutes`` cap) is not, and no realistic chain
outlives it. Both the ``deliver`` lease and the dispatch hold's route claims
get it. A plain delivery keeps the short lease — a claim a crashed delivery
leaves behind holds off the next one for its whole length."""


class DeliverRefused(LithosLoomError):
    """A precondition failed and nothing was written. Exits ``1``."""


class DeliverWrongCommand(LithosLoomError):
    """The run named is not this command's to deliver — another command owns
    it, and the message names that command. Nothing was written. Exits ``2``:
    a *usage* error, not a state the operator can resolve and retry here."""


class DeliverUncertain(LithosLoomError):
    """An external write may or may not have landed, and the read that would
    have settled it failed too. Never exit 1: "nothing was written" is exactly
    what this cannot be asserted. Exits ``2`` with what to re-run."""


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
    is: the *route* that raised it and the *run* it escalated.

    Both are needed to answer "is this THIS delivery's gate?". The route says
    whether a dispatch raised it at all (a subsystem's decision gate is never
    a delivery's to retire); the run says whether it was **this** dispatch —
    a story can legitimately match two routes, each with its own stopped run
    and its own open escalation.
    """

    gate_id: str
    route: str | None
    reason: str = ""
    run_id: str | None = None

    def describe(self, why: str = "") -> str:
        """How a finding names this gate — id, route, run, and (for a gate
        that was kept) why it was kept."""
        parts = [f"route {self.route or 'unrecorded'}"]
        if self.run_id:
            parts.append(f"run {self.run_id}")
        if why:
            parts.append(why)
        return f"{self.gate_id} ({', '.join(parts)})"


@dataclass(frozen=True)
class GateRetirement:
    """Which open ``human`` gates this delivery retires, and what stays.

    Selected by :meth:`StoryState.retirement`, which is where the whole rule
    lives; *routes* are the failed-attempt markers the retired gates authorise
    clearing (never every marker on the story — another route's failure record
    is that route's).
    """

    superseded: tuple[HumanGateRef, ...] = ()
    retained: tuple[str, ...] = ()
    """Gates left open, each already described with the reason it was kept."""
    routes: tuple[str, ...] = ()
    clears_human_gate_id: bool = True
    """Whether the story's ``needs_human_gate_id`` provenance may go. False
    only while it names a gate this delivery is KEEPING open: that key is then
    the last pointer to a live blocker, and dropping provenance for a gate
    that still holds the story is worse than leaving a stale key."""


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
    route_claims: tuple[str, ...] = ()
    """Live claims on the story held by something other than this command —
    i.e. a route dispatch in flight. The run writes its terminal
    ``state.json`` well before the daemon applies its result and raises the
    escalation, and the claim is held across that whole window, so it is the
    one signal that says "this run's lifecycle is not finished"."""

    def retirement(
        self, *, run_id: str, dispatch_routes: Sequence[str]
    ) -> GateRetirement:
        """Which open ``human`` gates THIS delivery retires (pure).

        Two keys, in order, both of which must say yes:

        1. **The route is a configured dispatch route** — an allowlist built
           from this host's ``[[routes]]``, not a list of the subsystem routes
           we happen to remember. A route nobody configured (a subsystem added
           later, a gate from another host) is never a stopped run's, and
           guessing wrong the other way completes a *consent* gate: an
           ``external-remediation`` gate's completion re-arms a paid budget,
           and unlike leaving a gate open it cannot be undone by doing nothing.
        2. **The gate names THIS run.** A story may match two dispatch routes,
           each with its own stopped run and its own open escalation; retiring
           route B's gate because route A's branch was delivered silently
           discards a decision nobody made. When no candidate names any run
           (an older gate, or an escalation raised without one) a **single**
           candidate is unambiguous and is retired; two are not, and neither
           goes. The run-dir-less ``--branch``/``--story`` form takes that same
           single-candidate rule, since it knows no run id at all.

        Everything not retired is returned described, with the reason, so the
        operator reads it in the finding rather than inferring it.
        """
        allowed = set(dispatch_routes)
        candidates: list[HumanGateRef] = []
        retained: list[str] = []
        for gate in self.human_gates:
            if gate.route in allowed:
                candidates.append(gate)
            else:
                retained.append(
                    gate.describe(
                        "not a dispatch route on this host — another "
                        "subsystem's escalation, yours to decide"
                    )
                )
        named = [gate for gate in candidates if gate.run_id]
        if run_id and any(gate.run_id == run_id for gate in candidates):
            superseded = [gate for gate in candidates if gate.run_id == run_id]
        elif named:
            # every candidate names a run, and none of them is ours
            superseded = []
        elif len(candidates) == 1:
            superseded = list(candidates)
        else:
            superseded = []
        chosen = {gate.gate_id for gate in superseded}
        kept_ids = {gate.gate_id for gate in self.human_gates} - chosen
        for gate in candidates:
            if gate.gate_id in chosen:
                continue
            retained.append(
                gate.describe(
                    "raised by another run"
                    if gate.run_id
                    else "cannot tell which run raised it — name the run to retire it"
                )
            )
        # Which failed-attempt markers this delivery may clear. The retired
        # gates' own routes always; every stale marker when NO dispatch-route
        # gate is open at all (nobody's live escalation is then being erased,
        # and an earlier pass of this same delivery may already have retired
        # the gate whose marker is left); nothing while a candidate gate this
        # delivery does not supersede is still open — that route's failure
        # record is that route's.
        if superseded:
            routes = sorted(
                {gate.route for gate in superseded if gate.route}
                & set(self.attempt_routes)
            )
        elif not candidates:
            routes = list(self.attempt_routes)
        else:
            routes = []
        pointer = self.metadata.get(STORY_HUMAN_GATE_ID_KEY)
        return GateRetirement(
            superseded=tuple(superseded),
            retained=tuple(retained),
            routes=tuple(routes),
            clears_human_gate_id=not (isinstance(pointer, str) and pointer in kept_ids),
        )

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
        """Every route with a failed-attempt marker on the story. Which of
        them a delivery may clear is :meth:`retirement`'s answer, not this
        one's — a marker belongs to the run that wrote it."""
        return tuple(
            sorted(
                key[len(LAST_ATTEMPT_KEY_PREFIX) :]
                for key in self.metadata
                if key.startswith(LAST_ATTEMPT_KEY_PREFIX)
            )
        )

    def escalation_landed(self, *, run_id: str, dispatch_routes: Sequence[str]) -> bool:
        """Whether the daemon has handed THIS run's stop over **and the story
        is still holding it** — the signal that no dispatch can be under way.

        Not just "the runner wrote something once": the handoff has to be
        something that is *still* keeping the story off the ready frontier,
        or the same interleaving comes back from the other end. Three shapes
        qualify, all read off the story and none of them a claim (a claim
        expires under a producer that is still alive — an unreachable Lithos
        outlives the TTL while the plugin keeps running):

        * an **open** loom ``human`` gate naming this run — the escalation
          itself, and it blocks the frontier;
        * an open gate this delivery would retire (:meth:`retirement`), which
          is how an escalation raised without a run id is recognised;
        * a failed-attempt marker naming this run **and naming no gate** —
          the marker-only ``[BlockerFailed]`` fallback, the one marker shape
          that suppresses dispatch on its own
          (:func:`~lithos_loom.subscriptions.dispatch_guards
          .declines_bootstrap_replay` declines a replay only while no
          ``gate_id`` is recorded).

        A marker that NAMES a gate is deliberately not enough: there the gate
        decides, and once the operator completes it the marker stays behind as
        pure history while the story goes back on the frontier. Reading it as
        a handoff would let a delivery run in exactly the window the route is
        walking from "ready" to "claimed" — and the run it dispatches would
        raise its own gate afterwards, over a story this command had just
        reported as delivered.
        """
        if run_id and any(gate.run_id == run_id for gate in self.human_gates):
            return True
        if self.retirement(run_id=run_id, dispatch_routes=dispatch_routes).superseded:
            return True
        if not run_id:
            return False
        return any(
            isinstance(marker, Mapping)
            and str(marker.get("run_id") or "") == run_id
            and not marker.get("gate_id")
            for key, marker in self.metadata.items()
            if key.startswith(LAST_ATTEMPT_KEY_PREFIX)
        )

    def unretired_run_gates(self, run_id: str) -> tuple[HumanGateRef, ...]:
        """Open loom ``human`` gates that could still be THIS run's escalation.

        A gate naming another run is certainly not; one naming this run
        certainly is; one naming no run might be. Nothing here depends on the
        *entitlement* :meth:`retirement` computes — that is this invocation's
        authority to complete a gate, and it moves with the host's configured
        routes and with how many unattributed candidates are open. Measuring
        "the swap is done" against the authority would call an empty
        entitlement a finished swap: the next invocation, under a config that
        does name the route, would retire the gate for real and find itself
        silenced by the record the first one wrote. Measured against the gates
        themselves, the answer only ever improves as gates are retired.
        """
        return tuple(
            gate
            for gate in self.human_gates
            if not gate.run_id or gate.run_id == run_id
        )

    def delivery_visible(self, run_id: str) -> bool:
        """Whether this story already carries a delivery of its own — an open
        ``pr`` gate, or a ``[ManualDelivery]`` marker naming this run.

        Read only as an *allowance*: it is what keeps an idempotent re-run
        (whose gates this delivery already retired) from tripping the
        lifecycle guard. Deliberately not ``pr_gate_id``, which outlives the
        gate it names and would let a re-developed story skip the guard.
        """
        marker = self.metadata.get(DELIVERY_MARKER_KEY)
        if (
            run_id
            and isinstance(marker, Mapping)
            and str(marker.get("run_id") or "") == run_id
        ):
            return True
        return bool(self.pr_gates)

    @property
    def task_text(self) -> str:
        """Title + body, the shape story-develop hands the coder (and the PR)."""
        body = self.description.strip()
        return f"{self.title}\n\n{body}" if body else self.title

    def delivery_marked(
        self, *, pr_url: str, run_id: str, gated: bool, swapped: bool
    ) -> bool:
        """Whether the story already records a ``[ManualDelivery]`` finding for
        this delivery **in this state** (:data:`DELIVERY_MARKER_KEY`).

        *gated* and *swapped* are part of the identity, not decoration — but
        as **floors**, not equalities. A ``--no-gate`` delivery's finding says
        the PR is UNMONITORED, so a later run that actually gates the same PR
        must post the corrected record rather than read that marker as
        "already said"; and a record written while the stop's human gate was
        still open does not describe the pass that finally completed it —
        step 5's contract is that the finding names the gates retired, so the
        run that retires them speaks. Neither floor runs the other way: a pass
        that achieves less than the record already carries (a later
        ``--no-gate`` over a gated delivery, a pass with nothing left to
        retire) corrects nothing and stays silent. An old marker without a key
        reads as ``False`` — the direction that re-posts.
        """
        marker = self.metadata.get(DELIVERY_MARKER_KEY)
        if not isinstance(marker, Mapping):
            return False
        return (
            marker.get("pr_url") == pr_url
            and str(marker.get("run_id") or "") == run_id
            and (bool(marker.get("gated")) or not gated)
            and (bool(marker.get("swapped")) or not swapped)
        )

    def delivery_recorded(self, gate_id: str, retirement: GateRetirement) -> bool:
        """Whether the story already carries everything THIS delivery records:
        the gate's id, the retirements it is entitled to make, and — only when
        it retired a gate — no stale needs-human provenance."""
        human_clear = (
            self.metadata.get(STORY_HUMAN_GATE_ID_KEY) is None
            if retirement.clears_human_gate_id
            else True
        )
        return (
            self.metadata.get(STORY_GATE_ID_KEY) == gate_id
            and human_clear
            and not retirement.routes
        )


async def read_story(
    client: Any, story_id: str, *, own_agents: Collection[str] = ()
) -> StoryState:
    """Read the story plus the open gates blocking it.

    The gates come from the story's incoming ``waits_on_gate`` **edges**, never
    from ``needs_human_gate_id``: that key is provenance only — stale after a
    partial write, and it names one gate where a story may carry several. Each
    human gate keeps its ``route`` and ``run_id``, which together decide
    whether this delivery supersedes it (:meth:`StoryState.retirement`).

    Read with ``task_status`` rather than ``task_get`` for one more field: the
    story's live **claims**. A route dispatch holds its claim from before the
    run starts until after its escalation is raised, so it is the signal that
    the run's lifecycle is still someone else's (see the ``route_claims``
    refusal in :mod:`cli.deliver`).
    """
    story = await client.task_status(task_id=story_id)
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
                    run_id=spec.run_id if spec is not None else None,
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
        route_claims=_live_claims(
            getattr(story, "claims", ()) or (), own_agents=own_agents
        ),
    )


def _live_claims(claims: Any, *, own_agents: Collection[str] = ()) -> tuple[str, ...]:
    """Describe the claims on the story that are NOT this command's own.

    Two of the claims on a story mid-delivery are ours: the ``deliver`` lease
    (excluded by aspect) and the **dispatch hold** — every configured route's
    aspect, claimed under :func:`dispatch_hold_agent`'s identity so that a
    daemon cannot dispatch the story behind the delivery. The hold sits on
    exactly the aspects a real dispatch would, so it can only be told apart by
    its *agent*: ``own_agents`` names this invocation's identities, and a
    claim under one of them is never a foreign dispatch (converge f6babfd8,
    correctness/f-001 — without it the command read its own hold back as a
    live run and never completed the human gate).

    A claim whose ``expires_at`` is in the past is spent and ignored; one that
    cannot be parsed counts as live (fail closed — a dispatch that may be
    running is not a dispatch that is not).
    """
    now = datetime.now(UTC)
    live: list[str] = []
    own = set(own_agents)
    for claim in claims:
        if not isinstance(claim, Mapping):
            continue
        aspect = str(claim.get("aspect") or "")
        if not aspect or aspect == DELIVER_ASPECT:
            continue
        if str(claim.get("agent") or "") in own:
            continue
        raw = claim.get("expires_at")
        if isinstance(raw, str) and raw:
            try:
                expires = datetime.fromisoformat(raw)
            except ValueError:
                expires = None
            if expires is not None:
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=UTC)
                if expires <= now:
                    continue
        live.append(f"{aspect} (agent {claim.get('agent') or '?'})")
    return tuple(live)


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
    swap_complete: bool = False
    """No open ``human`` gate that could be this run's escalation is left —
    the swap this command exists for has finished. Measured against the story's
    gates, never against this invocation's entitlement to retire them
    (:meth:`StoryState.unretired_run_gates`), so it cannot be made true by a
    config that simply declines to look. False on every path that ends early
    (a terminal story, a foreign ``pr`` gate, a gate that could not be
    raised), which is the direction that keeps the record re-postable."""
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
    dispatch_routes: Sequence[str],
) -> GateOutcome:
    """Steps 3 + 4: raise (or adopt) the ``pr`` gate, then retire this run's
    own human gate(s).

    Ordering is load-bearing: the ``pr`` gate must hold the story **before**
    any human gate is completed, or the story is momentarily on the ready
    frontier and a live runner could claim it into a second, duplicate run.
    So every path that ends without a gate for *pr_url* leaves every human
    gate open — the story stays blocked by the gate it already had, which is
    the safe direction.

    WHICH gates is :meth:`StoryState.retirement`'s rule (a configured dispatch
    route, naming this run); everything else is reported and kept.
    """
    outcome = GateOutcome()
    # Re-read under THIS client: the caller's snapshot predates the push and
    # the PR open, so a gate raised in between (a concurrent deliver, the
    # daemon) must be seen — adopting beats duplicating.
    # The dispatch hold is live on the story right now, under our hold
    # identity — name it, or this read mistakes it for a foreign run.
    live = await read_story(
        client, story.story_id, own_agents=(dispatch_hold_agent(agent),)
    )
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
    retirement = live.retirement(run_id=run_id, dispatch_routes=dispatch_routes)
    outcome.human_gates_retained = list(retirement.retained)

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
    # Only the retired gates' own failed-attempt markers are cleared: another
    # route's failure record is that route's, not this delivery's to erase.
    if not live.delivery_recorded(outcome.pr_gate_id, retirement):
        if await record_delivery_on_story(
            client,
            task_id=live.story_id,
            agent=agent,
            gate_id=outcome.pr_gate_id,
            routes=retirement.routes,
            clear_human_gate_id=retirement.clears_human_gate_id,
        ):
            outcome.story_recorded = True
        else:
            outcome.problems.append(
                "could not record pr_gate_id on the story, but the pr gate "
                "already blocks re-dispatch"
            )

    if live.route_claims:
        # A dispatch claimed the story while this delivery ran, so its own
        # escalation may not be raised yet (the runner writes the gate before
        # it releases). Completing what is open now could retire a gate the
        # run has not finished raising, or leave the story behind the one it
        # is about to raise with no record of why. The pr gate holds it either
        # way — that is the safe half, and it has been done.
        outcome.problems.append(
            "a route dispatch claimed this story while the delivery ran "
            f"({', '.join(live.route_claims)}), so NO needs-human gate was "
            "completed — its escalation may still be on its way. The pr gate "
            "holds the story; re-run once the dispatch has finished"
        )
    else:
        for human_gate_id in (gate.gate_id for gate in retirement.superseded):
            try:
                await client.task_complete(task_id=human_gate_id, agent=agent)
            except (LithosClientError, OSError) as exc:
                outcome.problems.append(
                    f"could not complete the needs-human gate {human_gate_id} "
                    f"({exc}); the story now carries two blockers — complete it "
                    "by hand (the pr gate holds the story, so this will not "
                    "re-dispatch)"
                )
            else:
                outcome.human_gates_completed.append(human_gate_id)
    # What the swap reached: no gate that could be this run's escalation is
    # still open. Read off the STORY's gates minus the ones just completed —
    # not off `retirement.superseded`, which is only this invocation's
    # authority and shrinks with the host's route config (a gate left open
    # because its route is not configured here is emphatically not a finished
    # swap; the invocation that can retire it must still be able to say so).
    outcome.swap_complete = not [
        gate
        for gate in live.unretired_run_gates(run_id)
        if gate.gate_id not in outcome.human_gates_completed
    ]
    # Read LAST: whether this delivery's provenance is already recorded
    # depends on the state it reached — a marker written by a `--no-gate` pass
    # does not describe the gated one that just completed, and one written
    # while the human gate was still open does not describe the pass that
    # completed it.
    outcome.finding_marked = live.delivery_marked(
        pr_url=pr_url,
        run_id=run_id,
        gated=outcome.pr_gate_id is not None,
        swapped=outcome.swap_complete,
    )
    return outcome


async def mark_delivery_finding(
    client: Any,
    *,
    story_id: str,
    pr_url: str,
    run_id: str,
    gated: bool,
    swapped: bool,
    agent: str,
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
        metadata={
            DELIVERY_MARKER_KEY: {
                "run_id": run_id,
                "pr_url": pr_url,
                # part of the identity: a later run that GATES this PR, or
                # that completes the human gate this pass left open, must post
                # the corrected record rather than read this one as "said"
                "gated": gated,
                "swapped": swapped,
            }
        },
    )
