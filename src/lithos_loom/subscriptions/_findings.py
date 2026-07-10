"""Shared one-shot-finding + de-dup-marker helpers (ARCH-7).

Two subscribers post a stable-prefixed finding exactly once and then write a
url-scoped task-metadata marker so the breadcrumb never fires twice:

- ``_develop_pr_merge`` — a delivered PR reached a closed end-state
  (``[DeliveredPRClosed]``), keyed by ``develop_pr_merge_state`` +
  ``develop_pr_merge_url``;
- ``_github_issue_push`` — a task's linked GitHub issue was deleted
  (``[LinkedIssueGone]``), keyed by ``github_issue_gone_url``.

The finding-then-mark control flow was byte-identical in both; it lives here
now. Callers supply their own marker dict + subsystem label. (``_github_issue_sync``
writes ``github_state_snapshot`` too, but batched into its drift-sync
``task_update`` — a different shape — so it is deliberately NOT a caller here.)
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lithos_loom.errors import LithosClientError
from lithos_loom.subscriptions import SubscriptionContext

__all__ = ["post_finding_then_mark", "write_marker"]


async def write_marker(
    ctx: SubscriptionContext,
    *,
    task_id: Any,
    marker: Mapping[str, Any],
    subsystem: str,
) -> None:
    """Write a de-dup marker via ``task_update``, swallowing ``task_not_found``.

    post-lithos#303 a terminal task still accepts ``task_update``, so
    ``task_not_found`` now only fires for a genuinely deleted task (nothing left
    to mark). Any other Lithos error warns as ``[Friction]`` and leaves the
    marker unset (→ retried by the caller's next cycle). Never raises.
    """
    try:
        await ctx.lithos.task_update(task_id=task_id, metadata=dict(marker))
    except LithosClientError as exc:
        if exc.code != "task_not_found":
            ctx.logger.warning(
                "[Friction] %s: marking task %s failed (%s)", subsystem, task_id, exc
            )


async def post_finding_then_mark(
    ctx: SubscriptionContext,
    *,
    task_id: Any,
    summary: str,
    marker: Mapping[str, Any],
    subsystem: str,
    retry_hint: str,
) -> None:
    """Post a one-shot prefixed finding, then write a scoped de-dup marker.

    Ordering is finding-then-mark: a crash between the two costs at most one
    duplicate finding next cycle — the ``marker`` is what makes the finding
    one-shot. A ``task_not_found`` posting the finding falls through to the mark
    (the terminal-task case). Any *other* error posting the finding warns
    (``retry_hint``) and returns WITHOUT marking, so the whole breadcrumb retries
    next cycle. The marker write's own errors are handled by :func:`write_marker`.
    Never raises.
    """
    try:
        await ctx.lithos.finding_post(task_id=task_id, summary=summary)
    except LithosClientError as exc:
        if exc.code != "task_not_found":
            ctx.logger.warning(
                "[Friction] %s: posting finding for task %s failed (%s); %s",
                subsystem,
                task_id,
                exc,
                retry_hint,
            )
            return  # leave the marker unset → retry next cycle
    await write_marker(ctx, task_id=task_id, marker=marker, subsystem=subsystem)
