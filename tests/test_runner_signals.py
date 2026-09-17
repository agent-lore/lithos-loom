"""SIGTERM as SystemExit (#407 slice 2a)."""

from __future__ import annotations

import signal
from collections.abc import Callable

import pytest

from lithos_loom.runner import signals


def test_install_makes_sigterm_raise_system_exit_143(monkeypatch) -> None:
    installed: list[tuple[int, object]] = []
    monkeypatch.setattr(signal, "signal", lambda sig, h: installed.append((sig, h)))
    signals.install_sigterm_exit()
    handler = next(h for sig, h in installed if sig == signal.SIGTERM)
    with pytest.raises(SystemExit) as exc:
        handler(signal.SIGTERM, None)  # type: ignore[operator]
    assert exc.value.code == 143


def test_a_second_sigterm_is_a_plain_kill_not_a_second_unwind(monkeypatch) -> None:
    # opus round 1 (Low): a second SIGTERM during teardown would raise
    # SystemExit from inside the `finally` and abandon the remaining
    # stop_container calls; the first delivery restores the default
    # disposition so the second one just ends the process
    installed: list[tuple[int, object]] = []
    monkeypatch.setattr(signal, "signal", lambda sig, h: installed.append((sig, h)))
    signals.install_sigterm_exit()
    handler = next(h for sig, h in installed if sig == signal.SIGTERM)
    with pytest.raises(SystemExit):
        handler(signal.SIGTERM, None)  # type: ignore[operator]
    assert (signal.SIGTERM, signal.SIG_DFL) in installed


def test_install_is_a_no_op_off_the_main_thread(monkeypatch) -> None:
    def refuse(sig, h):
        raise ValueError("signal only works in main thread")

    monkeypatch.setattr(signal, "signal", refuse)
    signals.install_sigterm_exit()  # no raise


# --- #407 slice 2b re-review: the child binds its lifetime to loom -----------


def test_bind_is_a_no_op_unless_loom_spawned_the_process(monkeypatch) -> None:
    # a hand-run `develop converge` in a terminal (or `setsid nohup … &`)
    # must never die with its shell — only a child loom spawned binds
    calls: list[tuple[int, int]] = []
    monkeypatch.delenv(signals.BOUND_ENV, raising=False)
    monkeypatch.setattr(
        signals, "set_pdeathsig", lambda opt, arg: calls.append((opt, arg))
    )
    signals.bind_lifetime_to_parent()
    assert calls == []


def _bound(monkeypatch, *, parent: int | None = None, start: str | None = None) -> None:
    import os

    monkeypatch.setenv(signals.BOUND_ENV, "1")
    monkeypatch.setenv(
        signals.PARENT_PID_ENV, str(os.getppid() if parent is None else parent)
    )
    if start is None:
        monkeypatch.delenv(signals.PARENT_START_ENV, raising=False)
    else:
        monkeypatch.setenv(signals.PARENT_START_ENV, start)


def test_bind_sets_pdeathsig_and_starts_the_spawner_watch(monkeypatch) -> None:
    calls: list[tuple[int, int]] = []
    watched: list[object] = []
    _bound(monkeypatch)
    monkeypatch.setattr(
        signals, "set_pdeathsig", lambda opt, arg: calls.append((opt, arg))
    )
    monkeypatch.setattr(
        signals, "start_parent_watch", lambda alive: watched.append(alive)
    )
    signals.bind_lifetime_to_parent()
    assert calls == [(signals.PR_SET_PDEATHSIG, signal.SIGTERM)]
    assert len(watched) == 1 and callable(watched[0])
    # the probe follows our real, live parent
    assert watched[0]() is True


def test_bind_exits_at_once_when_the_spawner_is_not_an_ancestor(monkeypatch) -> None:
    # Dave's re-review of #416 (High): a spawner that died before the bind
    # leaves us reparented — to pid 1, or under a child subreaper to
    # something else — so the check is against the pid the spawner passed,
    # not against 1. A process is never its own ancestor.
    import os

    _bound(monkeypatch, parent=os.getpid())
    monkeypatch.setattr(signals, "set_pdeathsig", lambda opt, arg: None)
    monkeypatch.setattr(signals, "start_parent_watch", lambda alive: None)
    with pytest.raises(SystemExit) as exc:
        signals.bind_lifetime_to_parent()
    assert exc.value.code == 143


