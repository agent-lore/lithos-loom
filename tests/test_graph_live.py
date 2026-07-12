"""Live-Lithos validation of the graph readiness model (Epic G US3).

The hermetic contract for the ready-queue is the ``FakeLithosClient`` model in
``tests/support/fake_lithos.py`` (pinned by ``tests/test_fake_lithos.py``). This
module checks that model against a *real* Lithos: it exercises the same
``blocks``-chain readiness, the ``task_complete`` unblocked set (US6), and the
**cancelled-blocker precondition** (a cancelled predecessor must keep its
dependent blocked, surfaced as ``blocker_unsatisfiable``).

Host/CI only — skipped unless ``LITHOS_URL`` is set. The URL is captured at
import time because the ``clean_loom_env`` autouse fixture clears ``LITHOS_*``
per test, so re-reading ``os.environ`` inside the body would always miss.
"""

from __future__ import annotations

import contextlib
import os
import uuid

import pytest

from lithos_loom.lithos_client import LithosClient

# Captured at import so the autouse env-clear can't hide it from the body.
_LIVE_URL = os.environ.get("LITHOS_URL")
_AGENT = os.environ.get("LITHOS_AGENT_ID", "lithos-loom-graph-live-test")

pytestmark = pytest.mark.skipif(
    _LIVE_URL is None, reason="requires a live Lithos (set LITHOS_URL)"
)


def _project() -> str:
    """A unique per-test project so parallel/repeat runs never collide."""
    return f"loom-glive-{uuid.uuid4().hex[:8]}"


async def test_live_blocks_chain_ready_blocked_and_unblocked() -> None:
    assert _LIVE_URL is not None
    project = _project()
    to_clean: list[str] = []
    async with LithosClient(_LIVE_URL, agent_id=_AGENT) as client:
        try:
            blocker = await client.task_create(
                title="[glive] blocker", tags=[project], metadata={"project": project}
            )
            to_clean.append(blocker)
            dependent = await client.task_create(
                title="[glive] dependent",
                tags=[project],
                metadata={"project": project},
                depends_on=[blocker],
            )
            to_clean.append(dependent)

            ready_ids = {t.id for t in await client.task_ready(project=project)}
            assert blocker in ready_ids
            assert dependent not in ready_ids

            blocked = await client.task_blocked(project=project)
            assert [bt.task.id for bt in blocked] == [dependent]
            reason = blocked[0].blockers[0]
            assert reason.kind == "task"
            assert reason.task_id == blocker

            # US6: completing the blocker returns the newly-ready dependent.
            unblocked = await client.task_complete(task_id=blocker)
            assert dependent in unblocked
            assert dependent in {t.id for t in await client.task_ready(project=project)}
        finally:
            for tid in to_clean:
                with contextlib.suppress(Exception):
                    await client.task_cancel(task_id=tid, reason="glive cleanup")


async def test_live_cancelled_blocker_keeps_dependent_unsatisfiable() -> None:
    """The epic-G precondition: a cancelled predecessor must NOT release its
    dependent. Lithos owns this correctness once Loom trusts the frontier."""
    assert _LIVE_URL is not None
    project = _project()
    to_clean: list[str] = []
    async with LithosClient(_LIVE_URL, agent_id=_AGENT) as client:
        try:
            blocker = await client.task_create(
                title="[glive] blocker", tags=[project], metadata={"project": project}
            )
            to_clean.append(blocker)
            dependent = await client.task_create(
                title="[glive] dependent",
                tags=[project],
                metadata={"project": project},
                depends_on=[blocker],
            )
            to_clean.append(dependent)

            await client.task_cancel(task_id=blocker, reason="glive: unsatisfiable")

            assert dependent not in {
                t.id for t in await client.task_ready(project=project)
            }
            blocked = await client.task_blocked(project=project)
            assert [bt.task.id for bt in blocked] == [dependent]
            assert blocked[0].blockers[0].kind == "blocker_unsatisfiable"
        finally:
            for tid in to_clean:
                with contextlib.suppress(Exception):
                    await client.task_cancel(task_id=tid, reason="glive cleanup")
