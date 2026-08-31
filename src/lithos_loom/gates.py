"""Gate domain helpers — the ``pr`` gate (Epic H) and the ``human`` gate (b91177d2).

A gate is a first-class Lithos task (``task_type="gate"``) joined to the story
it withholds by a ``waits_on_gate`` edge, so the story is *structurally*
blocked — absent from ``lithos_task_ready`` — until the gate is completed.
Two kinds live here:

- A **`pr` gate** (``metadata.gate_type="pr"``) models "PR raised, awaiting
  human merge". The github-watcher completes it on merge. It replaced the
  ``metadata.loom_delivered`` flag (retired in US11: the gate is the sole
  "awaiting merge" state).
- A **`human` gate raised by loom** (``gate_type="human"``, ``raised_by="loom"``)
  models "loom stopped and needs a decision" — the needs-human escalation
  convention. The route-runner raises one on every non-delivering run; the
  operator completes it to re-dispatch the story (or cancels the *story* to
  abandon). It carries a closed-vocabulary ``escalation_reason``, a one-line
  ``escalation_summary`` and a nested ``run_brief`` so the list views
  (``lithos-loom gates``, lens's Gates section, the Obsidian projection) are
  triageable at a glance. Operators raise their own ``human`` gates too; the
  ``raised_by`` provenance keeps loom's automation off those.

This module is the single home for each gate's shape: its creation, and the
metadata/edge reads the resolvers need. Gates are created via ``task_create``
and resolved via ``task_complete`` — there is no dedicated MCP tool. Every
field and error code here was pinned against the live Lithos server (see
[[lithos-schema-status]]): a gate requires a ``gate_type`` in
``ci|external_task|human|pr|timer``, and a ``waits_on_gate`` edge is rejected
``not_a_gate`` unless its ``from_task`` really is a gate.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from lithos_loom.errors import LithosClientError
from lithos_loom.github_client import parse_github_ref
from lithos_loom.lithos_client import Task, TaskClient

logger = logging.getLogger(__name__)

__all__ = [
    "ESCALATION_REASONS",
    "ESCALATION_SUMMARY_MAX_CHARS",
    "GATE_TYPE_HUMAN",
    "GATE_TYPE_PR",
    "NEEDS_HUMAN_TAG",
    "RAISED_BY_LOOM",
    "STORY_GATE_ID_KEY",
    "STORY_HUMAN_GATE_ID_KEY",
    "WAITS_ON_GATE",
    "GateWriter",
    "HumanGateSpec",
    "PrGateSpec",
    "create_human_gate",
    "create_human_gate_best_effort",
    "create_pr_gate",
    "create_pr_gate_best_effort",
    "human_gate_brief",
    "is_human_gate",
    "is_loom_human_gate",
    "is_pr_gate",
    "parse_human_gate",
    "parse_pr_gate",
    "waiter_of",
]

GATE_TYPE_PR = "pr"
"""``metadata.gate_type`` value for a PR-merge gate."""

GATE_TYPE_HUMAN = "human"
"""``metadata.gate_type`` value for a gate a person resolves."""

RAISED_BY_LOOM = "loom"
"""``metadata.raised_by`` value marking a ``human`` gate as loom's escalation
(as opposed to a gate the operator created by hand)."""

NEEDS_HUMAN_TAG = "needs-human"
"""Tag on every loom-raised ``human`` gate, for operator queries."""

WAITS_ON_GATE = "waits_on_gate"
"""Edge type joining a gate (from) to its blocked waiter (to)."""

STORY_GATE_ID_KEY = "pr_gate_id"
"""Story-side provenance marker: the id of the ``pr`` gate owning this task's
merge→complete lifecycle.