def test_bind_accepts_the_spawner_as_a_grandparent(monkeypatch) -> None:
    # f6537c52: the route command is `uv run python -m …` and uv SPAWNS python
    # rather than exec'ing it, so the plugin's parent is uv and the spawner
    # is one hop further up. Bound to the spawner as an ancestor, not as
    # getppid() — the check that exited every story-develop run at startup.
    import os

    grandparent = signals.parent_of(os.getppid())
    if grandparent is None or grandparent <= 1:
        pytest.skip("no grandparent above pid 1 to bind to here")
    watched: list[object] = []
    _bound(monkeypatch, parent=grandparent)
    monkeypatch.setattr(signals, "set_pdeathsig", lambda opt, arg: None)
    monkeypatch.setattr(
        signals, "start_parent_watch", lambda alive: watched.append(alive)
    )
    signals.bind_lifetime_to_parent()  # no SystemExit
    assert watched


def test_bind_never_accepts_init_as_the_spawner(monkeypatch) -> None:
    # a spawner pid of 1 (or 0, or garbage) is not a spawner — every process
    # descends from init, so accepting it would accept a reparented orphan
    for parent in (1, 0, -3):
        _bound(monkeypatch, parent=parent)
        with pytest.raises(SystemExit):
            signals.bind_lifetime_to_parent()
    monkeypatch.setenv(signals.PARENT_PID_ENV, "not-a-pid")
    with pytest.raises(SystemExit):
        signals.bind_lifetime_to_parent()


def test_bind_still_watches_the_spawner_where_prctl_is_unavailable(monkeypatch) -> None:
    # fail closed off Linux (Dave's re-review of #416): no PDEATHSIG is not
    # "no bind" — the portable spawner watch is what makes the run die with
    # its dispatcher everywhere
    watched: list[object] = []
    _bound(monkeypatch)

    def unavailable(opt, arg):
        raise OSError("no prctl here")

    monkeypatch.setattr(signals, "set_pdeathsig", unavailable)
    monkeypatch.setattr(
        signals, "start_parent_watch", lambda alive: watched.append(alive)
    )
    signals.bind_lifetime_to_parent()
    assert watched


def test_bind_never_raises_when_prctl_is_unavailable(monkeypatch) -> None:
    _bound(monkeypatch)

    def unavailable(opt, arg):
        raise OSError("no prctl here")

    monkeypatch.setattr(signals, "set_pdeathsig", unavailable)
    monkeypatch.setattr(signals, "start_parent_watch", lambda alive: None)
    signals.bind_lifetime_to_parent()  # no raise


# --- the spawner's identity: pid + start marker ------------------------------


def test_bind_watches_the_start_marker_the_spawner_stamped(monkeypatch) -> None:
    # the spawner's own start ticks travel in the env; a live pid with a
    # different marker is a REUSED pid, so the spawner is dead
    probes: list[tuple[int, int | None]] = []
    _bound(monkeypatch, start="777")
    monkeypatch.setattr(signals, "set_pdeathsig", lambda opt, arg: None)
    monkeypatch.setattr(
        signals, "spawner_alive", lambda pid, start: probes.append((pid, start)) or True
    )
    captured: list[Callable[[], bool | None]] = []
    monkeypatch.setattr(
        signals, "start_parent_watch", lambda alive: captured.append(alive)
    )
    signals.bind_lifetime_to_parent()
    captured[0]()
    import os

    assert probes == [(os.getppid(), 777)]


def test_bind_captures_the_start_marker_itself_when_the_spawner_sent_none(
    monkeypatch,
) -> None:
    import os

    _bound(monkeypatch, start="")  # the spawner could not read its own marker
    monkeypatch.setattr(signals, "set_pdeathsig", lambda opt, arg: None)
    monkeypatch.setattr(
        signals, "start_ticks", lambda pid: 4242 if pid == os.getppid() else None
    )
    probes: list[tuple[int, int | None]] = []
    monkeypatch.setattr(
        signals, "spawner_alive", lambda pid, start: probes.append((pid, start)) or True
    )
    captured: list[Callable[[], bool | None]] = []
    monkeypatch.setattr(
        signals, "start_parent_watch", lambda alive: captured.append(alive)
    )
    signals.bind_lifetime_to_parent()
    captured[0]()
    assert probes == [(os.getppid(), 4242)]


def test_spawner_alive_reads_a_reused_pid_as_dead(monkeypatch) -> None:
    monkeypatch.setattr(signals, "start_ticks", lambda pid: 9001)
    assert signals.spawner_alive(4242, 9001) is True
    assert signals.spawner_alive(4242, 1234) is False  # same number, other process


