"""Shadow auto-merge recording (review-convergence M1, task 664d84c4).

The sweep records on each ``pr`` gate the first head at which loom WOULD
have merged the PR — ``ready_to_merge``, a panel approval covering that
head, no open external review — and, when the gate resolves, what the
operator actually did. Zero tokens: a recording, never a decision.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from lithos_loom.errors import LithosClientError
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions.shadow_merge import (
    APPROVAL_KEY,
    OUTCOMES,
    SHADOW_KEY,
    approval_marker,
    observe,
    outcome,
    outcome_marker,
    record_shadow,
)
from tests.support import FakeLithosClient

_URL = "https://github.com/agent-lore/lithos-lens/pull/80"
_OTHER_URL = "https://github.com/agent-lore/lithos-lens/pull/81"
_A = "a" * 40  # the head the delivery panel approved
_B = "b" * 40
_C = "c" * 40
_T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _approval(head: str = _A, *, url: str = _URL) -> dict[str, Any]:
    marker = approval_marker(url, head, source="run", run_id="r1", now=_T0)
    assert marker is not None
    return marker


def _observe(
    meta: dict[str, Any],
    *,
    head: str = _A,
    state: str = "ready_to_merge",
    review_open: bool = False,
    at: datetime = _T0,
) -> dict[str, Any] | None:
    return observe(
        meta,
        pr_url=_URL,
        head_sha=head,
        state=state,
        review_open=review_open,
        now=at,
    )


def _step(meta: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """One sweep: observe, then fold the record back in as the write would."""
    record = _observe(meta, **kwargs)
    return {**meta, SHADOW_KEY: record} if record is not None else meta


def _loom_push(meta: dict[str, Any], sha: str, kind: str) -> dict[str, Any]:
    """The record a loom push leaves on the gate, as each dispatcher writes it."""
    if kind == "remediation":
        # a converged round: the outcome write records the status AND the push
        return {
            **meta,
            "external_remediation": {
                "pr_url": _URL,
                "last_loom_pushed_sha": sha,
                "last_status": "converged",
                "last_settled": True,
            },
        }
    if kind == "merge_gate":
        return {
            **meta,
            "merge_gate": {"pr_url": _URL, "pushed_sha": sha},
            "external_remediation": {"pr_url": _URL, "last_loom_pushed_sha": sha},
        }
    assert kind == "conflict_resolve"
    return {
        **meta,
        "conflict_resolve": {"pr_url": _URL, "pushed_sha": sha},
        "external_remediation": {"pr_url": _URL, "last_loom_pushed_sha": sha},
    }


# ── the approval record ──────────────────────────────────────────────────


def test_approval_marker_binds_a_full_sha_to_the_pr() -> None:
    marker = approval_marker(_URL, _A.upper(), source="deliver", run_id="", now=_T0)
    assert marker == {
        APPROVAL_KEY: {
            "pr_url": _URL,
            "head_sha": _A,
            "source": "deliver",
            "delivered_at": _T0.isoformat(),
        }
    }


def test_approval_marker_refuses_anything_but_a_full_sha() -> None:
    for head in ("", "abc123", None, "z" * 40, 42):
        assert approval_marker(_URL, head, source="run", now=_T0) is None


# ── would-merge ──────────────────────────────────────────────────────────


def test_would_merge_is_recorded_at_the_first_qualifying_head() -> None:
    record = _observe(_approval())
    assert record is not None
    assert record["basis"] == "approval"
    assert record["would_merge_at"] == _T0.isoformat()
    assert record["would_merge_head"] == _A
    assert record["delivered_at"] == _T0.isoformat()


def test_would_merge_is_recorded_once() -> None:
    meta = _step(_approval())
    later = _T0 + timedelta(hours=1)
    assert _observe(meta, at=later) is None  # nothing moved, nothing written


def test_not_ready_to_merge_does_not_qualify() -> None:
    for state in ("awaiting_review", "reconciling", "behind", "gate_failed"):
        record = _observe(_approval(), state=state)
        assert record is not None  # first sight is still recorded
        assert record["would_merge_at"] == ""


def test_a_head_the_approval_does_not_cover_does_not_qualify() -> None:
    record = _observe(_approval(), head=_B)  # a human pushed before first sight
    assert record is not None
    assert record["would_merge_at"] == ""
    assert record["approved_head"] == ""


def test_a_gate_without_an_approval_record_never_qualifies() -> None:
    record = _observe({})
    assert record is not None
    assert record["basis"] == "no_approval_record"
    assert record["would_merge_at"] == ""


def test_an_approval_for_another_pr_is_no_approval() -> None:
    record = _observe(_approval(url=_OTHER_URL))
    assert record is not None
    assert record["basis"] == "no_approval_record"


def test_an_open_review_blocks_its_head() -> None:
    record = _observe(_approval(), review_open=True)
    assert record is not None
    assert record["would_merge_at"] == ""
    assert record["review_open_head"] == _A


# ── pushes: who, and whether the approval carries ────────────────────────


def test_a_loom_push_carries_the_approval_to_the_new_head() -> None:
    for kind in ("remediation", "merge_gate", "conflict_resolve"):
        meta = _step(_approval(), state="behind")
        meta = _loom_push(meta, _B, kind)
        record = _observe(meta, head=_B)
        assert record is not None, kind
        assert record["approved_head"] == _B, kind
        assert record["would_merge_head"] == _B, kind


def test_a_human_push_drops_the_approval() -> None:
    meta = _step(_approval(), state="awaiting_review")
    record = _observe(meta, head=_B)
    assert record is not None
    assert record["approved_head"] == ""
    assert record["would_merge_at"] == ""


def test_a_remediation_round_after_a_human_push_re_approves() -> None:
    meta = _step(_approval(), state="awaiting_review")
    meta = _step(meta, head=_B, state="awaiting_review")  # human
    meta = _loom_push(meta, _C, "remediation")
    record = _observe(meta, head=_C)
    assert record is not None
    assert record["approved_head"] == _C
    assert record["would_merge_head"] == _C


def test_a_merge_gate_push_does_not_re_approve_after_a_human_push() -> None:
    meta = _step(_approval(), state="awaiting_review")
    meta = _step(meta, head=_B, state="awaiting_review")  # human
    meta = _loom_push(meta, _C, "merge_gate")
    record = _observe(meta, head=_C)
    assert record is not None
    assert record["approved_head"] == ""
    assert record["would_merge_at"] == ""


def test_an_open_review_survives_a_base_merge_but_not_a_remediation() -> None:
    meta = _step(_approval(), review_open=True)
    carried = _observe(_loom_push(meta, _B, "merge_gate"), head=_B)
    assert carried is not None
    assert carried["review_open_head"] == _B
    assert carried["would_merge_at"] == ""
    settled = _observe(_loom_push(meta, _B, "remediation"), head=_B)
    assert settled is not None
    assert settled["would_merge_head"] == _B


def test_pushes_after_would_merge_are_counted_by_pusher() -> None:
    meta = _step(_approval())
    meta = _step(_loom_push(meta, _B, "merge_gate"), head=_B)
    meta = _step(meta, head=_C)  # human
    record = meta[SHADOW_KEY]
    assert record["pushes_after_would_merge"] == {"loom": 1, "human": 1}


def test_pushes_before_would_merge_are_not_counted() -> None:
    meta = _step(_approval(), state="behind")
    meta = _step(_loom_push(meta, _B, "merge_gate"), head=_B)
    assert meta[SHADOW_KEY]["pushes_after_would_merge"] == {"loom": 0, "human": 0}


# ── invalidation ─────────────────────────────────────────────────────────


def test_a_human_push_after_would_merge_invalidates() -> None:
    meta = _step(_approval())
    later = _T0 + timedelta(hours=2)
    record = _observe(meta, head=_B, state="ready_to_merge", at=later)
    assert record is not None
    assert record["invalidated_at"] == later.isoformat()
    assert record["invalidated_reason"] == "human_push"
    assert record["would_merge_head"] == _A  # the first verdict is kept


def test_a_red_merge_gate_after_would_merge_invalidates() -> None:
    meta = _step(_approval())
    record = _observe(_loom_push(meta, _B, "merge_gate"), head=_B, state="gate_failed")
    assert record is not None
    assert record["invalidated_reason"] == "state:gate_failed"


def test_a_review_opened_after_would_merge_invalidates() -> None:
    meta = _step(_approval())
    record = _observe(meta, review_open=True)
    assert record is not None
    assert record["invalidated_reason"] == "review_open"


def test_a_transient_state_does_not_invalidate() -> None:
    meta = _step(_approval())
    for state in ("reconciling", "behind", "awaiting_review", "resolving_conflict"):
        record = _observe(meta, state=state)
        assert record is None or record["invalidated_at"] == "", state


def test_the_first_invalidation_is_kept() -> None:
    meta = _step(_approval())
    meta = _step(meta, review_open=True)
    first = meta[SHADOW_KEY]["invalidated_at"]
    meta = _step(meta, head=_B, at=_T0 + timedelta(days=1))  # human
    assert meta[SHADOW_KEY]["invalidated_at"] == first
    assert meta[SHADOW_KEY]["invalidated_reason"] == "review_open"


# ── url scoping ──────────────────────────────────────────────────────────


def test_a_record_about_another_pr_is_ignored() -> None:
    meta = {**_approval(), SHADOW_KEY: {"pr_url": _OTHER_URL, "would_merge_at": "x"}}
    record = _observe(meta)
    assert record is not None
    assert record["pr_url"] == _URL
    assert record["would_merge_at"] == _T0.isoformat()


def test_a_malformed_record_is_treated_as_absent() -> None:
    for junk in ("x", 3, ["a"], {"pr_url": _URL, "pushes_after_would_merge": "x"}):
        record = _observe({**_approval(), SHADOW_KEY: junk})
        assert record is not None
        assert record["pushes_after_would_merge"] == {"loom": 0, "human": 0}


# ── the operator's outcome ───────────────────────────────────────────────


def test_the_outcome_vocabulary() -> None:
    assert OUTCOMES == (
        "merged_at_head",
        "merged_after_pushes",
        "merged_never_would",
        "closed_unmerged",
        "gone",
        "waiter_resolved",
    )


def test_merged_at_the_would_merge_head() -> None:
    meta = _step(_approval())
    merged = _T0 + timedelta(hours=3)
    record = outcome(meta, pr_url=_URL, how="merged", head_sha=_A, merged_at=merged)
    assert record is not None
    assert record["outcome"] == "merged_at_head"
    assert record["merged_head"] == _A
    assert record["elapsed_s"] == 3 * 3600


def test_merged_after_pushes_counts_the_last_unswept_push() -> None:
    meta = _step(_approval())
    meta = _loom_push(meta, _B, "merge_gate")  # pushed, then merged before a sweep
    record = outcome(
        meta, pr_url=_URL, how="merged", head_sha=_B, merged_at=_T0 + timedelta(1)
    )
    assert record is not None
    assert record["outcome"] == "merged_after_pushes"
    assert record["pushes_after_would_merge"] == {"loom": 1, "human": 0}


def test_merged_although_loom_never_would() -> None:
    meta = _step(_approval(), state="awaiting_review")
    record = outcome(meta, pr_url=_URL, how="merged", head_sha=_A, merged_at=_T0)
    assert record is not None
    assert record["outcome"] == "merged_never_would"
    assert record["elapsed_s"] is None


def test_closed_gone_and_waiter_resolved_are_recorded_as_such() -> None:
    meta = _step(_approval())
    for how in ("closed_unmerged", "gone", "waiter_resolved"):
        record = outcome(meta, pr_url=_URL, how=how, now=_T0)
        assert record is not None
        assert record["outcome"] == how
        assert record["outcome_at"] == _T0.isoformat()


def test_a_gate_never_observed_still_gets_its_outcome() -> None:
    record = outcome({}, pr_url=_URL, how="merged", head_sha=_A, merged_at=_T0)
    assert record is not None
    assert record["basis"] == "no_approval_record"
    assert record["outcome"] == "merged_never_would"


def test_the_outcome_is_written_once_and_freezes_the_record() -> None:
    meta = {
        **_approval(),
        SHADOW_KEY: outcome(_step(_approval()), pr_url=_URL, how="gone", now=_T0),
    }
    assert outcome(meta, pr_url=_URL, how="merged", head_sha=_A, merged_at=_T0) is None
    assert outcome_marker(meta, pr_url=_URL, how="gone") == {}
    assert _observe(meta, head=_B) is None


def test_outcome_marker_wraps_the_record_for_a_shared_write() -> None:
    marker = outcome_marker(_step(_approval()), pr_url=_URL, how="closed_unmerged")
    assert marker[SHADOW_KEY]["outcome"] == "closed_unmerged"


# ── the write ────────────────────────────────────────────────────────────


def _ctx(client: Any) -> SubscriptionContext:
    return SubscriptionContext(
        lithos=client, logger=logging.getLogger("test"), agent_id="a"
    )


async def test_record_shadow_writes_only_when_the_record_moves() -> None:
    client = FakeLithosClient(agent_id="a")
    gate_id = await client.task_create(
        title="Awaiting merge: x",
        agent="a",
        task_type="gate",
        metadata={"gate_type": "pr", "pr_url": _URL, **_approval()},
    )
    gate = await client.task_get(task_id=gate_id)
    assert gate is not None
    ctx = _ctx(client)

    kwargs: dict[str, Any] = {
        "pr_url": _URL,
        "head_sha": _A,
        "state": "ready_to_merge",
        "review_open": False,
    }
    assert await record_shadow(gate.id, gate.metadata, ctx, **kwargs) is True
    stored = await client.task_get(task_id=gate_id)
    assert stored is not None
    assert stored.metadata[SHADOW_KEY]["would_merge_head"] == _A

    writes = len(client.calls_to("task_update"))
    assert await record_shadow(gate.id, stored.metadata, ctx, **kwargs) is False
    assert len(client.calls_to("task_update")) == writes


async def test_record_shadow_never_raises_on_a_failed_write() -> None:
    client = FakeLithosClient(agent_id="a")
    gate_id = await client.task_create(
        title="Awaiting merge: x",
        agent="a",
        task_type="gate",
        metadata={"gate_type": "pr", "pr_url": _URL, **_approval()},
    )
    client.raise_on["task_update"] = LithosClientError("unavailable", "down")
    landed = await record_shadow(
        gate_id,
        _approval(),
        _ctx(client),
        pr_url=_URL,
        head_sha=_A,
        state="ready_to_merge",
        review_open=False,
    )
    assert landed is False


def test_a_malformed_would_merge_time_never_breaks_the_resolution() -> None:
    meta = _step(_approval())
    for junk in ("not-a-time", "2026-10-01T12:00:00"):  # unparseable; naive
        bad = {**meta, SHADOW_KEY: {**meta[SHADOW_KEY], "would_merge_at": junk}}
        record = outcome(bad, pr_url=_URL, how="merged", head_sha=_A, merged_at=_T0)
        assert record is not None
        assert record["outcome"] == "merged_at_head"
        assert record["elapsed_s"] is None


# ── review findings (H1, M1, M3, elapsed) ─────────────────────────────────


def test_a_superseded_merge_gate_push_after_a_human_push_never_re_approves() -> None:
    """H1: the merge-gate's next run rewrites its record (no ``pushed_sha``)
    and its push also set ``last_loom_pushed_sha`` — that match alone is
    not a remediation round, and the human push reset the budget's status."""
    meta = _step(_approval())
    meta = _step(meta, head=_B)  # human
    meta = {
        **meta,
        "merge_gate": {"pr_url": _URL, "pushed_sha": ""},
        "external_remediation": {
            "pr_url": _URL,
            "last_loom_pushed_sha": _C,
            "last_status": "",
            "last_settled": False,
        },
    }
    record = _observe(meta, head=_C)
    assert record is not None
    assert record["approved_head"] == ""
    assert record["would_merge_head"] == _A  # never re-recorded at C
    assert record["last_would_merge_head"] == _A
    assert record["pushes_after_would_merge"] == {"loom": 1, "human": 1}


