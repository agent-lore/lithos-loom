"""``lithos-loom gates --shadow`` — the M1 agreement reading (664d84c4).

The sweep records on each ``pr`` gate when loom WOULD have merged and what
the operator did (:mod:`lithos_loom.subscriptions.shadow_merge`). This is
the read side: per gate, then an agreement rate per project and overall, in
the form the merge-policy checkpoint (3398a388) quotes.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import UTC, datetime
from typing import Any

import pytest

from lithos_loom.cli.gates_shadow import (
    VERDICT_AGREE,
    VERDICT_DISAGREE,
    VERDICT_EXCLUDED,
    VERDICT_OPEN,
    VERDICT_UNMEASURED,
    ShadowRow,
    collect_shadow_rows,
    parse_since,
    render_shadow_report,
    shadow_label,
    shadow_row,
)
from lithos_loom.lithos_client import Task
from tests.support import FakeLithosClient, make_task

_A = "a" * 40
_B = "b" * 40
_DELIVERED = "2026-10-05T09:00:00+00:00"


def _gate(
    gate_id: str,
    *,
    record: dict[str, Any] | None = None,
    project: str = "lithos-lens",
    number: int = 80,
    status: str = "open",
    delivered_at: str = _DELIVERED,
    gate_type: str = "pr",
) -> Task:
    url = f"https://github.com/agent-lore/{project}/pull/{number}"
    metadata: dict[str, Any] = {
        "gate_type": gate_type,
        "repo": f"agent-lore/{project}",
        "pr_number": number,
        "pr_url": url,
        "required_state": "merged",
        "project": project,
    }
    if delivered_at:
        metadata["delivered_approval"] = {
            "pr_url": url,
            "head_sha": _A,
            "source": "run",
            "delivered_at": delivered_at,
        }
    if record is not None:
        metadata["shadow_merge"] = {"pr_url": url, **record}
    resolved = datetime(2026, 10, 9, tzinfo=UTC) if status != "open" else None
    return make_task(
        gate_id,
        status=status,
        metadata=metadata,
        task_type="gate",
        resolved_at=resolved,
    )


def _record(
    *,
    basis: str = "approval",
    would: bool = True,
    outcome: str | None = None,
    invalidated: str = "",
    elapsed: int | None = 3600,
    pushes: tuple[int, int] = (0, 0),
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "basis": basis,
        "would_merge_at": "2026-10-06T10:00:00+00:00" if would else "",
        "would_merge_head": _A if would else "",
        "invalidated_at": "2026-10-06T11:00:00+00:00" if invalidated else "",
        "invalidated_reason": invalidated,
        "pushes_after_would_merge": {"loom": pushes[0], "human": pushes[1]},
    }
    if outcome is not None:
        record.update(outcome=outcome, outcome_at="2026-10-07T10:00:00+00:00")
        if outcome.startswith("merged"):
            record["elapsed_s"] = elapsed if would else None
            record["merged_head"] = _A if outcome == "merged_at_head" else _B
    return record


# ── one gate ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("outcome", "would", "verdict"),
    [
        ("merged_at_head", True, VERDICT_AGREE),
        ("merged_after_pushes", True, VERDICT_DISAGREE),
        ("merged_never_would", False, VERDICT_DISAGREE),
        ("closed_unmerged", False, VERDICT_AGREE),  # neither would merge it
        ("closed_unmerged", True, VERDICT_DISAGREE),  # loom would have merged it
        ("gone", True, VERDICT_EXCLUDED),
        ("waiter_resolved", False, VERDICT_EXCLUDED),
    ],
)
def test_each_outcome_reads_as_its_verdict(
    outcome: str, would: bool, verdict: str
) -> None:
    gate = _gate("g1", record=_record(outcome=outcome, would=would), status="completed")
    row = shadow_row(gate)
    assert row is not None
    assert row.outcome == outcome
    assert row.verdict == verdict


def test_an_open_gate_has_no_verdict_yet() -> None:
    row = shadow_row(_gate("g1", record=_record()))
    assert row is not None and row.verdict == VERDICT_OPEN


def test_a_gate_without_an_approval_record_is_unmeasured() -> None:
    gate = _gate(
        "g1",
        record=_record(
            basis="no_approval_record", would=False, outcome="merged_never_would"
        ),
        status="completed",
        delivered_at="",
    )
    row = shadow_row(gate)
    assert row is not None and row.verdict == VERDICT_UNMEASURED


def test_a_gate_with_no_shadow_record_is_unmeasured() -> None:
    row = shadow_row(_gate("g1", status="completed", delivered_at=""))
    assert row is not None
    assert row.verdict == VERDICT_UNMEASURED
    assert row.outcome is None


def test_a_record_about_another_pr_is_ignored() -> None:
    gate = _gate("g1", record=_record(outcome="merged_at_head"), status="completed")
    meta = dict(gate.metadata)
    meta["shadow_merge"] = {**meta["shadow_merge"], "pr_url": "https://x/pull/1"}
    row = shadow_row(dataclasses.replace(gate, metadata=meta))
    assert row is not None and row.outcome is None


def test_only_pr_gates_have_shadow_rows() -> None:
    assert shadow_row(_gate("g1", gate_type="human")) is None


def test_the_delivery_time_falls_back_to_the_gate_creation() -> None:
    created = datetime(2026, 10, 2, tzinfo=UTC)
    gate = dataclasses.replace(_gate("g1", delivered_at=""), created_at=created)
    row = shadow_row(gate)
    assert row is not None and row.delivered == created


@pytest.mark.parametrize(
    ("record", "label"),
    [
        (None, "—"),
        (_record(would=False), "not-yet"),
        (_record(), "would"),
        (_record(invalidated="human_push"), "invalidated"),
        (_record(basis="no_approval_record", would=False), "unmeasured"),
    ],
)
def test_the_listing_column_says_where_the_verdict_stands(
    record: dict[str, Any] | None, label: str
) -> None:
    assert shadow_label(_gate("g1", record=record)) == label


def test_the_listing_column_is_blank_for_other_gates() -> None:
    assert shadow_label(_gate("g1", gate_type="human")) == "—"


# ── the window ───────────────────────────────────────────────────────────


def test_parse_since_accepts_days_and_dates() -> None:
    now = datetime(2026, 10, 10, 12, tzinfo=UTC)
    assert parse_since("7d", now=now) == datetime(2026, 10, 3, 12, tzinfo=UTC)
    assert parse_since("2026-10-01", now=now) == datetime(2026, 10, 1, tzinfo=UTC)


@pytest.mark.parametrize("bad", ["", "7", "7w", "-3d", "yesterday", "2026-13-01"])
def test_parse_since_rejects_anything_else(bad: str) -> None:
    with pytest.raises(ValueError, match="--since"):
        parse_since(bad, now=datetime(2026, 10, 10, tzinfo=UTC))


async def test_collect_reads_open_and_resolved_pr_gates_in_the_window() -> None:
    client = FakeLithosClient(agent_id="a")
    client.add_task(_gate("g-open", record=_record()))
    client.add_task(
        _gate("g-done", record=_record(outcome="merged_at_head"), status="completed")
    )
    client.add_task(_gate("g-old", delivered_at="2026-09-01T00:00:00+00:00"))
    client.add_task(_gate("g-other", project="lithos-loom"))
    client.add_task(_gate("g-human", gate_type="human"))
    client.add_task(make_task("story", status="open"))

    rows = await collect_shadow_rows(
        client, since=datetime(2026, 10, 1, tzinfo=UTC), project="lithos-lens"
    )

    assert [r.gate_id for r in rows] == ["g-done", "g-open"]
    assert not client.mutating_calls


async def test_a_gate_whose_delivery_time_is_unknown_is_outside_any_window() -> None:
    client = FakeLithosClient(agent_id="a")
    client.add_task(_gate("g1", delivered_at=""))
    assert (
        await collect_shadow_rows(client, since=datetime(2026, 1, 1, tzinfo=UTC)) == []
    )
    assert [r.gate_id for r in await collect_shadow_rows(client)] == ["g1"]


# ── the report ───────────────────────────────────────────────────────────


def _rows(*specs: tuple[str, str, bool, str]) -> list[ShadowRow]:
    rows = []
    for i, (project, outcome, would, basis) in enumerate(specs):
        status = "open" if outcome == "open" else "completed"
        record = _record(
            basis=basis,
            would=would,
            outcome=None if outcome == "open" else outcome,
        )
        row = shadow_row(
            _gate(f"g{i}", record=record, project=project, number=i, status=status)
        )
        assert row is not None
        rows.append(row)
    return rows


def test_the_report_counts_agreement_per_project_and_overall() -> None:
    rows = _rows(
        ("lens", "merged_at_head", True, "approval"),
        ("lens", "merged_at_head", True, "approval"),
        ("lens", "merged_after_pushes", True, "approval"),
        ("lens", "closed_unmerged", True, "approval"),
        ("lens", "gone", False, "approval"),
        ("lens", "merged_never_would", False, "no_approval_record"),
        ("lens", "open", True, "approval"),
        ("loom", "merged_never_would", False, "approval"),
    )

    lines = render_shadow_report(rows)
    text = "\n".join(lines)

    lens = next(line for line in lines if line.startswith("lens:"))
    assert "agreement 2/4 (50%" in lens
    assert "1 merged after pushes" in lens
    assert "1 would-merge closed" in lens
    assert "0 merged never-would" in lens
    assert "excluded 1" in lens and "unmeasured 1" in lens and "open 1" in lens
    loom = next(line for line in lines if line.startswith("loom:"))
    assert "agreement 0/1" in loom
    overall = next(line for line in lines if line.startswith("overall:"))
    assert "agreement 2/5 (40%" in overall
    assert re.search(r"95% CI \d+%–\d+%", overall)
    assert "GATE" in lines[0] and "OUTCOME" in lines[0]
    assert text.count("\n") >= len(rows)


def test_the_report_with_nothing_resolved_says_so() -> None:
    lines = render_shadow_report(_rows(("lens", "open", True, "approval")))
    overall = next(line for line in lines if line.startswith("overall:"))
    assert "no measured resolved gate yet" in overall


def test_the_report_with_no_gates() -> None:
    assert render_shadow_report([]) == ["no pr gates in the window"]


# ── review findings ──────────────────────────────────────────────────────


async def test_a_naive_creation_time_is_read_as_utc_not_a_crash() -> None:
    gate = dataclasses.replace(
        _gate("g1", delivered_at=""),
        created_at=datetime(2026, 10, 2, 8),  # naive
    )
    client = FakeLithosClient(agent_id="a")
    client.add_task(gate)
    rows = await collect_shadow_rows(client, since=datetime(2026, 10, 1, tzinfo=UTC))
    assert [r.delivered for r in rows] == [datetime(2026, 10, 2, 8, tzinfo=UTC)]


def test_a_naive_delivery_time_is_read_as_utc() -> None:
    row = shadow_row(_gate("g1", delivered_at="2026-10-05T09:00:00"))
    assert row is not None
    assert row.delivered == datetime(2026, 10, 5, 9, tzinfo=UTC)


def test_an_unknown_outcome_is_not_counted_as_agreement() -> None:
    gate = _gate("g1", record=_record(outcome="bogus"), status="completed")
    row = shadow_row(gate)
    assert row is not None
    assert row.outcome is None
    assert row.verdict == VERDICT_UNMEASURED


@pytest.mark.parametrize("bad", ["99999999999d", "٣d"])
def test_parse_since_rejects_overflow_and_non_ascii_digits(bad: str) -> None:
    with pytest.raises(ValueError, match="--since"):
        parse_since(bad, now=datetime(2026, 10, 10, tzinfo=UTC))


def test_closed_after_an_invalidated_would_merge_is_still_a_disagreement() -> None:
    """The verdict is keyed on the FIRST would-merge moment: under auto-merge
    loom would already have merged then, whatever was invalidated later."""
    record = _record(outcome="closed_unmerged", invalidated="human_push")
    row = shadow_row(_gate("g1", record=record, status="completed"))
    assert row is not None and row.verdict == VERDICT_DISAGREE


def test_a_malformed_basis_reads_the_same_in_the_column_and_the_report() -> None:
    record = _record()
    record["basis"] = "junk"
    gate = _gate("g1", record=record)
    row = shadow_row(gate)
    assert row is not None and row.basis == "approval"
    assert shadow_label(gate) == "would"


def test_a_short_approval_head_is_no_approval() -> None:
    gate = _gate("g1", record={"basis": "junk"})
    meta = dict(gate.metadata)
    meta["delivered_approval"] = {**meta["delivered_approval"], "head_sha": "abc123"}
    row = shadow_row(dataclasses.replace(gate, metadata=meta))
    assert row is not None and row.basis == "no_approval_record"


def test_a_boolean_elapsed_is_no_elapsed() -> None:
    record = _record(outcome="merged_at_head")
    record["elapsed_s"] = True
    row = shadow_row(_gate("g1", record=record, status="completed"))
    assert row is not None and row.elapsed_s is None


def test_merged_after_pushes_is_split_by_whether_a_human_pushed() -> None:
    rows = [
        shadow_row(
            _gate(
                f"g{i}",
                record=_record(outcome="merged_after_pushes", pushes=pushes),
                number=i,
                status="completed",
            )
        )
        for i, pushes in enumerate(((1, 0), (0, 1), (2, 1)))
    ]
    overall = next(
        line
        for line in render_shadow_report([r for r in rows if r is not None])
        if line.startswith("overall:")
    )
    assert "3 merged after pushes (2 with a human push)" in overall


async def test_a_gate_delivered_exactly_at_the_window_start_is_in_it() -> None:
    client = FakeLithosClient(agent_id="a")
    client.add_task(_gate("g1", delivered_at="2026-10-01T00:00:00+00:00"))
    rows = await collect_shadow_rows(client, since=datetime(2026, 10, 1, tzinfo=UTC))
    assert [r.gate_id for r in rows] == ["g1"]
