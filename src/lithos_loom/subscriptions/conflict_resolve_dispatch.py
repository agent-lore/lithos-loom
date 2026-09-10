"""PRD S5, the watcher half: dispatch ``develop converge --resolve-conflicts``.

The base-move re-gate (S3) names a delivered PR's conflict — ``[PRConflicted]``
with the paths, a ``conflict`` record on the gate keyed on ``(head, base)``.
This is the resolver behind it: on a still-open ``pr`` gate whose merge-gate
record says ``conflict`` for the CURRENT sha pair, the sweep spawns the S5 CLI
half (``converge <pr> --resolve-conflicts --story <id>``) **once per sha pair**
— a failure never retries the same inputs (PRD S5: escalate, do not persist),
and a moved head or base is a genuinely new attempt that the merge-gate
re-runs first. One run in flight at a time (it spends a coder + a panel),
holding and held by remediation and the merge-gate on that PR (any of the
three may push to the branch). Outcomes, each a one-shot record on the gate:

* ``converged`` + pushed — the merge commit is loom's OWN push on the S5b
  budget (else the next sweep reads it as a human push and resets the
  remediation counter), and ``[ConflictResolved]`` on the story says what
  landed;
* ``not_converged`` / ``failed`` / ``conflict_unsupported`` — the residue is a
  human's: a loom ``human`` gate on the story (``conflict_unresolved``), the
  push sinks, ``[NeedsHuman]``, once per sha pair;
* ``no_conflict`` / ``merge_race`` / ``merged`` — the world moved on; record
  only, the sweep's other halves own what comes next;
* a crash, a repo-mismatch refusal — ``[Friction]`` on the story, a crash
  re-armed once per daemon boot (a restart is the operator's fix attempt).

Host dial ``[github_watcher] conflict_resolve_enabled``; per-project opt-out
``develop_conflict_resolve = false`` on the context doc (fail-closed when the
doc is unreadable — an unknown safety dial never authorises a paid run).
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from lithos_loom.errors import LithosClientError
from lithos_loom.gates import (
    ESCALATION_SUMMARY_MAX_CHARS,
    STORY_HUMAN_GATE_ID_KEY,
    PrGateSpec,
)
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._findings import post_finding_then_mark, write_marker
from lithos_loom.subscriptions._project_settings import (
    OriginRead,
    origin_read,
    read_project_flag,
    resolve_project_repo,
)
from lithos_loom.subscriptions._subprocess import spawn_command
from lithos_loom.subscriptions.escalation import Escalation, raise_needs_human
from lithos_loom.subscriptions.merge_gate_record import (
    read_record as read_merge_record,
)
from lithos_loom.subscriptions.remediation_budget import (
    REMEDIATION_KEY,
    RemediationNotifier,
    read_budget,
)

__all__ = [
    "CONFLICT_ACTIONS",
    "CONFLICT_RESOLVED",
    "CONFLICT_RESOLVE_KEY",
    "CONFLICT_RESOLVE_SETTING",
    "RUN_TIMEOUT_SECONDS",
    "ConflictResolveDispatch",
    "ConflictResolveRecord",
    "ConflictResolveSettings",
    "OriginRead",
    "origin_read",
    "read_record",
    "spawn_resolve",
]

# Stable, machine-parseable finding prefix (see AGENTS.md): loom resolved a
# delivered PR's conflict with its base and pushed the merge commit.
CONFLICT_RESOLVED = "[ConflictResolved]"

# Gate-metadata key holding the last resolution record (the sha pair + outcome).
CONFLICT_RESOLVE_KEY = "conflict_resolve"

# Per-project opt-out on the context doc's metadata (default on).
CONFLICT_RESOLVE_SETTING = "develop_conflict_resolve"

# A resolution is a coder session plus a panel loop: same ceiling as remediation.
RUN_TIMEOUT_SECONDS = 4 * 3600
_OUTPUT_TAIL_CHARS = 600

# Run statuses that are a human's to decide: the merge could not be composed
# to the panel's satisfaction, or it is a shape the coder cannot edit.
_ESCALATE: frozenset[str] = frozenset(
    {"not_converged", "failed", "conflict_unsupported"}
)

CONFLICT_ACTIONS = (
    "the story stays behind its pr gate; resolve the conflict by merging the "
    "base into the PR branch by hand (never rebase a delivered branch) — a "
    "human push re-keys every sweep — or re-run `develop converge <pr> "
    "--resolve-conflicts --story <id>` with a higher --max-rounds; complete "
    "this gate once decided"
)
"""What the operator can do about an unresolved conflict — none of it is a
re-dispatch of the story, so the runner's two actions would mislead here."""


