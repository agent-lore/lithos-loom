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


def test_a_host_that_cannot_identify_itself_records_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Best-effort: no identity (no procfs / no boot id) means no marker at all,
    # so prune falls back to containers + idle time instead of keeping the dir
    # forever on a marker it could never verify.
    monkeypatch.setattr(run_owner, "process_identity", lambda pid: None)
    run_owner.record_owner(tmp_path)
    assert run_owner.owner_recorded(tmp_path) is False


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
