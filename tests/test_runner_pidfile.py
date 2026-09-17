"""The supervisor's pidfile (#407 slice 3).

``lithos-loom drain`` finds the running daemon through it. It records a
process *identity* (pid + start time + host boot id, the slice 2b notion),
never a bare pid — a pid is reused, and a drain must never signal whatever
now wears the number.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from lithos_loom.runner import orphans, pidfile
from lithos_loom.runner.orphans import ProcessIdentity


def test_pidfile_lives_under_the_work_dir(tmp_path: Path) -> None:
    assert pidfile.pidfile_path(tmp_path) == tmp_path / "supervisor.pid"


def test_write_records_this_process_and_read_returns_it(tmp_path: Path) -> None:
    path = pidfile.pidfile_path(tmp_path)
    written = pidfile.write_pidfile(path)
    assert written.pid == os.getpid()
    assert pidfile.read_pidfile(path) == written
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {
        "pid": os.getpid(),
        "start_ticks": written.start_ticks,
        "host_boot": written.host_boot,
    }


def test_write_creates_the_directory(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "work" / "supervisor.pid"
    pidfile.write_pidfile(path)
    assert path.is_file()


def test_write_falls_back_to_the_bare_pid_when_identity_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # an unverifiable identity is still a signal target — drain falls back
    # to the kernel's own pid check for it (the slice 2b pid-0 stamp is a
    # different contract: there the zero authorizes a refund; here we need
    # something to signal)
    monkeypatch.setattr(orphans, "process_identity", lambda pid: None)
    written = pidfile.write_pidfile(tmp_path / "supervisor.pid")
    assert written == ProcessIdentity(pid=os.getpid(), start_ticks=0, host_boot="")


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps([1, 2, 3]),
        json.dumps({"pid": "x", "start_ticks": 1, "host_boot": "b"}),
        json.dumps({"pid": 0, "start_ticks": 1, "host_boot": "b"}),
        json.dumps({"pid": -4, "start_ticks": 1, "host_boot": "b"}),
        json.dumps({"pid": 4, "start_ticks": "soon", "host_boot": "b"}),
        json.dumps({"pid": 4, "start_ticks": 1, "host_boot": 7}),
        json.dumps({"pid": 4}),
    ],
)
def test_read_returns_none_for_a_missing_or_malformed_file(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / "supervisor.pid"
    assert pidfile.read_pidfile(path) is None
    path.write_text(content, encoding="utf-8")
    assert pidfile.read_pidfile(path) is None


def test_remove_only_when_the_file_still_names_the_writer(tmp_path: Path) -> None:
    path = tmp_path / "supervisor.pid"
    mine = pidfile.write_pidfile(path)
    later = ProcessIdentity(
        pid=mine.pid + 1, start_ticks=mine.start_ticks, host_boot=mine.host_boot
    )
    # a daemon that booted after us (or over a stale file) owns the file now
    pidfile.remove_pidfile(path, later)
    assert path.exists()
    pidfile.remove_pidfile(path, mine)
    assert not path.exists()
    pidfile.remove_pidfile(path, mine)  # idempotent: nothing to remove, no raise


def test_daemon_alive_verifies_the_identity_and_falls_back_to_the_pid() -> None:
    live = orphans.process_identity(os.getpid())
    if live is not None:
        assert pidfile.daemon_alive(live) is True
    # unverifiable identity: the kernel's pid check decides
    assert (
        pidfile.daemon_alive(
            ProcessIdentity(pid=os.getpid(), start_ticks=0, host_boot="")
        )
        is True
    )
    # a verifiable identity from another boot is dead, whatever wears the pid
    assert (
        pidfile.daemon_alive(
            ProcessIdentity(pid=os.getpid(), start_ticks=1, host_boot="not-this-boot")
        )
        is False
    )
    # an unverifiable identity whose pid the kernel cannot even represent
    assert (
        pidfile.daemon_alive(ProcessIdentity(pid=2**70, start_ticks=0, host_boot=""))
        is False
    )


# ── claim (review of this slice: check-then-write was a multi-second window) ──


def test_claim_records_this_process_when_no_file_exists(tmp_path: Path) -> None:
    path = tmp_path / "work" / "supervisor.pid"
    mine = pidfile.claim_pidfile(path)
    assert mine is not None and mine.pid == os.getpid()
    assert pidfile.read_pidfile(path) == mine
    assert [p.name for p in path.parent.iterdir()] == ["supervisor.pid"]  # no temp left


def test_claim_refuses_while_a_live_daemon_holds_the_file(tmp_path: Path) -> None:
    path = tmp_path / "supervisor.pid"
    holder = pidfile.write_pidfile(path)  # this process: alive by construction
    assert pidfile.claim_pidfile(path) is None
    assert pidfile.read_pidfile(path) == holder  # untouched


def test_claim_replaces_a_stale_file(tmp_path: Path) -> None:
    path = tmp_path / "supervisor.pid"
    path.write_text(
        json.dumps({"pid": 4242, "start_ticks": 1, "host_boot": "another-boot"}),
        encoding="utf-8",
    )
    mine = pidfile.claim_pidfile(path)
    assert mine is not None and pidfile.read_pidfile(path) == mine


def test_claim_replaces_a_malformed_file(tmp_path: Path) -> None:
    path = tmp_path / "supervisor.pid"
    path.write_text("torn", encoding="utf-8")
    mine = pidfile.claim_pidfile(path)
    assert mine is not None and pidfile.read_pidfile(path) == mine


def test_claim_raises_when_the_file_cannot_be_written(tmp_path: Path) -> None:
    blocker = tmp_path / "work"
    blocker.write_text("not a directory", encoding="utf-8")
    with pytest.raises(OSError):
        pidfile.claim_pidfile(blocker / "supervisor.pid")


def test_two_claimants_racing_the_same_stale_file_admit_exactly_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exclusive link is the arbiter: whoever links first wins; the
    other re-reads, finds a live holder and stands down."""
    path = tmp_path / "supervisor.pid"
    real_link = os.link
    first_claim = {"done": False}

    def racing_link(src: str, dst: str, *a: object, **kw: object) -> None:
        if not first_claim["done"]:
            first_claim["done"] = True
            # the other daemon landed in between (written as it would be,
            # from its own temp file — here a live identity: this process)
            me = orphans.process_identity(os.getpid()) or ProcessIdentity(
                pid=os.getpid(), start_ticks=0, host_boot=""
            )
            path.write_text(
                json.dumps(
                    {
                        "pid": me.pid,
                        "start_ticks": me.start_ticks,
                        "host_boot": me.host_boot,
                    }
                ),
                encoding="utf-8",
            )
        return real_link(src, dst, *a, **kw)

    monkeypatch.setattr(os, "link", racing_link)
    assert pidfile.claim_pidfile(path) is None
    assert pidfile.read_pidfile(path) is not None
