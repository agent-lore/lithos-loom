"""The supervisor's pidfile (#407 slice 3).

``lithos-loom drain`` finds the daemon through it. Ownership is an exclusive
``flock`` on the file's inode, held for the daemon's lifetime and released
by the kernel when the process ends — however it ends. The file records a
process *identity* (pid + start ticks + host boot id, the slice 2b notion),
never a bare pid: a pid is reused, and a drain must never signal whatever
now wears the number. Nothing ever unlinks the file: the lock decides
liveness, so there is no stale-eviction step for two claimants to race
(PR #418 review).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lithos_loom.runner import orphans, pidfile
from lithos_loom.runner.orphans import ProcessIdentity


def test_pidfile_lives_under_the_work_dir(tmp_path: Path) -> None:
    assert pidfile.pidfile_path(tmp_path) == tmp_path / "supervisor.pid"


def test_claim_records_this_process_and_read_returns_it(tmp_path: Path) -> None:
    path = pidfile.pidfile_path(tmp_path / "work")  # the directory is created
    claim = pidfile.claim_pidfile(path)
    assert claim is not None
    try:
        assert claim.identity.pid == os.getpid()
        assert pidfile.read_pidfile(path) == claim.identity
        assert json.loads(path.read_text(encoding="utf-8")) == {
            "pid": os.getpid(),
            "start_ticks": claim.identity.start_ticks,
            "host_boot": claim.identity.host_boot,
        }
        assert [p.name for p in path.parent.iterdir()] == ["supervisor.pid"]
    finally:
        claim.release()


def test_claim_falls_back_to_the_bare_pid_when_identity_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orphans, "process_identity", lambda pid: None)
    claim = pidfile.claim_pidfile(tmp_path / "supervisor.pid")
    assert claim is not None
    try:
        assert claim.identity == ProcessIdentity(
            pid=os.getpid(), start_ticks=0, host_boot=""
        )
    finally:
        claim.release()


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
        "",
    ],
)
def test_read_returns_none_for_a_missing_or_malformed_file(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / "supervisor.pid"
    assert pidfile.read_pidfile(path) is None
    path.write_text(content, encoding="utf-8")
    assert pidfile.read_pidfile(path) is None


# ── ownership ───────────────────────────────────────────────────────────


def test_a_second_claimant_is_refused_while_the_first_holds_and_admitted_after(
    tmp_path: Path,
) -> None:
    """PR #418 review (High): two boots against one work dir admit exactly
    one, and the arbiter is the lock — no read-then-evict step exists for
    the loser to delete the winner's claim with."""
    path = tmp_path / "supervisor.pid"
    first = pidfile.claim_pidfile(path)
    assert first is not None
    try:
        assert pidfile.claim_pidfile(path) is None
        assert pidfile.read_pidfile(path) == first.identity  # untouched
    finally:
        first.release()
    second = pidfile.claim_pidfile(path)
    assert second is not None
    second.release()


def test_a_claimant_in_another_process_is_refused_while_we_hold(
    tmp_path: Path,
) -> None:
    path = tmp_path / "supervisor.pid"
    claim = pidfile.claim_pidfile(path)
    assert claim is not None
    script = (
        "import sys; from pathlib import Path; from lithos_loom.runner import pidfile; "
        f"c = pidfile.claim_pidfile(Path({str(path)!r})); "
        "print('refused' if c is None else 'admitted'); "
        "c is None or c.release()"
    )
    try:
        out = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, check=True
        )
        assert out.stdout.strip() == "refused"
    finally:
        claim.release()
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "admitted"


def test_release_keeps_the_file_and_makes_it_stale(tmp_path: Path) -> None:
    """Nothing unlinks the pidfile: a released one is simply not held, and
    the identity it names — this very live process — does not make it a
    daemon. `drain` reports it stale; the next `run` claims over it."""
    path = tmp_path / "supervisor.pid"
    claim = pidfile.claim_pidfile(path)
    assert claim is not None
    assert pidfile.daemon_alive(path, claim.identity) is True
    claim.release()
    claim.release()  # idempotent
    assert path.exists()
    assert pidfile.daemon_alive(path, claim.identity) is False
    assert orphans.pid_alive(os.getpid()) is True  # the pid is fine; the daemon is not


def test_claim_over_a_stale_or_malformed_file_succeeds_and_rewrites_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "supervisor.pid"
    for content in (
        "torn",
        json.dumps({"pid": 4242, "start_ticks": 1, "host_boot": "x"}),
    ):
        path.write_text(content, encoding="utf-8")
        claim = pidfile.claim_pidfile(path)
        assert claim is not None
        try:
            assert pidfile.read_pidfile(path) == claim.identity
        finally:
            claim.release()


def test_claim_raises_when_the_file_cannot_be_created(tmp_path: Path) -> None:
    blocker = tmp_path / "work"
    blocker.write_text("not a directory", encoding="utf-8")
    with pytest.raises(OSError):
        pidfile.claim_pidfile(blocker / "supervisor.pid")