def test_an_unattributed_loom_push_still_carries_an_approval() -> None:
    meta = _step(_approval(), state="behind")
    meta = {
        **meta,
        "external_remediation": {"pr_url": _URL, "last_loom_pushed_sha": _B},
    }
    record = _observe(meta, head=_B)
    assert record is not None
    assert record["would_merge_head"] == _B


def _budget(**fields: Any) -> dict[str, Any]:
    return {"external_remediation": {"pr_url": _URL, **fields}}


def test_a_review_stays_open_until_a_round_reserved_after_it_settles() -> None:
    """M1: with remediation on, a review is consumed at ingestion; it is
    open until a round that started AFTER it settles the PR without a push
    (refuted / nothing to change) or pushes a fix."""
    stale = _budget(last_status="already_clean", last_settled=True)
    meta = _step({**_approval(), **stale}, review_open=True)
    assert meta[SHADOW_KEY]["review_open_head"] == _A
    assert meta[SHADOW_KEY]["would_merge_at"] == ""  # a stale settle is no answer

    running = _budget(in_flight_boot_id="boot-1", last_status="")
    meta = _step({**meta, **running}, state="reconciling")
    assert meta[SHADOW_KEY]["review_open_head"] == _A

    done = _budget(
        in_flight_boot_id="", last_status="triage_rejected", last_settled=True
    )
    meta = _step({**meta, **done})
    assert meta[SHADOW_KEY]["review_open_head"] == ""
    assert meta[SHADOW_KEY]["would_merge_head"] == _A


