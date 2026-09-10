"""The ``pr`` gate's reconciliation state (PRD pr-reconciliation S7).

Findings are history; the state answers "what is this PR's state right now".
It is DERIVED every sweep from the markers the dispatchers already keep on
the gate plus what is in flight in this process, written by the sweep alone
(ADR 0011 §3), and re-keyed on the PR url so a replacement PR starts fresh.
"""

from __future__ import annotations

import logging
from typing import Any

from lithos_loom.errors import LithosClientError
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions.conflict_resolve_record import ConflictResolveRecord
from lithos_loom.subscriptions.merge_gate_record import MergeGateRecord
from lithos_loom.subscriptions.reconciliation_state import (
    DETAIL_KEY,
    SINCE_KEY,
    STATE_KEY,
    STATE_URL_KEY,
    STATES,
    Busy,
    derive_state,
    record_state,
)
from lithos_loom.subscriptions.remediation_budget import RemediationBudget
from tests.support import FakeLithosClient

_URL = "https://github.com/agent-lore/lithos-lens/pull/80"
_HEAD = "h1"
_BASE = "b1"


class _PR:
    def __init__(self, mergeable: bool | None = True, mergeable_state: str = "clean"):
        self.mergeable = mergeable
        self.mergeable_state = mergeable_state
        self.head_sha = _HEAD
        self.base_sha = _BASE


def _derive(meta: dict[str, Any], *, pr: _PR | None = None, busy: Busy | None = None):
    return derive_state(meta, pr=pr or _PR(), pr_url=_URL, busy=busy or Busy())


def _regate(status: str, *, head: str = _HEAD, **fields: Any) -> dict[str, Any]:
    """A real merge-gate record, as `merge_gate_dispatch` writes it: the
    outcome is `status` (lowercase); `verdict` is GitHub-style RED/GREEN/None."""
    verdict = {"green": "GREEN", "red": "RED", "push_failed": "GREEN"}.get(status)
    record = MergeGateRecord(
        pr_url=_URL,
        head_sha=head,
        base_sha=_BASE,
        status=status,
        verdict=verdict,
        **fields,
    )
    return {"merge_gate": record.as_marker()}


def _conflict(status: str, *, head: str = _HEAD, **fields: Any) -> dict[str, Any]:
    record = ConflictResolveRecord(
        pr_url=_URL, head_sha=head, base_sha=_BASE, status=status, **fields
    )
    return {"conflict_resolve": record.as_marker()}


def _ctx(client: Any) -> SubscriptionContext:
    return SubscriptionContext(
        lithos=client, logger=logging.getLogger("test"), agent_id="a"
    )


# ── the vocabulary ───────────────────────────────────────────────────────


def test_the_seven_states_are_the_prd_vocabulary() -> None:
    assert STATES == (
        "awaiting_review",
        "reconciling",
        "behind",
        "resolving_conflict",
        "gate_failed",
        "needs_human",
        "ready_to_merge",
    )


# ── derivation, in precedence order ──────────────────────────────────────


def test_a_clean_pr_with_nothing_pending_is_ready_to_merge() -> None:
    assert _derive({}).state == "ready_to_merge"


def test_a_green_re_gate_on_the_current_pair_is_still_ready() -> None:
    assert _derive(_regate("green")).state == "ready_to_merge"


def test_an_empty_check_set_is_vacuously_ready() -> None:
    d = _derive(_regate("no_checks"))
    assert d.state == "ready_to_merge" and "no checks" in d.detail


def test_github_blocked_is_awaiting_review() -> None:
    d = _derive({}, pr=_PR(mergeable_state="blocked"))
    assert d.state == "awaiting_review" and "blocked" in d.detail


def test_github_unknown_landability_is_awaiting_review() -> None:
    d = _derive({}, pr=_PR(mergeable=None, mergeable_state=""))
    assert d.state == "awaiting_review"


def test_a_crashed_re_gate_is_awaiting_review_pending_its_retry() -> None:
    d = _derive(_regate("crashed"))
    assert d.state == "awaiting_review" and "crashed" in d.detail


def test_github_behind_is_behind() -> None:
    assert _derive({}, pr=_PR(mergeable_state="behind")).state == "behind"


def test_a_dirty_pr_is_behind_with_the_conflict_named() -> None:
    d = _derive({}, pr=_PR(mergeable=False, mergeable_state="dirty"))
    assert d.state == "behind" and "conflict" in d.detail


def test_a_conflict_verdict_from_the_re_gate_is_behind() -> None:
    assert _derive(_regate("conflict")).state == "behind"


