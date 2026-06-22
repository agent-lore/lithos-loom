"""Direct Lithos round-trip for ``story-develop`` (T8, PRD decision #11).

With ``--task-id`` (and not ``--no-lithos``) the plugin owns its Lithos I/O
directly: it fetches the task up front (title, description, acceptance
criteria, metadata) and posts the outcome back when the run ends — a
``[DevelopResult]`` finding carrying the verdicts + open findings, a
``[ReviewDispute]`` finding when a dispute deadlock stopped the run, and a
metadata update with the branch / status / cost plus the per-run review-metadata
record (profile, panel, findings-by-severity, gate verdict — ADR 0003 §11). The
daemon (T10) reuses the
identical path, and ``result.json`` still carries only ``status`` for the
runner — no double-application.

Task STATE deliberately does not transition by default: agent approval means
a reviewed-but-unmerged branch exists, not that the work is done — the
operator merges (and typically soaks) first, then completes the task.
``complete_task`` exists for operators who do want route-runner parity
(``--complete-on-approval``). There is nothing to release on failure: the
standalone plugin never claims the task (claiming is the daemon's collision
contract, T10).

The sync wrappers run one short-lived :class:`~lithos_loom.lithos_client.
LithosClient` connection per operation (one fetch at start, one post at end)
via ``asyncio.run`` — the plugin core stays synchronous and Lithos-free.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ...lithos_client import LithosClient

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .develop import DevelopResult

logger = logging.getLogger(__name__)

DEFAULT_LITHOS_URL = "http://localhost:8765"
# Stable agent id for findings attribution; intentionally not per-run so
# operator queries can filter all story-develop postings at once.
AGENT_ID = "lithos-loom-story-develop"
# Stable, machine-parseable finding prefixes (see AGENTS.md).
RESULT_PREFIX = "[DevelopResult]"
DISPUTE_PREFIX = "[ReviewDispute]"


@dataclass(frozen=True)
class TaskContext:
    """What the plugin needs from a Lithos task to run against it."""

    task_id: str
    title: str
    description: str
    acceptance_criteria: str | None  # metadata.acceptance_criteria, if set
    metadata: Mapping[str, Any]

    @property
    def task_text(self) -> str:
        """Title + body as the coder's task description."""
        body = (self.description or "").strip()
        return f"{self.title}\n\n{body}" if body else self.title


class LithosIOError(RuntimeError):
    """A Lithos round-trip operation failed (fetch is fatal; post is not)."""


def fetch_task_context(url: str, task_id: str) -> TaskContext:
    """Fetch the task and distil the run context. Raises :class:`LithosIOError`.

    A fetch failure is fatal to the run — without the task there is nothing
    to implement — so the caller surfaces it before any container spend.
    """

    async def _fetch() -> TaskContext:
        async with LithosClient(url, agent_id=AGENT_ID) as client:
            task = await client.task_get(task_id=task_id)
            if task is None:
                raise LithosIOError(f"task {task_id} not found at {url}")
            if task.status != "open":
                raise LithosIOError(
                    f"task {task_id} is {task.status}, not open — refusing to "
                    "develop against a terminal task"
                )
            ac = task.metadata.get("acceptance_criteria")
            return TaskContext(
                task_id=task.id,
                title=task.title,
                description=task.description or "",
                acceptance_criteria=ac if isinstance(ac, str) and ac.strip() else None,
                metadata=dict(task.metadata),
            )

    try:
        return asyncio.run(_fetch())
    except LithosIOError:
        raise
    except Exception as exc:  # connection/MCP errors: wrap with context
        raise LithosIOError(f"cannot fetch task {task_id} from {url}: {exc}") from exc


def _result_summary(result: DevelopResult) -> str:
    """The ``[DevelopResult]`` finding body: verdicts + open findings + refs."""
    lines = [
        f"{RESULT_PREFIX} {result.status.upper()}: {result.message}",
        "",
        f"branch: {result.branch}",
        f"worktree: {result.worktree}",
        f"run_id: {result.run_id} | rounds: {result.rounds} | "
        f"cost: ${result.total_cost_usd:.4f}",
    ]
    open_lines: list[str] = []
    for review in result.reviews:
        for f in review.findings:
            if f.is_open:
                open_lines.append(
                    f"- [{review.reviewer}/{f.finding_id}] {f.severity} "
                    f"({f.status}): {f.rationale or '(no rationale recorded)'}"
                )
    if open_lines:
        lines += ["", "open findings at exit:", *open_lines]
    if result.test_gate is not None:
        lines += [
            "",
            f"test gate: {result.test_gate.verdict} (`{result.test_gate.command}`)",
        ]
    if result.gate_findings:
        det_lines: list[str] = []
        for f in result.gate_findings:
            locus = f.file or f.package
            loc = f" [{locus}]" if locus else ""
            det_lines.append(
                f"- {f.finding_id} ({f.severity}): {f.rule}{loc} {f.message}"
            )
        lines += ["", "deterministic findings at exit:", *det_lines]
    return "\n".join(lines)


