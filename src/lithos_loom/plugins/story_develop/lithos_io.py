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
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ...lithos_client import LithosClient

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .develop import DevelopResult
    from .findings import DeferredFinding

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
    if result.blocking_checks:
        # Raw-exit checks (repo-parity / command overrides) that blocked at exit
        # (#273) — they leave no ledger finding, so name them here explicitly.
        lines += ["", "blocking gate checks:"]
        for c in result.blocking_checks:
            lines.append(f"- {c.name}: {c.verdict} (`{c.command}`)")
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


def _deferred_section(spawns: Sequence[DeferredSpawn]) -> str:
    """The ``[DevelopResult]`` block naming every deferred finding (819370e5).

    Needed because ``_result_summary``'s open-findings section filters on
    ``is_open`` — a deferred (resolved) finding would otherwise vanish from
    the record. Each line says where the finding went, so the operator can
    disagree with the deferral by reopening the spawned task.
    """
    lines = ["deferred out-of-scope findings (spun out, non-blocking):"]
    for d in spawns:
        f = d.finding
        where = (
            f"spawned task {d.task_id} (discovered_from)"
            if d.task_id
            else "SPAWN FAILED — preserved here, file manually"
        )
        because = (
            f" — deferred because: {f.deferral_reason}" if f.deferral_reason else ""
        )
        lines.append(
            f"- [{f.reviewer}/{f.finding_id}] {f.severity}: "
            f"{f.rationale or '(no rationale recorded)'}{because} -> {where}"
        )
    return "\n".join(lines)


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


@dataclass(frozen=True)
class DeferredSpawn:
    """One deferred finding's spawn outcome (819370e5).

    ``task_id`` is the created Lithos task (linked ``discovered_from`` the
    story), or ``None`` when the spawn failed — the finding text then stays
    preserved in the ``[DevelopResult]`` summary with a loud note, so a
    Lithos hiccup degrades to exactly the pre-escape record rather than
    losing the finding.
    """

    finding: DeferredFinding
    task_id: str | None


def spawn_deferred_tasks(
    url: str,
    task_id: str,
    result: DevelopResult,
) -> list[DeferredSpawn]:
    """Spin each ``out-of-scope`` finding into its own Lithos task (819370e5).

    One task per deferred finding, created via ``task_spawn`` so the
    ``discovered_from`` edge (non-blocking, "found while executing the
    story") lands atomically with it. ``inherit_tags=False`` is load-bearing:
    inheriting would copy the story's ``trigger:*`` tag onto the spawned task
    and auto-dispatch it — the disposition exists to hand the finding to a
    HUMAN queue, not to recurse. Best-effort per finding, same
    never-fail-a-finished-run policy as :func:`post_results`.

    Deliberately not de-duplicated across runs: a retried run whose reviewer
    defers the same defect again files a second task. The tasks are cheap,
    both carry ``deferred_from_task`` provenance for an operator query, and
    any content-based dedup would silently drop a genuinely new finding that
    happens to share wording.
    """
    if not result.deferred_findings:
        return []

    async def _spawn() -> list[DeferredSpawn]:
        spawns: list[DeferredSpawn] = []
        async with LithosClient(url, agent_id=AGENT_ID) as client:
            for f in result.deferred_findings:
                headline = ((f.rationale or "").strip().splitlines() or [""])[0][:80]
                title = f"[deferred {f.severity}] {headline or f.finding_id}"
                files = "\n".join(f"- {path}" for path in f.files) or "(none listed)"
                # The parse mandates deferral_reason for out-of-scope; empty
                # only for entries predating the handoff key.
                why = f.deferral_reason or "(not recorded)"
                description = (
                    f"Deferred out-of-scope finding from story-develop run "
                    f"{result.run_id} (branch {result.branch}).\n\n"
                    f"The {f.reviewer} reviewer judged this REAL but not the "
                    f"story's to fix, and the run approved without it "
                    f"(status out-of-scope, 819370e5).\n\n"
                    f"severity: {f.severity}\n"
                    f"reviewer: {f.reviewer} (finding {f.finding_id})\n"
                    f"files/evidence:\n{files}\n\n"
                    f"the defect (original finding rationale, verbatim):\n"
                    f"{f.rationale}\n\n"
                    f"why it was deferred (the reviewer's disposition, "
                    f"verbatim):\n{why}"
                )
                try:
                    spawned = await client.task_spawn(
                        source_task_id=task_id,
                        title=title,
                        description=description,
                        relation_type="discovered_from",
                        inherit_project=True,
                        inherit_tags=False,
                        metadata={
                            "deferred_from_task": task_id,
                            "deferred_from_run": result.run_id,
                            "deferred_by_reviewer": f.reviewer,
                            "deferred_finding_id": f.finding_id,
                            "deferred_severity": f.severity,
                        },
                    )
                except Exception as exc:
                    logger.warning(
                        "[Friction] story-develop %s: spawning deferred task for "
                        "%s/%s failed (%s); the finding text stays in the "
                        "[DevelopResult] summary — file it manually",
                        result.run_id,
                        f.reviewer,
                        f.finding_id,
                        exc,
                    )
                    spawns.append(DeferredSpawn(finding=f, task_id=None))
                else:
                    spawns.append(DeferredSpawn(finding=f, task_id=spawned))
        return spawns

    try:
        return asyncio.run(_spawn())
    except Exception as exc:
        logger.warning(
            "[Friction] story-develop %s: deferred-task spawning failed "
            "wholesale (%s); finding text stays in the [DevelopResult] summary",
            result.run_id,
            exc,
        )
        return [
            DeferredSpawn(finding=f, task_id=None) for f in result.deferred_findings
        ]


def post_results(
    url: str,
    task_id: str,
    result: DevelopResult,
    *,
    pr_url: str | None = None,
    delivery: Any = None,
    deferred_spawns: Sequence[DeferredSpawn] | None = None,
) -> bool:
    """Post the run outcome back to the task. Returns True when fully posted.

    *delivery* (a ``DeliveryOutcome``) supersedes *pr_url* and corrects the
    reported spend: the Copilot fix round happens AFTER ``develop()`` returns,
    so the result object alone would understate cost and omit the round.

    *deferred_spawns* (from :func:`spawn_deferred_tasks`) records where each
    ``out-of-scope`` finding went; without it, any deferred findings still
    render in the summary as unspawned (the record is never lost, 819370e5).

    A post failure must NOT fail the run — the work exists on the branch
    regardless — so errors are logged as friction and ``False`` is returned
    for the caller to surface.
    """
    if deferred_spawns is None:
        deferred_spawns = [
            DeferredSpawn(finding=f, task_id=None) for f in result.deferred_findings
        ]
    total_cost = result.total_cost_usd + (
        delivery.extra_cost_usd if delivery is not None else 0.0
    )
    # Local import: the module keeps `develop` out of its runtime import surface
    # (DevelopResult is TYPE_CHECKING-only) — reuse the one severity-count helper
    # so the metadata patch + state.json can't drift.
    from .develop import findings_by_severity

    async def _post() -> None:
        async with LithosClient(url, agent_id=AGENT_ID) as client:
            summary = _result_summary(result)
            if deferred_spawns:
                summary += "\n\n" + _deferred_section(deferred_spawns)
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
                "develop_findings_by_severity": findings_by_severity(result.reviews),
            }
            if result.review_profile:
                metadata["develop_review_profile_used"] = result.review_profile
            spawned_ids = [d.task_id for d in deferred_spawns if d.task_id]
            if spawned_ids:
                metadata["develop_deferred_tasks"] = spawned_ids
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
