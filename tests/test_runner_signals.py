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


def test_bind_sets_pdeathsig_to_sigterm_when_loom_spawned_the_process(
    monkeypatch,
) -> None:
    calls: list[tuple[int, int]] = []
    monkeypatch.setenv(signals.BOUND_ENV, "1")
    monkeypatch.setattr(
        signals, "set_pdeathsig", lambda opt, arg: calls.append((opt, arg))
    )
    monkeypatch.setattr(signals.os, "getppid", lambda: 4242)
    signals.bind_lifetime_to_parent()
    assert calls == [(signals.PR_SET_PDEATHSIG, signal.SIGTERM)]


def test_bind_exits_at_once_when_the_parent_already_died(monkeypatch) -> None:
    # the parent died between the spawn and the bind: PDEATHSIG would never
    # fire (it is set after the death), so the check is explicit
    monkeypatch.setenv(signals.BOUND_ENV, "1")
    monkeypatch.setattr(signals, "set_pdeathsig", lambda opt, arg: None)
    monkeypatch.setattr(signals.os, "getppid", lambda: 1)
    with pytest.raises(SystemExit) as exc:
        signals.bind_lifetime_to_parent()
    assert exc.value.code == 143


def test_bind_never_raises_when_prctl_is_unavailable(monkeypatch) -> None:
    monkeypatch.setenv(signals.BOUND_ENV, "1")

    def unavailable(opt, arg):
        raise OSError("no prctl here")

    monkeypatch.setattr(signals, "set_pdeathsig", unavailable)
    monkeypatch.setattr(signals.os, "getppid", lambda: 4242)
    signals.bind_lifetime_to_parent()  # no raise