async def spawn_resolve(cmd: list[str]) -> tuple[int, str]:
    """Run the resolve subprocess (cancellation-safe, bounded)."""
    return await spawn_command(
        cmd, timeout=RUN_TIMEOUT_SECONDS, label="conflict-resolve"
    )


Spawn = Callable[[list[str]], Awaitable[tuple[int, str]]]
Hold = Callable[[str], bool]


@dataclass(frozen=True)
class ConflictResolveSettings:
    """Host-side knobs the watcher child threads in from its config."""

    enabled: bool = True
    projects: Mapping[str, Path] = field(default_factory=dict)
    work_dir: Path = Path(".")
    config_path: Path | None = None
    # the push sinks for the needs-human gate an unresolved conflict raises
    notifier: RemediationNotifier | None = None


@dataclass(frozen=True)
class ConflictResolveRecord:
    """The gate's ``conflict_resolve`` marker: the sha pair + the outcome."""

    pr_url: str
    head_sha: str
    base_sha: str
    status: str = ""
    attempts: int = 0
    pushed_sha: str = ""
    needs_human_gate_id: str = ""
    boot_id: str = ""
    message: str = ""

    def as_marker(self) -> dict[str, Any]:
        return {
            "pr_url": self.pr_url,
            "head_sha": self.head_sha,
            "base_sha": self.base_sha,
            "status": self.status,
            "attempts": self.attempts,
            "pushed_sha": self.pushed_sha,
            "needs_human_gate_id": self.needs_human_gate_id,
            "boot_id": self.boot_id,
            "message": self.message,
        }


def read_record(gate: Any, pr_url: str) -> ConflictResolveRecord | None:
    """The gate's record; ``None`` for an absent / foreign-url marker."""
    raw = gate.metadata.get(CONFLICT_RESOLVE_KEY)
    if not isinstance(raw, dict) or raw.get("pr_url") != pr_url:
        return None

    def _s(key: str) -> str:
        value = raw.get(key)
        return value if isinstance(value, str) else ""

    attempts = raw.get("attempts")
    return ConflictResolveRecord(
        pr_url=pr_url,
        head_sha=_s("head_sha"),
        base_sha=_s("base_sha"),
        status=_s("status"),
        attempts=attempts if isinstance(attempts, int) and attempts >= 0 else 0,
        pushed_sha=_s("pushed_sha"),
        needs_human_gate_id=_s("needs_human_gate_id"),
        boot_id=_s("boot_id"),
        message=_s("message"),
    )


