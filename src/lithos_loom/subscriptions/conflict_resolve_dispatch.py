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
from lithos_loom.gates import PrGateSpec
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._project_settings import (
    OriginRead,
    origin_read,
    read_project_flag,
    resolve_project_repo,
)
from lithos_loom.subscriptions._subprocess import spawn_command
from lithos_loom.subscriptions.conflict_resolve_outcome import (
    clear_breadcrumb,
    escalate,
    paths_of,
    post_finding,
    post_friction,
    story_escalated,
    strict_write,
    write_once,
    write_record,
)
from lithos_loom.subscriptions.conflict_resolve_record import (
    CONFLICT_ACTIONS,
    CONFLICT_RESOLVE_KEY,
    CONFLICT_RESOLVE_SETTING,
    CONFLICT_RESOLVED,
    PUSHED_BREADCRUMB_KEY,
    STRICT_WRITE_DELAYS,
    ConflictResolveRecord,
    Debt,
    read_record,
)
from lithos_loom.subscriptions.merge_gate_record import (
    read_record as read_merge_record,
)
from lithos_loom.subscriptions.remediation_budget import (
    REMEDIATION_KEY,
    RemediationBudget,
    RemediationNotifier,
    read_budget,
)

__all__ = [
    "CONFLICT_ACTIONS",
    "CONFLICT_RESOLVED",
    "CONFLICT_RESOLVE_KEY",
    "CONFLICT_RESOLVE_SETTING",
    "PUSHED_BREADCRUMB_KEY",
    "RUN_TIMEOUT_SECONDS",
    "STRICT_WRITE_DELAYS",
    "ConflictResolveDispatch",
    "ConflictResolveRecord",
    "ConflictResolveSettings",
    "OriginRead",
    "origin_read",
    "read_record",
    "spawn_resolve",
]

# A resolution is a coder session plus a panel loop: same ceiling as remediation.
RUN_TIMEOUT_SECONDS = 4 * 3600
_OUTPUT_TAIL_CHARS = 600

