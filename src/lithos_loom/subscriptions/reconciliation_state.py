"""The ``pr`` gate's reconciliation state (PRD pr-reconciliation S7).

``[PRConflicted]``, ``[MergeGateFailed]`` and friends are audit history; an
append-only finding stream cannot answer *"what is the state of this PR right
now?"*. The answer lives on the ``pr`` gate in Lithos (ADR 0011 §2) as four
flat, scalar, ``metadata_match``-queryable keys:

* :data:`STATE_KEY` — one of :data:`STATES`;
* :data:`DETAIL_KEY` — one line saying why (≤ :data:`DETAIL_MAX_CHARS`);
* :data:`SINCE_KEY` — when the state last *changed* (a detail move keeps it);
* :data:`STATE_URL_KEY` — the PR the state describes, so a replacement PR
  on the same gate starts fresh instead of inheriting a stale verdict.

**Derived, not tracked.** Every dispatcher already keeps its own url-scoped
record on the gate (landability, the re-gate, the conflict resolver, the
remediation budget, the merge marker); the state is a pure function of
those records, the freshly fetched PR, and what is in flight in this
process (:class:`Busy`). :func:`derive_state` is that function; the sweep
calls :func:`record_state` once per still-open gate, after every
dispatcher has run, on a re-read of the gate. A closed / deleted PR writes
its state in the same marker write as its merge marker
(:func:`closed_state_marker`).

**One writer** (ADR 0011 §3): only the reconcile sweep writes these keys —
``lithos_task_update`` has no compare-and-swap, so a second writer could
lose a transition. The CLI, Lens and any webhook read; they never write.

**Precedence.** The states are ranked by what the operator must do first:
``needs_human`` (automation stopped: an escalation gate exists, the PR was
closed or deleted, or a dispatcher refused for a reason only the operator
can fix) › ``resolving_conflict`` (the resolver is running) › ``reconciling``
(a remediation or re-gate run is in flight, or a trigger is parked behind
one) › ``gate_failed`` (the project's check-set went red on the trial merge)
› ``behind`` (the base moved: a conflict awaits the resolver, or a green
merge commit is waiting to be pushed) › ``awaiting_review`` (GitHub still
wants reviews or checks, or has not classified the PR yet) › ``ready_to_merge``
(landable, clean, and the current pair's trial merge is green, vacuous, or was
not required).

**One-sweep staleness is by design.** The PR is fetched at the top of the
sweep; a resolver or re-gate that pushes during it moves the head, but the
state is derived from that fetch and from records keyed on it, so one sweep
may describe the pre-push PR. The next sweep re-fetches and re-derives.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from lithos_loom.errors import LithosClientError
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._findings import write_marker
from lithos_loom.subscriptions.pr_landability import classify_landability

__all__ = [
    "DETAIL_KEY",
    "DETAIL_MAX_CHARS",
    "SINCE_KEY",
    "STATES",
    "STATE_KEY",
    "STATE_URL_KEY",
    "Busy",
    "Derived",
    "closed_state_marker",
    "derive_state",
    "record_state",
]

logger = logging.getLogger(__name__)

STATE_KEY = "reconciliation_state"
DETAIL_KEY = "reconciliation_detail"
SINCE_KEY = "reconciliation_since"
STATE_URL_KEY = "reconciliation_pr_url"
DETAIL_MAX_CHARS = 200

STATES: tuple[str, ...] = (
    "awaiting_review",
    "reconciling",
    "behind",
    "resolving_conflict",
    "gate_failed",
    "needs_human",
    "ready_to_merge",
)

# The other markers the derivation reads — named here rather than imported
# from their owners so this module depends on none of the dispatchers (it
# runs after all of them and must not pull their machinery in).
_MERGE_GATE = "merge_gate"
_CONFLICT_RESOLVE = "conflict_resolve"
_REMEDIATION = "external_remediation"
_REMEDIATION_PENDING = "external_remediation_pending"
_MERGE_STATE = "develop_pr_merge_state"
_MERGE_STATE_URL = "develop_pr_merge_url"

# A dispatcher status the operator must fix (a config / checkout problem);
# automation will not retry on its own until the mapping moves.
_REFUSALS = frozenset(
    {
        "repo_mismatch",
        "checkout_unresolved",
        "config_unresolved",
        "fork_unsupported",
        "no_project",
    }
)

_SUBSYSTEM = "reconciliation-state"


@dataclass(frozen=True)
class Busy:
    """What this process has in flight on the PR right now."""

    remediation: bool = False
    merge_gate: bool = False
    conflict_resolve: bool = False  # the coder / panel is running
    conflict_debt: bool = False  # it pushed; only its record write is pending


@dataclass(frozen=True)
class Derived:
    state: str
    detail: str


def _record(meta: Mapping[str, Any], key: str, pr_url: str) -> Mapping[str, Any]:
    """The url-scoped record under *key*, or ``{}`` when absent, malformed
    or about another PR."""
    raw = meta.get(key)
    if isinstance(raw, Mapping) and raw.get("pr_url") == pr_url:
        return raw
    return {}


def _current_pair(record: Mapping[str, Any], pr: Any) -> bool:
    return record.get("head_sha") == getattr(pr, "head_sha", None) and record.get(
        "base_sha"
    ) == getattr(pr, "base_sha", None)


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def derive_state(
    meta: Mapping[str, Any], *, pr: Any, pr_url: str, busy: Busy
) -> Derived:
    """The PR's state from the gate's markers + the fetched PR + what runs
    (see the module doc for the precedence). Pure; never raises on a
    malformed marker — a marker it cannot read is a marker that is absent."""
    merge_state = meta.get(_MERGE_STATE)
    if (
        isinstance(merge_state, str)
        and merge_state in ("closed_unmerged", "gone")
        and meta.get(_MERGE_STATE_URL) == pr_url
    ):
        return Derived("needs_human", _closed_detail(merge_state))

    conflict = _record(meta, _CONFLICT_RESOLVE, pr_url)
    conflict_now = _current_pair(conflict, pr)
    if conflict_now and _str(conflict.get("needs_human_gate_id")):
        return Derived(
            "needs_human",
            f"conflict unresolved — decision gate {conflict['needs_human_gate_id']}",
        )
    if conflict_now and _str(conflict.get("status")) in _REFUSALS:
        return Derived(
            "needs_human", f"conflict resolver refused: {conflict['status']}"
        )

    budget = _record(meta, _REMEDIATION, pr_url)
    if _str(budget.get("needs_human_gate_id")):
        return Derived(
            "needs_human",
            "external-remediation budget exhausted — decision gate "
            f"{budget['needs_human_gate_id']}",
        )

    regate = _record(meta, _MERGE_GATE, pr_url)
    regate_now = _current_pair(regate, pr)
    if regate_now and _str(regate.get("status")) in _REFUSALS:
        return Derived("needs_human", f"merge-gate refused: {regate['status']}")

    if busy.conflict_resolve or (
        conflict_now and _str(conflict.get("status")) == "running"
    ):
        return Derived("resolving_conflict", "converge --resolve-conflicts running")
    if busy.conflict_debt:
        return Derived("reconciling", "resolved conflict pushed; record write pending")
    if busy.merge_gate:
        return Derived("reconciling", "merge-gate run in flight")
    if busy.remediation:
        return Derived("reconciling", "external-review remediation in flight")
    if _record(meta, _REMEDIATION_PENDING, pr_url):
        return Derived("reconciling", "remediation trigger parked behind a busy run")

    # The re-gate's OUTCOME is its `status` (green / red / errored / no_checks
    # / conflict / push_failed / crashed / a refusal); `verdict` is the
    # GitHub-style RED / GREEN / None beside it and never the classifier.
    outcome = _str(regate.get("status")) if regate_now else ""
    if outcome in ("red", "errored"):
        return Derived("gate_failed", f"trial merge check-set: {outcome}")
    if outcome == "conflict":
        return Derived("behind", "trial merge conflicts with the base's tip")
    if outcome == "push_failed":
        return Derived(
            "behind",
            "green merge commit not pushed: "
            + (_str(regate.get("push_error")) or "push failed"),
        )

    landability = classify_landability(pr)
    mergeable_state = _str(getattr(pr, "mergeable_state", ""))
    if landability == "dirty":
        return Derived("behind", "GitHub reports conflicts with the base")
    if landability == "unknown":
        return Derived("awaiting_review", "GitHub has not classified the PR yet")
    if mergeable_state == "behind":
        return Derived("behind", "base moved; awaiting the re-gate")
    if mergeable_state == "blocked":
        return Derived("awaiting_review", "GitHub: blocked (reviews/checks required)")
    if mergeable_state and mergeable_state != "clean":
        return Derived("awaiting_review", f"GitHub: {mergeable_state}")
    if outcome == "green" or not regate_now:
        return Derived("ready_to_merge", "landable; trial merge green or not required")
    if outcome == "no_checks":
        return Derived("ready_to_merge", "landable; the project runs no checks")
    return Derived("awaiting_review", f"trial merge {outcome}; awaiting the re-gate")


def _closed_detail(merge_state: str) -> str:
    if merge_state == "gone":
        return "delivered PR no longer exists (404); complete the gate or re-point it"
    return "delivered PR closed unmerged; complete the gate to proceed or re-point it"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _marker(derived: Derived, pr_url: str, since: str) -> dict[str, Any]:
    return {
        STATE_KEY: derived.state,
        DETAIL_KEY: derived.detail[:DETAIL_MAX_CHARS],
        SINCE_KEY: since,
        STATE_URL_KEY: pr_url,
    }


def closed_state_marker(pr_url: str, merge_state: str) -> dict[str, Any]:
    """The state keys for a closed / deleted PR, folded into the merge
    marker's own write so the two never disagree."""
    return _marker(Derived("needs_human", _closed_detail(merge_state)), pr_url, _now())