class ConflictResolveDispatch:
    """Owns the single-flight dispatch of ``develop converge --resolve-conflicts``."""

    def __init__(
        self,
        settings: ConflictResolveSettings,
        *,
        spawn: Spawn | None = None,
        hold: Hold | None = None,
        boot_id: str | None = None,
    ) -> None:
        self._settings = settings
        self._spawn: Spawn = spawn if spawn is not None else spawn_resolve
        self._hold = hold
        self._boot_id = boot_id or uuid.uuid4().hex
        self._task: asyncio.Task[None] | None = None
        self._in_flight_pr_url = ""

    # ── in-flight state ────────────────────────────────────────────────

    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def busy_on(self, pr_url: str) -> bool:
        """The other dispatchers' hold: a merge commit + fixes may land on
        this PR's branch at any moment."""
        return self.busy() and self._in_flight_pr_url == pr_url

    async def drain(self) -> None:
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)

    async def shutdown(self) -> None:
        """Cancel + await the in-flight run; the cancellation-safe spawn
        terminates the converge child so a stopped loom leaves no orphan
        pushing to PRs."""
        if self.busy() and self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    # ── the decision ───────────────────────────────────────────────────

    async def consider(
        self,
        gate: Any,
        spec: PrGateSpec,
        story_id: str | None,
        pr: Any,
        ctx: SubscriptionContext,
        *,
        hold: bool = False,
    ) -> str:
        """Decide for one still-open ``pr`` gate; returns a label for the log."""
        if not self._settings.enabled:
            return "disabled"
        if story_id is None:
            return "no_story"
        head = getattr(pr, "head_sha", "") or ""
        base = getattr(pr, "base_sha", "") or ""
        if not head or not base:
            return "unknown_shas"
        merge = read_merge_record(gate, spec.pr_url)
        if (
            merge is None
            or merge.status != "conflict"
            or (merge.head_sha, merge.base_sha) != (head, base)
        ):
            return "no_conflict"  # nothing current to resolve; S3 decides first
        prior = read_record(gate, spec.pr_url)
        attempts = 1
        if prior is not None and (prior.head_sha, prior.base_sha) == (head, base):
            # one attempt per sha pair (PRD S5) — except a crash, which a
            # restart re-arms once (the operator's fix attempt)
            if prior.status == "crashed" and prior.boot_id != self._boot_id:
                attempts = 1
            else:
                return "unchanged"
        if await _story_escalated(story_id, ctx):
            return "escalated"  # a human gate already waits on this story
        if hold:
            return "held"
        if self.busy():
            return "busy"
        resolved = await resolve_project_repo(
            gate, story_id, self._settings.projects, ctx
        )
        if resolved is None:
            return "no_project"  # the merge-gate already said so on the story
        slug, repo = resolved
        dial = await read_project_flag(
            slug, CONFLICT_RESOLVE_SETTING, ctx, subsystem="conflict-resolve"
        )
        if dial is None:
            return "dial_unreadable"  # fail closed; retried next sweep
        if not dial:
            return "opted_out"
        seen = await origin_read(repo)
        if seen.repo is None:
            return "checkout_unresolved"
        if seen.repo.lower() != spec.repo.lower():
            return "repo_mismatch"  # the merge-gate refused and said so already
        ctx.logger.info(
            "conflict-resolve: dispatching converge --resolve-conflicts for %s "
            "(head %s, base %s, attempt %d)",
            spec.pr_url,
            head[:12],
            base[:12],
            attempts,
        )
        self._in_flight_pr_url = spec.pr_url
        self._task = asyncio.create_task(
            self._run(gate.id, story_id, spec, repo, head, base, attempts, ctx),
            name=f"conflict-resolve-{spec.pr_number}",
        )
        return "dispatched"

    # ── the run ────────────────────────────────────────────────────────

    def command(
        self, spec: PrGateSpec, repo: Path, json_path: Path, story_id: str
    ) -> list[str]:
        cmd = [
            sys.executable,
            "-m",
            "lithos_loom",
            "develop",
            "converge",
            str(spec.pr_number),
            "--resolve-conflicts",
            "--story",
            story_id,
            "--repo",
            str(repo),
            "--expect-repo",
            spec.repo,
            "--json",
            str(json_path),
        ]
        if self._settings.config_path is not None:
            cmd += ["--config", str(self._settings.config_path)]
        return cmd

    def _json_path(self, gate_id: str) -> Path:
        path = (
            self._settings.work_dir
            / "github-watcher"
            / f"conflict-resolve-{gate_id}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        return path

    @staticmethod
    def _load(path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    async def _run(
        self,
        gate_id: str,
        story_id: str,
        spec: PrGateSpec,
        repo: Path,
        head: str,
        base: str,
        attempts: int,
        ctx: SubscriptionContext,
    ) -> None:
        """One resolve subprocess and its outcome. Never raises."""
        record = ConflictResolveRecord(
            spec.pr_url, head, base, attempts=attempts, boot_id=self._boot_id
        )
        try:
            await self._run_inner(gate_id, story_id, spec, repo, record, ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the slot must always free cleanly
            ctx.logger.exception("conflict-resolve: run for %s raised", spec.pr_url)
            await _post_friction(
                gate_id,
                story_id,
                replace(record, status="crashed", message=f"raised {exc!r}"[:300]),
                f"raised {type(exc).__name__}: {exc}",
                ctx,
            )
        finally:
            self._in_flight_pr_url = ""

    async def _run_inner(
        self,
        gate_id: str,
        story_id: str,
        spec: PrGateSpec,
        repo: Path,
        record: ConflictResolveRecord,
        ctx: SubscriptionContext,
    ) -> None:
        path = self._json_path(gate_id)
        rc, output = await self._spawn(self.command(spec, repo, path, story_id))
        data = self._load(path)
        tail = output[-_OUTPUT_TAIL_CHARS:] if output else "(no output)"
        if data is None:
            await _post_friction(
                gate_id,
                story_id,
                replace(record, status="crashed", message=f"exit {rc}"),
                f"failed (exit {rc}) without a result; output tail: {tail}",
                ctx,
            )
            return
        status = str(data.get("status") or "unknown")
        record = replace(
            record, status=status, message=str(data.get("message") or "")[:300]
        )
        ctx.logger.info(
            "conflict-resolve: run for %s finished: %s", spec.pr_url, status
        )
        if status == "repo_mismatch":
            await _post_friction(
                gate_id,
                story_id,
                record,
                f"the checkout at {repo} is not {spec.repo} "
                f"(origin says {data.get('actual_repo') or 'unknown'}); the run "
                "refused before touching anything — fix `[projects.<slug>].repo`",
                ctx,
            )
        elif status == "converged" and data.get("pushed") is True:
            await _record_resolved(gate_id, story_id, spec, record, data, ctx)
        elif status in _ESCALATE:
            await _escalate(gate_id, story_id, spec, record, data, self._settings, ctx)
        else:
            # no_conflict / merge_race / merged / fork_unsupported / an
            # unpushed converge: the world moved on, or nothing to do —
            # the sweep's other halves own what comes next.
            await _write_record(gate_id, record, ctx)


async def _story_escalated(story_id: str, ctx: SubscriptionContext) -> bool:
    """Does an OPEN loom human gate already wait on the story? The record on
    the gate is the once-per-key guard; this is the belt for a record that
    failed to land after the gate was raised (the story still names it), so
    a paid run is never repeated and a second gate never raised."""
    try:
        story = await ctx.lithos.task_get(task_id=story_id)
        if story is None:
            return False
        gate_id = story.metadata.get(STORY_HUMAN_GATE_ID_KEY)
        if not isinstance(gate_id, str) or not gate_id:
            return False
        human = await ctx.lithos.task_get(task_id=gate_id)
    except LithosClientError:
        return True  # unknown is not permission for a paid run; retried next sweep
    return human is not None and human.status == "open"


# ── outcomes ───────────────────────────────────────────────────────────


async def _write_record(
    gate_id: str, record: ConflictResolveRecord, ctx: SubscriptionContext
) -> bool:
    return await write_marker(
        ctx,
        task_id=gate_id,
        marker={CONFLICT_RESOLVE_KEY: record.as_marker()},
        subsystem="conflict-resolve",
    )


async def _post_friction(
    gate_id: str,
    story_id: str,
    record: ConflictResolveRecord,
    detail: str,
    ctx: SubscriptionContext,
) -> None:
    await post_finding_then_mark(
        ctx,
        task_id=story_id,
        summary=(
            f"[Friction] conflict-resolve: resolving {record.pr_url}'s conflict "
            f"with its base @ {record.base_sha[:12]} (head {record.head_sha[:12]}) "
            f"{detail} (attempt {record.attempts}; a daemon restart retries a "
            f"crash once, a head or base move re-keys it)"
        ),
        marker={CONFLICT_RESOLVE_KEY: record.as_marker()},
        subsystem="conflict-resolve",
        retry_hint="will retry after a restart or a move",
        marker_task_id=gate_id,
    )


def _paths_of(data: Mapping[str, Any]) -> list[str]:
    conflict = data.get("conflict")
    raw = conflict.get("paths") if isinstance(conflict, dict) else None
    return [p for p in (raw if isinstance(raw, list) else []) if isinstance(p, str)]


async def _record_resolved(
    gate_id: str,
    story_id: str,
    spec: PrGateSpec,
    record: ConflictResolveRecord,
    data: Mapping[str, Any],
    ctx: SubscriptionContext,
) -> None:
    """Converged + pushed: the merge commit is loom's own push on the S5b
    budget — record and budget in ONE write, made FIRST (the push has
    happened; a breadcrumb that cannot post must never cost the budget its
    sha, and the resolver's trigger is gone once the head moved, so nothing
    would re-derive it) — then say what landed."""
    pushed = str(data.get("pushed_sha") or "")
    record = replace(record, pushed_sha=pushed)
    marker: dict[str, Any] = {CONFLICT_RESOLVE_KEY: record.as_marker()}
    try:
        fresh = await ctx.lithos.task_get(task_id=gate_id)
    except LithosClientError:
        fresh = None
    if fresh is not None and pushed:
        budget = read_budget(fresh, spec.pr_url)
        marker[REMEDIATION_KEY] = replace(
            budget, last_loom_pushed_sha=pushed
        ).as_marker()
    await write_marker(
        ctx, task_id=gate_id, marker=marker, subsystem="conflict-resolve"
    )
    paths = _paths_of(data)
    rounds = data.get("rounds")
    cost = data.get("total_cost_usd")
    summary = (
        f"{CONFLICT_RESOLVED} conflict-resolve: delivered PR {spec.pr_url}'s "
        f"conflict with its base @ {record.base_sha[:12]} in "
        f"{len(paths)} path(s) ({', '.join(paths) or 'unnamed'}) was resolved "
        f"by loom and pushed as {pushed[:12]} onto the PR branch after "
        f"{rounds} round(s) (${cost}); the composed tree passed the "
        f"project's check-set and the panel. The next sweep re-gates at the "
        f"new head."
    )
    try:
        await ctx.lithos.finding_post(task_id=story_id, summary=summary)
    except LithosClientError as exc:
        ctx.logger.warning(
            "[Friction] conflict-resolve: posting %s for story %s failed (%s); "
            "the record and the budget landed",
            CONFLICT_RESOLVED,
            story_id,
            exc,
        )


async def _escalate(
    gate_id: str,
    story_id: str,
    spec: PrGateSpec,
    record: ConflictResolveRecord,
    data: Mapping[str, Any],
    settings: ConflictResolveSettings,
    ctx: SubscriptionContext,
) -> None:
    """The residue is a human's: raise the loom ``human`` gate on the story,
    once per sha pair (the record carries the gate id)."""
    paths = _paths_of(data)
    brief: dict[str, Any] = {
        "pr_url": spec.pr_url,
        "head_sha": record.head_sha,
        "base_sha": record.base_sha,
        "paths": paths,
        "status": record.status,
        "rounds": data.get("rounds"),
        "fixer_commits": data.get("fixer_commits"),
        "cost_usd": data.get("total_cost_usd"),
        "message": record.message,
    }
    escalation = Escalation(
        reason="conflict_unresolved",
        summary=(
            f"loom could not resolve {spec.pr_url}'s conflict with its base "
            f"@ {record.base_sha[:12]} in {', '.join(paths) or 'unnamed paths'} "
            f"— run {record.status}: {record.message}"
        )[:ESCALATION_SUMMARY_MAX_CHARS],
        brief=brief,
    )

    async def _record(human_gate_id: str) -> bool:
        ok = await _write_record(
            gate_id, replace(record, needs_human_gate_id=human_gate_id), ctx
        )
        try:
            await ctx.lithos.task_update(
                task_id=story_id,
                agent=ctx.agent_id,
                metadata={STORY_HUMAN_GATE_ID_KEY: human_gate_id},
            )
        except LithosClientError as exc:
            ctx.logger.warning(
                "[Friction] conflict-resolve: recording gate %s on story %s "
                "failed (%s)",
                human_gate_id,
                story_id,
                exc,
            )
            return False
        return ok

    human_gate_id, problem = await raise_needs_human(
        ctx.lithos,
        task_id=story_id,
        route="conflict-resolve",
        agent=ctx.agent_id,
        escalation=escalation,
        notifier=settings.notifier,
        actions=CONFLICT_ACTIONS,
        record=_record,
        record_problem=(
            "could not record the gate on the resolve record / story — a "
            "restart may raise a second gate for the same conflict"
        ),
    )
    if human_gate_id is None:
        # the record still lands, so the same sha pair is never re-run
        await _post_friction(
            gate_id,
            story_id,
            record,
            f"ended {record.status} and the needs-human gate could not be raised "
            f"({problem or 'unknown'}); the conflict is a human's to resolve",
            ctx,
        )
