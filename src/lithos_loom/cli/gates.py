"""``lithos-loom gates`` — read-only inventory of open gates.

A gate (``task_type="gate"``) blocks its story by a ``waits_on_gate`` edge
(see :mod:`lithos_loom.gates`). Two kinds matter to loom's own automation:

* a **`pr` gate** (Epic H) — "PR raised, awaiting human merge"; the
  github-watcher resolves it when the PR merges;
* a **loom-raised `human` gate** (b91177d2) — "loom stopped and needs a
  decision"; the operator resolves it (complete → the story re-dispatches;
  cancel the *story* → abandon), and it carries the escalation reason +
  summary this listing shows.

The operator's own ``human`` gates (and any ``timer`` / ``ci`` /
``external_task`` gates) are listed too, with their wiring health, but carry
no loom-specific columns.

This command is that view. It is **read-only** — it lists open gates and, for
each, the story it blocks plus a one-word *health* classifying the gate/waiter
wiring the resolvers depend on:

* ``ok`` — the gate has an open waiter and readable loom metadata (working as
  intended).
* ``orphan`` — the gate has no ``waits_on_gate`` edge, so it blocks nothing
  (a resolver has no story to act on).
* ``malformed`` — a ``pr`` gate whose PR metadata is missing/ill-typed
  (:func:`~lithos_loom.gates.parse_pr_gate` can't read a PR to watch; the
  resolver marks it ``unparseable`` and its waiter stays blocked forever), or
  a loom ``human`` gate with no ``escalation_reason``.
* ``waiter-gone`` — the ``waits_on_gate`` edge points at a task that no longer
  exists.
* ``waiter-resolved`` — the waiter is already completed/cancelled while the
  gate is still open (the resolve never landed the gate side).

The classification mirrors the branches
:func:`~lithos_loom.subscriptions._develop_pr_merge.reconcile_pr_gate` reasons
about, so the listing tells the operator *why* a gate isn't progressing without
touching GitHub or mutating anything.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from lithos_loom.gates import (
    GATE_TYPE_HUMAN,
    GATE_TYPE_PR,
    is_loom_human_gate,
    parse_human_gate,
    parse_pr_gate,
    waiter_of,
)
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

_NO_REF = "—"


@dataclass(frozen=True)
class GateRow:
    """One open gate plus its waiter, as the listing renders it."""

    gate_id: str
    gate_title: str
    gate_type: str
    repo: str | None
    pr_number: int | None
    pr_url: str | None
    waiter_id: str | None
    waiter_title: str | None
    waiter_status: str | None
    health: str
    escalation_reason: str | None = None
    escalation_summary: str | None = None

    @property
    def pr_label(self) -> str:
        """``owner/repo#42`` for a parseable ``pr`` gate, ``—`` otherwise."""
        if self.repo is not None and self.pr_number is not None:
            return f"{self.repo}#{self.pr_number}"
        return _NO_REF

    @property
    def ref_label(self) -> str:
        """The REF column: the watched PR for a ``pr`` gate, the escalation
        reason for a loom ``human`` gate, ``—`` for anything else."""
        if self.gate_type == GATE_TYPE_PR:
            return self.pr_label
        return self.escalation_reason or _NO_REF


def classify_gate(gate: Task, waiter_id: str | None, waiter: Task | None) -> GateRow:
    """Classify one gate + its waiter into a :class:`GateRow` (pure).

    *waiter* is the ``task_get`` of *waiter_id* (or ``None`` when there is no
    waiter edge, or the edge dangles). Health precedence follows what an
    operator can act on: ``orphan`` first (no waiter → nothing else about the
    gate matters), then ``malformed`` (a real story is stranded on a gate a
    resolver cannot read), then the waiter-side anomalies, then ``ok``.
    """
    gate_type = str((gate.metadata or {}).get("gate_type") or "?")
    pr_spec = parse_pr_gate(gate) if gate_type == GATE_TYPE_PR else None
    human_spec = parse_human_gate(gate) if is_loom_human_gate(gate) else None
    malformed = (gate_type == GATE_TYPE_PR and pr_spec is None) or (
        is_loom_human_gate(gate) and human_spec is None
    )
    if waiter_id is None:
        health = HEALTH_ORPHAN
    elif malformed:
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
        gate_type=gate_type,
        repo=pr_spec.repo if pr_spec else None,
        pr_number=pr_spec.pr_number if pr_spec else None,
        pr_url=pr_spec.pr_url if pr_spec else None,
        waiter_id=waiter_id,
        waiter_title=waiter.title if waiter else None,
        waiter_status=waiter.status if waiter else None,
        health=health,
        escalation_reason=human_spec.reason if human_spec else None,
        escalation_summary=human_spec.summary if human_spec else None,
    )


async def collect_gate_rows(client: TaskClient) -> list[GateRow]:
    """Enumerate open gates and classify each (read-only).

    One ``task_list(status="open")`` sweep, then per gate one
    ``task_edge_list`` (via :func:`~lithos_loom.gates.waiter_of`) and — only
    when there is a waiter — one ``task_get`` for the waiter's live status. No
    mutating call is issued. Rows are sorted by gate id for a stable listing.
    """
    tasks = await client.task_list(status="open")
    rows: list[GateRow] = []
    for gate in tasks:
        if gate.task_type != "gate":
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
    single "no open gates" line; otherwise a header + one row per gate (a loom
    ``human`` gate adds an indented line with its escalation summary — the
    "why" an operator triages by) + a summary counting healthy vs.
    needs-attention gates, then per-type and per-health breakdowns.
    """
    if not rows:
        return ["no open gates"]

    headers = ("GATE", "TYPE", "REF", "WAITER", "WAITER STATUS", "HEALTH")
    cells = [
        (
            row.gate_id,
            row.gate_type,
            row.ref_label,
            row.waiter_id or _NO_REF,
            row.waiter_status or _NO_REF,
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
    for row, cell in zip(rows, cells, strict=True):
        lines.append(_fmt(cell))
        if row.escalation_summary:
            lines.append(f"    ↳ {row.escalation_summary}")

    counts = Counter(row.health for row in rows)
    healthy = counts[HEALTH_OK]
    attention = len(rows) - healthy
    plural = "gate" if len(rows) == 1 else "gates"
    lines.append("")
    lines.append(
        f"{len(rows)} open {plural}: {healthy} healthy, "
        f"{attention} need{'s' if attention == 1 else ''} attention"
    )
    types = Counter(row.gate_type for row in rows)
    loom_human = sum(
        1
        for row in rows
        if row.gate_type == GATE_TYPE_HUMAN and row.escalation_reason is not None
    )
    type_parts = [f"{types[t]} {t}" for t in sorted(types)]
    if loom_human:
        type_parts.append(f"{loom_human} raised by loom")
    lines.append(f"by type: {', '.join(type_parts)}")
    breakdown = ", ".join(
        f"{counts[health]} {health}" for health in HEALTH_ORDER if counts[health]
    )
    lines.append(f"by health: {breakdown}")
    return lines
