"""``github-issue-sync`` subscription handler — Slice 7.1.

Consumes ``github.issue.seen`` events from the github_issue_watcher
source and reconciles each issue against Lithos:

- **New issue (no linkage marker)**: create a Lithos task, then write
  ``<!-- lithos:<task_id> -->`` into the GitHub issue body so the next
  poll recognises the linkage.
- **Existing marker → open task**: no-op (the watcher will re-emit on
  every poll until the cursor catches up).
- **Existing marker → closed-completed on GH**: ``task_complete``.
- **Existing marker → closed-not_planned on GH**: ``task_cancel``.
- **Marker deleted by operator but a Lithos task carries
  ``metadata.github_issue_url`` for this URL**: re-write the marker on
  GitHub. Don't create a duplicate task.
- **Marker points at a deleted Lithos task** (operator removed it):
  treat as new and create a fresh task + marker.

State on the Lithos task:

    title       = issue.title
    description = issue.body
    tags        = issue.labels + ["github-issue"]
    metadata    = {
      project: <slug>,
      github_issue_url: <url>,
      github_issue_number: N,
      github_labels: [<labels>],  # snapshotted for future drift sync
    }

The exclude-filter knobs (``github_issue_exclude_labels`` /
``..._authors``) from the PRD are deferred to Slice 7.2 alongside the
label-drift sync — their storage shape (tag-name escaping for labels
containing colons / brackets) is non-trivial and isn't blocking the
inbound-mirror MVP.
"""

from __future__ import annotations

import logging
from typing import Any

from lithos_loom.bus import Event
from lithos_loom.errors import LithosClientError
from lithos_loom.github_client import (
    GitHubClient,
    GitHubError,
    apply_marker,
    strip_marker,
)
from lithos_loom.lithos_client import Task
from lithos_loom.subscriptions import Handler, SubscriptionContext

__all__ = ["EVENT_TYPE", "make_handler"]

logger = logging.getLogger(__name__)

EVENT_TYPE = "github.issue.seen"
GITHUB_ISSUE_TAG = "github-issue"
"""Tag added to every Loom-created task derived from a GitHub issue.
Lets the operator filter tasks by origin without inspecting metadata."""


def make_handler(github: GitHubClient) -> Handler:
    """Build a stateful handler bound to the shared GitHub client.

    The handler closes over ``github`` so it doesn't need a per-call
    factory. Production wires this once in the github-watcher child
    next to the watcher source.
    """

    async def handle(event: Event, ctx: SubscriptionContext) -> None:
        if event.type != EVENT_TYPE:
            ctx.logger.debug(
                "github-issue-sync: ignoring unexpected event type %s", event.type
            )
            return

        payload = event.payload
        issue = _ParsedIssue.from_payload(payload)
        if issue is None:
            ctx.logger.warning(
                "github-issue-sync: malformed payload for %s: %r",
                event.type,
                dict(payload),
            )
            return

        await _reconcile(issue, ctx, github)

    return handle


# ── Parsed event shape ────────────────────────────────────────────────


