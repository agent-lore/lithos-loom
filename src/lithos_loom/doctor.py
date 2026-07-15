"""Check-runner framework + per-domain probes for ``lithos-loom doctor``.

Vault probes verify ``vault_path`` exists, ``_lithos/`` is creatable,
and a write+read round-trip works.

Project-context probes verify every TOML ``[projects.<slug>]`` entry
has a matching Lithos project-context doc at ``projects/<slug>/``
(i.e. the slug is canonical, not just host-local). Lithos is the
project registry; a TOML entry referencing a slug Lithos doesn't know
about is a misconfiguration the operator should fix.

Public surface:

* :class:`CheckResult` — frozen dataclass with ``name``, ``passed``,
  ``message``.
* :func:`run_vault_checks` — returns ``list[CheckResult]``; empty
  when ``[obsidian_sync]`` isn't configured (caller decides how to
  report the skip).
* :func:`run_project_checks` — async; returns ``list[CheckResult]``
  for the TOML-vs-Lithos slug presence check. Skips cleanly when
  ``[projects]`` is empty or Lithos is unreachable (transient
  outages mustn't fail doctor).
* :func:`run_task_graph_checks` — async; probes the Lithos task-graph
  extension end to end (Epic G US1). Returns one ``task_graph_extension``
  :class:`CheckResult`; the daemon boot gate refuses to start on a fail.
* :func:`format_results` — pretty-print to a list of lines for the
  CLI to echo.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from lithos_loom.config import LoomConfig
from lithos_loom.errors import LithosClientError
from lithos_loom.lithos_client import Blocker, NoteSummary, TaskClient

_PROJECTS_PATH_PREFIX = "projects/"
_PROJECT_CONTEXT_TAG = "project-context"

TASK_GRAPH_CHECK = "task_graph_extension"
"""Name of the task-graph capability check (Epic G US1).

The daemon boot gate keys off this name: a failing ``task_graph_extension``
result means refuse to start. A connectivity failure surfaces under the
separate ``lithos_unreachable`` name so callers can tell "server incompatible"
from "server unreachable"."""

_PROBE_TASK_TITLE = "[loom-doctor] task-graph probe (auto-cleaned)"

PROBE_FILENAME = ".doctor-probe.tmp"
"""Fixed filename used for the write+read round-trip probe.