The gate + its ``waits_on_gate`` edge are the authoritative state; this is the
inverse link recorded on the story so an operator can see which gate withholds
it without walking edges. (Before US11 its *presence* also told the legacy
``develop_pr_url`` sweep to stand aside; that sweep and ``loom_delivered`` are
now gone, so this is provenance only.)"""

STORY_HUMAN_GATE_ID_KEY = "needs_human_gate_id"
"""Story-side provenance marker: the id of the loom-raised ``human`` gate
currently withholding this story. Provenance ONLY, never a guard — the gate's
``waits_on_gate`` edge is what keeps the story off the ready frontier, and the
runner clears this key when it dispatches the story again."""

ESCALATION_SUMMARY_MAX_CHARS = 200
"""Cap on ``escalation_summary`` so the flat key stays one list-view line."""

ESCALATION_REASONS: frozenset[str] = frozenset(
    {
        # story-develop's own stop statuses, verbatim
        "max_rounds",
        "stalled",
        "disputed",
        "cost_exceeded",
        # a `failed` DevelopResult, split by which turn died
        "coder_failed",
        "reviewer_failed",
        "failed",
        # an agent-subprocess infra death (auth 401, stream disconnect — 5dbeb0c8)
        "infra",
        # the approved branch could not be delivered as a PR
        "delivery",
        # the runner never got a usable result: missing / malformed result.json
        "contract_violation",
        # the plugin overran the route's max_runtime_seconds
        "timeout",
        # a usage-limited run exhausted its T10 re-dispatch budget
        "resume_exhausted",
        # github-watcher: the delivered PR closed unmerged / was deleted (04c2448b)
        "pr_closed_unmerged",
        "pr_gone",
        "unknown",
    }
)
"""The closed vocabulary of ``metadata.escalation_reason`` values.