class _ParsedIssue:
    """Strongly-typed view of the bus payload.

    Lives next to the handler because nothing else needs it. Constructed
    via :meth:`from_payload` to centralise the malformed-payload guard.
    """

    __slots__ = (
        "author",
        "body",
        "html_url",
        "labels",
        "number",
        "repo",
        "slug",
        "state",
        "state_reason",
        "title",
    )

    def __init__(
        self,
        *,
        slug: str,
        repo: str,
        number: int,
        title: str,
        body: str,
        state: str,
        state_reason: str | None,
        labels: list[str],
        author: str,
        html_url: str,
    ) -> None:
        self.slug = slug
        self.repo = repo
        self.number = number
        self.title = title
        self.body = body
        self.state = state
        self.state_reason = state_reason
        self.labels = labels
        self.author = author
        self.html_url = html_url

    @classmethod
    def from_payload(cls, payload: Any) -> _ParsedIssue | None:
        try:
            return cls(
                slug=str(payload["slug"]),
                repo=str(payload["repo"]),
                number=int(payload["number"]),
                title=str(payload["title"]),
                body=str(payload.get("body") or ""),
                state=str(payload["state"]),
                state_reason=_optional_str(payload.get("state_reason")),
                labels=list(payload.get("labels") or ()),
                author=str(payload.get("author") or ""),
                html_url=str(payload["html_url"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return None


# ── Reconciliation ─────────────────────────────────────────────────────


async def _reconcile(
    issue: _ParsedIssue, ctx: SubscriptionContext, github: GitHubClient
) -> None:
    from lithos_loom.github_client import parse_marker

    marker_task_id = parse_marker(issue.body)
    if marker_task_id is not None:
        existing = await _fetch_task(ctx, marker_task_id)
        if existing is not None:
            await _reconcile_existing(issue, existing, ctx)
            return
        # Marker points at a missing task — operator likely deleted it
        # in Lithos. Fall through and create a fresh task; the marker
        # writer below will overwrite the stale marker.
        ctx.logger.info(
            "github-issue-sync: marker on issue %s/#%d points at missing "
            "task %s; recreating",
            issue.repo,
            issue.number,
            marker_task_id,
        )

    # No marker — try to find a Lithos task that already tracks this URL.
    matching = await _find_task_by_url(ctx, issue.html_url)
    if matching is not None:
        # Operator deleted the marker but the task still exists. Re-
        # write the marker rather than creating a duplicate task.
        ctx.logger.info(
            "github-issue-sync: re-writing missing marker on %s/#%d → task %s",
            issue.repo,
            issue.number,
            matching.id,
        )
        await _apply_marker_safe(github, issue, matching.id, ctx)
        # Also reconcile in case the issue was closed during the marker-less window.
        await _reconcile_existing(issue, matching, ctx)
        return

    # No marker, no matching task. Skip closed issues — they were closed
    # without ever having existed in Lithos and we don't backfill historic
    # closures.
    if issue.state == "closed":
        ctx.logger.debug(
            "github-issue-sync: skipping already-closed %s/#%d (no Lithos task)",
            issue.repo,
            issue.number,
        )
        return

    await _create_task_and_mark(issue, ctx, github)


async def _reconcile_existing(
    issue: _ParsedIssue, task: Task, ctx: SubscriptionContext
) -> None:
    """Apply GH state to a known Lithos task. Idempotent.

    Slice 7.2 layers three branches on top of the original close mirror:

    1. **Drift sync** (always runs): title / body / labels / state-snapshot.
       Builds a single merged ``task_update`` payload so a steady-state poll
       costs zero round-trips and a poll that observes multiple drifts costs
       exactly one.
    2. **Reopen finding**: terminal Lithos task + GH-open + snapshot bump
       fires ``[ReopenRequested]`` once. The snapshot transition (handled
       in step 1) is what de-dupes subsequent polls.
    3. **Close mirror** (Slice 7.1, preserved): GH-closed + Lithos-open
       triggers ``task_complete`` / ``task_cancel`` based on ``state_reason``.

    Reopen detection must compare the *current* snapshot value, so it
    inspects ``task.metadata`` BEFORE drift sync rewrites it.
    """
    # Reopen detection reads the snapshot before drift sync mutates it.
    prior_snapshot = task.metadata.get("github_state_snapshot")
    reopen_fired = (
        task.status in ("completed", "cancelled")
        and issue.state == "open"
        and prior_snapshot != "open"
    )
    # When reopen fires and the finding_post fails (transient Lithos
    # error, MCP outage) we must NOT let drift sync advance
    # github_state_snapshot to "open" — otherwise the next poll's
    # ``prior_snapshot != "open"`` guard short-circuits and the finding
    # is silently lost (PR-review finding 4, 2026-05-30). Default to
    # "snapshot may advance"; only veto when the finding actually fails.
    freeze_snapshot = False
    if reopen_fired:
        ctx.logger.info(
            "github-issue-sync: reopen detected on %s/#%d (task %s)",
            issue.repo,
            issue.number,
            task.id,
        )
        try:
            await ctx.lithos.finding_post(
                task_id=task.id,
                summary=(
                    f"[ReopenRequested] GH issue {issue.repo}#{issue.number} reopened"
                ),
                agent=ctx.agent_id,
            )
        except (LithosClientError, OSError) as exc:
            ctx.logger.warning(
                "[Friction] github-issue-sync: ReopenRequested finding_post for %s "
                "failed (%s); leaving github_state_snapshot=%r so the next poll "
                "retries",
                task.id,
                exc,
                prior_snapshot,
            )
            freeze_snapshot = True

    await _sync_drift(issue, task, ctx, freeze_state_snapshot=freeze_snapshot)

    if issue.state != "closed":
        return

    if task.status != "open":
        # Already terminal in Lithos. Idempotent skip — re-emitting an
        # event for a closed-on-GH issue that's already closed in Lithos
        # is the common steady-state case.
        ctx.logger.debug(
            "github-issue-sync: %s/#%d closed and task %s already %s — no-op",
            issue.repo,
            issue.number,
            task.id,
            task.status,
        )
        return

    if issue.state_reason == "completed":
        ctx.logger.info(
            "github-issue-sync: completing task %s (closed via %s/#%d)",
            task.id,
            issue.repo,
            issue.number,
        )
        await _safe_call(
            ctx,
            ctx.lithos.task_complete(task_id=task.id, agent=ctx.agent_id),
            describe=f"complete task {task.id}",
        )
    elif issue.state_reason == "not_planned":
        ctx.logger.info(
            "github-issue-sync: cancelling task %s (closed as not_planned via %s/#%d)",
            task.id,
            issue.repo,
            issue.number,
        )
        await _safe_call(
            ctx,
            ctx.lithos.task_cancel(
                task_id=task.id,
                agent=ctx.agent_id,
                reason=f"GH closed as not_planned: {issue.html_url}",
            ),
            describe=f"cancel task {task.id}",
        )
    else:
        ctx.logger.info(
            "github-issue-sync: %s/#%d closed without state_reason; "
            "leaving task %s open",
            issue.repo,
            issue.number,
            task.id,
        )


# ── Slice 7.2: drift sync helpers ─────────────────────────────────────


async def _sync_drift(
    issue: _ParsedIssue,
    task: Task,
    ctx: SubscriptionContext,
    *,
    freeze_state_snapshot: bool = False,
) -> None:
    """Mirror GH-side drift (title / body / labels) into Lithos.

    Build a single merged ``task_update`` payload to keep steady-state
    polls cheap. The state-snapshot field rides on the same write so the
    reopen-finding de-dupe stays consistent without an extra round-trip.

    ``freeze_state_snapshot=True`` is set by ``_reconcile_existing`` only
    when a reopen finding_post just failed: holding the snapshot at its
    prior value (typically ``"closed"``) keeps the next poll's
    closed-to-open guard true, so the finding gets retried instead of
    permanently de-duped against a stale snapshot bump.
    """
    updates: dict[str, Any] = {}
    metadata_updates: dict[str, Any] = {}

    if issue.title != task.title:
        updates["title"] = issue.title

    body_sans_marker = strip_marker(issue.body)
    current_desc = (task.description or "").strip()
    if body_sans_marker != current_desc:
        updates["description"] = body_sans_marker

    raw_snapshot = task.metadata.get("github_labels") or ()
    old_snapshot: list[str] = [str(label) for label in raw_snapshot]
    new_labels = list(issue.labels)
    if set(old_snapshot) != set(new_labels):
        new_tags = _merge_tags_preserving_operator_adds(
            list(task.tags), old_snapshot, new_labels
        )
        if set(new_tags) != set(task.tags):
            updates["tags"] = new_tags
        metadata_updates["github_labels"] = new_labels

    if (
        not freeze_state_snapshot
        and task.metadata.get("github_state_snapshot") != issue.state
    ):
        metadata_updates["github_state_snapshot"] = issue.state

    if metadata_updates:
        updates["metadata"] = metadata_updates

    if not updates:
        return

    await _safe_call(
        ctx,
        ctx.lithos.task_update(
            task_id=task.id,
            agent=ctx.agent_id,
            **updates,
        ),
        describe=f"drift-sync task {task.id}",
    )


def _merge_tags_preserving_operator_adds(
    current: list[str],
    old_snapshot: list[str],
    new_labels: list[str],
) -> list[str]:
    """Reconcile Lithos task tags against a GH label diff.

    - Remove tags that were in the *prior* GH snapshot but are no longer
      in GH's current label list (GH-side removals propagate).
    - Add tags that are in GH's current label list but not yet on the task
      (GH-side additions propagate).
    - Preserve everything else — operator-added Lithos tags survive
      because they were never in any GH snapshot.

    Order-stable: existing tags keep their relative position; new GH
    labels append at the end.
    """
    removed = set(old_snapshot) - set(new_labels)
    seen: set[str] = set()
    result: list[str] = []
    for tag in current:
        if tag in removed or tag in seen:
            continue
        result.append(tag)
        seen.add(tag)
    for tag in new_labels:
        if tag in seen:
            continue
        result.append(tag)
        seen.add(tag)
    return result


async def _create_task_and_mark(
    issue: _ParsedIssue, ctx: SubscriptionContext, github: GitHubClient
) -> None:
    """Two-step: create the Lithos task, then write the marker on GitHub.

    If the marker write fails after task creation we end up with a Lithos
    task referencing the URL but no linkage marker on the issue. The
    next poll's no-marker / matching-URL branch picks this up and
    re-tries the marker write — eventually consistent.
    """
    metadata: dict[str, Any] = {
        "project": issue.slug,
        "github_issue_url": issue.html_url,
        "github_issue_number": issue.number,
        "github_labels": list(issue.labels),
        # Slice 7.2: bootstrap the snapshot so the reopen-finding de-dupe
        # has a baseline. Without it, a legacy migration path treats a
        # missing snapshot as "unknown" and could fire one spurious
        # finding on the first poll after close→reopen.
        "github_state_snapshot": issue.state,
    }
    tags = list(issue.labels) + [GITHUB_ISSUE_TAG]
    try:
        task_id = await ctx.lithos.task_create(
            title=issue.title,
            description=issue.body,
            agent=ctx.agent_id,
            tags=tags,
            metadata=metadata,
        )
    except (LithosClientError, OSError) as exc:
        ctx.logger.warning(
            "[Friction] github-issue-sync: task_create failed for %s/#%d: %s",
            issue.repo,
            issue.number,
            exc,
        )
        return

    ctx.logger.info(
        "github-issue-sync: created task %s for %s/#%d",
        task_id,
        issue.repo,
        issue.number,
    )
    await _apply_marker_safe(github, issue, task_id, ctx)


async def _apply_marker_safe(
    github: GitHubClient,
    issue: _ParsedIssue,
    task_id: str,
    ctx: SubscriptionContext,
) -> None:
    """Write the canonical marker to the issue body, swallowing GH errors.

    Re-fetches the issue body via ``github.get_issue`` immediately before
    the PATCH so an operator edit during the poll-to-PATCH window
    survives. GitHub's ``PATCH /issues/{n}`` is full-body replacement
    with no optimistic locking — the race window can't be closed, but
    fetching just before writing shrinks it from "one poll interval +
    handler latency" to "single round-trip latency".

    If the re-fetch fails (404, transport) we fall back to the body
    carried in the event payload — losing an operator-edit window is
    better than not writing the marker at all (which would cause the
    next poll to walk the orphan-marker recovery path and produce a
    duplicate write attempt).

    A marker-write failure is recoverable — the next poll's matching-URL
    branch will retry. We don't propagate the error because that would
    surface the issue to retry logic that would just re-do the
    already-successful task_create.
    """
    body_source = issue.body
    try:
        fresh = await github.get_issue(issue.repo, issue.number)
    except GitHubError as exc:
        ctx.logger.debug(
            "github-issue-sync: get_issue for marker write on %s/#%d "
            "failed (%s); using poll-event body",
            issue.repo,
            issue.number,
            exc,
        )
    else:
        if fresh is not None:
            body_source = fresh.body

    new_body = apply_marker(body_source, task_id)
    try:
        await github.update_issue_body(issue.repo, issue.number, new_body)
    except GitHubError as exc:
        ctx.logger.warning(
            "[Friction] github-issue-sync: marker write failed for %s/#%d "
            "(task %s): %s",
            issue.repo,
            issue.number,
            task_id,
            exc,
        )


# ── Lithos lookup helpers ─────────────────────────────────────────────


async def _fetch_task(ctx: SubscriptionContext, task_id: str) -> Task | None:
    try:
        return await ctx.lithos.task_get(task_id=task_id)
    except LithosClientError as exc:
        if exc.code == "task_not_found":
            return None
        ctx.logger.warning("github-issue-sync: task_get(%s) failed: %s", task_id, exc)
        return None


async def _find_task_by_url(ctx: SubscriptionContext, url: str) -> Task | None:
    """Scan open + closed tasks for one whose metadata carries ``url``.

    Used only on the operator-deleted-marker recovery path, so the
    linear cost is bounded — open-task counts in the operator's
    workspace are typically O(10–100), and closed-task scan is only
    triggered when the open scan misses.
    """
    for status in ("open", "completed", "cancelled"):
        try:
            tasks = await ctx.lithos.task_list(status=status)
        except (LithosClientError, OSError) as exc:
            ctx.logger.warning(
                "github-issue-sync: task_list(status=%s) failed during "
                "marker-recovery: %s",
                status,
                exc,
            )
            continue
        for task in tasks:
            if task.metadata.get("github_issue_url") == url:
                return task
    return None


async def _safe_call(ctx: SubscriptionContext, coro: Any, *, describe: str) -> None:
    """Await ``coro`` swallowing typed errors as [Friction] log lines.

    The handler's retry-and-friction layer (in SubscriptionRunner)
    only re-fires on uncaught exceptions; we want most Lithos errors
    to log + drop rather than retry (a missing task isn't going to
    appear if we re-run the same call).
    """
    try:
        await coro
    except (LithosClientError, OSError) as exc:
        ctx.logger.warning("[Friction] github-issue-sync: %s failed: %s", describe, exc)
