"""``lithos-loom gates --shadow`` — the M1 agreement reading (664d84c4).

The reconcile sweep records on each ``pr`` gate the first head at which
loom WOULD have merged it and, when the gate resolves, what the operator
did (:mod:`lithos_loom.subscriptions.shadow_merge`). This is the read side:
one row per ``pr`` gate, then an agreement rate per project and overall,
with its 95% Wilson interval — the figure the merge-policy checkpoint
(3398a388) quotes.

**Verdicts.** A resolved gate with an approval record is *measured*:

* agree — merged at the would-merge head; or closed unmerged when loom never
  reached would-merge (neither would merge it);
* disagree — merged after further pushes (loom would have merged earlier),
  merged although loom never would, or closed unmerged after loom would have
  merged it;
* excluded — ``gone`` / ``waiter_resolved``: the PR or the story went away,
  no verdict either side.

A gate with no approval record (delivered before the record existed, or by
a path that cannot bind one) is *unmeasured*, never a disagreement; an open
gate has no verdict yet. Read-only: two ``task_list`` calls.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from lithos_loom.evals.review.stats import wilson_interval
from lithos_loom.gates import GATE_TYPE_PR, parse_pr_gate
from lithos_loom.lithos_client import Task, TaskClient
from lithos_loom.subscriptions.shadow_merge import (
    APPROVAL_KEY,
    BASES,
    OUTCOMES,
    SHADOW_KEY,
)

__all__ = [
    "VERDICT_AGREE",
    "VERDICT_DISAGREE",
    "VERDICT_EXCLUDED",
    "VERDICT_OPEN",
    "VERDICT_UNMEASURED",
    "ShadowRow",
    "collect_shadow_rows",
    "parse_since",
    "render_shadow_report",
    "shadow_label",
    "shadow_row",
]

VERDICT_AGREE = "agree"
VERDICT_DISAGREE = "disagree"
VERDICT_EXCLUDED = "excluded"
VERDICT_OPEN = "open"
VERDICT_UNMEASURED = "unmeasured"

# the three ways loom and the operator part company, as the roll-up names them
_DISAGREEMENTS = (
    ("merged_after_pushes", "merged after pushes"),
    ("merged_never_would", "merged never-would"),
    ("would_merge_closed", "would-merge closed"),
)
_EXCLUDED_OUTCOMES = frozenset({"gone", "waiter_resolved"})

_NO_VALUE = "—"
_DAYS_RE = re.compile(r"([0-9]+)d\Z")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _utc(value: datetime | None) -> datetime | None:
    """A naive time is read as UTC (as Lithos stamps them), so a window
    comparison never mixes naive and aware."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _time(value: object) -> datetime | None:
    try:
        return _utc(datetime.fromisoformat(_str(value)))
    except ValueError:
        return None


def _basis(meta: Mapping[str, Any], record: Mapping[str, Any], pr_url: str) -> str:
    """The record's basis, or — when it is missing or unreadable — what the
    delivery recorded: a full approved sha is an approval, anything else none."""
    basis = _str(record.get("basis"))
    if basis in BASES:
        return basis
    head = _str(_record(meta, APPROVAL_KEY, pr_url).get("head_sha")).strip().lower()
    return "approval" if _FULL_SHA_RE.match(head) else "no_approval_record"


def _record(meta: Mapping[str, Any], key: str, pr_url: str) -> Mapping[str, Any]:
    raw = meta.get(key)
    if isinstance(raw, Mapping) and raw.get("pr_url") == pr_url:
        return raw
    return {}


