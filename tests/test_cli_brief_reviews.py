"""``lithos-loom brief-reviews`` — the brief-review record, read back.

Phase 2 (10cc6310) records a structural ``check`` on every drafted
``brief_review`` entry and, at approval, what the operator did to each item.
This is the read side: entries by outcome, approval edits by kind, the
entries that skipped their gate under the dial, and the check's false-hold
rate. Hand-built records on a ``FakeLithosClient``; no live Lithos.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from lithos_loom import main as main_module
from lithos_loom.cli.brief_reviews import (
    brief_reviews_output,
    collect_brief_reviews,
    false_holds,
    render_brief_reviews,
)
from lithos_loom.errors import LithosClientError
from lithos_loom.lithos_client import Task
from lithos_loom.main import app
from tests.support import FakeLithosClient, make_task

runner = CliRunner()

_DRAFTED = "2026-10-10T09:00:00+00:00"
_CHECK_PASSED = {"facts_only": True, "passed": True, "problems": []}
_CHECK_HELD = {
    "facts_only": True,
    "passed": False,
    "problems": ["F2: no Basis line"],
}

_KINDS = {"S": "scope_cut", "F": "fact", "D": "decision"}


def _entry(
    run_id: str,
    *,
    outcome: str = "approved",
    mode: str = "full",
    items: tuple[str, ...] = ("F1",),
    outcomes: dict[str, str] | None = None,
    check: dict[str, Any] | None = None,
    drafted_at: str = _DRAFTED,
    **extra: Any,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "run_id": run_id,
        "mode": mode,
        "base_sha": "a" * 40,
        "drafted_at": drafted_at,
        "outcome": outcome,
        "items": [{"id": i, "kind": _KINDS[i[0]], "sha": "0" * 16} for i in items],
    }
    if outcomes is not None:
        entry["outcomes"] = outcomes
    if check is not None:
        entry["check"] = check
    entry.update(extra)
    return entry


def _story(
    story_id: str,
    *entries: dict[str, Any],
    project: str | None = "k2",
    status: str = "completed",
) -> Task:
    metadata: dict[str, Any] = {"brief_review": list(entries)}
    if project is not None:
        metadata["project"] = project
    return make_task(story_id, status=status, metadata=metadata)


def _k2_stories() -> list[Task]:
    """Ten approved full-mode entries, as the K2 slices left them: no fact
    edited or cut — one decision edited, one scope cut cut, one fact added."""
    stories = []
    for n in range(10):
        outcomes = {"S1": "unchanged", "F1": "unchanged", "D1": "unchanged"}
        if n == 0:
            outcomes["D1"] = "edited"
        if n == 1:
            outcomes["S1"] = "cut"
        if n == 2:
            outcomes["F2"] = "added"
        stories.append(
            _story(
                f"k2-{n:02d}",
                _entry(
                    f"run-{n}",
                    items=("S1", "F1", "D1"),
                    outcomes=outcomes,
                    approved_at="2026-10-11T09:00:00+00:00",
                    gate_id=f"gate-{n}",
                ),
            )
        )
    return stories


async def _rows(*stories: Task, **kwargs: Any) -> list[dict[str, Any]]:
    return await collect_brief_reviews(
        FakeLithosClient(agent_id="a", tasks=stories), **kwargs
    )


def _line(lines: list[str], prefix: str) -> str:
    return next(line for line in lines if line.startswith(prefix))


# ── the report ──────────────────────────────────────────────────────────


async def test_the_k2_records_read_ten_approved_and_no_fact_edited_or_cut() -> None:
    lines = render_brief_reviews(await _rows(*_k2_stories()))

    assert lines[0] == "10 brief-review entries on 10 stories"
    header = _line(lines, "PROJECT").split()
    row = _line(lines, "k2").split()
    assert dict(zip(header, row, strict=True)) == {
        "PROJECT": "k2",
        "MODE": "full",
        "PENDING": "0",
        "APPROVED": "10",
        "AUTO_APPENDED": "0",
        "NO_CHANGE": "0",
        "FAILED": "0",
        "OTHER": "—",
        "TOTAL": "10",
    }
    assert "approval edits (10 approved entries)" in lines
    assert _line(lines, "S ").split() == ["S", "9", "0", "1", "0"]
    assert _line(lines, "F ").split() == ["F", "10", "0", "0", "1"]
    assert _line(lines, "D ").split() == ["D", "9", "1", "0", "0"]
    assert "facts edited or cut at a gate: 0" in lines


async def test_a_fact_edited_or_cut_at_a_gate_is_counted() -> None:
    rows = await _rows(
        _story(
            "s1",
            _entry(
                "r1",
                items=("F1", "F2", "F3"),
                outcomes={"F1": "edited", "F2": "cut", "F3": "unchanged"},
            ),
        )
    )
    assert "facts edited or cut at a gate: 2" in render_brief_reviews(rows)


async def test_an_added_item_takes_its_kind_from_its_id_letter() -> None:
    rows = await _rows(
        _story(
            "s1",
            _entry("r1", items=("F1",), outcomes={"F1": "unchanged", "D4": "added"}),
        )
    )
    lines = render_brief_reviews(rows)
    assert _line(lines, "D ").split() == ["D", "0", "0", "0", "1"]
    assert _line(lines, "F ").split() == ["F", "1", "0", "0", "0"]


async def test_an_auto_appended_full_entry_is_listed_as_auto_applied() -> None:
    rows = await _rows(
        _story(
            "s1",
            _entry("r-auto", outcome="auto_appended", check=_CHECK_PASSED),
            _entry("r-quiet", outcome="no_change", items=(), check=_CHECK_PASSED),
        ),
        _story(
            "s2",
            _entry("r-delta", outcome="auto_appended", mode="delta", check=_CHECK_HELD),
        ),
    )
    lines = render_brief_reviews(rows)

    head = _line(lines, "auto-applied")
    assert head.startswith("auto-applied (full mode, dial on): 2")
    assert "delta rechecks that skipped their gate: 1" in head
    section = lines[lines.index(head) : lines.index(head) + 4]
    auto = [line for line in section if line.startswith("s1 ")]
    assert len(auto) == 2
    assert "r-auto" in auto[0] and "auto_appended" in auto[0]
    assert "facts-only, passed" in auto[0]
    assert "r-quiet" in auto[1] and "no_change" in auto[1]
    # the delta recheck is in the outcome table, not the dial-governed list
    assert not any("r-delta" in line for line in lines)
    delta = next(line.split() for line in lines if line.split()[:2] == ["k2", "delta"])
    assert delta[2:5] == ["0", "0", "1"]  # pending, approved, auto_appended


async def test_a_held_facts_only_entry_approved_unchanged_is_a_false_hold() -> None:
    rows = await _rows(
        _story(
            "s1",
            _entry(
                "r-false",
                items=("F1", "F2"),
                check=_CHECK_HELD,
                outcomes={"F1": "unchanged", "F2": "unchanged"},
            ),
        ),
        _story(
            "s2",
            _entry(
                "r-right",
                items=("F1",),
                check={**_CHECK_HELD, "problems": ["F1: names no file"]},
                outcomes={"F1": "edited"},
            ),
        ),
    )
    lines = render_brief_reviews(rows)

    head = _line(lines, "check false holds")
    assert head == (
        "check false holds: 1/2 held facts-only drafts approved all-unchanged (50%)"
    )
    assert lines[lines.index(head) + 1] == "  s1 r-false — problems: F2: no Basis line"
    assert not any("names no file" in line for line in lines if "r-right" in line)

    held, false = false_holds(rows)
    assert [r["run_id"] for r in held] == ["r-false", "r-right"]
    assert [r["run_id"] for r in false] == ["r-false"]


async def test_a_pending_held_entry_awaits_approval_and_counts_in_neither() -> None:
    rows = await _rows(
        _story(
            "s1",
            _entry("r-wait", outcome="pending", check=_CHECK_HELD),
            status="open",
        )
    )
    lines = render_brief_reviews(rows)

    assert _line(lines, "check false holds") == (
        "check false holds: no held facts-only draft approved yet"
    )
    assert "awaiting approval: 1 held facts-only drafts" in lines
    assert "  s1 r-wait — problems: F2: no Basis line" in lines
    assert false_holds(rows) == ([], [])


async def test_an_entry_with_no_check_is_outside_the_false_hold_count() -> None:
    """Pre-phase-2 and failed entries carry no ``check``: never read as
    facts_only false, and in neither side of the rate."""
    rows = await _rows(
        _story(
            "s1",
            _entry("r-old", outcomes={"F1": "unchanged"}),
            _entry("r-fail", outcome="failed", items=(), note="no draft"),
        )
    )
    lines = render_brief_reviews(rows)

    assert false_holds(rows) == ([], [])
    assert _line(lines, "no check:").startswith("no check: 2 entries")
    assert _line(lines, "check false holds").endswith(
        "no held facts-only draft approved yet"
    )


async def test_an_approved_entry_with_no_outcomes_is_not_a_false_hold() -> None:
    rows = await _rows(_story("s1", _entry("r1", check=_CHECK_HELD)))
    held, false = false_holds(rows)
    assert len(held) == 1 and false == []


async def test_an_added_item_means_the_operator_did_not_agree() -> None:
    rows = await _rows(
        _story(
            "s1",
            _entry(
                "r1",
                check=_CHECK_HELD,
                outcomes={"F1": "unchanged", "F2": "added"},
            ),
        )
    )
    held, false = false_holds(rows)
    assert len(held) == 1 and false == []


def test_no_entries_says_so() -> None:
    assert render_brief_reviews([]) == ["no brief-review entries in the window"]


async def test_an_unknown_outcome_is_counted_as_other() -> None:
    lines = render_brief_reviews(await _rows(_story("s1", _entry("r1", outcome="?"))))
    row = _line(lines, "k2").split()
    assert row[-2:] == ["1", "1"]


# ── selection ───────────────────────────────────────────────────────────


async def test_collect_lists_the_projects_stories_in_every_status() -> None:
    client = FakeLithosClient(
        agent_id="a",
        tasks=[
            _story("s1", _entry("r1"), status="completed"),
            _story("s2", _entry("r2"), project="other"),
            _story("s3", _entry("r3"), status="cancelled"),
            make_task("g1", task_type="gate", metadata={"project": "k2"}),
        ],
    )
    rows = await collect_brief_reviews(client, project="k2")

    assert [r["story_id"] for r in rows] == ["s1", "s3"]
    assert client.calls_to("task_list") == [
        {
            "status": None,
            "with_claims": False,
            "resolved_since": None,
            "tags": None,
            "metadata_match": {"project": "k2"},
            "task_type": "task",
        }
    ]
    assert client.mutating_calls == []


async def test_without_project_every_story_is_read_projectless_as_dash() -> None:
    rows = await _rows(
        _story("s2", _entry("r2"), project="other"),
        _story("s1", _entry("r1"), project=None),
        make_task("plain"),  # no record: no rows
    )
    assert [(r["story_id"], r["project"]) for r in rows] == [
        ("s1", "—"),
        ("s2", "other"),
    ]


async def test_since_keeps_entries_by_drafted_at_inclusive() -> None:
    since = datetime(2026, 10, 10, 9, tzinfo=UTC)
    rows = await _rows(
        _story(
            "s1",
            _entry("old", drafted_at="2026-10-09T09:00:00+00:00"),
            _entry("edge", drafted_at=_DRAFTED),
            _entry("naive", drafted_at="2026-10-12T09:00:00"),
            _entry("junk", drafted_at="yesterday"),
        ),
        since=since,
    )
    assert [r["run_id"] for r in rows] == ["edge", "naive"]


async def test_without_since_an_unreadable_drafted_at_is_kept() -> None:
    rows = await _rows(_story("s1", _entry("junk", drafted_at="")))
    assert [r["run_id"] for r in rows] == ["junk"]


# ── --json ──────────────────────────────────────────────────────────────


async def test_json_emits_one_row_per_entry_with_every_key() -> None:
    approved = _entry(
        "r1",
        check=_CHECK_PASSED,
        outcomes={"F1": "unchanged"},
        approved_at="2026-10-11T09:00:00+00:00",
        gate_id="gate-1",
        operator_note="ok",
        superseded_gates=["gate-0"],
    )
    failed = _entry("r0", outcome="failed", items=(), note="timed out")
    rows = await _rows(_story("s2", failed, approved), _story("s1", _entry("r9")))

    (text,) = brief_reviews_output(rows, as_json=True)
    parsed = json.loads(text)

    assert [(r["story_id"], r["run_id"]) for r in parsed] == [
        ("s1", "r9"),
        ("s2", "r0"),
        ("s2", "r1"),
    ]
    assert parsed[2] == {**approved, "story_id": "s2", "project": "k2"}
    assert parsed[1]["note"] == "timed out"
    assert "cost_usd" not in parsed[2]


# ── CLI ─────────────────────────────────────────────────────────────────


def _patch_client(monkeypatch: pytest.MonkeyPatch, fake: FakeLithosClient) -> None:
    monkeypatch.setattr(main_module, "LithosClient", lambda *a, **k: fake)


def test_cli_prints_the_report(
    loom_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeLithosClient(tasks=_k2_stories())
    _patch_client(monkeypatch, fake)

    result = runner.invoke(app, ["brief-reviews"])

    assert result.exit_code == 0, result.output
    assert "10 brief-review entries on 10 stories" in result.output
    assert "facts edited or cut at a gate: 0" in result.output
    assert fake.mutating_calls == []


def test_cli_json(loom_config_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, FakeLithosClient(tasks=_k2_stories()))

    result = runner.invoke(app, ["brief-reviews", "--json"])

    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert len(rows) == 10
    assert {r["outcome"] for r in rows} == {"approved"}


def test_cli_project(loom_config_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeLithosClient(
        tasks=[_story("s1", _entry("r1")), _story("s2", _entry("r2"), project="x")]
    )
    _patch_client(monkeypatch, fake)

    result = runner.invoke(app, ["brief-reviews", "--project", "x"])

    assert result.exit_code == 0, result.output
    assert "1 brief-review entries on 1 stories" in result.output
    assert fake.calls_to("task_list")[0]["metadata_match"] == {"project": "x"}


def test_cli_since(loom_config_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeLithosClient(
        tasks=[_story("s1", _entry("r1", drafted_at="2000-01-01T00:00:00+00:00"))]
    )
    _patch_client(monkeypatch, fake)

    result = runner.invoke(app, ["brief-reviews", "--since", "2026-10-01"])

    assert result.exit_code == 0, result.output
    assert "no brief-review entries in the window" in result.output


def test_cli_rejects_a_bad_since_before_any_io(
    loom_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeLithosClient()
    _patch_client(monkeypatch, fake)

    result = runner.invoke(app, ["brief-reviews", "--since", "last week"])

    assert result.exit_code == 2
    assert "--since" in result.output
    assert fake.calls == []


def test_cli_reports_lithos_unreachable(
    loom_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_client(
        monkeypatch, FakeLithosClient(fail_connect=OSError("connection refused"))
    )

    result = runner.invoke(app, ["brief-reviews"])

    assert result.exit_code == 1
    assert "could not reach Lithos" in result.output


def test_cli_reports_a_lithos_error(
    loom_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeLithosClient()
    fake.raise_on["task_list"] = LithosClientError("internal_error", "boom")
    _patch_client(monkeypatch, fake)

    result = runner.invoke(app, ["brief-reviews"])

    assert result.exit_code == 1
    assert "reading brief reviews failed" in result.output
