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


async def test_task_update_metadata_none_value_deletes_the_key() -> None:
    # Lithos #290: a metadata key with value None deletes it; unmentioned keys
    # are preserved; an empty dict is a no-op for task_update.
    client = FakeLithosClient(tasks=(make_task("t1", metadata={"a": 1, "b": 2}),))
    await client.task_update(task_id="t1", metadata={"a": None, "c": 3})
    stored = await client.task_get(task_id="t1")
    assert stored is not None and stored.metadata == {"b": 2, "c": 3}
    await client.task_update(task_id="t1", metadata={})  # no-op
    stored = await client.task_get(task_id="t1")
    assert stored is not None and stored.metadata == {"b": 2, "c": 3}


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


async def test_note_write_update_preserves_omitted_fields() -> None:
    # Real note_write omits None args, so an update preserves existing
    # tags/path/status/metadata rather than resetting them to defaults.
    client = FakeLithosClient(
        notes=(
            make_note(
                "n1",
                path="projects/p/x.md",
                tags=("keep",),
                status="active",
                metadata={"a": 1},
            ),
        )
    )
    res = await client.note_write(title="T2", content="new body", id="n1")
    assert res.note is not None
    assert res.note.tags == ("keep",)
    assert res.note.path == "projects/p/x.md"
    assert res.note.status == "active"
    assert res.note.metadata == {"a": 1}
    assert res.note.body == "new body"  # content is always applied


async def test_note_write_update_metadata_semantics() -> None:
    client = FakeLithosClient(notes=(make_note("n1", metadata={"a": 1, "b": 2}),))
    # None value deletes a key; unmentioned keys preserved; non-null merges.
    res = await client.note_write(
        title="T", content="c", id="n1", metadata={"a": None, "c": 3}
    )
    assert res.note is not None and res.note.metadata == {"b": 2, "c": 3}
    # metadata=None preserves existing.
    res = await client.note_write(title="T", content="c", id="n1")
    assert res.note is not None and res.note.metadata == {"b": 2, "c": 3}
    # metadata={} clears all (unlike task_update, where {} is a no-op).
    res = await client.note_write(title="T", content="c", id="n1", metadata={})
    assert res.note is not None and res.note.metadata == {}


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


def test_fake_exposes_the_role_protocol_attributes_at_runtime() -> None:
    # A cheap smoke check only: @runtime_checkable isinstance verifies method
    # *presence*, not signatures / async-ness / keyword-only params / returns.
    # The authoritative signature-level conformance check is the pyright-verified
    # static assignments (`_fake_conforms` / `_real_conforms`) in
    # tests/support/fake_lithos.py — this just catches a grossly missing method.
    client = FakeLithosClient()
    assert isinstance(client, TaskClient)
    assert isinstance(client, NoteClient)
    assert isinstance(client, FindingClient)
    assert isinstance(client, LithosClientProtocol)


# ── graph / readiness model (Epic G US3) ────────────────────────────────────
#
# The fake owns an in-memory edge store + readiness/blocked model that mirrors
# Lithos's server-side ready-queue: a ``blocks`` predecessor blocks a task until
# it is *completed* (a *cancelled* predecessor keeps the task blocked as
# ``blocker_unsatisfiable`` — the epic-G precondition), and a gate blocks its
# waiter until resolved. This is the hermetic contract; test_graph_live.py
# validates it against a real Lithos.


async def _blocker_chain() -> tuple[FakeLithosClient, str, str]:
    """A fake holding one open blocker and one dependent linked by a
    ``blocks`` edge (created via ``task_create(depends_on=...)``). Returns
    the client + the two ids."""
    client = FakeLithosClient(agent_id="a1")
    blocker = await client.task_create(
        title="blocker", tags=["g"], metadata={"project": "p"}
    )
    dependent = await client.task_create(
        title="dependent", tags=["g"], metadata={"project": "p"}, depends_on=[blocker]
    )
    return client, blocker, dependent


async def test_fake_depends_on_creates_blocks_edge_and_blocks_dependent() -> None:
    client, blocker, dependent = await _blocker_chain()

    ready = await client.task_ready(project="p")
    blocked = await client.task_blocked(project="p")

    assert [t.id for t in ready] == [blocker]  # only the free head is ready
    assert [bt.task.id for bt in blocked] == [dependent]
    reasons = blocked[0].blockers
    assert [b.kind for b in reasons] == ["task"]
    assert reasons[0].task_id == blocker
    assert reasons[0].type == "blocks"


