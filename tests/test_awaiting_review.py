"""Tests for the obsidian-awaiting-review handler (#113).

Drives the handler directly with synthetic Events against a tmp_path vault —
rendering, add/remove lifecycle, and the content-hash + atomic-write invariants.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from lithos_loom.bus import Event
from lithos_loom.config import LoomConfig, ObsidianSyncConfig, OrchestratorConfig
from lithos_loom.subscriptions import _awaiting_review
from lithos_loom.subscriptions._awaiting_review import make_handler
from tests.support import FakeLithosClient, make_task

_NOTE = Path("_lithos/awaiting-review.md")
_DELIVERED = {
    # US11: a delivered task is identified by its develop_pr_url alone (open +
    # PR url); the loom_delivered marker is retired.
    "develop_pr_url": "https://github.com/agent-lore/lithos/pull/363",
    "project": "lithos-core",
}


@dataclass
class _StubCtx:
    logger: logging.Logger


def _ctx() -> _StubCtx:
    return _StubCtx(logger=logging.getLogger("test.awaiting_review"))


def _cfg(tmp_path: Path) -> LoomConfig:
    return LoomConfig(
        orchestrator=OrchestratorConfig(
            agent_id="lithos-orchestrator-test",
            lithos_url="http://localhost:8765",
        ),
        obsidian_sync=ObsidianSyncConfig(vault_path=tmp_path),
    )


def _event(
    event_type: str,
    *,
    task_id: str,
    title: str = "test task",
    status: str = "open",
    metadata: Mapping[str, Any] | None = None,
) -> Event:
    return Event(
        type=event_type,
        timestamp=datetime.now(UTC),
        payload={
            "id": task_id,
            "title": title,
            "status": status,
            "tags": [],
            "metadata": dict(metadata or {}),
            "claims": [],
        },
    )


def _note(tmp_path: Path) -> str:
    return (tmp_path / _NOTE).read_text()


async def test_delivered_task_is_listed(tmp_path: Path) -> None:
    handler, _ = make_handler(_cfg(tmp_path))
    await handler(
        _event(
            "lithos.task.created",
            task_id="t1",
            title="Add lithos_note_update",
            metadata=_DELIVERED,
        ),
        _ctx(),
    )
    content = _note(tmp_path)
    assert "# PRs awaiting review" in content
    assert "**Add lithos_note_update**" in content
    assert "[PR #363](https://github.com/agent-lore/lithos/pull/363)" in content
    assert "#project/lithos-core" in content


async def test_completed_task_is_removed(tmp_path: Path) -> None:
    handler, _ = make_handler(_cfg(tmp_path))
    await handler(
        _event("lithos.task.created", task_id="t1", metadata=_DELIVERED), _ctx()
    )
    assert "PR #363" in _note(tmp_path)
    await handler(
        _event("lithos.task.completed", task_id="t1", metadata=_DELIVERED), _ctx()
    )
    content = _note(tmp_path)
    assert "PR #363" not in content
    assert "_No PRs awaiting review._" in content


async def test_non_delivered_task_absent(tmp_path: Path) -> None:
    handler, _ = make_handler(_cfg(tmp_path))
    await handler(
        _event("lithos.task.created", task_id="t1", title="plain task"), _ctx()
    )
    assert "_No PRs awaiting review._" in _note(tmp_path)
    assert "plain task" not in _note(tmp_path)


async def test_open_task_without_pr_url_absent(tmp_path: Path) -> None:
    """An open task with no develop_pr_url is not awaiting review (US11: the PR
    url alone is the marker, so its absence is the sole exclusion)."""
    handler, _ = make_handler(_cfg(tmp_path))
    await handler(
        _event(
            "lithos.task.created",
            task_id="t1",
            metadata={"project": "lithos-core"},  # no develop_pr_url
        ),
        _ctx(),
    )
    assert "_No PRs awaiting review._" in _note(tmp_path)


async def test_unchanged_set_does_not_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writes: list[Path] = []
    real = _awaiting_review.write_file_atomic

    async def counting(path: Path, content: str) -> None:
        writes.append(path)
        await real(path, content)

    monkeypatch.setattr(_awaiting_review, "write_file_atomic", counting)
    handler, _ = make_handler(_cfg(tmp_path))
    await handler(
        _event("lithos.task.created", task_id="t1", metadata=_DELIVERED), _ctx()
    )
    assert len(writes) == 1
    # A later event that leaves the delivered set unchanged writes nothing.
    await handler(_event("lithos.task.created", task_id="other", title="plain"), _ctx())
    assert len(writes) == 1


async def test_atomic_write_leaves_no_temp(tmp_path: Path) -> None:
    handler, _ = make_handler(_cfg(tmp_path))
    await handler(
        _event("lithos.task.created", task_id="t1", metadata=_DELIVERED), _ctx()
    )
    assert list((tmp_path / "_lithos").glob(".*.tmp*")) == []


# --- cold-start reconcile (#113 review) ----------------------------------------


async def test_reconcile_cold_start_collapses_stale_note(tmp_path: Path) -> None:
    # A note written by a previous run; on restart there are zero open tasks and
    # the delivered task resolved outside the replay window → no events fire.
    note = tmp_path / _NOTE
    note.parent.mkdir(parents=True)
    note.write_text("# PRs awaiting review\n\n- **Old** — [PR #1](https://x/pull/1)\n")
    _, reconcile = make_handler(_cfg(tmp_path))
    fake = FakeLithosClient()  # zero open tasks
    await reconcile(fake)
    assert fake.calls_to("task_list")[0]["status"] == "open"
    content = note.read_text()
    assert "Old" not in content
    assert "_No PRs awaiting review._" in content


async def test_reconcile_lists_open_delivered(tmp_path: Path) -> None:
    _, reconcile = make_handler(_cfg(tmp_path))
    fake = FakeLithosClient(
        tasks=(
            make_task(
                "t1", title="Add note_update", status="open", metadata=dict(_DELIVERED)
            ),
            make_task("t2", title="plain", status="open", metadata={}),
        )
    )
    await reconcile(fake)
    assert fake.calls_to("task_list")[0]["status"] == "open"
    content = _note(tmp_path)
    assert "**Add note_update**" in content
    assert "plain" not in content