def test_holder_alive_answers_from_the_lock(tmp_path: Path) -> None:
    path = tmp_path / "supervisor.pid"
    assert pidfile.holder_alive(path) is False  # no file, no holder
    claim = pidfile.claim_pidfile(path)
    assert claim is not None
    try:
        assert pidfile.holder_alive(path) is True
    finally:
        claim.release()
    assert pidfile.holder_alive(path) is False


def test_daemon_alive_falls_back_to_the_identity_when_the_lock_is_unknowable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "supervisor.pid"
    monkeypatch.setattr(pidfile, "holder_alive", lambda p: None)

    def naming(identity: ProcessIdentity) -> ProcessIdentity:
        path.write_text(
            json.dumps(
                {
                    "pid": identity.pid,
                    "start_ticks": identity.start_ticks,
                    "host_boot": identity.host_boot,
                }
            ),
            encoding="utf-8",
        )
        return identity

    live = orphans.process_identity(os.getpid())
    if live is not None:
        assert pidfile.daemon_alive(path, naming(live)) is True
    unverifiable = naming(ProcessIdentity(pid=os.getpid(), start_ticks=0, host_boot=""))
    assert pidfile.daemon_alive(path, unverifiable) is True  # the kernel's pid check
    other_boot = naming(
        ProcessIdentity(pid=os.getpid(), start_ticks=1, host_boot="not-this")
    )
    assert pidfile.daemon_alive(path, other_boot) is False
    huge = naming(ProcessIdentity(pid=2**70, start_ticks=0, host_boot=""))
    assert pidfile.daemon_alive(path, huge) is False


# ── the lock must name the identity (PR #418 re-review) ─────────────────


_HOLDER_SCRIPT = """
import sys, time
from pathlib import Path
from lithos_loom.runner import pidfile
c = pidfile.claim_pidfile(Path(sys.argv[1]))
print("held" if c is not None else "refused", flush=True)
if c is not None:
    while True:
        time.sleep(0.2)
"""


def test_daemon_alive_is_false_for_an_identity_a_successor_replaced(
    tmp_path: Path,
) -> None:
    """A exited, B claimed within the poll: the lock is held, but by B —
    `daemon_alive(path, A)` must say A is gone (else `drain` waits forever
    on a daemon that ended, and never signals the one that replaced it)."""
    path = tmp_path / "supervisor.pid"
    first = pidfile.claim_pidfile(path)
    assert first is not None
    first.release()  # A exited
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_SCRIPT, str(path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"  # B claimed
        successor = pidfile.read_pidfile(path)
        assert successor is not None and successor.pid == holder.pid
        assert pidfile.holder_alive(path) is True
        assert pidfile.daemon_alive(path, first.identity) is False
        assert pidfile.daemon_alive(path, successor) is True
    finally:
        holder.kill()
        holder.wait()


def test_daemon_alive_rejects_a_positively_dead_identity_under_a_held_lock(
    tmp_path: Path,
) -> None:
    """The claim handoff: B holds the lock but has not rewritten the
    content yet, so the file still names A. A is positively dead (its
    start marker no longer matches); the held lock must not resurrect it."""
    path = tmp_path / "supervisor.pid"
    claim = pidfile.claim_pidfile(path)  # we are B, holding the lock
    assert claim is not None
    try:
        dead = ProcessIdentity(
            pid=os.getpid(), start_ticks=1, host_boot=orphans.host_boot_id()
        )
        path.write_text(
            json.dumps(
                {"pid": dead.pid, "start_ticks": 1, "host_boot": dead.host_boot}
            ),
            encoding="utf-8",
        )
        assert pidfile.read_pidfile(path) == dead
        assert pidfile.holder_alive(path) is True
        assert pidfile.daemon_alive(path, dead) is False
    finally:
        claim.release()


def test_daemon_alive_under_an_unknowable_lock_still_needs_the_content_to_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "supervisor.pid"
    monkeypatch.setattr(pidfile, "holder_alive", lambda p: None)
    live = orphans.process_identity(os.getpid())
    if live is None:
        pytest.skip("no process identity on this host")
    other = ProcessIdentity(
        pid=live.pid + 1, start_ticks=live.start_ticks, host_boot=live.host_boot
    )
    path.write_text(
        json.dumps(
            {
                "pid": other.pid,
                "start_ticks": other.start_ticks,
                "host_boot": other.host_boot,
            }
        ),
        encoding="utf-8",
    )
    assert pidfile.daemon_alive(path, live) is False  # the file no longer names it


# ── the whole identity is written (PR #418 re-review, Medium) ───────────


def test_claim_writes_the_whole_identity_when_the_kernel_writes_short(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: real_write(fd, data[:1]))
    path = tmp_path / "supervisor.pid"
    claim = pidfile.claim_pidfile(path)
    assert claim is not None
    try:
        assert pidfile.read_pidfile(path) == claim.identity
    finally:
        claim.release()


def test_claim_raises_when_the_write_makes_no_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "write", lambda fd, data: 0)
    with pytest.raises(OSError):
        pidfile.claim_pidfile(tmp_path / "supervisor.pid")
    assert pidfile.holder_alive(tmp_path / "supervisor.pid") is False  # nothing held
