"""Budget policy exercised through lifecycle events, without dispatcher internals."""

from __future__ import annotations

import ast
import logging
from pathlib import Path

from lithos_loom.gates import create_pr_gate, parse_pr_gate
from lithos_loom.runner.orphans import ProcessIdentity
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions.remediation_budget import (
    PENDING_KEY,
    RemediationSettings,
    read_budget,
)
from lithos_loom.subscriptions.remediation_lifecycle import RemediationLifecycle
from tests.support import FakeLithosClient


async def test_refund_allowance_survives_shutdown_and_renews_on_human_push(
    tmp_path: Path,
) -> None:
    """Reserve → no-change → shutdown → repeat → human push, via one seam."""
    client = FakeLithosClient()
    ctx = SubscriptionContext(
        lithos=client, logger=logging.getLogger(__name__), agent_id="a"
    )
    url = "https://github.com/agent-lore/lithos-loom/pull/12"
    story = await client.task_create(title="Story", agent="a")
    gate_id = await create_pr_gate(
        client, story_id=story, story_title="Story", pr_url=url, project="p", agent="a"
    )
    settings = RemediationSettings(trusted_bots=(), budget=2, work_dir=tmp_path)
    identity = ProcessIdentity(pid=123, start_ticks=456, host_boot="host-boot")

    async def current() -> RemediationLifecycle:
        gate = await client.task_get(task_id=gate_id)
        assert gate is not None
        spec = parse_pr_gate(gate)
        assert spec is not None
        return RemediationLifecycle(
            ctx,
            gate_id=gate_id,
            spec=spec,
            snapshot=read_budget(gate, url),
            settings=settings,
            story_id=story,
        )

    first = await current()
    await first.observe_head("initial")
    await client.task_update(task_id=gate_id, metadata={PENDING_KEY: {"pr_url": url}})
    assert await first.reserve(boot_id="watcher-boot", identity=identity)
    gate = await client.task_get(task_id=gate_id)
    assert gate is not None and not gate.metadata.get(PENDING_KEY)
    assert read_budget(gate, url).rounds_used == 1
    await first.finished(
        data={"status": "already_clean", "succeeded": True},
        returncode=0,
        output="",
        repo=tmp_path,
    )
    assert await first.lost_run() is None  # no double refund at shutdown
    second = await current()
    assert second.snapshot.rounds_used == 0
    assert second.snapshot.no_change_refunded
    assert not second.snapshot.in_flight_boot_id

    assert await second.reserve(boot_id="watcher-boot", identity=identity)
    await second.finished(
        data={"status": "already_clean", "succeeded": True},
        returncode=0,
        output="",
        repo=tmp_path,
    )
    third = await current()
    assert third.snapshot.rounds_used == 1  # the second no-change run is spent
    assert third.snapshot.no_change_refunded
    await third.observe_head("human-push")
    assert third.snapshot.rounds_used == 0
    assert not third.snapshot.no_change_refunded
    assert await third.reserve(boot_id="watcher-boot", identity=identity)
    await third.finished(
        data={"status": "already_clean", "succeeded": True},
        returncode=0,
        output="",
        repo=tmp_path,
    )
    assert (await current()).snapshot.rounds_used == 0


def test_only_lifecycle_implementation_constructs_budget_updates() -> None:
    """Keep snapshot-writing knowledge from leaking back into callers."""
    root = Path(__file__).resolve().parents[1] / "src" / "lithos_loom"
    writers: list[str] = []
    for path in root.rglob("*.py"):
        if "remediation_lifecycle" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Dict):
                continue
            if any(
                isinstance(key, ast.Name)
                and key.id == "REMEDIATION_KEY"
                or isinstance(key, ast.Constant)
                and key.value == "external_remediation"
                for key in node.keys
            ):
                writers.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not writers, "Budget updates must go through lifecycle events: " + ", ".join(
        writers
    )


async def test_a_round_records_when_it_was_reserved_and_whether_its_panel_approved(
    tmp_path: Path,
) -> None:
    """Review-convergence M1 reads both: a round answers an open review only
    when it was reserved after it, and re-approves only when its OWN loop ran
    and the panel approved — triage's round-0 `already_clean` ran neither."""
    client = FakeLithosClient()
    ctx = SubscriptionContext(
        lithos=client, logger=logging.getLogger(__name__), agent_id="a"
    )
    url = "https://github.com/agent-lore/lithos-loom/pull/12"
    story = await client.task_create(title="Story", agent="a")
    gate_id = await create_pr_gate(
        client, story_id=story, story_title="Story", pr_url=url, project="p", agent="a"
    )
    settings = RemediationSettings(trusted_bots=(), budget=5, work_dir=tmp_path)
    identity = ProcessIdentity(pid=123, start_ticks=456, host_boot="host-boot")

    async def current() -> RemediationLifecycle:
        gate = await client.task_get(task_id=gate_id)
        assert gate is not None
        spec = parse_pr_gate(gate)
        assert spec is not None
        return RemediationLifecycle(
            ctx,
            gate_id=gate_id,
            spec=spec,
            snapshot=read_budget(gate, url),
            settings=settings,
            story_id=story,
        )

    for data, approved in (
        (
            {
                "status": "already_clean",
                "succeeded": True,
                "rounds": 1,
                "develop_status": "approved",
            },
            True,
        ),
        (
            {
                "status": "already_clean",
                "succeeded": True,
                "rounds": 0,
                "develop_status": None,
            },
            False,
        ),
    ):
        run = await current()
        assert await run.reserve(boot_id="watcher-boot", identity=identity)
        reserved = (await current()).snapshot
        assert reserved.last_reserved_at
        assert reserved.last_panel_approved is False  # reserving clears it
        await run.finished(data=data, returncode=0, output="", repo=tmp_path)
        done = (await current()).snapshot
        assert done.last_panel_approved is approved, data
        assert done.last_reserved_at == reserved.last_reserved_at