def test_spawner_alive_holds_through_a_transient_proc_failure(monkeypatch) -> None:
    # PR #417 review (Medium): the spawner stamped clock ticks from /proc; one
    # probe later /proc does not answer. Before, `start_ticks` fell through to
    # `ps` and read epoch seconds — a different number for the same live
    # spawner, so this returned False and the watch SIGTERMed a healthy run.
    # Now the marker is unknown and the pid's liveness holds the run.
    import os

    from lithos_loom.runner import orphans

    stamped = signals.start_ticks(os.getpid())
    assert stamped is not None
    monkeypatch.setattr(orphans, "_PROCFS", True)
    monkeypatch.setattr(orphans, "_proc_start_ticks", lambda pid: None)

    def never(pid):
        raise AssertionError("ps consulted on a procfs host")

    monkeypatch.setattr(orphans, "_ps_start_epoch", never)
    assert signals.spawner_alive(os.getpid(), stamped) is True


def test_spawner_alive_falls_back_to_pid_liveness_without_a_marker(monkeypatch) -> None:
    monkeypatch.setattr(signals, "start_ticks", lambda pid: None)
    monkeypatch.setattr(signals, "pid_alive", lambda pid: False)
    assert signals.spawner_alive(4242, None) is False
    assert signals.spawner_alive(4242, 9001) is False
    monkeypatch.setattr(signals, "pid_alive", lambda pid: None)
    assert signals.spawner_alive(4242, 9001) is None  # unverifiable, not dead


# --- the ancestor walk --------------------------------------------------------


def test_spawner_is_ancestor_walks_past_an_intermediary(monkeypatch) -> None:
    # us(100) ← uv(90) ← route-runner(80) ← supervisor(70) ← init(1)
    chain = {100: 90, 90: 80, 80: 70, 70: 1}
    monkeypatch.setattr(signals.os, "getppid", lambda: 90)
    monkeypatch.setattr(signals, "parent_of", lambda pid: chain.get(pid))
    assert signals.spawner_is_ancestor(90) is True  # direct parent
    assert signals.spawner_is_ancestor(80) is True  # through uv
    assert signals.spawner_is_ancestor(70) is True
    assert signals.spawner_is_ancestor(60) is False  # not on the chain
    assert signals.spawner_is_ancestor(1) is False  # init is never a spawner


def test_spawner_is_ancestor_stops_at_a_reparented_chain(monkeypatch) -> None:
    # the spawner died in the spawn-to-bind window: uv was reparented to init
    monkeypatch.setattr(signals.os, "getppid", lambda: 90)
    monkeypatch.setattr(signals, "parent_of", lambda pid: {90: 1}.get(pid))
    assert signals.spawner_is_ancestor(80) is False


def test_spawner_is_ancestor_gives_up_on_an_unreadable_link(monkeypatch) -> None:
    monkeypatch.setattr(signals.os, "getppid", lambda: 90)
    monkeypatch.setattr(signals, "parent_of", lambda pid: None)
    assert signals.spawner_is_ancestor(80) is False


def test_spawner_is_ancestor_bounds_the_walk(monkeypatch) -> None:
    # a cycle in a lying parent_of must not spin forever
    monkeypatch.setattr(signals.os, "getppid", lambda: 90)
    monkeypatch.setattr(signals, "parent_of", lambda pid: 90)
    assert signals.spawner_is_ancestor(80) is False


def test_parent_of_reads_the_real_process_tree() -> None:
    import os

    assert signals.parent_of(os.getpid()) == os.getppid()
    assert signals.parent_of(0) is None
    assert signals.parent_of(-1) is None


# --- the watch ----------------------------------------------------------------