async def test_fake_completed_blocker_unblocks_and_reports_unblocked() -> None:
    client, blocker, dependent = await _blocker_chain()

    unblocked = await client.task_complete(task_id=blocker)

    assert unblocked == [dependent]
    assert [t.id for t in await client.task_ready(project="p")] == [dependent]
    assert await client.task_blocked(project="p") == []


async def test_fake_cancelled_blocker_keeps_dependent_unsatisfiable() -> None:
    """The epic-G precondition: a cancelled predecessor does NOT release its
    dependent — it stays blocked as ``blocker_unsatisfiable``."""
    client, blocker, dependent = await _blocker_chain()

    await client.task_cancel(task_id=blocker)

    assert await client.task_ready(project="p") == []
    blocked = await client.task_blocked(project="p")
    assert [bt.task.id for bt in blocked] == [dependent]
    reason = blocked[0].blockers[0]
    assert reason.kind == "blocker_unsatisfiable"
    assert reason.status == "cancelled"


async def test_fake_gate_blocks_waiter_until_resolved() -> None:
    client = FakeLithosClient(agent_id="a1")
    gate = await client.task_create(
        title="human gate", task_type="gate", metadata={"project": "p"}
    )
    waiter = await client.task_create(title="waits", metadata={"project": "p"})
    await client.task_edge_upsert(
        from_task_id=gate, to_task_id=waiter, type="waits_on_gate"
    )

    blocked = await client.task_blocked(project="p")
    assert [bt.task.id for bt in blocked] == [waiter]
    assert blocked[0].blockers[0].kind == "gate"
    # A gate/epic is itself never offered as ready work.
    assert gate not in [t.id for t in await client.task_ready(project="p")]

    unblocked = await client.task_complete(task_id=gate)
    assert waiter in unblocked
    assert waiter in [t.id for t in await client.task_ready(project="p")]


async def test_fake_ready_never_offers_gate_or_epic_tasks() -> None:
    client = FakeLithosClient(agent_id="a1")
    await client.task_create(title="epic", task_type="epic", metadata={"project": "p"})
    await client.task_create(title="gate", task_type="gate", metadata={"project": "p"})
    plain = await client.task_create(title="plain", metadata={"project": "p"})
    assert [t.id for t in await client.task_ready(project="p")] == [plain]


async def test_fake_task_ready_filters_by_tags_and_metadata() -> None:
    client = FakeLithosClient(agent_id="a1")
    a = await client.task_create(
        title="a", tags=["x"], metadata={"project": "p", "phase": "impl"}
    )
    await client.task_create(
        title="b", tags=["y"], metadata={"project": "p", "phase": "review"}
    )
    ready = await client.task_ready(
        project="p", tags=["x"], metadata_match={"phase": "impl"}
    )
    assert [t.id for t in ready] == [a]


async def test_fake_edge_upsert_is_idempotent_and_replaces_metadata() -> None:
    """The real tool is an *upsert*: a repeat on the same (from, to, type)
    updates the one edge (full metadata replace) instead of duplicating it.
    Verified against live Lithos: second call's metadata wins, one edge left."""
    client = FakeLithosClient(agent_id="a1")
    a = await client.task_create(title="a")
    b = await client.task_create(title="b")

    await client.task_edge_upsert(
        from_task_id=a, to_task_id=b, type="blocks", metadata={"round": 1, "keep": "x"}
    )
    await client.task_edge_upsert(
        from_task_id=a, to_task_id=b, type="blocks", metadata={"round": 2}
    )

    edges = await client.task_edge_list(task_id=b)
    assert len(edges) == 1  # not duplicated
    assert edges[0].metadata == {"round": 2}  # full replace, not merge/preserve
    # And task_blocked reports a single reason, not two.
    blocked = await client.task_blocked()
    assert len(blocked[0].blockers) == 1