def test_a_green_merge_whose_push_failed_is_behind() -> None:
    meta = _regate("push_failed", behind=True, pushed_sha="", push_error="rejected")
    d = _derive(meta)
    assert d.state == "behind" and "rejected" in d.detail


def test_a_red_or_errored_re_gate_is_gate_failed() -> None:
    for status in ("red", "errored"):
        d = _derive(_regate(status))
        assert d.state == "gate_failed" and status in d.detail


def test_a_re_gate_record_for_another_pair_is_ignored() -> None:
    assert _derive(_regate("red", head="old")).state == "ready_to_merge"


def test_a_re_gate_record_for_another_pr_url_is_ignored() -> None:
    meta = _regate("red")
    meta["merge_gate"]["pr_url"] = "https://github.com/x/y/pull/1"
    assert _derive(meta).state == "ready_to_merge"


def test_an_in_flight_remediation_or_re_gate_is_reconciling() -> None:
    assert _derive({}, busy=Busy(remediation=True)).state == "reconciling"
    d = _derive({}, busy=Busy(merge_gate=True))
    assert d.state == "reconciling" and "merge-gate" in d.detail


def test_a_parked_remediation_trigger_is_reconciling() -> None:
    meta = {"external_remediation_pending": {"pr_url": _URL}}
    d = _derive(meta)
    assert d.state == "reconciling" and "parked" in d.detail


def test_a_held_conflict_debt_is_reconciling_not_resolving() -> None:
    """The resolver pushed; only its bookkeeping write is pending. The
    coder is not running, so the row must not say it is."""
    d = _derive({}, busy=Busy(conflict_debt=True))
    assert d.state == "reconciling" and "pushed" in d.detail


def test_a_running_conflict_resolver_is_resolving_conflict() -> None:
    assert _derive({}, busy=Busy(conflict_resolve=True)).state == "resolving_conflict"
    assert _derive(_conflict("running")).state == "resolving_conflict"


def test_resolving_conflict_outranks_reconciling() -> None:
    d = _derive({}, busy=Busy(merge_gate=True, conflict_resolve=True))
    assert d.state == "resolving_conflict"


def test_an_escalated_conflict_is_needs_human_naming_the_gate() -> None:
    meta = _conflict("not_converged", needs_human_gate_id="gate-h")
    d = _derive(meta, pr=_PR(mergeable=False, mergeable_state="dirty"))
    assert d.state == "needs_human" and "gate-h" in d.detail


def test_an_exhausted_remediation_budget_is_needs_human() -> None:
    budget = RemediationBudget(pr_url=_URL, rounds_used=2, needs_human_gate_id="gate-h")
    d = _derive(
        {"external_remediation": budget.as_marker()}, busy=Busy(merge_gate=True)
    )
    assert d.state == "needs_human" and "gate-h" in d.detail


def test_a_closed_or_deleted_pr_is_needs_human() -> None:
    for marker in ("closed_unmerged", "gone"):
        meta = {"develop_pr_merge_state": marker, "develop_pr_merge_url": _URL}
        d = _derive(meta)
        expected = "closed" if marker == "closed_unmerged" else "404"
        assert d.state == "needs_human" and expected in d.detail


def test_a_dispatcher_refusal_the_operator_must_fix_is_needs_human() -> None:
    for status in ("repo_mismatch", "checkout_unresolved", "config_unresolved"):
        d = _derive(_regate(status))
        assert d.state == "needs_human" and status in d.detail


def test_a_stale_escalation_for_an_old_pair_does_not_hold_needs_human() -> None:
    """A human push re-keys the pair; the old pair's escalation is history."""
    meta = _conflict("not_converged", head="old", needs_human_gate_id="gate-h")
    assert _derive(meta).state == "ready_to_merge"


def test_malformed_markers_never_raise() -> None:
    meta = {
        "merge_gate": "nonsense",
        "conflict_resolve": 7,
        "external_remediation": None,
        "external_remediation_pending": [],
        "develop_pr_merge_state": {"x": 1},
    }
    assert _derive(meta).state == "ready_to_merge"


# ── recording ────────────────────────────────────────────────────────────


async def _gate(client: FakeLithosClient, metadata: dict[str, Any] | None = None):
    gate_id = await client.task_create(
        title="Awaiting merge: x",
        agent="a",
        task_type="gate",
        metadata={"gate_type": "pr", "pr_url": _URL, **(metadata or {})},
    )
    gate = await client.task_get(task_id=gate_id)
    assert gate is not None
    return gate