def test_the_spawner_watch_terminates_us_when_the_spawner_dies(monkeypatch) -> None:
    import os
    import threading

    states = iter([True, None, False])  # alive, unverifiable, dead
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(signals.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    stop = threading.Event()
    signals.watch_parent(lambda: next(states), interval=0.0, stop=stop)
    assert killed == [(os.getpid(), signal.SIGTERM)]


def test_the_spawner_watch_never_fires_on_an_unverifiable_probe(monkeypatch) -> None:
    # a transient /proc or ps failure must hold, not end, a running agent
    import threading

    killed: list = []
    monkeypatch.setattr(signals.os, "kill", lambda pid, sig: killed.append(1))
    stop = threading.Event()
    answers = iter([None, None, None])

    def probe():
        try:
            return next(answers)
        except StopIteration:
            stop.set()
            return None

    signals.watch_parent(probe, interval=0.0, stop=stop)
    assert killed == []


def test_the_spawner_watch_stops_quietly_when_asked(monkeypatch) -> None:
    import threading

    killed: list = []
    monkeypatch.setattr(signals.os, "kill", lambda pid, sig: killed.append(1))
    stop = threading.Event()
    stop.set()
    signals.watch_parent(lambda: True, interval=0.0, stop=stop)
    assert killed == []


# --- the spawner's side --------------------------------------------------------


def test_bound_child_env_stamps_marker_pid_and_start(monkeypatch) -> None:
    import os

    monkeypatch.setattr(
        signals, "start_ticks", lambda pid: 31337 if pid == os.getpid() else None
    )
    env = signals.bound_child_env({"KEEP": "me"})
    assert env["KEEP"] == "me"
    assert env[signals.BOUND_ENV] == "1"
    assert env[signals.PARENT_PID_ENV] == str(os.getpid())
    assert env[signals.PARENT_START_ENV] == "31337"


def test_bound_child_env_sends_an_empty_start_when_it_cannot_read_its_own(
    monkeypatch,
) -> None:
    monkeypatch.setattr(signals, "start_ticks", lambda pid: None)
    assert signals.bound_child_env({})[signals.PARENT_START_ENV] == ""


# --- through a real process tree ---------------------------------------------
#
# The shape that #416's in-process tests never exercised: loom → intermediary
# → bound child, the intermediary being whatever the configured command puts
# between them (`uv run` in production; a python `subprocess.call` here — a
# `sh -c` would exec its last simple command and collapse the hop).

_CHILD_BINDS_AND_REPORTS = """
import os, sys, time
from collections.abc import Callable

from lithos_loom.runner import signals
signals.install_sigterm_exit()
try:
    signals.bind_lifetime_to_parent()
except SystemExit as exc:
    print(f"exit:{exc.code}", flush=True)
    raise
print(f"bound:{os.getpid()}", flush=True)
if len(sys.argv) > 1:
    with open(sys.argv[1], "w") as f:
        f.write(str(os.getpid()))
    time.sleep(30)
"""

_ONE_HOP = (
    "import subprocess, sys; "
    "sys.exit(subprocess.call([sys.executable, '-c', sys.argv[1], *sys.argv[2:]]))"
)


def _spawn_through_intermediary(env: dict[str, str], *child_args: str):
    import subprocess
    import sys

    return subprocess.Popen(
        [sys.executable, "-c", _ONE_HOP, _CHILD_BINDS_AND_REPORTS, *child_args],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def test_a_bound_child_behind_an_intermediary_binds_instead_of_exiting() -> None:
    # the f6537c52 regression, end to end: PARENT_PID is the grandparent
    proc = _spawn_through_intermediary(signals.bound_child_env())
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 0, out
    assert out.startswith("bound:"), out


def test_a_bound_child_whose_spawner_is_not_an_ancestor_exits_143() -> None:
    import subprocess
    import sys

    # a live process that is NOT above us in the tree: a sibling
    sibling = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        env = {
            **signals.bound_child_env(),
            signals.PARENT_PID_ENV: str(sibling.pid),
            signals.PARENT_START_ENV: "",
        }
        proc = _spawn_through_intermediary(env)
        out, _ = proc.communicate(timeout=30)
        assert proc.returncode == 143, out
        assert "exit:143" in out
    finally:
        sibling.kill()
        sibling.wait()


_SPAWNER_THAT_DIES = """
import subprocess, sys, time
from collections.abc import Callable

from lithos_loom.runner import signals
one_hop, child_src, pid_file = sys.argv[1], sys.argv[2], sys.argv[3]
subprocess.Popen([sys.executable, "-c", one_hop, child_src, pid_file],
                 env=signals.bound_child_env())
time.sleep(60)  # killed from outside, never exits on its own
"""


def test_killing_the_spawner_ends_a_bound_child_behind_a_surviving_intermediary(
    tmp_path,
) -> None:
    # PDEATHSIG cannot do this one: the child's immediate parent (the
    # intermediary) outlives the spawner. Only a watch on the SPAWNER can.
    import os
    import subprocess
    import sys
    import time

    pid_file = tmp_path / "child.pid"
    spawner = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _SPAWNER_THAT_DIES,
            _ONE_HOP,
            _CHILD_BINDS_AND_REPORTS,
            str(pid_file),
        ]
    )
    child_pid = 0
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not pid_file.exists():
            time.sleep(0.05)
        assert pid_file.exists(), "the bound child never reported in"
        child_pid = int(pid_file.read_text())
        assert signals.pid_alive(child_pid) is True
        spawner.kill()  # SIGKILL: no shutdown path, the case #407 slice 2b is about
        spawner.wait()
        deadline = time.monotonic() + signals.PARENT_WATCH_INTERVAL * 5 + 5
        while time.monotonic() < deadline and signals.pid_alive(child_pid) is True:
            time.sleep(0.1)
        assert signals.pid_alive(child_pid) is not True, (
            "the bound child outlived its spawner"
        )
    finally:
        if spawner.poll() is None:
            spawner.kill()
            spawner.wait()
        if child_pid:
            with __import__("contextlib").suppress(ProcessLookupError):
                os.kill(child_pid, signal.SIGKILL)