async def record_state(
    gate: Any, pr: Any, pr_url: str, ctx: SubscriptionContext, *, busy: Busy
) -> str | None:
    """Derive the still-open gate's state from the gate AS IT IS NOW and
    write it when it moved. Returns the new state on a transition, ``None``
    otherwise (unchanged, a detail-only move, or a failed write — the next
    sweep re-derives). Never raises.

    Re-reads the gate first: the dispatchers wrote their records earlier in
    this same sweep, so the gate the sweep was handed is stale. A failed
    re-read derives from the stale copy rather than skipping the sweep.
    """
    fresh = gate
    try:
        latest = await ctx.lithos.task_get(task_id=gate.id)
        if latest is not None:
            fresh = latest
    except (LithosClientError, OSError) as exc:
        ctx.logger.warning(
            "%s: could not re-read gate %s (%s); deriving from the sweep's copy",
            _SUBSYSTEM,
            gate.id,
            exc,
        )
    raw_meta = getattr(fresh, "metadata", None)
    meta: Mapping[str, Any] = raw_meta if isinstance(raw_meta, Mapping) else {}
    derived = derive_state(meta, pr=pr, pr_url=pr_url, busy=busy)
    same_pr = meta.get(STATE_URL_KEY) == pr_url
    previous = meta.get(STATE_KEY) if same_pr else None
    transition = derived.state != previous
    if not transition and meta.get(DETAIL_KEY) == derived.detail:
        return None
    kept = meta.get(SINCE_KEY)
    since = kept if not transition and isinstance(kept, str) and kept else _now()
    landed = await write_marker(
        ctx,
        task_id=gate.id,
        marker=_marker(derived, pr_url, since),
        subsystem=_SUBSYSTEM,
    )
    if not landed:
        return None
    if transition:
        ctx.logger.info(
            "%s: %s %s → %s (%s)",
            _SUBSYSTEM,
            pr_url,
            previous or "unset",
            derived.state,
            derived.detail,
        )
        return derived.state
    return None