async def test_record_writes_state_detail_url_and_since_on_a_change() -> None:
    client = FakeLithosClient(agent_id="a")
    gate = await _gate(client)
    ctx = _ctx(client)

    changed = await record_state(gate, _PR(), _URL, ctx, busy=Busy())

    assert changed == "ready_to_merge"
    stored = await client.task_get(task_id=gate.id)
    assert stored is not None
    assert stored.metadata[STATE_KEY] == "ready_to_merge"
    assert stored.metadata[STATE_URL_KEY] == _URL
    assert isinstance(stored.metadata[SINCE_KEY], str) and stored.metadata[SINCE_KEY]
    assert isinstance(stored.metadata[DETAIL_KEY], str)


async def test_record_is_a_noop_when_nothing_changed() -> None:
    client = FakeLithosClient(agent_id="a")
    gate = await _gate(client)
    ctx = _ctx(client)
    await record_state(gate, _PR(), _URL, ctx, busy=Busy())
    gate = await client.task_get(task_id=gate.id)
    assert gate is not None
    writes = len(client.calls_to("task_update"))

    changed = await record_state(gate, _PR(), _URL, ctx, busy=Busy())

    assert changed is None
    assert len(client.calls_to("task_update")) == writes


async def test_record_keeps_since_when_only_the_detail_moves() -> None:
    client = FakeLithosClient(agent_id="a")
    gate = await _gate(client)
    ctx = _ctx(client)
    await record_state(gate, _PR(mergeable_state="blocked"), _URL, ctx, busy=Busy())
    gate = await client.task_get(task_id=gate.id)
    assert gate is not None
    since = gate.metadata[SINCE_KEY]

    changed = await record_state(
        gate, _PR(mergeable=None, mergeable_state=""), _URL, ctx, busy=Busy()
    )

    assert changed is None  # same state, new detail — not a transition
    stored = await client.task_get(task_id=gate.id)
    assert stored is not None
    assert stored.metadata[SINCE_KEY] == since
    assert "blocked" not in stored.metadata[DETAIL_KEY]


async def test_record_refreshes_the_gate_before_deriving() -> None:
    """The dispatchers wrote their markers earlier this sweep; the gate in
    hand is stale. The state is derived from the gate as it is NOW."""
    client = FakeLithosClient(agent_id="a")
    gate = await _gate(client)
    ctx = _ctx(client)
    await client.task_update(
        task_id=gate.id,
        metadata=_regate("red"),
    )

    changed = await record_state(gate, _PR(), _URL, ctx, busy=Busy())

    assert changed == "gate_failed"


async def test_record_falls_back_to_the_stale_gate_when_the_refresh_fails() -> None:
    client = FakeLithosClient(agent_id="a")
    gate = await _gate(client)
    ctx = _ctx(client)

    async def failing_task_get(**kwargs: Any) -> Any:
        raise LithosClientError("server_error", "down")

    client.task_get = failing_task_get  # type: ignore[method-assign]

    changed = await record_state(gate, _PR(), _URL, ctx, busy=Busy())

    assert changed == "ready_to_merge"


async def test_record_rekeys_a_state_left_by_a_replaced_pr() -> None:
    client = FakeLithosClient(agent_id="a")
    gate = await _gate(
        client,
        {
            STATE_KEY: "needs_human",
            STATE_URL_KEY: "https://github.com/agent-lore/lithos-lens/pull/79",
            SINCE_KEY: "2026-01-01T00:00:00+00:00",
        },
    )
    ctx = _ctx(client)

    changed = await record_state(gate, _PR(), _URL, ctx, busy=Busy())

    assert changed == "ready_to_merge"
    stored = await client.task_get(task_id=gate.id)
    assert stored is not None
    assert stored.metadata[STATE_URL_KEY] == _URL
    assert stored.metadata[SINCE_KEY] != "2026-01-01T00:00:00+00:00"


async def test_record_survives_a_failed_write() -> None:
    client = FakeLithosClient(agent_id="a")
    gate = await _gate(client)
    ctx = _ctx(client)

    async def failing_update(**kwargs: Any) -> Any:
        raise LithosClientError("server_error", "down")

    client.task_update = failing_update  # type: ignore[method-assign]

    assert await record_state(gate, _PR(), _URL, ctx, busy=Busy()) is None


