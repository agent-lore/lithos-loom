"""Tests for the per-run owner marker (``story_develop.run_owner``).

The marker is what lets ``lithos-loom develop prune`` tell a run that is merely
slow — fetching, checking out, between agent turns, with no container up at all
— from one that was killed an hour ago. It is therefore read in a decision that
deletes a git worktree: absent means "fall back to the other signals", present
but unusable means "keep the dir", and only a positively-dead identity clears it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from lithos_loom.plugins.story_develop import run_owner
from lithos_loom.runner import orphans


def test_record_then_read_round_trips_this_process(tmp_path: Path) -> None:
    run_owner.record_owner(tmp_path)
    assert run_owner.owner_recorded(tmp_path) is True
    identity = run_owner.read_owner(tmp_path)
    assert identity is not None
    assert identity.pid == os.getpid()
    # and it is recognised as the live process it is
    assert orphans.identity_alive(identity) is True
    # the write is atomic: no temp file is left beside the marker
    assert [p.name for p in tmp_path.iterdir()] == [run_owner.OWNER_FILE]


def test_no_marker_is_absent_not_unreadable(tmp_path: Path) -> None:
    assert run_owner.owner_recorded(tmp_path) is False
    assert run_owner.read_owner(tmp_path) is None


def test_a_host_that_cannot_identify_itself_still_records_that(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # NOT best-effort (round-2 review, correctness/f-001): no identity (no
    # procfs, no `ps`, no boot id) still writes a marker — one that says so.
    # Recording nothing would read as "an old run dir" and hand the verdict to
    # the idle window, which deletes a run that is merely still fetching.
    monkeypatch.setattr(run_owner, "process_identity", lambda pid: None)
    run_owner.record_owner(tmp_path)
    assert run_owner.owner_recorded(tmp_path) is True  # present…
    assert run_owner.read_owner(tmp_path) is None  # …and deliberately unusable


def test_a_marker_that_cannot_be_written_raises(tmp_path: Path) -> None:
    # The last resort: a producer that cannot stamp its run dir must fail
    # BEFORE it starts the fetch + checkout, not run on unstampable.
    with pytest.raises(OSError):
        run_owner.record_owner(tmp_path / "does-not-exist")


def test_a_planted_marker_symlink_is_replaced_not_followed(tmp_path: Path) -> None:
    # The marker sits at a predictable path in a tree this subsystem documents
    # as agent-writable. A link planted where it goes must never turn the stamp
    # into a host-privileged write through it.
    outside = tmp_path / "operator_secret.json"
    outside.write_text("keep me")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / run_owner.OWNER_FILE).symlink_to(outside)

    run_owner.record_owner(run_dir)

    assert outside.read_text() == "keep me"  # the write did not land there
    assert not (run_dir / run_owner.OWNER_FILE).is_symlink()
    identity = run_owner.read_owner(run_dir)
    assert identity is not None and identity.pid == os.getpid()


def test_a_symlinked_marker_reads_as_present_but_unusable(tmp_path: Path) -> None:
    # The inbound half: a marker pointed at a host file would otherwise hand
    # the liveness decision (and the delete) to content outside the run dir.
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"pid": 1, "start_ticks": 5, "host_boot": "b"}))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / run_owner.OWNER_FILE).symlink_to(elsewhere)

    assert run_owner.owner_recorded(run_dir) is True  # present: keep the dir
    assert run_owner.read_owner(run_dir) is None  # but never believed


def test_an_oversized_marker_is_not_read_whole(tmp_path: Path) -> None:
    # Bounded like every other agent-adjacent read in this subsystem — a marker
    # is three small fields, so anything bigger is not one, however well it
    # parses.
    (tmp_path / run_owner.OWNER_FILE).write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "start_ticks": 5,
                "host_boot": "b",
                "pad": "x" * 8192,
            }
        )
    )
    assert run_owner.owner_recorded(tmp_path) is True
    assert run_owner.read_owner(tmp_path) is None


def test_the_temp_path_is_not_a_name_anyone_can_pre_plant(tmp_path: Path) -> None:
    # The write goes to a random name opened O_CREAT|O_EXCL|O_NOFOLLOW, so a
    # link left at a *guessable* temp path cannot redirect a host-privileged
    # truncate-and-write to whatever it points at (the repo's `.<name>.tmp.<rand>`
    # convention, and the PR #289 class `config.py` records as critical).
    outside = tmp_path / "operator_secret.json"
    outside.write_text("keep me")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / f".{run_owner.OWNER_FILE}.tmp").symlink_to(outside)

    run_owner.record_owner(run_dir)

    assert outside.read_text() == "keep me"
    identity = run_owner.read_owner(run_dir)
    assert identity is not None and identity.pid == os.getpid()
    # and the stamp left no temp file of its own behind
    assert sorted(p.name for p in run_dir.iterdir()) == [
        f".{run_owner.OWNER_FILE}.tmp",
        run_owner.OWNER_FILE,
    ]


@pytest.mark.parametrize(
    "payload",
    [
        "{not json",
        json.dumps([1, 2]),
        json.dumps({"pid": 0, "start_ticks": 5, "host_boot": "b"}),
        json.dumps({"pid": -1, "start_ticks": 5, "host_boot": "b"}),
        json.dumps({"pid": True, "start_ticks": 5, "host_boot": "b"}),
        json.dumps({"pid": "7", "start_ticks": 5, "host_boot": "b"}),
        json.dumps({"pid": 7, "start_ticks": 0, "host_boot": "b"}),
        json.dumps({"pid": 7, "start_ticks": 5, "host_boot": ""}),
        json.dumps({"pid": 7, "start_ticks": 5}),
    ],
)
def test_an_unusable_marker_reads_as_present_but_unknown(
    tmp_path: Path, payload: str
) -> None:
    # Present (so prune keeps the dir) but unreadable (so it never claims the
    # owner is gone). Every field is validated: the run dir's own agents write
    # into this tree, and `pid=0` would make `os.kill(0, 0)` report our own
    # process group as the owner, forever.
    (tmp_path / run_owner.OWNER_FILE).write_text(payload)
    assert run_owner.owner_recorded(tmp_path) is True
    assert run_owner.read_owner(tmp_path) is None


def test_a_reused_pid_is_not_the_recorded_owner(tmp_path: Path) -> None:
    # The identity, not the number: a live pid whose recorded start time does
    # not match it is a REUSED pid — the run that stamped it is gone.
    (tmp_path / run_owner.OWNER_FILE).write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "start_ticks": 1,
                "host_boot": orphans.host_boot_id(),
            }
        )
    )
    identity = run_owner.read_owner(tmp_path)
    assert identity is not None
    assert orphans.identity_alive(identity) is False