@dataclass(frozen=True)
class ShadowRow:
    """One ``pr`` gate's shadow record, as the report reads it."""

    gate_id: str
    project: str
    pr_label: str
    status: str
    delivered: datetime | None
    basis: str
    would_merge_head: str
    invalidated_reason: str
    outcome: str | None
    elapsed_s: int | None
    pushes_loom: int
    pushes_human: int

    @property
    def disagreement(self) -> str:
        """Which way loom and the operator parted (a key of
        :data:`_DISAGREEMENTS`), or ``""``. Keyed on the FIRST would-merge
        moment — under auto-merge loom would already have merged then, so a
        later invalidation does not withdraw it."""
        if self.outcome in ("merged_after_pushes", "merged_never_would"):
            return self.outcome
        if self.outcome == "closed_unmerged" and self.would_merge_head:
            return "would_merge_closed"
        return ""

    @property
    def verdict(self) -> str:
        if self.basis != "approval":
            return VERDICT_UNMEASURED
        if self.outcome is None:
            return VERDICT_OPEN if self.status == "open" else VERDICT_UNMEASURED
        if self.outcome in _EXCLUDED_OUTCOMES:
            return VERDICT_EXCLUDED
        return VERDICT_DISAGREE if self.disagreement else VERDICT_AGREE


def shadow_row(gate: Task) -> ShadowRow | None:
    """The gate's :class:`ShadowRow`, or ``None`` for anything but a
    parseable ``pr`` gate (pure)."""
    meta: Mapping[str, Any] = gate.metadata or {}
    if meta.get("gate_type") != GATE_TYPE_PR:
        return None
    spec = parse_pr_gate(gate)
    if spec is None:
        return None
    approval = _record(meta, APPROVAL_KEY, spec.pr_url)
    record = _record(meta, SHADOW_KEY, spec.pr_url)
    outcome = _str(record.get("outcome"))
    elapsed = record.get("elapsed_s")
    pushes = record.get("pushes_after_would_merge")
    pushes = pushes if isinstance(pushes, Mapping) else {}
    return ShadowRow(
        gate_id=gate.id,
        project=_str(meta.get("project")) or _NO_VALUE,
        pr_label=f"{spec.repo}#{spec.pr_number}",
        status=gate.status,
        delivered=_time(approval.get("delivered_at")) or _utc(gate.created_at),
        basis=_basis(meta, record, spec.pr_url),
        would_merge_head=_str(record.get("would_merge_head")),
        invalidated_reason=_str(record.get("invalidated_reason")),
        # an outcome outside the vocabulary is no outcome, never an agreement
        outcome=outcome if outcome in OUTCOMES else None,
        elapsed_s=_int_or_none(elapsed),
        pushes_loom=_count(pushes.get("loom")),
        pushes_human=_count(pushes.get("human")),
    )


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _count(value: object) -> int:
    return _int_or_none(value) or 0


def shadow_label(gate: Task) -> str:
    """The SHADOW column of the default ``gates`` listing: where an open
    ``pr`` gate's verdict stands (``would`` / ``not-yet`` / ``invalidated`` /
    ``unmeasured``), ``—`` for any other gate or a gate not yet swept."""
    meta: Mapping[str, Any] = gate.metadata or {}
    pr_url = _str(meta.get("pr_url"))
    record = _record(meta, SHADOW_KEY, pr_url) if pr_url else {}
    if meta.get("gate_type") != GATE_TYPE_PR or not record:
        return _NO_VALUE
    if _basis(meta, record, pr_url) != "approval":
        return VERDICT_UNMEASURED
    if _str(record.get("invalidated_at")):
        return "invalidated"
    return "would" if _str(record.get("would_merge_at")) else "not-yet"


def parse_since(value: str, *, now: datetime | None = None) -> datetime:
    """``--since``: ``<N>d`` (N×24h before *now*) or ``YYYY-MM-DD`` (UTC
    midnight). Raises ``ValueError`` naming the flag for anything else."""
    days = _DAYS_RE.match(value)
    if days:
        try:
            return (now or datetime.now(UTC)) - timedelta(days=int(days.group(1)))
        except OverflowError:
            pass
    if _DATE_RE.match(value):
        try:
            return datetime.fromisoformat(value).replace(tzinfo=UTC)
        except ValueError:
            pass
    raise ValueError(
        f"--since takes <N>d (e.g. 30d) or a date YYYY-MM-DD, not {value!r}"
    )


