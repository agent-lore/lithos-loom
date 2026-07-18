"""``lithos-loom gates`` — read-only inventory of open ``pr`` gates (Epic H).

A ``pr`` gate (``task_type="gate"``, ``metadata.gate_type="pr"``) models
"PR raised, awaiting human merge" and blocks its story by a ``waits_on_gate``
edge (see :mod:`lithos_loom.gates`). The github-watcher resolves a gate when
its PR merges; until then the operator has no single view of *which* gates are
open and whether each is wired to a healthy waiter.

This command is that view. It is **read-only** — it lists open gates and, for
each, the story it blocks plus a one-word *health* classifying the gate/waiter
wiring the resolver depends on:

* ``ok`` — the gate has an open waiter and parseable PR metadata (awaiting
  merge, working as intended).
* ``orphan`` — the gate has no ``waits_on_gate`` edge, so it blocks nothing
  (the resolver's ``_gate_closed`` has no story to post a finding on).
* ``malformed`` — the gate's PR metadata is missing/ill-typed, so
  :func:`~lithos_loom.gates.parse_pr_gate` can't read a PR to watch; the
  resolver marks it ``unparseable`` and its waiter stays blocked forever.
* ``waiter-gone`` — the ``waits_on_gate`` edge points at a task that no longer
  exists.
* ``waiter-resolved`` — the waiter is already completed/cancelled while the
  gate is still open (the merge→complete never landed the gate side).

The classification mirrors the branches
:func:`~lithos_loom.subscriptions._develop_pr_merge.reconcile_pr_gate` reasons
about, so the listing tells the operator *why* a gate isn't progressing without
touching GitHub or mutating anything.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from lithos_loom.gates import is_pr_gate, parse_pr_gate, waiter_of
from lithos_loom.lithos_client import Task, TaskClient

__all__ = [
    "HEALTH_MALFORMED",
    "HEALTH_OK",
    "HEALTH_ORDER",
    "HEALTH_ORPHAN",
    "HEALTH_WAITER_GONE",
    "HEALTH_WAITER_RESOLVED",
    "GateRow",
    "classify_gate",
    "collect_gate_rows",
    "render_report",
]

HEALTH_OK = "ok"
HEALTH_ORPHAN = "orphan"
HEALTH_MALFORMED = "malformed"
HEALTH_WAITER_GONE = "waiter-gone"
HEALTH_WAITER_RESOLVED = "waiter-resolved"

# Canonical display order for the by-health footer: the healthy state first,
# then the needs-attention classes in the resolver's precedence order (the same
# order §4.4a of SPECIFICATION.md lists them).
HEALTH_ORDER = (
    HEALTH_OK,
    HEALTH_ORPHAN,
    HEALTH_MALFORMED,
    HEALTH_WAITER_GONE,
    HEALTH_WAITER_RESOLVED,
)


@dataclass(frozen=True)
class GateRow:
    """One open ``pr`` gate plus its waiter, as the listing renders it."""

    gate_id: str
    gate_title: str
    repo: str | None
    pr_number: int | None
    pr_url: str | None
    waiter_id: str | None
    waiter_title: str | None
    waiter_status: str | None
    health: str

    @property
    def pr_label(self) -> str:
        """``owner/repo#42`` for a parseable gate, ``—`` when malformed."""
        if self.repo is not None and self.pr_number is not None:
            return f"{self.repo}#{self.pr_number}"
        return "—"


def classify_gate(gate: Task, waiter_id: str | None, waiter: Task | None) -> GateRow:
    """Classify one gate + its waiter into a :class:`GateRow` (pure).

    *waiter* is the ``task_get`` of *waiter_id* (or ``None`` when there is no
    waiter edge, or the edge dangles). Health precedence follows what an
    operator can act on: ``orphan`` first (no waiter → nothing else about the
    gate matters), then ``malformed`` (a real story is stranded on an
    unwatchable PR), then the waiter-side anomalies, then ``ok``.
    """
    spec = parse_pr_gate(gate)
    if waiter_id is None:
        health = HEALTH_ORPHAN
    elif spec is None:
        health = HEALTH_MALFORMED
    elif waiter is None:
        health = HEALTH_WAITER_GONE
    elif waiter.status != "open":
        health = HEALTH_WAITER_RESOLVED
    else:
        health = HEALTH_OK
    return GateRow(
        gate_id=gate.id,
        gate_title=gate.title,
        repo=spec.repo if spec else None,
        pr_number=spec.pr_number if spec else None,
        pr_url=spec.pr_url if spec else None,
        waiter_id=waiter_id,
        waiter_title=waiter.title if waiter else None,
        waiter_status=waiter.status if waiter else None,
        health=health,
    )


async def collect_gate_rows(client: TaskClient) -> list[GateRow]:
    """Enumerate open ``pr`` gates and classify each (read-only).

    One ``task_list(status="open")`` sweep, then per gate one
    ``task_edge_list`` (via :func:`~lithos_loom.gates.waiter_of`) and — only
    when there is a waiter — one ``task_get`` for the waiter's live status. No
    mutating call is issued. Rows are sorted by gate id for a stable listing.
    """
    tasks = await client.task_list(status="open")
    rows: list[GateRow] = []
    for gate in tasks:
        if not is_pr_gate(gate):
            continue
        waiter_id = await waiter_of(client, gate.id)
        waiter = (
            await client.task_get(task_id=waiter_id) if waiter_id is not None else None
        )
        rows.append(classify_gate(gate, waiter_id, waiter))
    rows.sort(key=lambda r: r.gate_id)
    return rows


def render_report(rows: list[GateRow]) -> list[str]:
    """Render the gate listing as aligned text lines (pure).

    Returns a list of lines the caller ``typer.echo``es. Empty input yields a
    single "no open pr gates" line; otherwise a header + one row per gate + a
    summary counting healthy vs. needs-attention gates, then a per-health
    breakdown counting each health class present (in :data:`HEALTH_ORDER`).
    """
    if not rows:
        return ["no open pr gates"]

    headers = ("GATE", "PR", "WAITER", "WAITER STATUS", "HEALTH")
    cells = [
        (
            row.gate_id,
            row.pr_label,
            row.waiter_id or "—",
            row.waiter_status or "—",
            row.health,
        )
        for row in rows
    ]
    widths = [
        max(len(headers[col]), *(len(cell[col]) for cell in cells))
        for col in range(len(headers))
    ]

    def _fmt(values: tuple[str, ...]) -> str:
        return "  ".join(v.ljust(widths[col]) for col, v in enumerate(values)).rstrip()

    lines = [_fmt(headers)]
    lines.extend(_fmt(cell) for cell in cells)

    counts = Counter(row.health for row in rows)
    healthy = counts[HEALTH_OK]
    attention = len(rows) - healthy
    plural = "gate" if len(rows) == 1 else "gates"
    lines.append("")
    lines.append(
        f"{len(rows)} open pr {plural}: {healthy} healthy, "
        f"{attention} need{'s' if attention == 1 else ''} attention"
    )
    breakdown = ", ".join(
        f"{counts[health]} {health}" for health in HEALTH_ORDER if counts[health]
    )
    lines.append(f"by health: {breakdown}")
    return lines
