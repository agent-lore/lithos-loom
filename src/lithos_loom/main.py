"""Top-level CLI dispatcher for the ``lithos-loom`` binary.

Subcommands:

* ``lithos-loom run`` — start the daemon (supervisor + child processes)
* ``lithos-loom doctor`` — verify the vault is writable, Lithos speaks
  the task-graph extension, and project TOML entries match Lithos
* ``lithos-loom validate-config`` — typecheck the TOML config
* ``lithos-loom validate-config --dry-run`` — also poll Lithos and print
  which routes / subscriptions would fire for each open task
* ``lithos-loom config --show`` — print the merged effective config
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import typer

from lithos_loom.bus import Event, EventBus
from lithos_loom.cli import develop_app, obsidian_sync_app, project_app, task_app
from lithos_loom.cli.gates import collect_gate_rows, render_report
from lithos_loom.config import (
    LoomConfig,
    RouteConfig,
    SubscriptionConfig,
    load_config,
)
from lithos_loom.doctor import (
    CheckResult,
    format_results,
    run_project_checks,
    run_task_graph_checks,
    run_vault_checks,
)
from lithos_loom.errors import LithosClientError, LithosLoomError
from lithos_loom.lithos_client import BlockedTask, Blocker, LithosClient, Task
from lithos_loom.subscriptions import (
    SUBSCRIPTION_ACTIONS,
    SubscriptionContext,
    build_runners,
)
from lithos_loom.subscriptions._noop import handle as _noop_handle
from lithos_loom.subscriptions.route_runner import READY_QUERY_LIMIT
from lithos_loom.supervisor import Supervisor, default_categories

app = typer.Typer(
    name="lithos-loom",
    help="Workflow orchestration daemon for Lithos tasks.",
    no_args_is_help=True,
    add_completion=True,
)
app.add_typer(task_app, name="task")
app.add_typer(project_app, name="project")
app.add_typer(obsidian_sync_app, name="obsidian-sync")
app.add_typer(develop_app, name="develop")

# On-demand eval harnesses (#183) — not part of `make check`; host-only.
from lithos_loom.evals.review.cli import eval_app  # noqa: E402

app.add_typer(eval_app, name="eval")


@app.command()
def run(
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Explicit TOML config path (overrides LITHOS_LOOM_CONFIG).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview matched tasks; no claims or writes.",
    ),
) -> None:
    """Start the daemon: poll Lithos, claim matching tasks, dispatch plugins."""
    cfg = _load_or_exit(config)
    if dry_run:
        # `lithos-loom run --dry-run` is shorthand for the dedicated
        # validate-config subcommand below, which is the canonical home
        # for the simulation logic. Forward and exit with its code.
        raise typer.Exit(_run_dry_run(cfg))
    # Configure root logging so the supervisor's own INFO/WARNING lines
    # (spawned child, [Friction] child crash, SIGKILL fallback) reach the
    # operator. Child processes call basicConfig themselves; this only
    # affects the parent. basicConfig is a no-op if pytest has already
    # attached its capture handler, so tests are unaffected.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs every MCP-over-SSE message at INFO, drowning out our own
    # operational logs. Demote to WARNING — connection failures still
    # surface, per-call traffic doesn't.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # Boot gate (Epic G US1): refuse to start against a Lithos that lacks the
    # task-graph extension — the runner's dependency scheduling relies on it, so
    # an incompatible server must surface at boot, not mid-PRD. This is a real
    # startup round-trip; if Lithos is unreachable / mid-restart the daemon also
    # won't start (re-run once Lithos is back).
    _require_task_graph_or_exit(cfg)
    sup = Supervisor(cfg, default_categories())
    exit_code = asyncio.run(sup.run())
    raise typer.Exit(exit_code)


@app.command()
def doctor(
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Explicit TOML config path.",
    ),
) -> None:
    """Verify the vault is writable, Lithos speaks the task-graph extension,
    and project TOML entries match Lithos.

    Runs three vault probes (vault_path exists, ``_lithos/`` creatable,
    write+read round-trip), probes the Lithos task-graph extension end to end
    (the same capability the daemon boot gate requires — Epic G US1), and
    verifies every TOML ``[projects.<slug>]`` entry has a matching Lithos
    project-context doc.

    Exit codes: 0 if all checks passed (or were skipped); 1 if any
    check failed; 2 if the config couldn't be loaded.
    """
    cfg = _load_or_exit(config)
    typer.echo(f"lithos-loom doctor: {cfg.source_path}")

    vault_results = run_vault_checks(cfg)
    if vault_results:
        for line in format_results(vault_results):
            typer.echo(line)
    else:
        typer.echo("  ⊘ vault probe skipped: no [obsidian_sync] in config")

    # Lithos task-graph capability: the same probe the daemon boot gate runs.
    # Always checked (it's a server capability, not project-dependent); a
    # connectivity failure surfaces as a single failing check, not a crash.
    task_graph_results = asyncio.run(_run_task_graph_checks_async(cfg))
    for line in format_results(task_graph_results):
        typer.echo(line)

    # TOML project entries must match Lithos project-context docs. Skip
    # cleanly when [projects] is empty; otherwise spin up a one-shot
    # LithosClient. Transport failures surface as a single failing
    # check rather than crashing the doctor run.
    project_results: list = []
    if cfg.projects:
        project_results = asyncio.run(_run_project_checks_async(cfg))
        for line in format_results(project_results):
            typer.echo(line)
    else:
        typer.echo("  ⊘ project probe skipped: [projects] table is empty")

    all_results = vault_results + task_graph_results + project_results
    failed = [r for r in all_results if not r.passed]
    passed = [r for r in all_results if r.passed]
    if failed:
        typer.echo(f"FAIL: {len(passed)} passed, {len(failed)} failed")
        raise typer.Exit(1)
    typer.echo(f"OK: {len(passed)} passed, 0 failed")


async def _run_project_checks_async(cfg: LoomConfig) -> list:
    """One-shot ``LithosClient`` wrapper around
    :func:`run_project_checks`. Mirrors the
    :func:`_create_task_async` pattern in ``cli/task.py``."""
    async with LithosClient(
        cfg.orchestrator.lithos_url, agent_id=cfg.orchestrator.agent_id
    ) as client:
        return await run_project_checks(cfg, client)


async def _run_task_graph_checks_async(cfg: LoomConfig) -> list[CheckResult]:
    """One-shot ``LithosClient`` wrapper around :func:`run_task_graph_checks`.

    A failure to even connect (``async with`` entry raises) is classified as
    ``lithos_unreachable`` — distinct from the ``task_graph_extension`` failure
    the probe itself returns when the server is reachable but incompatible —
    so callers can word the message accordingly. Both still fail the boot gate.

    ``LithosClient.__aenter__`` surfaces a transport failure as whatever the
    MCP/anyio connect raised — a plain ``OSError`` or, when it happens inside a
    task group, an ``ExceptionGroup`` wrapping (e.g.) ``httpx.ConnectError`` —
    so the catch spans both, plus ``LithosClientError``. We catch
    ``ExceptionGroup`` (all-``Exception`` leaves) rather than the wider
    ``BaseExceptionGroup`` so a group carrying ``KeyboardInterrupt`` /
    ``SystemExit`` / bare ``CancelledError`` still propagates.
    """
    try:
        async with LithosClient(
            cfg.orchestrator.lithos_url, agent_id=cfg.orchestrator.agent_id
        ) as client:
            return await run_task_graph_checks(client, agent=cfg.orchestrator.agent_id)
    except (LithosClientError, OSError, ExceptionGroup) as exc:
        return [
            CheckResult(
                "lithos_unreachable",
                False,
                f"could not reach Lithos at {cfg.orchestrator.lithos_url}: {exc}",
            )
        ]


def _require_task_graph_or_exit(cfg: LoomConfig) -> None:
    """Boot gate: run the task-graph probe and refuse to start on any failure.

    Echoes each check line, then exits non-zero if the extension is missing /
    broken or Lithos is unreachable — the daemon cannot schedule dependencies
    without the server-side ready-queue, so it must not start half-crippled.
    """
    results = asyncio.run(_run_task_graph_checks_async(cfg))
    for line in format_results(results):
        typer.echo(line)
    if any(not r.passed for r in results):
        typer.echo(
            "refusing to start: the Lithos task-graph extension is required "
            "(run `lithos-loom doctor` to diagnose)"
        )
        raise typer.Exit(1)


@app.command("validate-config")
def validate_config(
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Explicit TOML config path.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        "-n",
        help=(
            "Connect to Lithos and simulate routing against the current "
            "open-task list; print which routes/subscriptions would fire. "
            "Non-mutating."
        ),
    ),
) -> None:
    """Typecheck the TOML; with ``--dry-run`` also simulate routing."""
    cfg = _load_or_exit(config)
    typer.echo(f"OK: {cfg.source_path}")
    typer.echo(f"  orchestrator.agent_id: {cfg.orchestrator.agent_id}")
    typer.echo(f"  orchestrator.lithos_url: {cfg.orchestrator.lithos_url}")
    typer.echo(f"  projects: {sorted(cfg.projects)}")
    typer.echo(f"  routes: {[r.name for r in cfg.routes]}")
    typer.echo(f"  subscriptions: {[s.name for s in cfg.subscriptions]}")
    if cfg.environment:
        typer.echo(f"  environment: {cfg.environment}")
    if dry_run:
        raise typer.Exit(_run_dry_run(cfg))


@app.command("config")
def show_config(
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Explicit TOML config path.",
    ),
    show: bool = typer.Option(
        False, "--show", help="Print the merged effective config."
    ),
) -> None:
    """Inspect the loaded configuration."""
    if not show:
        typer.echo("Use --show to print the merged effective config.")
        raise typer.Exit(2)
    cfg = _load_or_exit(config)
    typer.echo(repr(cfg))


@app.command("gates")
def gates(
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Explicit TOML config path (overrides LITHOS_LOOM_CONFIG).",
    ),
) -> None:
    """List open PR gates and each gate's waiter health (read-only).

    A ``pr`` gate models "PR raised, awaiting human merge" and blocks its story
    by a ``waits_on_gate`` edge (Epic H). This command enumerates the open
    gates and, for each, the story it blocks plus a one-word *health*
    (``ok`` / ``orphan`` / ``malformed`` / ``waiter-gone`` /
    ``waiter-resolved``) classifying the wiring the resolver depends on — so a
    stuck gate is diagnosable without touching GitHub or mutating anything.

    Non-mutating: one open-task sweep plus a per-gate edge/waiter read. Exit
    codes: `0` on a successful listing (regardless of gate health); `1` if the
    config can't load or Lithos is unreachable.
    """
    cfg = _load_or_exit(config)
    try:
        rows = asyncio.run(_collect_gates_async(cfg))
    except OSError as exc:
        typer.echo(
            f"lithos-loom: could not reach Lithos at "
            f"{cfg.orchestrator.lithos_url} ({exc}); "
            "run `lithos-loom doctor` to diagnose connectivity",
            err=True,
        )
        raise typer.Exit(1) from exc
    except LithosClientError as exc:
        typer.echo(f"lithos-loom: listing gates failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    for line in render_report(rows):
        typer.echo(line)


async def _collect_gates_async(cfg: LoomConfig) -> list:
    """One-shot ``LithosClient`` wrapper around :func:`collect_gate_rows`.

    Mirrors the ``_create_task_async`` / ``_rows_from_lithos`` pattern: the
    client is an async context manager, so the sync Typer command wraps it in
    ``asyncio.run``."""
    async with LithosClient(
        cfg.orchestrator.lithos_url, agent_id=cfg.orchestrator.agent_id
    ) as client:
        return await collect_gate_rows(client)


def _load_or_exit(config: Path | None) -> LoomConfig:
    try:
        return load_config(config)
    except LithosLoomError as exc:
        typer.echo(f"lithos-loom: {exc}", err=True)
        sys.exit(1)


# ── --dry-run simulation ───────────────────────────────────────────────


def _run_dry_run(cfg: LoomConfig) -> int:
    """Execute the dry-run simulation and return a CLI exit code."""
    try:
        return asyncio.run(_dry_run_async(cfg))
    except OSError as exc:
        typer.echo(
            f"lithos-loom: could not reach Lithos at "
            f"{cfg.orchestrator.lithos_url} ({exc}); "
            "run `lithos-loom doctor` to diagnose connectivity",
            err=True,
        )
        return 2
    except LithosLoomError as exc:
        typer.echo(f"lithos-loom: dry-run failed: {exc}", err=True)
        return 1


async def _dry_run_async(cfg: LoomConfig) -> int:
    async with LithosClient(
        cfg.orchestrator.lithos_url, agent_id=cfg.orchestrator.agent_id
    ) as client:
        tasks = await client.task_list(status="open", with_claims=True)
        # US7: readiness and the reasons behind it are Lithos's answer. The
        # dry-run asks the same ready-queue the runner dispatches off (US4),
        # rather than re-deriving it from metadata.depends_on (the mirror
        # US5 deletes) — so the report can't drift from the runtime.
        ready = await client.task_ready(limit=READY_QUERY_LIMIT, with_claims=False)
        blocked = await client.task_blocked(limit=READY_QUERY_LIMIT)
    _print_dry_run_report(cfg, tasks, _Readiness.from_queries(ready, blocked))
    return 0


@dataclass(frozen=True)
class _Readiness:
    """Lithos's answer to "what can run, and why not" (US7).

    Bundles one ``task_ready`` + one ``task_blocked`` sweep so the report can
    render each task's outcome without a per-task round-trip.
    """

    ready_ids: frozenset[str]
    blockers: Mapping[str, tuple[Blocker, ...]]
    truncated: bool

    @classmethod
    def from_queries(cls, ready: list[Task], blocked: list[BlockedTask]) -> _Readiness:
        return cls(
            ready_ids=frozenset(task.id for task in ready),
            blockers={bt.task.id: bt.blockers for bt in blocked},
            # Neither query has a per-task filter, so a full page means the
            # sweep was cut short and rows below may be wrong. Say so rather
            # than printing a confidently incomplete picture.
            truncated=(
                len(ready) >= READY_QUERY_LIMIT or len(blocked) >= READY_QUERY_LIMIT
            ),
        )

    def defer_reason(self, task_id: str) -> str:
        """Why ``task_id`` isn't dispatchable, in Lithos's own terms."""
        blockers = self.blockers.get(task_id, ())
        if blockers:
            return "; ".join(_format_blocker(b) for b in blockers)
        # Ready-queue omissions that aren't blocker-shaped: a gate/epic task
        # (never dispatchable work) or a frontier this sweep didn't see.
        return "not on Lithos's ready frontier"


def _format_blocker(blocker: Blocker) -> str:
    """Render one structured blocker reason for the dry-run table.

    ``kind`` is the machine-meaningful part (``task`` — just waiting; ``gate``;
    ``blocker_unsatisfiable`` — the predecessor was cancelled, so this needs
    intervention; ``cycle``), and the id + status say WHICH predecessor. Falls
    back to the server's own message when there's no id to name.
    """
    if blocker.task_id:
        status = f" ({blocker.status})" if blocker.status else ""
        return f"{blocker.kind}: {blocker.task_id}{status}"
    return f"{blocker.kind}: {blocker.message}" if blocker.message else blocker.kind


def _print_dry_run_report(
    cfg: LoomConfig,
    tasks: list[Task],
    readiness: _Readiness,
) -> int:
    """Emit the dry-run table + orphan / dead-config summary."""
    typer.echo("")
    typer.echo("── Dry-run simulation ──────────────────────────────────")
    typer.echo(f"  open tasks:     {len(tasks)}")
    typer.echo(f"  routes:         {len(cfg.routes)}")
    typer.echo(f"  subscriptions:  {len(cfg.subscriptions)}")
    if readiness.truncated:
        typer.echo(
            f"  ⚠ the ready/blocked sweep hit its {READY_QUERY_LIMIT}-task query "
            "limit; rows below may be incomplete"
        )
    typer.echo("")

    # Tag-matched (whether or not ready) vs would-claim-now. Orphan / dead
    # config are *routing* questions, so they key off MATCHED: a deferred task
    # is routed and waiting on the ready-queue, not missing config, and saying
    # otherwise would send the operator to fix routing that is already correct.
    matched_routes: set[str] = set()
    fired_subs: set[str] = set()
    orphan_tasks: list[Task] = []
    deferred_tasks: list[Task] = []

    sub_predicates = _build_subscription_predicates(cfg.subscriptions)

    if not tasks:
        typer.echo("  (no open tasks; nothing to simulate)")
    for task in tasks:
        any_match = False
        route_fired = False
        route_matched = False
        title_summary = f"{task.id}  {task.title!r}"
        typer.echo(title_summary)
        for route in cfg.routes:
            would_fire, defer_reason = _route_outcome(route, task, readiness)
            if would_fire:
                marker = "✓ (claim)"
            elif defer_reason:
                marker = f"deferred ({defer_reason})"
            else:
                marker = "—"
            typer.echo(f"    route:{route.name:<30} {marker}")
            if would_fire or defer_reason:
                matched_routes.add(route.name)
                route_matched = True
                any_match = True
            route_fired = route_fired or would_fire
        for spec in cfg.subscriptions:
            would_fire = sub_predicates[spec.name](task)
            marker = "✓ (would fire)" if would_fire else "—"
            typer.echo(f"    subscription:{spec.name:<23} {marker}")
            if would_fire:
                fired_subs.add(spec.name)
                any_match = True
        if not any_match:
            orphan_tasks.append(task)
        elif route_matched and not route_fired:
            deferred_tasks.append(task)

    typer.echo("")
    typer.echo("── Summary ─────────────────────────────────────────────")
    if orphan_tasks:
        typer.echo(f"  orphan tasks ({len(orphan_tasks)}):")
        for task in orphan_tasks:
            typer.echo(f"    {task.id}  {task.title!r}")
    else:
        typer.echo("  no orphan tasks")

    if deferred_tasks:
        # Not a config problem — surfaced so the shorter orphan list doesn't
        # read as "nothing to do here". Per-task reasons are in the table.
        typer.echo(
            f"  deferred tasks ({len(deferred_tasks)}) — routed, waiting on "
            "Lithos's ready-queue:"
        )
        for task in deferred_tasks:
            typer.echo(f"    {task.id}  {task.title!r}")

    dead_routes = [r.name for r in cfg.routes if r.name not in matched_routes]
    dead_subs = [s.name for s in cfg.subscriptions if s.name not in fired_subs]
    if dead_routes:
        typer.echo(f"  dead routes ({len(dead_routes)}):")
        for name in dead_routes:
            typer.echo(f"    {name}")
    if dead_subs:
        typer.echo(f"  dead subscriptions ({len(dead_subs)}):")
        for name in dead_subs:
            typer.echo(f"    {name}")
    if not dead_routes and not dead_subs:
        typer.echo("  no dead config (every route + subscription matched ≥1 task)")

    return 0


def _route_outcome(
    route: RouteConfig,
    task: Task,
    readiness: _Readiness,
) -> tuple[bool, str | None]:
    """Mirror :class:`RouteRunner`: status + tags + Lithos's ready frontier.

    Returns ``(would_fire, defer_reason)``. The defer reason is non-None when
    the tag filter passes but Lithos doesn't consider the task ready — the
    operator should see "deferred" *and why* (which predecessor, which gate,
    or a cycle), not just "—", so "doesn't match" and "matches-but-blocked"
    stay distinguishable.
    """
    if task.status != "open":
        return False, None
    if not set(route.match.tags).issubset(set(task.tags)):
        return False, None
    if task.id in readiness.ready_ids:
        return True, None
    return False, readiness.defer_reason(task.id)


def _build_subscription_predicates(
    subs: Iterable[SubscriptionConfig],
) -> dict[str, Any]:
    """Compile each subscription into a callable ``(task) -> bool`` predicate.

    Uses :func:`build_runners` so the dry-run uses exactly the matcher
    machinery the runtime would — same structural-match semantics, same
    where-expression scope, same handler-action validation.

    The handler map is every known action bound to the stateless ``noop``
    handler: the dry-run never dispatches (``lithos=None``), so the handler
    body is irrelevant — the map exists only so ``build_runners`` can
    validate each config action against :data:`SUBSCRIPTION_ACTIONS` and
    reject a typo'd action as an unknown handler.
    """
    handlers: dict[str, Any] = dict.fromkeys(SUBSCRIPTION_ACTIONS, _noop_handle)
    bus = EventBus()
    ctx = SubscriptionContext(
        lithos=None,  # never invoked: dry-run does not dispatch handlers
        logger=logging.getLogger("lithos_loom.dry_run"),
        agent_id="dry-run",
    )
    runners = build_runners(bus=bus, specs=tuple(subs), handlers=handlers, ctx=ctx)
    sub_to_test: dict[str, Any] = {}
    for runner in runners:
        sub = runner.subscription

        def _predicate(task: Task, sub_local: Any = sub) -> bool:
            # A subscription "would fire" for this task iff there is at
            # least one event type in its on-list whose synthetic event
            # for this task passes the structural match + where predicate.
            # Hard-coding type="lithos.task.created" would silently report
            # `on = "lithos.task.updated"` subscriptions as never firing.
            payload = MappingProxyType(
                {
                    "id": task.id,
                    "title": task.title,
                    "status": task.status,
                    "tags": list(task.tags),
                    "metadata": dict(task.metadata),
                    "claims": [dict(c) for c in task.claims],
                }
            )
            timestamp = datetime.now(UTC)
            for event_type in sub_local.event_types:
                evt = Event(type=event_type, timestamp=timestamp, payload=payload)
                if sub_local.matches(evt):
                    return True
            return False

        sub_to_test[runner.spec.name] = _predicate
    return sub_to_test


if __name__ == "__main__":
    app()