Single name (rather than timestamped) so re-runs don't accumulate
files in the vault. Deleted on successful round-trip; left on
failure for operator inspection.
"""


@dataclass(frozen=True)
class CheckResult:
    """One doctor-check outcome.

    The CLI walks a ``list[CheckResult]`` to compute the summary +
    exit code. Frozen so test fixtures can rely on equality.
    """

    name: str
    passed: bool
    message: str


def run_vault_checks(cfg: LoomConfig) -> list[CheckResult]:
    """Run the three vault probes against ``cfg``.

    Returns an empty list when ``[obsidian_sync]`` isn't configured —
    the caller (the ``doctor`` CLI command) prints a skip note in that
    case rather than treating it as a failure. Respects the spawn-gate
    model: hosts that don't run the projection child shouldn't see
    spurious vault failures.

    Short-circuits on the first failed check (subsequent checks would
    cascade — there's no point trying to write a probe file when the
    vault directory itself doesn't exist).
    """
    obs = cfg.obsidian_sync
    if obs is None:
        return []
    results: list[CheckResult] = [_check_vault_path_exists(obs.vault_path)]
    if not results[-1].passed:
        return results
    results.append(_check_lithos_subdir_creatable(obs.vault_path))
    if not results[-1].passed:
        return results
    results.append(_check_probe_write_read(obs.vault_path))
    return results


def _check_vault_path_exists(vault_path: Path) -> CheckResult:
    """Verify ``vault_path`` exists as a directory.

    ``Path.exists()`` follows symlinks, so a broken symlink reports
    as missing — the right behaviour (operator's config points at
    something that isn't actually there).
    """
    if not vault_path.exists():
        return CheckResult(
            "vault_path_exists",
            False,
            f"{vault_path} does not exist",
        )
    if not vault_path.is_dir():
        return CheckResult(
            "vault_path_exists",
            False,
            f"{vault_path} exists but is not a directory",
        )
    return CheckResult("vault_path_exists", True, str(vault_path))


def _check_lithos_subdir_creatable(vault_path: Path) -> CheckResult:
    """Verify ``<vault_path>/_lithos/`` exists or can be created.

    ``mkdir(parents=True, exist_ok=True)`` is idempotent — already-
    present subdir is fine. Catches ``OSError`` (permissions,
    read-only mount, weird filesystem) and reports the underlying
    message so the operator can act.
    """
    subdir = vault_path / "_lithos"
    try:
        subdir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return CheckResult(
            "lithos_subdir_creatable",
            False,
            f"could not create {subdir}: {exc}",
        )
    return CheckResult("lithos_subdir_creatable", True, str(subdir))


def _check_probe_write_read(vault_path: Path) -> CheckResult:
    """Write a dated probe string, read it back, assert equality.

    Cleans up on success. Leaves the probe file on disk for operator
    inspection when the round-trip fails — the dated content makes
    it easy to spot when investigating.
    """
    probe = vault_path / "_lithos" / PROBE_FILENAME
    content = f"lithos-loom doctor probe at {datetime.now(UTC).isoformat()}\n"
    try:
        probe.write_text(content, encoding="utf-8")
        readback = probe.read_text(encoding="utf-8")
    except OSError as exc:
        return CheckResult(
            "probe_write_read_roundtrip",
            False,
            f"could not write/read {probe}: {exc}",
        )
    if readback != content:
        return CheckResult(
            "probe_write_read_roundtrip",
            False,
            f"readback mismatch at {probe}",
        )
    # Best-effort cleanup; a failed unlink doesn't invalidate the
    # round-trip success (the file's just lingering, not corrupting).
    with contextlib.suppress(OSError):
        probe.unlink()
    return CheckResult(
        "probe_write_read_roundtrip",
        True,
        f"{len(content)} bytes round-tripped",
    )


def format_results(results: list[CheckResult]) -> list[str]:
    """Render check results as indented bullet lines for CLI echo."""
    lines: list[str] = []
    for r in results:
        mark = "✓" if r.passed else "✗"
        lines.append(f"  {mark} {r.name}: {r.message}")
    return lines


# ── Project-context probes ──────────────────────────────────────────────


class _ProjectsClient(Protocol):
    """Minimum surface the project-context probe depends on.

    Kept narrow so tests can stub with a couple of methods rather
    than the full :class:`~lithos_loom.lithos_client.LithosClient`."""

    async def note_list(
        self,
        *,
        path_prefix: str | None = None,
        tags: list[str] | None = None,
        limit: int = 100,
    ) -> list[NoteSummary]: ...


async def run_project_checks(
    cfg: LoomConfig,
    client: _ProjectsClient,
) -> list[CheckResult]:
    """Verify every TOML ``[projects.<slug>]`` entry has a matching
    Lithos project-context doc.

    Returns:

    - Empty list when ``[projects]`` is empty — nothing to check.
    - One ``CheckResult`` per TOML slug. Pass = Lithos has at least
      one ``project-context``-tagged doc whose path falls under
      ``projects/<slug>/``. Fail = no match (operator should either
      create the Lithos doc or drop the TOML stanza).
    - A single failing ``lithos_unreachable`` result if the
      ``note_list`` call raises a transport error
      (``LithosClientError`` / ``OSError``). Doctor doesn't fail the
      run on transient connectivity issues — the operator can
      retry — but the message tells them so.

    Lithos-side docs WITHOUT a TOML entry are legitimate (other
    hosts may have automation for them, or they may be non-coding
    projects that never need an automation overlay). We don't surface
    those here — that's ``project list``'s job.
    """
    if not cfg.projects:
        return []
    try:
        summaries = await client.note_list(
            path_prefix=_PROJECTS_PATH_PREFIX,
            tags=[_PROJECT_CONTEXT_TAG],
        )
    except (LithosClientError, OSError) as exc:
        return [
            CheckResult(
                "lithos_unreachable",
                False,
                f"could not enumerate Lithos projects: {exc}",
            )
        ]

    lithos_slugs = {s.slug for s in summaries if s.slug}
    results: list[CheckResult] = []
    for slug in sorted(cfg.projects):
        if slug in lithos_slugs:
            results.append(
                CheckResult(
                    f"toml_project[{slug}]",
                    True,
                    "matches Lithos project context doc",
                )
            )
        else:
            results.append(
                CheckResult(
                    f"toml_project[{slug}]",
                    False,
                    (
                        f"no Lithos doc tagged 'project-context' under "
                        f"projects/{slug}/ — either create one in Lithos "
                        f"or remove the TOML stanza"
                    ),
                )
            )
    return results


# ── Task-graph capability probe (Epic G US1) ────────────────────────────


async def run_task_graph_checks(client: TaskClient, *, agent: str) -> list[CheckResult]:
    """Probe the Lithos task-graph extension end to end.

    Asserts the surface the extension advertises actually persists, not just
    that the tools return without error:

    * ``task_type`` — create a non-default ``epic`` and read it back with
      ``task_get`` (a server that ignores the field would default to ``task``);
    * ready-queue — a ``blocks`` predecessor excludes the dependent from
      :meth:`task_ready` and names it in :meth:`task_blocked` (``kind="task"``);
    * the epic-G precondition — cancelling the blocker keeps the dependent
      blocked as ``blocker_unsatisfiable`` (a released dependent-of-a-
      cancelled-task would be dispatched wrongly);
    * ``task_spawn`` — the follow-on task exists (``task_get``) *and* its
      ``discovered_from`` source edge was persisted (``task_edge_list``).

    The probe tasks are cancelled on the way out (even on failure).

    Returns exactly one :class:`CheckResult` named ``task_graph_extension``:
    passing when every invariant holds, failing on the first violation or on
    a graph-tool error (the boot gate refuses to start on the failure). A
    transport error mid-probe is folded into the same failing check — a fully
    unreachable server is classified by the caller's connect wrapper as
    ``lithos_unreachable`` instead.
    """
    probe_tag = f"loom-doctor-probe:{uuid4().hex}"
    created: list[str] = []

    def _fail(message: str) -> list[CheckResult]:
        return [CheckResult(TASK_GRAPH_CHECK, False, message)]

    try:
        # task_type must persist server-side. A server that ignores it would
        # store the default "task", so create a *non-default* epic and read it
        # back — ``any(t.task_type)`` would pass on the client-side default and
        # prove nothing.
        epic = await client.task_create(
            title=_PROBE_TASK_TITLE, agent=agent, tags=[probe_tag], task_type="epic"
        )
        created.append(epic)
        epic_record = await client.task_get(task_id=epic)
        if epic_record is None or epic_record.task_type != "epic":
            got = "missing" if epic_record is None else repr(epic_record.task_type)
            return _fail(
                f"task_type did not persist (created an epic, read back {got})"
            )

        blocker = await client.task_create(
            title=_PROBE_TASK_TITLE, agent=agent, tags=[probe_tag]
        )
        created.append(blocker)
        dependent = await client.task_create(
            title=_PROBE_TASK_TITLE, agent=agent, tags=[probe_tag]
        )
        created.append(dependent)
        await client.task_edge_upsert(
            from_task_id=blocker, to_task_id=dependent, type="blocks", agent=agent
        )

        ready_ids = {t.id for t in await client.task_ready(tags=[probe_tag])}
        if blocker not in ready_ids or dependent in ready_ids:
            return _fail(
                "ready-queue did not honour the blocks edge "
                "(blocker missing from, or dependent leaked into, task_ready)"
            )

        reasons = await _blockers_of(client, dependent, tag=probe_tag)
        if not any(b.kind == "task" and b.task_id == blocker for b in reasons):
            return _fail(
                "task_blocked did not report the open predecessor as a blocker"
            )

        # Precondition: cancelling the blocker must NOT release the dependent.
        await client.task_cancel(
            task_id=blocker, agent=agent, reason="doctor task-graph probe"
        )
        if dependent in {t.id for t in await client.task_ready(tags=[probe_tag])}:
            return _fail(
                "cancelled blocker wrongly released its dependent — the ready-queue "
                "would dispatch a task whose predecessor was cancelled"
            )
        reasons = await _blockers_of(client, dependent, tag=probe_tag)
        if not any(b.kind == "blocker_unsatisfiable" for b in reasons):
            return _fail("a cancelled blocker did not surface as blocker_unsatisfiable")

        gate_failure = await _probe_gates(client, agent, probe_tag, created)
        if gate_failure is not None:
            return [gate_failure]

        # task_spawn must persist BOTH the follow-on task and its source edge —
        # a bare returned id proves neither.
        spawned = await client.task_spawn(
            source_task_id=dependent, title=_PROBE_TASK_TITLE, agent=agent
        )
        created.append(spawned)
        if await client.task_get(task_id=spawned) is None:
            return _fail(
                "task_spawn returned an id but the spawned task does not exist"
            )
        spawn_edges = await client.task_edge_list(task_id=spawned)
        if not any(
            e.type == "discovered_from" and e.from_task_id == dependent
            for e in spawn_edges
        ):
            return _fail(
                "task_spawn did not persist the discovered_from edge from its source"
            )
    except (LithosClientError, OSError) as exc:
        return _fail(f"task-graph extension probe failed: {exc}")
    finally:
        for task_id in created:
            with contextlib.suppress(LithosClientError, OSError):
                await client.task_cancel(
                    task_id=task_id, agent=agent, reason="doctor task-graph probe"
                )

    return [
        CheckResult(
            TASK_GRAPH_CHECK,
            True,
            "task-graph extension present (task_type persistence, ready/blocked "
            "+ cancelled-blocker precondition, gate block + resolution, "
            "task_spawn task+edge — all verified)",
        )
    ]


async def _probe_gates(
    client: TaskClient, agent: str, probe_tag: str, created: list[str]
) -> CheckResult | None:
    """Probe gate semantics (extension Phase 3). ``None`` = all good.

    Epic H encodes "PR raised, awaiting human merge" as a `pr` gate, so a
    server without gate semantics would leave every delivered story either
    stuck behind a gate nothing can resolve, or — worse — re-developed into a
    duplicate PR because the gate never withheld it from the ready frontier.

    Appends everything it creates to *created* so the caller's ``finally``
    cleans up even when a leg fails mid-probe.
    """
    gate = await client.task_create(
        title=_PROBE_TASK_TITLE,
        agent=agent,
        tags=[probe_tag],
        task_type="gate",
        metadata={"gate_type": "pr"},
    )
    created.append(gate)
    waiter = await client.task_create(
        title=_PROBE_TASK_TITLE, agent=agent, tags=[probe_tag]
    )
    created.append(waiter)
    await client.task_edge_upsert(
        from_task_id=gate, to_task_id=waiter, type="waits_on_gate", agent=agent
    )

    ready_ids = {t.id for t in await client.task_ready(tags=[probe_tag])}
    if waiter in ready_ids or gate in ready_ids:
        return CheckResult(
            TASK_GRAPH_CHECK,
            False,
            "an unresolved gate did not withhold its waiter from task_ready "
            "(or the gate itself leaked in as workable)",
        )
    reasons = await _blockers_of(client, waiter, tag=probe_tag)
    if not any(b.kind == "gate" and b.task_id == gate for b in reasons):
        return CheckResult(
            TASK_GRAPH_CHECK,
            False,
            "task_blocked did not report the open gate as a blocker",
        )

    # Resolving a gate = completing it, and that must release the waiter.
    unblocked = await client.task_complete(task_id=gate, agent=agent)
    if waiter not in unblocked:
        return CheckResult(
            TASK_GRAPH_CHECK,
            False,
            "completing a gate did not report its waiter as newly unblocked",
        )
    if waiter not in {t.id for t in await client.task_ready(tags=[probe_tag])}:
        return CheckResult(
            TASK_GRAPH_CHECK,
            False,
            "completing a gate did not release its waiter into task_ready",
        )
    return None


async def _blockers_of(
    client: TaskClient, task_id: str, *, tag: str
) -> tuple[Blocker, ...]:
    """The structured blocker reasons Lithos reports for ``task_id`` within the
    probe's tag scope (empty if it isn't in the blocked set)."""
    blocked = {bt.task.id: bt for bt in await client.task_blocked(tags=[tag])}
    waiting = blocked.get(task_id)
    return waiting.blockers if waiting is not None else ()