async def collect_shadow_rows(
    client: TaskClient,
    *,
    since: datetime | None = None,
    project: str | None = None,
) -> list[ShadowRow]:
    """Every ``pr`` gate delivered at or after *since* (open, and completed —
    a gate delivered in the window resolved in it too, so the completed
    listing is narrowed server-side; a completed gate Lithos stamped with no
    ``resolved_at`` falls outside a window there), in *project* when given. A
    gate whose delivery time is unknown is outside any window. Read-only;
    sorted by gate id."""
    gates = [
        *await client.task_list(status="open", task_type="gate"),
        *await client.task_list(
            status="completed", task_type="gate", resolved_since=since
        ),
    ]
    rows = []
    for gate in gates:
        row = shadow_row(gate)
        if row is None or (project is not None and row.project != project):
            continue
        if since is not None and (row.delivered is None or row.delivered < since):
            continue
        rows.append(row)
    rows.sort(key=lambda r: r.gate_id)
    return rows


def render_shadow_report(rows: list[ShadowRow]) -> list[str]:
    """The per-gate table, then one roll-up line per project and one
    ``overall:`` line (pure)."""
    if not rows:
        return ["no pr gates in the window"]
    headers = (
        "GATE",
        "PROJECT",
        "PR",
        "DELIVERED",
        "WOULD-MERGE",
        "OUTCOME",
        "VERDICT",
        "ELAPSED",
        "PUSHES L/H",
    )
    cells = [_cells(row) for row in rows]
    widths = [
        max(len(headers[col]), *(len(cell[col]) for cell in cells))
        for col in range(len(headers))
    ]

    def _fmt(values: tuple[str, ...]) -> str:
        return "  ".join(v.ljust(widths[col]) for col, v in enumerate(values)).rstrip()

    lines = [_fmt(headers), *(_fmt(cell) for cell in cells), ""]
    for project in sorted({row.project for row in rows}):
        lines.append(_rollup(project, [r for r in rows if r.project == project]))
    lines.append(_rollup("overall", rows))
    lines.append(
        "unmeasured = no approval record (delivered before M1, or by a path that "
        "binds none); read the rate only over gates delivered after R5 (--since)"
    )
    return lines


def _cells(row: ShadowRow) -> tuple[str, ...]:
    would = row.would_merge_head[:12] or _NO_VALUE
    if row.invalidated_reason:
        would += f" ✗{row.invalidated_reason}"
    elapsed = f"{row.elapsed_s / 3600:.1f}h" if row.elapsed_s is not None else _NO_VALUE
    return (
        row.gate_id,
        row.project,
        row.pr_label,
        row.delivered.date().isoformat() if row.delivered else _NO_VALUE,
        would,
        row.outcome or row.status,
        row.verdict,
        elapsed,
        f"{row.pushes_loom}/{row.pushes_human}",
    )


def _rollup(name: str, rows: list[ShadowRow]) -> str:
    verdicts = Counter(row.verdict for row in rows)
    agree, disagree = verdicts[VERDICT_AGREE], verdicts[VERDICT_DISAGREE]
    measured = agree + disagree
    kinds = Counter(row.disagreement for row in rows if row.verdict == VERDICT_DISAGREE)
    rest = (
        f"excluded {verdicts[VERDICT_EXCLUDED]}, "
        f"unmeasured {verdicts[VERDICT_UNMEASURED]}, open {verdicts[VERDICT_OPEN]}"
    )
    if not measured:
        return f"{name}: no measured resolved gate yet — {rest}"
    lo, hi = wilson_interval(agree, measured)
    human = sum(
        1
        for row in rows
        if row.disagreement == "merged_after_pushes" and row.pushes_human
    )
    split = ", ".join(
        f"{kinds[key]} {label}"
        + (f" ({human} with a human push)" if key == "merged_after_pushes" else "")
        for key, label in _DISAGREEMENTS
    )
    return (
        f"{name}: agreement {agree}/{measured} ({agree / measured:.0%}, "
        f"95% CI {lo:.0%}–{hi:.0%}) — disagree: {split}; {rest}"
    )