def test_a_round_that_did_not_settle_leaves_the_review_open() -> None:
    meta = _step(_approval(), review_open=True)
    meta = _step({**meta, **_budget(in_flight_boot_id="b")}, state="reconciling")
    failed = _budget(
        in_flight_boot_id="", last_status="not_converged", last_settled=False
    )
    meta = _step({**meta, **failed})
    assert meta[SHADOW_KEY]["review_open_head"] == _A
    assert meta[SHADOW_KEY]["would_merge_at"] == ""


def test_a_round_in_flight_at_the_pin_counts_as_seen() -> None:
    """The dispatch happens in the same sweep as the ingestion, before the
    shadow record is written — so the pin itself sees the reservation."""
    meta = _step({**_approval(), **_budget(in_flight_boot_id="b")}, review_open=True)
    done = _budget(in_flight_boot_id="", last_status="already_clean", last_settled=True)
    meta = _step({**meta, **done})
    assert meta[SHADOW_KEY]["review_open_head"] == ""


def test_a_human_push_merged_before_the_next_sweep_invalidates() -> None:
    """M3: the merge-time push gets the invalidation a sweep would have
    recorded, so the record is the same however fast the operator merges."""
    meta = _step(_approval())
    merged = _T0 + timedelta(hours=1)
    record = outcome(meta, pr_url=_URL, how="merged", head_sha=_B, merged_at=merged)
    assert record is not None
    assert record["outcome"] == "merged_after_pushes"
    assert record["invalidated_reason"] == "human_push"
    assert record["invalidated_at"] == merged.isoformat()


def test_a_merge_before_the_would_merge_stamp_reads_zero_elapsed() -> None:
    meta = _step(_approval())
    early = _T0 - timedelta(seconds=5)
    record = outcome(meta, pr_url=_URL, how="merged", head_sha=_A, merged_at=early)
    assert record is not None
    assert record["elapsed_s"] == 0