Closed on purpose: operator queries (``metadata_match``), the ``gates`` CLI
and lens badges key on it, so a caller passing anything else fails at the
call site (``ValueError``) rather than landing a mystery on the board.
"""


class GateWriter(Protocol):
    """The three Lithos calls gate creation needs (a structural subset of
    :class:`~lithos_loom.lithos_client.TaskClient`)."""

    async def task_create(
        self,
        *,
        title: str,
        agent: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        task_type: str | None = None,
        parent_task_id: str | None = None,
        depends_on: list[str] | None = None,
    ) -> str: ...

    async def task_edge_upsert(
        self,
        *,
        from_task_id: str,
        to_task_id: str,
        type: str,
        agent: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None: ...

    async def task_cancel(self, *, task_id: str, agent: str | None = None) -> Any: ...


@dataclass(frozen=True)
class PrGateSpec:
    """The PR a ``pr`` gate watches, read back from its metadata."""

    repo: str
    pr_number: int
    pr_url: str


@dataclass(frozen=True)
class HumanGateSpec:
    """What a loom-raised ``human`` gate escalated, read back from its metadata."""

    reason: str
    summary: str
    route: str | None
    story_id: str | None
    run_id: str | None
    brief: Mapping[str, Any]


async def _create_gate_with_edge(
    client: GateWriter,
    *,
    story_id: str,
    title: str,
    metadata: dict[str, object],
    agent: str,
    description: str | None = None,
    tags: list[str] | None = None,
) -> str:
    """Create a gate task and its ``waits_on_gate`` edge to *story_id*,
    all-or-nothing from the caller's point of view.

    On a Lithos failure *after* the gate task is created but before the edge
    lands, the orphan gate is best-effort cancelled so the open-gate set never
    accrues a gate with no waiter, then the original error propagates.
    Gate-type-agnostic: the ``pr`` and ``human`` creators supply the shape.
    """
    gate_id = await client.task_create(
        title=title,
        agent=agent,
        description=description,
        tags=tags,
        metadata=metadata,
        task_type="gate",
    )
    try:
        await client.task_edge_upsert(
            from_task_id=gate_id,
            to_task_id=story_id,
            type=WAITS_ON_GATE,
            agent=agent,
        )
    except (OSError, LithosClientError):
        # A gate with no waiter blocks nothing and would linger in the open-gate
        # set; drop it so creation is all-or-nothing from the caller's POV.
        with contextlib.suppress(OSError, LithosClientError):
            await client.task_cancel(task_id=gate_id, agent=agent)
        raise
    return gate_id


# ── pr gates (Epic H) ────────────────────────────────────────────────────


async def create_pr_gate(
    client: GateWriter,
    *,
    story_id: str,
    story_title: str,
    pr_url: str,
    project: str | None,
    agent: str,
) -> str:
    """Create a ``pr`` gate for *story_id*'s delivered PR and link it.

    Returns the new gate's id. Raises ``ValueError`` when *pr_url* is not a
    parseable GitHub PR url (the caller cannot build a resolvable gate without
    ``repo`` + ``pr_number``). See :func:`_create_gate_with_edge` for the
    all-or-nothing contract.
    """
    ref = parse_github_ref(pr_url)
    if ref is None or ref.kind != "pull":
        raise ValueError(f"not a GitHub PR url: {pr_url!r}")

    metadata: dict[str, object] = {
        "gate_type": GATE_TYPE_PR,
        "repo": ref.repo,
        "pr_number": ref.number,
        "required_state": "merged",
        "pr_url": pr_url,
    }
    if project:
        metadata["project"] = project

    return await _create_gate_with_edge(
        client,
        story_id=story_id,
        title=f"Awaiting merge: {story_title}",
        metadata=metadata,
        agent=agent,
    )


async def create_pr_gate_best_effort(
    client: GateWriter,
    *,
    story_id: str,
    story_title: str,
    pr_url: object,
    project: str | None,
    agent: str,
) -> tuple[str | None, str | None]:
    """Create a ``pr`` gate for a delivered story, degrading instead of raising.

    Returns ``(gate_id, problem)``: *gate_id* is the created gate (``None`` if
    none could be made), and *problem* is an operator-facing reason the caller
    can fold into a ``[Friction]`` finding (``None`` on success).

    Best-effort by design: a delivered branch + PR exist regardless of whether
    the gate lands, so a missing / non-string / malformed *pr_url*, or a failed
    write, must not fail delivery. But the gate is now the *sole* merge-tracking
    and re-dispatch guard (US11 retired ``loom_delivered`` and the legacy
    ``develop_pr_url`` sweep), so a failure has no fallback: the *problem* string
    says so loudly and the caller surfaces it as ``[Friction]``. This holds the
    "why a gate couldn't be created" classification so the caller keeps only the
    release + friction orchestration.
    """
    if not (isinstance(pr_url, str) and pr_url):
        return None, (
            "plugin reported success with no pr_url — no pr gate created; this "
            "delivered story has no merge-tracking gate and a daemon restart "
            "could re-develop it into a duplicate PR — and no external-review "
            "monitoring: reviews on this PR will not be ingested or "
            "remediated (the retired inline round is no fallback). Merge the "
            "PR or create a gate manually"
        )
    try:
        gate_id = await create_pr_gate(
            client,
            story_id=story_id,
            story_title=story_title,
            pr_url=pr_url,
            project=project,
            agent=agent,
        )
    except (ValueError, OSError, LithosClientError):
        logger.exception("creating pr gate for story %s failed", story_id)
        return None, (
            "could not create the pr gate — this delivered story has no "
            "merge-tracking gate and a daemon restart could re-develop it into "
            "a duplicate PR — and no external-review monitoring: reviews on "
            "this PR will not be ingested or remediated (the retired inline "
            "round is no fallback). Merge the PR or create a gate manually"
        )
    logger.info("created pr gate %s for story %s", gate_id, story_id)
    return gate_id, None


def is_pr_gate(task: Task) -> bool:
    """Whether *task* is a ``pr`` gate (type + ``gate_type`` metadata)."""
    return (
        task.task_type == "gate"
        and (task.metadata or {}).get("gate_type") == GATE_TYPE_PR
    )


def parse_pr_gate(task: Task) -> PrGateSpec | None:
    """Read a ``pr`` gate's watched PR out of its metadata, or ``None``.

    ``None`` when a field is missing or the wrong type — a malformed gate the
    resolver cannot act on (it surfaces that as ``[Friction]`` rather than
    guessing). ``bool`` is rejected for ``pr_number`` (it is an ``int``
    subclass, but never a valid PR number)."""
    md = task.metadata or {}
    repo = md.get("repo")
    number = md.get("pr_number")
    pr_url = md.get("pr_url")
    if (
        isinstance(repo, str)
        and repo
        and isinstance(number, int)
        and not isinstance(number, bool)
        and isinstance(pr_url, str)
        and pr_url
    ):
        return PrGateSpec(repo=repo, pr_number=number, pr_url=pr_url)
    return None


# ── human gates raised by loom (b91177d2) ────────────────────────────────


def _truncate_summary(summary: str) -> str:
    text = " ".join(summary.split()) or "no reason given"
    if len(text) <= ESCALATION_SUMMARY_MAX_CHARS:
        return text
    return text[: ESCALATION_SUMMARY_MAX_CHARS - 1] + "…"


def human_gate_brief(
    *,
    story_title: str,
    story_id: str,
    reason: str,
    summary: str,
    run_id: str | None,
    brief: Mapping[str, Any] | None,
) -> str:
    """The gate's description: the decision brief an operator reads before
    acting, so the investigation is not redone by hand.

    Mirrors what the August rescues each needed — run id, rounds, cost, branch,
    what was blocking — and states the two actions and their consequences.
    """
    b = dict(brief or {})
    lines = [
        f"Loom stopped working on **{story_title}** (`{story_id}`) "
        "and needs a decision.",
        "",
        f"**Why it stopped:** `{reason}` — {summary}",
    ]
    run_bits: list[str] = []
    if run_id:
        run_bits.append(f"run `{run_id}`")
    if b.get("rounds") is not None:
        run_bits.append(f"{b['rounds']} round(s)")
    if b.get("cost_usd") is not None:
        with contextlib.suppress(TypeError, ValueError):
            run_bits.append(f"${float(b['cost_usd']):.2f}")
    if b.get("branch"):
        run_bits.append(f"branch `{b['branch']}`")
    if run_bits:
        lines.append(f"**Run:** {', '.join(run_bits)}")
    if b.get("test_gate_verdict"):
        lines.append(f"**Test gate:** {b['test_gate_verdict']}")
    if b.get("findings_by_severity"):
        lines.append(f"**Open findings by severity:** {b['findings_by_severity']}")
    if b.get("worktree"):
        lines.append(f"**Worktree:** `{b['worktree']}`")
    if b.get("conversation_log"):
        lines.append(f"**Conversation log:** `{b['conversation_log']}`")
    for key, value in b.items():
        if key not in {
            "rounds",
            "cost_usd",
            "branch",
            "test_gate_verdict",
            "findings_by_severity",
            "worktree",
            "conversation_log",
        }:
            lines.append(f"**{key}:** {value}")
    lines += [
        "",
        "**What to do:**",
        "- Complete this gate → loom re-dispatches the story. Edit the story's "
        "description / acceptance criteria first if the brief needs sharpening.",
        "- Cancel the *story* (not this gate) → abandon it. Cancelling the gate "
        "instead strands the story: a cancelled gate can never be satisfied.",
    ]
    return "\n".join(lines)


async def create_human_gate(
    client: GateWriter,
    *,
    story_id: str,
    story_title: str,
    project: str | None,
    agent: str,
    route: str,
    reason: str,
    summary: str,
    run_id: str | None = None,
    brief: Mapping[str, Any] | None = None,
    description: str | None = None,
) -> str:
    """Raise a loom ``human`` gate on *story_id* and link it (the escalation
    primitive).

    Returns the new gate's id. Raises ``ValueError`` when *reason* is outside
    :data:`ESCALATION_REASONS`. Metadata keys are flat and named so the list
    views show reason + summary + provenance first; *brief* lands nested under
    ``run_brief`` (full detail on the gate's page, not on the row). See
    :func:`_create_gate_with_edge` for the all-or-nothing contract.
    """
    if reason not in ESCALATION_REASONS:
        raise ValueError(
            f"unknown escalation reason {reason!r}; expected one of "
            f"{sorted(ESCALATION_REASONS)}"
        )
    summary_line = _truncate_summary(summary)
    metadata: dict[str, object] = {
        "gate_type": GATE_TYPE_HUMAN,
        "raised_by": RAISED_BY_LOOM,
        "route": route,
        "story_id": story_id,
        "escalation_reason": reason,
        "escalation_summary": summary_line,
    }
    if project:
        metadata["project"] = project
    if run_id:
        metadata["run_id"] = run_id
    if brief:
        metadata["run_brief"] = dict(brief)
    tags = [NEEDS_HUMAN_TAG]
    if project:
        tags.insert(0, f"project:{project}")
    return await _create_gate_with_edge(
        client,
        story_id=story_id,
        title=f"Needs human: {story_title}",
        metadata=metadata,
        agent=agent,
        tags=tags,
        description=description
        or human_gate_brief(
            story_title=story_title,
            story_id=story_id,
            reason=reason,
            summary=summary_line,
            run_id=run_id,
            brief=brief,
        ),
    )


async def create_human_gate_best_effort(
    client: GateWriter,
    *,
    story_id: str,
    story_title: str,
    project: str | None,
    agent: str,
    route: str,
    reason: str,
    summary: str,
    run_id: str | None = None,
    brief: Mapping[str, Any] | None = None,
) -> tuple[str | None, str | None]:
    """Raise a loom ``human`` gate, degrading instead of raising.

    Returns ``(gate_id, problem)`` like :func:`create_pr_gate_best_effort`.
    A failure here means the story has NO structural blocker — the caller's
    failed-attempt marker (``dispatch_guards``) is then the only thing
    between the story and a bootstrap re-run, and the *problem* string says
    so for the ``[Friction]`` finding.
    """
    try:
        gate_id = await create_human_gate(
            client,
            story_id=story_id,
            story_title=story_title,
            project=project,
            agent=agent,
            route=route,
            reason=reason,
            summary=summary,
            run_id=run_id,
            brief=brief,
        )
    except (ValueError, OSError, LithosClientError) as exc:
        logger.exception("creating needs-human gate for story %s failed", story_id)
        return None, (
            f"could not create the needs-human gate ({exc}) — nothing "
            "structurally blocks this story, so only the failed-attempt marker "
            "stops a restart from re-running it; create a human gate on it "
            "manually or edit the story to retry"
        )
    logger.info("raised needs-human gate %s for story %s", gate_id, story_id)
    return gate_id, None


def is_human_gate(task: Task) -> bool:
    """Whether *task* is a ``human`` gate — loom's or the operator's own."""
    return (
        task.task_type == "gate"
        and (task.metadata or {}).get("gate_type") == GATE_TYPE_HUMAN
    )


def is_loom_human_gate(task: Task) -> bool:
    """Whether *task* is a ``human`` gate loom raised (``raised_by=loom``).

    The distinction matters: loom's resolver, notifier and hygiene act only on
    its own gates — a gate the operator created by hand is theirs to run.
    """
    return (
        is_human_gate(task) and (task.metadata or {}).get("raised_by") == RAISED_BY_LOOM
    )


def parse_human_gate(task: Task) -> HumanGateSpec | None:
    """Read a loom ``human`` gate's escalation out of its metadata, or ``None``
    when it carries no ``escalation_reason`` (an operator's own gate, or a
    malformed one)."""
    md = task.metadata or {}
    reason = md.get("escalation_reason")
    if not isinstance(reason, str) or not reason:
        return None
    summary = md.get("escalation_summary")
    brief = md.get("run_brief")
    return HumanGateSpec(
        reason=reason,
        summary=summary if isinstance(summary, str) else "",
        route=_opt_str(md.get("route")),
        story_id=_opt_str(md.get("story_id")),
        run_id=_opt_str(md.get("run_id")),
        brief=dict(brief) if isinstance(brief, Mapping) else {},
    )


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


# ── shared reads ─────────────────────────────────────────────────────────


async def waiter_of(client: TaskClient, gate_id: str) -> str | None:
    """The story a gate blocks — the ``to`` of its outgoing ``waits_on_gate``
    edge — or ``None`` for an orphan gate (no waiter)."""
    edges = await client.task_edge_list(
        task_id=gate_id, direction="outgoing", types=[WAITS_ON_GATE]
    )
    for edge in edges:
        return edge.to_task_id
    return None
