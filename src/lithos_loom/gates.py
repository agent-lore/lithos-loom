"""PR-gate domain helpers (Epic H).

A **`pr` gate** models "PR raised, awaiting human merge" as a first-class
Lithos task (``task_type="gate"``, ``metadata.gate_type="pr"``) joined to the
story by a ``waits_on_gate`` edge. The story is then *structurally* blocked
until the gate is resolved — the github-watcher completes the gate on merge —
replacing the ``metadata.loom_delivered`` flag the runner previously used
(retired in US11: the gate is now the sole "awaiting merge" state).

This module is the single home for the gate's shape: its creation, and the
metadata/edge reads the resolver needs. Gates are created via ``task_create``
and resolved via ``task_complete`` — there is no dedicated MCP tool. Every
field and error code here was pinned against the live Lithos server (see
[[lithos-schema-status]]): a gate requires a ``gate_type`` in
``ci|external_task|human|pr|timer``, and a ``waits_on_gate`` edge is rejected
``not_a_gate`` unless its ``from_task`` really is a gate.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass

from lithos_loom.errors import LithosClientError
from lithos_loom.github_client import parse_github_ref
from lithos_loom.lithos_client import Task, TaskClient

logger = logging.getLogger(__name__)

__all__ = [
    "GATE_TYPE_PR",
    "STORY_GATE_ID_KEY",
    "WAITS_ON_GATE",
    "PrGateSpec",
    "create_pr_gate",
    "create_pr_gate_best_effort",
    "is_pr_gate",
    "parse_pr_gate",
    "waiter_of",
]

GATE_TYPE_PR = "pr"
"""``metadata.gate_type`` value for a PR-merge gate."""

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


@dataclass(frozen=True)
class PrGateSpec:
    """The PR a ``pr`` gate watches, read back from its metadata."""

    repo: str
    pr_number: int
    pr_url: str


async def create_pr_gate(
    client: TaskClient,
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
    ``repo`` + ``pr_number``). On a Lithos failure *after* the gate task is
    created but before the edge lands, the orphan gate is best-effort cancelled
    so the open-gate set never accrues a gate with no waiter, then the original
    error propagates.
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

    gate_id = await client.task_create(
        title=f"Awaiting merge: {story_title}",
        agent=agent,
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
        # set; drop it so create_pr_gate is all-or-nothing from the caller's POV.
        with contextlib.suppress(OSError, LithosClientError):
            await client.task_cancel(task_id=gate_id, agent=agent)
        raise
    return gate_id


async def create_pr_gate_best_effort(
    client: TaskClient,
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
            "could re-develop it into a duplicate PR. Merge the PR or create a "
            "gate manually"
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
            "a duplicate PR. Merge the PR or create a gate manually"
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


async def waiter_of(client: TaskClient, gate_id: str) -> str | None:
    """The story a gate blocks — the ``to`` of its outgoing ``waits_on_gate``
    edge — or ``None`` for an orphan gate (no waiter)."""
    edges = await client.task_edge_list(
        task_id=gate_id, direction="outgoing", types=[WAITS_ON_GATE]
    )
    for edge in edges:
        return edge.to_task_id
    return None
