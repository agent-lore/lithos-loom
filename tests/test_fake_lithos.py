"""Tests for the shared in-memory LithosClient fake (ARCH-4).

The fake is test infrastructure many suites depend on, so its behaviour is
pinned directly: seeding, in-memory mutations, call recording, injected
failures, and structural conformance to the role Protocols.
"""

from __future__ import annotations

import pytest

from lithos_loom.lithos_client import (
    FindingClient,
    LithosClientProtocol,
    NoteClient,
    TaskClient,
)
from tests.support import FakeLithosClient, make_note, make_task
from tests.support.fake_lithos import RESOLVED_AT

# ── construction + context manager ─────────────────────────────────────────


async def test_is_a_drop_in_async_context_manager() -> None:
    client = FakeLithosClient("http://lithos", agent_id="a1")
    assert client.base_url == "http://lithos" and client.agent_id == "a1"
    async with client as entered:
        assert entered is client


async def test_fail_connect_raises_on_entry() -> None:
    client = FakeLithosClient(fail_connect=ConnectionError("down"))
    with pytest.raises(ConnectionError):
        async with client:
            pass


# ── task surface ────────────────────────────────────────────────────────────


async def test_task_list_filters_by_status_and_records() -> None:
    client = FakeLithosClient(
        tasks=(make_task("t1", status="open"), make_task("t2", status="completed"))
    )
    got = await client.task_list(status="open")
    assert [t.id for t in got] == ["t1"]
    assert client.calls_to("task_list") == [
        {"status": "open", "with_claims": False, "resolved_since": None}
    ]


async def test_task_get_and_status_return_stored_or_none() -> None:
    client = FakeLithosClient(tasks=(make_task("t1"),))
    assert (await client.task_get(task_id="t1")).id == "t1"  # type: ignore[union-attr]
    assert await client.task_get(task_id="missing") is None
    assert (await client.task_status(task_id="t1")).id == "t1"  # type: ignore[union-attr]


async def test_task_create_mints_stores_and_returns_id() -> None:
    client = FakeLithosClient()
    task_id = await client.task_create(title="new", tags=["x"], metadata={"k": "v"})
    stored = await client.task_get(task_id=task_id)
    assert stored is not None
    assert stored.title == "new" and stored.tags == ("x",)
    assert stored.metadata == {"k": "v"} and stored.status == "open"
    assert client.calls_to("task_create")[0]["title"] == "new"


async def test_task_update_merges_metadata_and_tags() -> None:
    client = FakeLithosClient(tasks=(make_task("t1", metadata={"a": 1}),))
    await client.task_update(task_id="t1", metadata={"b": 2}, tags=["z"])
    stored = await client.task_get(task_id="t1")
    assert stored is not None
    assert stored.metadata == {"a": 1, "b": 2} and stored.tags == ("z",)


async def test_task_update_missing_is_noop() -> None:
    client = FakeLithosClient()
    await client.task_update(task_id="missing", title="x")  # no raise
    assert await client.task_get(task_id="missing") is None


async def test_complete_and_cancel_transition_state_visible_to_status() -> None:
    client = FakeLithosClient(tasks=(make_task("t1"), make_task("t2")))
    await client.task_complete(task_id="t1")
    await client.task_cancel(task_id="t2", reason="dup")
    t1 = await client.task_status(task_id="t1")
    t2 = await client.task_status(task_id="t2")
    assert t1 is not None and t1.status == "completed" and t1.resolved_at == RESOLVED_AT
    assert t2 is not None and t2.status == "cancelled"


async def test_claim_renew_return_receipts_release_records() -> None:
    client = FakeLithosClient(tasks=(make_task("t1"),))
    r1 = await client.task_claim(task_id="t1", aspect="develop")
    r2 = await client.task_renew(task_id="t1", aspect="develop")
    await client.task_release(task_id="t1", aspect="develop")
    assert r1 and r2 and r1 != r2
    assert client.called("task_release")


# ── note surface ────────────────────────────────────────────────────────────


async def test_note_read_by_id_and_path() -> None:
    note = make_note("n1", path="projects/p/context.md")
    client = FakeLithosClient(notes=(note,))
    assert (await client.note_read(id="n1")).id == "n1"  # type: ignore[union-attr]
    by_path = await client.note_read(path="projects/p/context.md")
    assert by_path is not None and by_path.id == "n1"
    assert await client.note_read(id="nope") is None


async def test_note_write_creates_then_updates_with_version_bump() -> None:
    client = FakeLithosClient()
    created = await client.note_write(title="T", content="body", id="n1")
    assert created.status == "created" and created.note is not None
    assert created.note.version == 1
    updated = await client.note_write(title="T2", content="body2", id="n1")
    assert updated.status == "updated" and updated.note is not None
    assert updated.note.version == 2


async def test_note_list_filters_by_prefix_tags_metadata_and_limit() -> None:
    client = FakeLithosClient(
        notes=(
            make_note("n1", path="projects/a/x.md", tags=("t",), metadata={"m": 1}),
            make_note("n2", path="projects/b/y.md", tags=("t",), metadata={"m": 2}),
            make_note("n3", path="projects/a/z.md", tags=("other",)),
        )
    )
    by_prefix = await client.note_list(path_prefix="projects/a/")
    assert {n.id for n in by_prefix} == {"n1", "n3"}
    by_tag = await client.note_list(tags=["t"])
    assert {n.id for n in by_tag} == {"n1", "n2"}
    by_meta = await client.note_list(metadata_match={"m": 1})
    assert [n.id for n in by_meta] == ["n1"]
    assert len(await client.note_list(limit=1)) == 1


async def test_note_delete_returns_whether_it_existed() -> None:
    client = FakeLithosClient(notes=(make_note("n1"),))
    assert await client.note_delete(id="n1") is True
    assert await client.note_delete(id="n1") is False


# ── finding surface ──────────────────────────────────────────────────────────


async def test_finding_post_records_and_returns_id() -> None:
    client = FakeLithosClient()
    fid = await client.finding_post(task_id="t1", summary="looks off")
    assert fid is not None
    assert client.findings == [{"id": fid, "task_id": "t1", "summary": "looks off"}]


# ── call inspection + failure injection ──────────────────────────────────────


async def test_mutating_calls_tracks_only_state_changes() -> None:
    client = FakeLithosClient(tasks=(make_task("t1"),))
    await client.task_list()
    await client.task_get(task_id="t1")
    await client.task_complete(task_id="t1")
    # reads excluded, the completion included
    assert client.mutating_calls == ["task_complete"]


async def test_raise_on_injects_a_per_method_failure() -> None:
    client = FakeLithosClient(tasks=(make_task("t1"),))
    client.raise_on["finding_post"] = RuntimeError("lithos down")
    with pytest.raises(RuntimeError):
        await client.finding_post(task_id="t1", summary="s")
    # the attempt is still recorded before it raises
    assert client.called("finding_post")


# ── Protocol conformance ─────────────────────────────────────────────────────


def test_fake_satisfies_the_role_protocols() -> None:
    client = FakeLithosClient()
    assert isinstance(client, TaskClient)
    assert isinstance(client, NoteClient)
    assert isinstance(client, FindingClient)
    assert isinstance(client, LithosClientProtocol)
