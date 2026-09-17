"""SIGTERM as SystemExit (#407 slice 2a)."""

from __future__ import annotations

import signal

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


def _bound(monkeypatch, *, parent: int | None = None) -> None:
    import os

    monkeypatch.setenv(signals.BOUND_ENV, "1")
    monkeypatch.setenv(
        signals.PARENT_PID_ENV, str(os.getppid() if parent is None else parent)
    )


def test_bind_sets_pdeathsig_and_starts_the_parent_watch(monkeypatch) -> None:
    calls: list[tuple[int, int]] = []
    watched: list[int] = []
    _bound(monkeypatch)
    monkeypatch.setattr(
        signals, "set_pdeathsig", lambda opt, arg: calls.append((opt, arg))
    )
    monkeypatch.setattr(
        signals, "start_parent_watch", lambda expected: watched.append(expected)
    )
    signals.bind_lifetime_to_parent()
    import os

    assert calls == [(signals.PR_SET_PDEATHSIG, signal.SIGTERM)]
    assert watched == [os.getppid()]


def test_bind_exits_at_once_when_the_parent_is_not_the_one_that_spawned_us(
    monkeypatch,
) -> None:
    # Dave's re-review of #416 (High): a parent that died before the bind is
    # reparented — to pid 1, or under a child subreaper to something else —
    # so the check is against the pid the spawner passed, not against 1
    _bound(monkeypatch, parent=4242)
    monkeypatch.setattr(signals, "set_pdeathsig", lambda opt, arg: None)
    monkeypatch.setattr(signals, "start_parent_watch", lambda expected: None)
    with pytest.raises(SystemExit) as exc:
        signals.bind_lifetime_to_parent()
    assert exc.value.code == 143


def test_bind_still_watches_the_parent_where_prctl_is_unavailable(monkeypatch) -> None:
    # fail closed off Linux (Dave's re-review of #416): no PDEATHSIG is not
    # "no bind" — the portable parent watch is what makes the run die with
    # its dispatcher everywhere
    watched: list[int] = []
    _bound(monkeypatch)

    def unavailable(opt, arg):
        raise OSError("no prctl here")

    monkeypatch.setattr(signals, "set_pdeathsig", unavailable)
    monkeypatch.setattr(
        signals, "start_parent_watch", lambda expected: watched.append(expected)
    )
    signals.bind_lifetime_to_parent()
    assert watched


def test_the_parent_watch_terminates_us_when_the_parent_changes(monkeypatch) -> None:
    import os
    import threading

    parents = iter([4242, 4242, 1])
    monkeypatch.setattr(signals.os, "getppid", lambda: next(parents))
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(signals.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    stop = threading.Event()
    signals.watch_parent(4242, interval=0.0, stop=stop)
    assert killed == [(os.getpid(), signal.SIGTERM)]


def test_the_parent_watch_stops_quietly_when_asked(monkeypatch) -> None:
    import threading

    monkeypatch.setattr(signals.os, "getppid", lambda: 4242)
    killed: list = []
    monkeypatch.setattr(signals.os, "kill", lambda pid, sig: killed.append(1))
    stop = threading.Event()
    stop.set()
    signals.watch_parent(4242, interval=0.0, stop=stop)
    assert killed == []


def test_bind_never_raises_when_prctl_is_unavailable(monkeypatch) -> None:
    _bound(monkeypatch)

    def unavailable(opt, arg):
        raise OSError("no prctl here")

    monkeypatch.setattr(signals, "set_pdeathsig", unavailable)
    monkeypatch.setattr(signals, "start_parent_watch", lambda expected: None)
    signals.bind_lifetime_to_parent()  # no raise