# Run statuses that are a human's to decide: the merge could not be composed
# to the panel's satisfaction, or it is a shape the coder cannot edit.
_ESCALATE: frozenset[str] = frozenset(
    {"not_converged", "failed", "conflict_unsupported"}
)


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
        # every (pr_url, head, base) this boot has spent on — the in-memory
        # half of the once-per-pair bound, which no failed write can erase
        self._attempted: set[tuple[str, str, str]] = set()
        # post-push writes that have not landed yet: pr_url → the marker to
        # flush + the finding to post once it does; the PR stays held
        self._debts: dict[str, Debt] = {}

    # ── in-flight state ────────────────────────────────────────────────

    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def busy_on(self, pr_url: str) -> bool:
        """The other dispatchers' hold: a merge commit + fixes may land on
        this PR's branch at any moment — or HAVE landed and the write naming
        the push as loom's own has not (a held debt): remediation's head
        observation must stay inert until it does, or it reads the merge
        commit as a human push and resets the S5b budget."""
        return (self.busy() and self._in_flight_pr_url == pr_url) or (
            pr_url in self._debts
        )

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
        if spec.pr_url in self._debts:
            # a pushed resolution whose record + budget write has not landed:
            # flush it before anything else (the PR stays held meanwhile)
            return await self._settle_debt(spec.pr_url, ctx)
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
        if (spec.pr_url, head, base) in self._attempted:
            return "unchanged"  # this boot already spent on the pair
        prior = read_record(gate, spec.pr_url)
        same_key = prior is not None and (prior.head_sha, prior.base_sha) == (
            head,
            base,
        )
        if same_key and prior is not None:
            # one attempt per sha pair (PRD S5). Re-armed only by: a crash or
            # an abandoned reservation from ANOTHER boot (a restart is the
            # operator's fix attempt), or — decided below, once the sweep has
            # observed the checkout — a repo-mismatch refusal whose settle key
            # moved (PR #366 review F4).
            rebooted = prior.status in ("crashed", "running") and (
                prior.boot_id != self._boot_id
            )
            if not rebooted and prior.status != "repo_mismatch":
                return "unchanged"
        if await story_escalated(story_id, ctx):
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
        if (
            same_key
            and prior is not None
            and prior.status == "repo_mismatch"
            and (prior.repo_path, prior.origin_seen) == (str(repo), seen.repo.lower())
        ):
            return "unchanged"  # the refusal settled on what the sweep still sees
        attempts = 1
        # reserve the attempt on the gate BEFORE the spawn (PR #366 review
        # F2): the once-per-pair bound must not depend on a breadcrumb the
        # crashed run's outcome path may fail to write
        reservation = ConflictResolveRecord(
            spec.pr_url,
            head,
            base,
            status="running",
            attempts=attempts,
            boot_id=self._boot_id,
            repo_path=str(repo),
            origin_seen=seen.repo.lower(),
        )
        if not await strict_write(
            gate.id, {CONFLICT_RESOLVE_KEY: reservation.as_marker()}, ctx
        ):
            ctx.logger.warning(
                "[Friction] conflict-resolve: could not reserve the attempt on "
                "gate %s for %s; nothing spawned (retried next sweep)",
                gate.id,
                spec.pr_url,
            )
            return "reserve_failed"
        self._attempted.add((spec.pr_url, head, base))
        ctx.logger.info(
            "conflict-resolve: dispatching converge --resolve-conflicts for %s "
            "(head %s, base %s, attempt %d)",
            spec.pr_url,
            head[:12],
            base[:12],
            attempts,
        )
        self._in_flight_pr_url = spec.pr_url
        budget = read_budget(gate, spec.pr_url)  # the dispatch-time snapshot
        self._task = asyncio.create_task(
            self._run(gate.id, story_id, spec, repo, reservation, budget, ctx),
            name=f"conflict-resolve-{spec.pr_number}",
        )
        return "dispatched"

    async def _settle_debt(self, pr_url: str, ctx: SubscriptionContext) -> str:
        debt = self._debts[pr_url]
        if not await write_once(debt.gate_id, debt.marker, ctx):
            return "debt_pending"
        del self._debts[pr_url]
        await clear_breadcrumb(debt.story_id, ctx)
        await post_finding(debt.story_id, debt.summary, ctx)
        return "debt_settled"

    async def recover_debt(
        self,
        gate: Any,
        spec: PrGateSpec,
        story_id: str | None,
        ctx: SubscriptionContext,
    ) -> None:
        """Re-arm a held debt a previous boot left behind: the story's
        breadcrumb names a push the gate's budget does not yet know as
        loom's own. Called by the sweep BEFORE remediation observes the head,
        so the PR is held from the first sweep after a restart. Never raises."""
        if story_id is None or spec.pr_url in self._debts:
            return
        try:
            story = await ctx.lithos.task_get(task_id=story_id)
        except LithosClientError:
            return  # retried next sweep; the hold is what matters and it is cheap
        crumb = None if story is None else story.metadata.get(PUSHED_BREADCRUMB_KEY)
        if not isinstance(crumb, dict) or crumb.get("pr_url") != spec.pr_url:
            return
        pushed = crumb.get("pushed_sha")
        if not isinstance(pushed, str) or not pushed:
            return
        budget = read_budget(gate, spec.pr_url)
        if budget.last_loom_pushed_sha == pushed:
            await clear_breadcrumb(story_id, ctx)  # it landed after all
            return
        record = read_record(gate, spec.pr_url)
        if record is None or record.pushed_sha != pushed:
            record = ConflictResolveRecord(
                spec.pr_url,
                head_sha=str(crumb.get("head_sha") or ""),
                base_sha=str(crumb.get("base_sha") or ""),
                status="converged",
                attempts=1,
                pushed_sha=pushed,
                message="recovered from the story breadcrumb after a restart",
            )
        marker = {
            CONFLICT_RESOLVE_KEY: record.as_marker(),
            REMEDIATION_KEY: replace(budget, last_loom_pushed_sha=pushed).as_marker(),
        }
        summary = (
            f"{CONFLICT_RESOLVED} conflict-resolve: loom's push {pushed[:12]} onto "
            f"{spec.pr_url} (a conflict resolution recorded after a restart) is "
            f"now on the record; the next sweep re-gates at the new head."
        )
        self._debts[spec.pr_url] = Debt(gate.id, story_id, marker, summary)
        ctx.logger.warning(
            "conflict-resolve: recovered a held debt for %s from the story "
            "breadcrumb (push %s not yet on the budget); holding the PR",
            spec.pr_url,
            pushed[:12],
        )

    # ── the run ────────────────────────────────────────────────────────

    def command(
        self,
        spec: PrGateSpec,
        repo: Path,
        json_path: Path,
        story_id: str,
        *,
        head: str = "",
        base: str = "",
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
        # PR #366 review F3: the child re-fetches the PR — pin it to the pair
        # the sweep authorised, or it could spend and push on other inputs
        if head:
            cmd += ["--expect-head", head]
        if base:
            cmd += ["--expect-base", base]
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
        record: ConflictResolveRecord,
        budget: RemediationBudget,
        ctx: SubscriptionContext,
    ) -> None:
        """One resolve subprocess and its outcome. Never raises."""
        try:
            await self._run_inner(gate_id, story_id, spec, repo, record, budget, ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the slot must always free cleanly
            ctx.logger.exception("conflict-resolve: run for %s raised", spec.pr_url)
            await post_friction(
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
        budget: RemediationBudget,
        ctx: SubscriptionContext,
    ) -> None:
        path = self._json_path(gate_id)
        rc, output = await self._spawn(
            self.command(
                spec,
                repo,
                path,
                story_id,
                head=record.head_sha,
                base=record.base_sha,
            )
        )
        data = self._load(path)
        tail = output[-_OUTPUT_TAIL_CHARS:] if output else "(no output)"
        if data is None:
            await post_friction(
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
            # the child refused before touching anything: nothing was spent,
            # so the boot's memory of the pair must not block the re-arm the
            # settle key (mapped path + origin) grants when either moves
            self._attempted.discard((spec.pr_url, record.head_sha, record.base_sha))
            await post_friction(
                gate_id,
                story_id,
                record,
                f"the checkout at {repo} is not {spec.repo} "
                f"(origin says {data.get('actual_repo') or 'unknown'}); the run "
                "refused before touching anything — fix `[projects.<slug>].repo`",
                ctx,
            )
        elif status == "converged" and data.get("pushed") is True:
            await self._record_resolved(
                gate_id, story_id, spec, record, data, budget, ctx
            )
        elif status in _ESCALATE:
            await escalate(
                gate_id,
                story_id,
                spec,
                record,
                data,
                self._settings.notifier,
                ctx,
            )
        else:
            # no_conflict / head_moved / base_moved / merge_race / merged /
            # fork_unsupported / an unpushed converge: the world moved on, or
            # nothing to do — the sweep's other halves own what comes next.
            await write_record(gate_id, record, ctx)

    async def _record_resolved(
        self,
        gate_id: str,
        story_id: str,
        spec: PrGateSpec,
        record: ConflictResolveRecord,
        data: Mapping[str, Any],
        budget: RemediationBudget,
        ctx: SubscriptionContext,
    ) -> None:
        """Converged + pushed: the merge commit is loom's own push on the S5b
        budget — record and budget in ONE write, made FIRST and STRICTLY
        (PR #366 review F1): the push has happened, and once the head moved
        the trigger is gone, so nothing would re-derive a lost sha. The
        budget comes from a fresh read of the gate, else the dispatch-time
        snapshot (no other writer moved it: the PR was held). A write that
        still does not land becomes a held debt — the PR stays `busy_on`
        (remediation's head observation inert), the story gets an honest
        [Friction], and the next sweeps retry the write; the success finding
        posts only once it landed."""
        pushed = str(data.get("pushed_sha") or "")
        record = replace(record, pushed_sha=pushed)
        # the breadcrumb first, on the STORY: survives a gate write outage and
        # a restart (recover_debt reads it) — cleared once the record landed
        await write_once(
            story_id,
            {
                PUSHED_BREADCRUMB_KEY: {
                    "pr_url": spec.pr_url,
                    "pushed_sha": pushed,
                    "head_sha": record.head_sha,
                    "base_sha": record.base_sha,
                }
            },
            ctx,
        )
        try:
            fresh = await ctx.lithos.task_get(task_id=gate_id)
        except LithosClientError as exc:
            ctx.logger.warning(
                "[Friction] conflict-resolve: re-reading gate %s to record loom's "
                "push %s failed (%s); recording from the dispatch-time budget",
                gate_id,
                pushed[:12],
                exc,
            )
            fresh = None
        if fresh is not None:
            budget = read_budget(fresh, spec.pr_url)
        marker: dict[str, Any] = {
            CONFLICT_RESOLVE_KEY: record.as_marker(),
            REMEDIATION_KEY: replace(budget, last_loom_pushed_sha=pushed).as_marker(),
        }
        paths = paths_of(data)
        summary = (
            f"{CONFLICT_RESOLVED} conflict-resolve: delivered PR {spec.pr_url}'s "
            f"conflict with its base @ {record.base_sha[:12]} in "
            f"{len(paths)} path(s) ({', '.join(paths) or 'unnamed'}) was resolved "
            f"by loom and pushed as {pushed[:12]} onto the PR branch after "
            f"{data.get('rounds')} round(s) (${data.get('total_cost_usd')}); the "
            f"composed tree passed the project's check-set and the panel. The "
            f"next sweep re-gates at the new head."
        )
        if await strict_write(gate_id, marker, ctx):
            await clear_breadcrumb(story_id, ctx)
            await post_finding(story_id, summary, ctx)
            return
        self._debts[spec.pr_url] = Debt(gate_id, story_id, marker, summary)
        await post_finding(
            story_id,
            (
                f"[Friction] conflict-resolve: loom resolved {spec.pr_url}'s conflict "
                f"and pushed {pushed[:12]}, but recording that push on gate "
                f"{gate_id} did not land after {len(STRICT_WRITE_DELAYS) + 1} "
                f"attempts; the PR stays held and the record is retried every "
                f"sweep until it lands (nothing else acts on this PR meanwhile)"
            ),
            ctx,
        )