def test_closed_state_marker_is_needs_human() -> None:
    from lithos_loom.subscriptions.reconciliation_state import closed_state_marker

    marker = closed_state_marker(_URL, "closed_unmerged")
    assert marker[STATE_KEY] == "needs_human"
    assert marker[STATE_URL_KEY] == _URL
    assert "closed" in marker[DETAIL_KEY] and SINCE_KEY in marker


# ── review #369: absence is not "not required"; escalation without an id ─


from lithos_loom.subscriptions.reconciliation_state import Dispositions  # noqa: E402


def _derive_d(meta: dict[str, Any], *, pr: _PR | None = None, **kw: Any):
    busy = kw.pop("busy", Busy())
    return derive_state(
        meta, pr=pr or _PR(), pr_url=_URL, busy=busy, dispositions=Dispositions(**kw)
    )


def test_a_required_re_gate_that_has_not_run_is_not_ready() -> None:
    """Review #369 F1: no current record means "not evaluated", not
    "not required" — only a disabled dispatcher makes absence fine."""
    for label in ("unknown_shas", "no_story", "project_settings_unavailable"):
        d = _derive_d({}, regate=label)
        assert d.state == "awaiting_review" and label in d.detail, label


def test_a_re_gate_queued_behind_another_pr_is_reconciling() -> None:
    for label in ("deferred_busy", "deferred_remediation", "probing", "dispatched"):
        d = _derive_d({}, regate=label)
        assert d.state == "reconciling" and "re-gate" in d.detail, label


def test_an_unreadable_live_base_tip_is_never_ready() -> None:
    pr = _PR()
    pr.base_sha = ""  # the sweep's "could not read the base" sentinel
    d = _derive_d({}, pr=pr, regate="unknown_shas")
    assert d.state == "awaiting_review" and "base" in d.detail
    d = _derive_d(_regate("green"), pr=pr, regate="unchanged")
    assert d.state != "ready_to_merge"


def test_a_disabled_or_unconfigured_re_gate_makes_absence_not_required() -> None:
    assert _derive_d({}, regate=None).state == "ready_to_merge"
    for label in ("disabled", "project_disabled"):
        assert _derive_d({}, regate=label).state == "ready_to_merge", label


def test_a_green_current_record_with_unchanged_label_is_ready() -> None:
    assert _derive_d(_regate("green"), regate="unchanged").state == "ready_to_merge"


def test_a_refusal_label_is_needs_human_even_without_a_record() -> None:
    for label in ("repo_mismatch", "checkout_unresolved", "fork_unsupported"):
        d = _derive_d({}, regate=label)
        assert d.state == "needs_human" and label in d.detail, label


def test_a_story_already_escalated_outranks_a_stale_running_record() -> None:
    """Review #369 F3: the gate id write is best-effort; the resolver's
    `escalated` disposition (an open human gate on the story) is the
    authoritative signal and must not be shadowed by the `running` record."""
    d = _derive_d(_conflict("running"), conflict="escalated")
    assert d.state == "needs_human" and "decision" in d.detail


def test_a_settled_conflict_outcome_without_a_gate_id_is_needs_human() -> None:
    for status in ("not_converged", "failed", "conflict_unsupported"):
        d = _derive_d(
            _conflict(status), pr=_PR(mergeable=False, mergeable_state="dirty")
        )
        assert d.state == "needs_human" and status in d.detail, status


def test_an_exhausted_budget_without_a_gate_id_is_needs_human() -> None:
    d = _derive_d({}, remediation_exhausted=True, busy=Busy(merge_gate=True))
    assert d.state == "needs_human" and "exhausted" in d.detail


def test_the_detail_is_truncated_at_the_source() -> None:
    from lithos_loom.subscriptions.reconciliation_state import DETAIL_MAX_CHARS

    meta = _regate("push_failed", behind=True, pushed_sha="", push_error="x" * 500)
    assert len(_derive(meta).detail) == DETAIL_MAX_CHARS


async def test_record_is_idempotent_for_an_over_long_detail() -> None:
    """Review #369 F4: compare what would be STORED, or a long push error
    rewrites byte-identical state every sweep."""
    client = FakeLithosClient(agent_id="a")
    gate = await _gate(
        client, _regate("push_failed", behind=True, pushed_sha="", push_error="x" * 500)
    )
    ctx = _ctx(client)
    await record_state(gate, _PR(), _URL, ctx, busy=Busy())
    gate = await client.task_get(task_id=gate.id)
    assert gate is not None
    writes = len(client.calls_to("task_update"))

    assert await record_state(gate, _PR(), _URL, ctx, busy=Busy()) is None
    assert len(client.calls_to("task_update")) == writes
