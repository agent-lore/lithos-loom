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