def _findings_by_severity(result: DevelopResult) -> dict[str, int]:
    """Count the final panel's findings by severity (ADR 0003 §11 calibration).

    Spans every reviewer's findings in the final round, all statuses — the
    review-output signal correlated against post-merge outcomes later. Canonical
    severities are always present (zero-filled) so the record has a stable shape;
    any off-rubric severity an `invalid` review emits is still counted under its
    own key rather than dropped.
    """
    counts: dict[str, int] = {"critical": 0, "major": 0, "minor": 0}
    for review in result.reviews:
        for f in review.findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
    return counts


def _delivery_section(delivery: Any) -> str:
    """The Copilot-round block of the ``[DevelopResult]`` finding."""
    lines = [f"pull request: {delivery.pr_url}"]
    if delivery.copilot_reviewed:
        if delivery.fix_pushed:
            fix = f"fix pushed ({delivery.fix_sha})"
        elif delivery.fix_committed:
            fix = f"fix prepared but HELD BACK (gate {delivery.fix_gate_verdict})"
        else:
            fix = "no code change"
        incomplete = "" if delivery.copilot_settled else " — INCOMPLETE (see note)"
        lines.append(
            f"copilot round: {delivery.comments_count} comment(s); {fix}; "
            f"{delivery.replies_posted} repl(ies) posted{incomplete}"
        )
    for note in delivery.notes:
        lines.append(f"note: {note}")
    return "\n".join(lines)


def post_results(
    url: str,
    task_id: str,
    result: DevelopResult,
    *,
    pr_url: str | None = None,
    delivery: Any = None,
) -> bool:
    """Post the run outcome back to the task. Returns True when fully posted.

    *delivery* (a ``DeliveryOutcome``) supersedes *pr_url* and corrects the
    reported spend: the Copilot fix round happens AFTER ``develop()`` returns,
    so the result object alone would understate cost and omit the round.

    A post failure must NOT fail the run — the work exists on the branch
    regardless — so errors are logged as friction and ``False`` is returned
    for the caller to surface.
    """
    total_cost = result.total_cost_usd + (
        delivery.extra_cost_usd if delivery is not None else 0.0
    )

    async def _post() -> None:
        async with LithosClient(url, agent_id=AGENT_ID) as client:
            summary = _result_summary(result)
            if delivery is not None:
                summary += "\n\n" + _delivery_section(delivery)
                if delivery.extra_cost_usd:
                    summary += f"\ntotal cost incl. copilot round: ${total_cost:.4f}"
            elif pr_url:
                summary += f"\n\npull request: {pr_url}"
            await client.finding_post(task_id=task_id, summary=summary)
            if result.status == "disputed":
                await client.finding_post(
                    task_id=task_id,
                    summary=(
                        f"{DISPUTE_PREFIX} story-develop run {result.run_id} "
                        f"stopped on a dispute deadlock: {result.message} — "
                        "a human needs to arbitrate (see the conversation log "
                        f"at {result.conversation_log})."
                    ),
                )
            metadata: dict[str, Any] = {
                "develop_status": result.status,
                "develop_branch": result.branch,
                "develop_run_id": result.run_id,
                "develop_rounds": result.rounds,
                "develop_cost_usd": round(total_cost, 4),
                # Review-metadata record (#139/ADR 0003 §11): the profile that
                # ran, the panel, and its findings-by-severity + gate verdict —
                # the per-run signal correlated against post-merge outcomes. Kept
                # under output-only keys so they never clash with the operator's
                # `develop_review_profile` *input* selection key.
                "develop_review_panel": [r.reviewer for r in result.reviews],
                "develop_findings_by_severity": _findings_by_severity(result),
            }
            if result.review_profile:
                metadata["develop_review_profile_used"] = result.review_profile
            if result.test_gate is not None:
                metadata["develop_test_gate_verdict"] = result.test_gate.verdict
            effective_pr_url = delivery.pr_url if delivery is not None else pr_url
            if effective_pr_url:
                metadata["develop_pr_url"] = effective_pr_url
            await client.task_update(task_id=task_id, metadata=metadata)

    try:
        asyncio.run(_post())
        return True
    except Exception as exc:
        logger.warning(
            "[Friction] story-develop %s: posting results to Lithos task %s "
            "failed (%s); the branch is intact — post manually if needed",
            result.run_id,
            task_id,
            exc,
        )
        return False


def complete_task(url: str, task_id: str, result: DevelopResult) -> bool:
    """Mark the task completed (``--complete-on-approval`` opt-in only).

    Only meaningful for APPROVED runs; the caller gates on that. Returns
    True on success; failure logs friction and returns False (same
    never-fail-a-finished-run policy as :func:`post_results`).
    """

    async def _complete() -> None:
        async with LithosClient(url, agent_id=AGENT_ID) as client:
            await client.task_complete(task_id=task_id)

    try:
        asyncio.run(_complete())
        return True
    except Exception as exc:
        logger.warning(
            "[Friction] story-develop %s: completing Lithos task %s failed "
            "(%s); complete it manually",
            result.run_id,
            task_id,
            exc,
        )
        return False
