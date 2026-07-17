"""Tests for ``lithos-loom gates`` (Epic H — read-only PR-gate listing).

Two layers, mirroring ``test_cli_project``:

1. Pure-function tests for :func:`classify_gate` and :func:`render_report` —
   no I/O, one per health class, locking the classification precedence.
2. CLI integration via ``CliRunner``, stubbing ``LithosClient`` in the
   ``lithos_loom.main`` namespace so no real HTTP round trip happens, and
   asserting the command is non-mutating.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from lithos_loom import main as main_module
from lithos_loom.cli.gates import (
    HEALTH_MALFORMED,
    HEALTH_OK,
    HEALTH_ORPHAN,
    HEALTH_WAITER_GONE,
    HEALTH_WAITER_RESOLVED,
    classify_gate,
    render_report,
)
from lithos_loom.errors import LithosClientError
from lithos_loom.main import app
from tests.support import FakeLithosClient, make_task

runner = CliRunner()

_PR_URL = "https://github.com/agent-lore/lithos-loom/pull/42"
_GATE_META = {
    "gate_type": "pr",
    "repo": "agent-lore/lithos-loom",
    "pr_number": 42,
    "pr_url": _PR_URL,
    "required_state": "merged",
}


def _gate(gate_id: str = "gate-1", *, metadata: dict | None = None):
    return make_task(
        gate_id,
        title="Awaiting merge: Wire the thing",
        task_type="gate",
        metadata=metadata if metadata is not None else dict(_GATE_META),
    )


def _story(story_id: str = "story-1", *, status: str = "open"):
    return make_task(story_id, title="Wire the thing", status=status)


# ── Pure: classify_gate ────────────────────────────────────────────────


def test_classify_ok_open_waiter_and_parseable() -> None:
    row = classify_gate(_gate(), "story-1", _story())
    assert row.health == HEALTH_OK
    assert row.repo == "agent-lore/lithos-loom"
    assert row.pr_number == 42
    assert row.pr_label == "agent-lore/lithos-loom#42"
    assert row.waiter_id == "story-1"
    assert row.waiter_status == "open"


def test_classify_orphan_when_no_waiter() -> None:
    """No ``waits_on_gate`` edge → the gate blocks nothing."""
    row = classify_gate(_gate(), None, None)
    assert row.health == HEALTH_ORPHAN
    assert row.waiter_id is None
    assert row.waiter_status is None


def test_classify_malformed_when_pr_metadata_unreadable() -> None:
    """Missing repo/pr_number/pr_url → parse_pr_gate returns None."""
    bad = _gate(metadata={"gate_type": "pr"})
    row = classify_gate(bad, "story-1", _story())
    assert row.health == HEALTH_MALFORMED
    assert row.pr_label == "—"
    assert row.repo is None


def test_classify_waiter_gone_when_edge_dangles() -> None:
    """The edge names a waiter that no longer exists (task_get → None)."""
    row = classify_gate(_gate(), "story-1", None)
    assert row.health == HEALTH_WAITER_GONE


def test_classify_waiter_resolved_when_story_terminal() -> None:
    """Waiter already completed while the gate is still open — anomalous."""
    row = classify_gate(_gate(), "story-1", _story(status="completed"))
    assert row.health == HEALTH_WAITER_RESOLVED
    assert row.waiter_status == "completed"


def test_classify_orphan_precedes_malformed() -> None:
    """A gate that is both orphan and malformed reads as orphan — with no
    waiter the unparseable PR strands no story."""
    bad = _gate(metadata={"gate_type": "pr"})
    assert classify_gate(bad, None, None).health == HEALTH_ORPHAN


# ── Pure: render_report ────────────────────────────────────────────────


def test_render_empty() -> None:
    assert render_report([]) == ["no open pr gates"]


def test_render_summary_counts_health() -> None:
    rows = [
        classify_gate(_gate("gate-1"), "story-1", _story("story-1")),
        classify_gate(_gate("gate-2"), None, None),
    ]
    lines = render_report(rows)
    text = "\n".join(lines)
    assert "GATE" in text and "HEALTH" in text
    assert "gate-1" in text and "gate-2" in text
    assert "2 open pr gates: 1 healthy, 1 needs attention" in text


def test_render_singular_summary() -> None:
    rows = [classify_gate(_gate("gate-1"), "story-1", _story("story-1"))]
    assert "1 open pr gate: 1 healthy, 0 need attention" in "\n".join(
        render_report(rows)
    )


# ── CLI integration ────────────────────────────────────────────────────


def _patch_client(monkeypatch: pytest.MonkeyPatch, fake: FakeLithosClient) -> None:
    monkeypatch.setattr(main_module, "LithosClient", lambda *a, **k: fake)


def test_gates_lists_open_pr_gate_with_waiter(
    loom_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeLithosClient(tasks=[_gate("gate-1"), _story("story-1")])
    fake.add_edge(from_task_id="gate-1", to_task_id="story-1", type="waits_on_gate")
    _patch_client(monkeypatch, fake)

    result = runner.invoke(app, ["gates"])

    assert result.exit_code == 0, result.output
    assert "gate-1" in result.output
    assert "agent-lore/lithos-loom#42" in result.output
    assert "story-1" in result.output
    assert HEALTH_OK in result.output
    assert "1 open pr gate" in result.output


def test_gates_ignores_non_gate_tasks(
    loom_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only ``pr`` gates are listed; plain open tasks are skipped."""
    fake = FakeLithosClient(tasks=[_story("just-a-task")])
    _patch_client(monkeypatch, fake)

    result = runner.invoke(app, ["gates"])

    assert result.exit_code == 0, result.output
    assert "no open pr gates" in result.output
    assert "just-a-task" not in result.output


def test_gates_flags_orphan_gate(
    loom_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gate with no waiter edge is surfaced as orphan (needs attention)."""
    fake = FakeLithosClient(tasks=[_gate("gate-1")])
    _patch_client(monkeypatch, fake)

    result = runner.invoke(app, ["gates"])

    assert result.exit_code == 0, result.output
    assert HEALTH_ORPHAN in result.output
    assert "1 healthy" not in result.output
    assert "0 healthy, 1 needs attention" in result.output


def test_gates_is_non_mutating(
    loom_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeLithosClient(tasks=[_gate("gate-1"), _story("story-1")])
    fake.add_edge(from_task_id="gate-1", to_task_id="story-1", type="waits_on_gate")
    _patch_client(monkeypatch, fake)

    result = runner.invoke(app, ["gates"])

    assert result.exit_code == 0, result.output
    assert fake.mutating_calls == []


def test_gates_reports_lithos_unreachable(
    loom_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeLithosClient(fail_connect=OSError("connection refused"))
    _patch_client(monkeypatch, fake)

    result = runner.invoke(app, ["gates"])

    assert result.exit_code == 1
    assert "could not reach Lithos" in result.output


def test_gates_reports_client_error(
    loom_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeLithosClient(tasks=[_gate("gate-1")])
    fake.raise_on["task_list"] = LithosClientError("internal_error", "boom")
    _patch_client(monkeypatch, fake)

    result = runner.invoke(app, ["gates"])

    assert result.exit_code == 1
    assert "listing gates failed" in result.output