async def test_fake_edge_upsert_rejects_self_edge() -> None:
    client = FakeLithosClient(agent_id="a1")
    t = await client.task_create(title="t")
    with pytest.raises(Exception):  # noqa: B017 — LithosClientError, kept loose for RED
        await client.task_edge_upsert(from_task_id=t, to_task_id=t, type="blocks")


async def test_fake_edge_upsert_rejects_missing_task() -> None:
    client = FakeLithosClient(agent_id="a1")
    t = await client.task_create(title="t")
    with pytest.raises(Exception):  # noqa: B017
        await client.task_edge_upsert(from_task_id=t, to_task_id="ghost", type="blocks")


async def test_fake_edge_upsert_rejects_blocks_cycle() -> None:
    client = FakeLithosClient(agent_id="a1")
    a = await client.task_create(title="a")
    b = await client.task_create(title="b", depends_on=[a])  # a blocks b
    with pytest.raises(Exception):  # noqa: B017 — b -> a would close the loop
        await client.task_edge_upsert(from_task_id=b, to_task_id=a, type="blocks")


async def test_fake_task_blocked_reports_cycle_kind() -> None:
    """A cycle can't be built through ``task_edge_upsert`` (it rejects), so a
    test injects one via ``add_edge`` to pin the cycle blocker kind."""
    client = FakeLithosClient(agent_id="a1")
    a = await client.task_create(title="a", metadata={"project": "p"})
    b = await client.task_create(title="b", metadata={"project": "p"})
    client.add_edge(from_task_id=a, to_task_id=b, type="blocks")
    client.add_edge(from_task_id=b, to_task_id=a, type="blocks")

    blocked = await client.task_blocked(project="p")
    kinds = {bt.task.id: bt.blockers[0].kind for bt in blocked}
    assert kinds == {a: "cycle", b: "cycle"}


async def test_fake_task_children_returns_parent_child() -> None:
    client = FakeLithosClient(agent_id="a1")
    parent = await client.task_create(title="epic", task_type="epic")
    child = await client.task_create(title="child", parent_task_id=parent)
    other = await client.task_create(title="unrelated")

    kids = await client.task_children(task_id=parent)
    kid_ids = [t.id for t in kids]
    assert child in kid_ids
    assert other not in kid_ids


async def test_fake_task_edge_list_reports_direction() -> None:
    client, blocker, dependent = await _blocker_chain()

    incoming = await client.task_edge_list(task_id=dependent)
    outgoing = await client.task_edge_list(task_id=blocker)

    assert [(e.type, e.direction) for e in incoming] == [("blocks", "incoming")]
    assert [(e.type, e.direction) for e in outgoing] == [("blocks", "outgoing")]


async def test_fake_task_edge_list_filters_by_type() -> None:
    client = FakeLithosClient(agent_id="a1")
    a = await client.task_create(title="a")
    b = await client.task_create(title="b", parent_task_id=a)  # parent_child edge
    await client.task_create(title="c", depends_on=[a])  # blocks edge from a

    only_blocks = await client.task_edge_list(task_id=a, types=["blocks"])
    assert [e.type for e in only_blocks] == ["blocks"]
    assert b  # silence unused


async def test_fake_task_spawn_creates_task_and_blocking_edge() -> None:
    client = FakeLithosClient(agent_id="a1")
    src = await client.task_create(title="src", metadata={"project": "p"})
    spawned = await client.task_spawn(
        source_task_id=src, title="follow-on", relation_type="blocks"
    )

    # The spawned task exists and is blocked by its source.
    assert (await client.task_get(task_id=spawned)) is not None
    blocked = await client.task_blocked(project="p")
    assert spawned in [bt.task.id for bt in blocked]


async def test_fake_graph_writes_are_recorded_as_mutating() -> None:
    client = FakeLithosClient(agent_id="a1")
    a = await client.task_create(title="a")
    b = await client.task_create(title="b")
    await client.task_edge_upsert(from_task_id=a, to_task_id=b, type="blocks")
    await client.task_spawn(source_task_id=a, title="s")
    # reads don't count; writes do
    await client.task_ready()
    await client.task_blocked()
    await client.task_edge_list(task_id=a)
    assert "task_edge_upsert" in client.mutating_calls
    assert "task_spawn" in client.mutating_calls
    assert "task_ready" not in client.mutating_calls
