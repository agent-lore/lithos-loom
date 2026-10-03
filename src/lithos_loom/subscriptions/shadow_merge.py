"""Shadow auto-merge recording (review-convergence M1, task 664d84c4).

The merge-policy dial (human → shadow → canary) is turned on a measured
agreement rate or not at all. This module is that measurement: on each
delivered PR's ``pr`` gate it records the first head at which loom WOULD
have merged the PR, and — when the gate resolves — what the operator
actually did. It decides nothing and spends no tokens.

**Two gate keys.**

* :data:`APPROVAL_KEY` — ``{pr_url, head_sha, source, run_id?,
  delivered_at}``, written ONCE in the gate's creation metadata by the
  delivery (the daemon's delivering exit, or ``develop deliver`` when the
  run's approval is bound to the delivered head and no chained converge
  ran). The head the delivery panel approved; nothing else in Lithos holds
  it.
* :data:`SHADOW_KEY` — the shadow record, url-scoped like every other
  dispatcher record (a replacement PR on the same gate starts fresh).
  Written by the reconcile sweep alone (the ADR 0011 §3 single-writer
  rule, for the same reason: ``task_update`` has no compare-and-swap).

**Would-merge**, on one head: the gate's reconciliation state is
``ready_to_merge``, a panel approval covers that head, and no external
review is open on it. Recorded once (``would_merge_at`` / ``_head``); later
qualifying heads move only ``last_would_merge_head``.

**Tracked, not derived** — the opposite of the reconciliation state: this
record remembers (the first verdict, who pushed since, the outcome), so
it lives here rather than beside :mod:`.reconciliation_state`, whose state
is a pure function of the gate as it is now.

**Whose push, and does the approval carry.** A head change is attributed to
loom when the new head is a sha a loom dispatcher recorded pushing
(``merge_gate.pushed_sha``, ``conflict_resolve.pushed_sha``,
``external_remediation.last_loom_pushed_sha``), and to a human otherwise. A
remediation round that converged ran the panel over the PR, so its push
re-approves; any other loom push — a merge-gate base merge, a conflict
resolution, or one whose own record has since been rewritten — is verified
but reviews nothing new, so it CARRIES an approval the previous head had
(and an open review the previous head had). A human push drops the
approval; it also resets the remediation budget, so a "converged" status
can never be read across one (see :func:`_pusher`).

**Open external review.** Ingestion consumes the actionable rows it posts,
and a remediation that declines them (off, project opt-out, untrusted
author, exhausted) leaves nothing durable, so the sweep passes
``review_open`` and the record pins it to the head (``review_open_head``).
A remediation round answers it: a converged push clears it with the head;
a round seen in flight after the pin (``review_round_seen``) that settles
without a push (findings refuted, nothing to change) clears it in place.

**Invalidation** (first one kept): after would-merge, a human push, an
open review, or a stop state (``gate_failed`` / ``needs_human``). Transient
states (a re-gate in flight after a base move) are not a verdict.

**Outcome** (:data:`OUTCOMES`, written once; it freezes the record):
merged at the would-merge head; merged after further pushes (counted by
pusher); merged although loom never reached would-merge; closed unmerged;
gone (404); story already terminal (``waiter_resolved``). A gate with no
approval record carries ``basis = no_approval_record`` — the report counts
it as unmeasured, never as a disagreement.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._findings import write_marker

__all__ = [
    "APPROVAL_KEY",
    "BASES",
    "OUTCOMES",
    "SHADOW_KEY",
    "approval_marker",
    "observe",
    "outcome",
    "outcome_marker",
    "record_shadow",
]

APPROVAL_KEY = "delivered_approval"
SHADOW_KEY = "shadow_merge"

BASES: tuple[str, ...] = ("approval", "no_approval_record")
OUTCOMES: tuple[str, ...] = (
    "merged_at_head",
    "merged_after_pushes",
    "merged_never_would",
    "closed_unmerged",
    "gone",
    "waiter_resolved",
)
# what the sweep reports at gate resolution (``merged`` is classified above)
_RESOLUTIONS = frozenset({"merged", "closed_unmerged", "gone", "waiter_resolved"})

# Read by name rather than imported from their owners, as
# :mod:`.reconciliation_state` does: this module runs after every
# dispatcher and must not pull their machinery in.
_MERGE_GATE = "merge_gate"
_CONFLICT_RESOLVE = "conflict_resolve"
_REMEDIATION = "external_remediation"

# a remediation round ran the panel over the PR; the rest only verified
# what they pushed (a green trial merge, a panel over the composed tree) —
# `loom` is a loom push whose own record has since moved on
_CARRYING_PUSHES = frozenset({"merge_gate", "conflict_resolve", "loom"})
# a remediation round that settled the PR without pushing: every finding
# refuted, or nothing to change (#380)
_NO_CHANGE_SETTLES = frozenset({"already_clean", "triage_rejected"})
_STOP_STATES = frozenset({"gate_failed", "needs_human"})

_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_SUBSYSTEM = "shadow-merge"


def _full_sha(value: object) -> str:
    if not isinstance(value, str):
        return ""
    sha = value.strip().lower()
    return sha if _FULL_SHA_RE.match(sha) else ""


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _record(meta: Mapping[str, Any], key: str, pr_url: str) -> Mapping[str, Any]:
    raw = meta.get(key)
    if isinstance(raw, Mapping) and raw.get("pr_url") == pr_url:
        return raw
    return {}


def approval_marker(
    pr_url: str,
    head_sha: object,
    *,
    source: str,
    run_id: str = "",
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """The :data:`APPROVAL_KEY` entry for a gate's creation metadata, or
    ``None`` when *head_sha* is not a full object name (a short or missing
    record binds nothing — the gate then reads ``no_approval_record``)."""
    head = _full_sha(head_sha)
    if not head:
        return None
    record: dict[str, Any] = {"pr_url": pr_url, "head_sha": head, "source": source}
    if run_id:
        record["run_id"] = run_id
    record["delivered_at"] = (now or datetime.now(UTC)).isoformat()
    return {APPROVAL_KEY: record}


def _fresh(meta: Mapping[str, Any], pr_url: str) -> dict[str, Any]:
    approval = _record(meta, APPROVAL_KEY, pr_url)
    approved = _full_sha(approval.get("head_sha"))
    return {
        "pr_url": pr_url,
        "basis": "approval" if approved else "no_approval_record",
        "delivered_at": _str(approval.get("delivered_at")),
        "approved_head": approved,
        "observed_head": approved,
        "would_merge_at": "",
        "would_merge_head": "",
        "last_would_merge_head": "",
        "invalidated_at": "",
        "invalidated_reason": "",
        "pushes_after_would_merge": {"loom": 0, "human": 0},
        "review_open_head": "",
        "review_round_seen": False,
    }


def _load(meta: Mapping[str, Any], pr_url: str) -> dict[str, Any] | None:
    """The stored record for *pr_url*, normalised field by field (a field
    it cannot read is a field at its default), or ``None`` when absent."""
    raw = _record(meta, SHADOW_KEY, pr_url)
    if not raw:
        return None
    base = _fresh(meta, pr_url)
    loaded = {
        key: raw[key] if isinstance(raw.get(key), str) else default
        for key, default in base.items()
        if isinstance(default, str)
    }
    if raw.get("basis") not in BASES:
        loaded["basis"] = base["basis"]
    loaded["review_round_seen"] = raw.get("review_round_seen") is True
    pushes = raw.get("pushes_after_would_merge")
    pushes = pushes if isinstance(pushes, Mapping) else {}
    loaded["pushes_after_would_merge"] = {
        "loom": _count(pushes.get("loom")),
        "human": _count(pushes.get("human")),
    }
    if _str(raw.get("outcome")) in OUTCOMES:
        loaded["outcome"] = raw["outcome"]
        for key in ("outcome_at", "merged_head"):
            loaded[key] = _str(raw.get(key))
        if isinstance(raw.get("elapsed_s"), int):
            loaded["elapsed_s"] = raw["elapsed_s"]
    return loaded


def _pusher(meta: Mapping[str, Any], pr_url: str, head: str) -> str:
    """Which loom dispatcher pushed *head*, or ``human``.

    ``last_loom_pushed_sha`` is EVERY loom push (the merge-gate and the
    resolver record theirs there too, and their own records are rewritten
    by their next run), so a match there is a remediation round only when
    the budget also says its last round converged — and a human push
    resets the budget, so that status cannot predate one. Anything else
    matching it is a loom push of unknown kind: it carries, never approves.
    """
    for key, kind in (
        (_MERGE_GATE, "merge_gate"),
        (_CONFLICT_RESOLVE, "conflict_resolve"),
    ):
        if _full_sha(_record(meta, key, pr_url).get("pushed_sha")) == head:
            return kind
    budget = _record(meta, _REMEDIATION, pr_url)
    if _full_sha(budget.get("last_loom_pushed_sha")) != head:
        return "human"
    converged = budget.get("last_status") == "converged"
    return "remediation" if converged and budget.get("last_settled") is True else "loom"


def _review_settled(
    record: dict[str, Any], meta: Mapping[str, Any], pr_url: str
) -> None:
    """Track the remediation round that answers an open review; clear the
    pin when that round settled the PR without a push (a push clears it in
    :func:`_apply_push`). Only a round seen in flight AFTER the pin counts:
    the reservation clears the last status, so an older settle is no answer.
    """
    if record["review_open_head"] != record["observed_head"]:
        return
    budget = _record(meta, _REMEDIATION, pr_url)
    if budget.get("in_flight_boot_id"):
        record["review_round_seen"] = True
    elif (
        record["review_round_seen"]
        and budget.get("last_settled") is True
        and budget.get("last_status") in _NO_CHANGE_SETTLES
    ):
        record["review_open_head"] = ""
        record["review_round_seen"] = False


def _apply_push(
    record: dict[str, Any], meta: Mapping[str, Any], pr_url: str, head: str
) -> tuple[dict[str, Any], str]:
    """Move the record onto *head*; returns it and who pushed (``""`` when
    the head did not move or this is the first sight with nothing to
    compare against)."""
    prev = record["observed_head"]
    if not prev or head == prev:
        return {**record, "observed_head": head}, ""
    pusher = _pusher(meta, pr_url, head)
    approved = record["approved_head"]
    review = record["review_open_head"]
    if pusher == "remediation":
        approved, review = head, ""
    elif pusher in _CARRYING_PUSHES:
        approved = head if approved == prev else approved
        review = head if review == prev else review
    else:
        approved = ""
    seen = record["review_round_seen"] and review == head
    pushes = dict(record["pushes_after_would_merge"])
    if record["would_merge_at"]:
        side = "human" if pusher == "human" else "loom"
        pushes[side] += 1
    return {
        **record,
        "observed_head": head,
        "approved_head": approved,
        "review_open_head": review,
        "review_round_seen": seen,
        "pushes_after_would_merge": pushes,
    }, pusher


def _invalidation(record: Mapping[str, Any], pusher: str, head: str, state: str) -> str:
    if pusher == "human":
        return "human_push"
    if record["review_open_head"] == head:
        return "review_open"
    if state in _STOP_STATES:
        return f"state:{state}"
    return ""


def observe(
    meta: Mapping[str, Any],
    *,
    pr_url: str,
    head_sha: str,
    state: str,
    review_open: bool,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """One sweep's observation of a still-open gate: the updated record, or
    ``None`` when nothing moved (or the record already has its outcome, or
    the head is unknown). Pure; never raises on a malformed record."""
    head = _full_sha(head_sha)
    stored = _load(meta, pr_url)
    if not head or (stored is not None and "outcome" in stored):
        return None
    record, pusher = _apply_push(stored or _fresh(meta, pr_url), meta, pr_url, head)
    if review_open:
        record["review_open_head"] = head
        record["review_round_seen"] = False
    _review_settled(record, meta, pr_url)
    at = (now or datetime.now(UTC)).isoformat()
    qualifies = (
        record["basis"] == "approval"
        and state == "ready_to_merge"
        and record["approved_head"] == head
        and record["review_open_head"] != head
    )
    if qualifies:
        if not record["would_merge_at"]:
            record["would_merge_at"] = at
            record["would_merge_head"] = head
        record["last_would_merge_head"] = head
    elif record["would_merge_at"] and not record["invalidated_at"]:
        reason = _invalidation(record, pusher, head, state)
        if reason:
            record["invalidated_at"] = at
            record["invalidated_reason"] = reason
    return None if record == stored else record


def outcome(
    meta: Mapping[str, Any],
    *,
    pr_url: str,
    how: str,
    head_sha: str = "",
    merged_at: datetime | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """The record with the operator's outcome, or ``None`` when one is
    already recorded. *how* is the gate's resolution: ``merged`` (classified
    against the would-merge head), ``closed_unmerged``, ``gone`` or
    ``waiter_resolved``."""
    if how not in _RESOLUTIONS:
        raise ValueError(f"unknown gate resolution: {how!r}")
    stored = _load(meta, pr_url)
    if stored is not None and "outcome" in stored:
        return None
    record = stored or _fresh(meta, pr_url)
    when = now or datetime.now(UTC)
    if how != "merged":
        return {**record, "outcome": how, "outcome_at": when.isoformat()}
    head = _full_sha(head_sha)
    pusher = ""
    if head:
        record, pusher = _apply_push(record, meta, pr_url, head)
    at = merged_at or when
    record = {**record, "outcome_at": at.isoformat(), "merged_head": head}
    if record["would_merge_at"] and not record["invalidated_at"] and pusher:
        # the push no sweep saw gets the invalidation a sweep would have
        # recorded, so the record does not depend on how fast the merge was
        reason = _invalidation(record, pusher, head, "")
        if reason:
            record = {
                **record,
                "invalidated_at": at.isoformat(),
                "invalidated_reason": reason,
            }
    if not record["would_merge_at"]:
        return {**record, "outcome": "merged_never_would", "elapsed_s": None}
    verdict = (
        "merged_at_head"
        if head == record["would_merge_head"]
        else "merged_after_pushes"
    )
    return {
        **record,
        "outcome": verdict,
        "elapsed_s": _elapsed(record["would_merge_at"], at),
    }


def _elapsed(since: str, at: datetime) -> int | None:
    """Seconds from *since* to *at*, or ``None`` when *since* is not a
    timezone-aware ISO time — the resolution path must never raise on a
    hand-edited record."""
    try:
        start = datetime.fromisoformat(since)
        # a merge between the sweep's fetch and its stamp, or clock skew
        return max(0, int((at - start).total_seconds()))
    except (ValueError, TypeError):
        return None


def outcome_marker(
    meta: Mapping[str, Any],
    *,
    pr_url: str,
    how: str,
    head_sha: str = "",
    merged_at: datetime | None = None,
) -> dict[str, Any]:
    """:func:`outcome` as a marker to fold into the resolution's own write,
    or ``{}`` when the outcome is already recorded."""
    record = outcome(
        meta, pr_url=pr_url, how=how, head_sha=head_sha, merged_at=merged_at
    )
    return {SHADOW_KEY: record} if record is not None else {}


async def record_shadow(
    gate_id: str,
    meta: Mapping[str, Any],
    ctx: SubscriptionContext,
    *,
    pr_url: str,
    head_sha: str,
    state: str,
    review_open: bool,
) -> bool:
    """Observe a still-open gate and write its record when it moved. Returns
    whether a write landed. Never raises: a lost write is re-derived from
    the PR next sweep (only a push between the two goes unattributed)."""
    record = observe(
        meta, pr_url=pr_url, head_sha=head_sha, state=state, review_open=review_open
    )
    if record is None:
        return False
    before = _load(meta, pr_url) or {}
    landed = await write_marker(
        ctx, task_id=gate_id, marker={SHADOW_KEY: record}, subsystem=_SUBSYSTEM
    )
    if landed and record["would_merge_at"] and not before.get("would_merge_at"):
        ctx.logger.info(
            "%s: %s — loom would merge at %s",
            _SUBSYSTEM,
            pr_url,
            record["would_merge_head"][:12],
        )
    if landed and record["invalidated_at"] and not before.get("invalidated_at"):
        ctx.logger.info(
            "%s: %s — would-merge invalidated (%s)",
            _SUBSYSTEM,
            pr_url,
            record["invalidated_reason"],
        )
    return landed
