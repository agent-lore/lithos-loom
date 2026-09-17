"""`lithos-loom drain` (#407 slice 3).

Finds the daemon through the supervisor's pidfile, verifies the identity
it names is the process still running, sends SIGUSR1, and waits for the
daemon to exit — so the operator's restart is safe by construction: the
children stop admitting new dispatch, finish what is in flight, exit.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from lithos_loom.cli import drain as drain_mod
from lithos_loom.cli.drain import DrainOutcome, drain_daemon
from lithos_loom.main import app
from lithos_loom.runner import pidfile
from lithos_loom.runner.orphans import ProcessIdentity

runner = CliRunner()

_IDENTITY = ProcessIdentity(pid=4242, start_ticks=100, host_boot="boot-1")


class _Host:
    """A scripted host: who is alive, what signals were sent, how time passes."""

    def __init__(self, *, alive_for: int, kill_error: BaseException | None = None):
        self._alive_for = alive_for  # liveness polls answering True
        self._kill_error = kill_error
        self.signals: list[tuple[int, int]] = []
        self.slept: list[float] = []
        self.now = 0.0

    def alive(self, identity: ProcessIdentity) -> bool:
        assert identity == _IDENTITY
        if self._alive_for > 0:
            self._alive_for -= 1
            return True
        return False

    def kill(self, pid: int, sig: int) -> None:
        if self._kill_error is not None:
            raise self._kill_error
        self.signals.append((pid, sig))

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def clock(self) -> float:
        return self.now

    def drain(self, path: Path, *, timeout: float = 0.0) -> DrainOutcome:
        return drain_daemon(
            path,
            timeout=timeout,
            poll=0.5,
            alive=self.alive,
            kill=self.kill,
            sleep=self.sleep,
            clock=self.clock,
        )


def _pidfile(tmp_path: Path) -> Path:
    path = tmp_path / "supervisor.pid"
    path.write_text(
        '{"pid": 4242, "start_ticks": 100, "host_boot": "boot-1"}', encoding="utf-8"
    )
    return path


def test_no_pidfile_is_no_daemon(tmp_path: Path) -> None:
    host = _Host(alive_for=0)
    outcome = host.drain(tmp_path / "supervisor.pid")
    assert outcome.code == 1
    assert "no daemon pidfile" in outcome.message
    assert str(tmp_path / "supervisor.pid") in outcome.message
    assert host.signals == []


def test_a_stale_pidfile_is_reported_not_signalled(tmp_path: Path) -> None:
    host = _Host(alive_for=0)
    outcome = host.drain(_pidfile(tmp_path))
    assert outcome.code == 1
    assert "stale" in outcome.message and "4242" in outcome.message
    assert host.signals == []


def test_signals_sigusr1_then_waits_for_the_daemon_to_exit(tmp_path: Path) -> None:
    # alive at the pre-check, alive for two polls, then gone
    host = _Host(alive_for=3)
    outcome = host.drain(_pidfile(tmp_path))
    assert outcome.code == 0
    assert host.signals == [(4242, signal.SIGUSR1)]
    assert host.slept == [0.5, 0.5]
    assert "exited" in outcome.message


def test_timeout_leaves_the_daemon_draining_and_says_so(tmp_path: Path) -> None:
    host = _Host(alive_for=10**6)
    outcome = host.drain(_pidfile(tmp_path), timeout=1.2)
    assert outcome.code == 2
    assert host.signals == [(4242, signal.SIGUSR1)]
    assert host.now >= 1.2
    assert "still draining" in outcome.message
    assert "SIGTERM" in outcome.message  # the escalation is the operator's


def test_a_pid_that_vanished_between_check_and_signal_is_not_alive(
    tmp_path: Path,
) -> None:
    host = _Host(alive_for=1, kill_error=ProcessLookupError())
    outcome = host.drain(_pidfile(tmp_path))
    assert outcome.code == 1
    assert "exited" in outcome.message or "gone" in outcome.message


def test_a_signal_we_may_not_send_is_reported(tmp_path: Path) -> None:
    host = _Host(alive_for=1, kill_error=PermissionError("not ours"))
    outcome = host.drain(_pidfile(tmp_path))
    assert outcome.code == 1
    assert "permission" in outcome.message.lower()


def test_drain_default_waits_without_a_deadline(tmp_path: Path) -> None:
    host = _Host(alive_for=50)
    outcome = host.drain(_pidfile(tmp_path), timeout=0.0)
    assert outcome.code == 0
    assert len(host.slept) == 49


# ── the typer command ────────────────────────────────────────────────────


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        "[orchestrator]\n"
        'agent_id = "lithos-orchestrator-test"\n'
        'lithos_url = "http://localhost:8765"\n'
        f'work_dir = "{tmp_path / "work"}"\n',
        encoding="utf-8",
    )
    return path


def test_command_reads_the_pidfile_under_the_configured_work_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    pidfile.write_pidfile(pidfile.pidfile_path(work))  # this test process
    sent: list[tuple[int, int]] = []
    polls = {"n": 0}

    def _alive(identity: ProcessIdentity) -> bool:
        polls["n"] += 1
        return polls["n"] <= 2

    monkeypatch.setattr(drain_mod, "daemon_alive", _alive)
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(drain_mod.time, "sleep", lambda s: None)

    result = runner.invoke(app, ["drain", "--config", str(config), "--timeout", "5"])
    assert result.exit_code == 0, result.output
    assert sent == [(os.getpid(), signal.SIGUSR1)]
    assert "exited" in result.output


def test_command_exit_codes_follow_the_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    result = runner.invoke(app, ["drain", "--config", str(config)])
    assert result.exit_code == 1
    assert "no daemon pidfile" in result.output

    def _stuck(path: Path, **kw: Any) -> DrainOutcome:
        return DrainOutcome(2, "drain: daemon pid 1 still draining")

    monkeypatch.setattr(drain_mod, "drain_daemon", _stuck)
    monkeypatch.setattr("lithos_loom.main.drain_daemon", _stuck, raising=False)
    result = runner.invoke(app, ["drain", "--config", str(config), "--timeout", "3"])
    assert result.exit_code == 2, result.output
    assert "still draining" in result.output


def test_a_negative_timeout_is_rejected(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["drain", "--config", str(_config(tmp_path)), "--timeout", "-1"]
    )
    assert result.exit_code != 0


def test_timeout_message_names_the_timeout_not_the_overshoot(tmp_path: Path) -> None:
    host = _Host(alive_for=10**6)
    outcome = host.drain(_pidfile(tmp_path), timeout=1.2)
    assert outcome.code == 2
    assert "after 1.2s" in outcome.message


def test_any_other_os_error_from_the_signal_is_an_exit_code_not_a_traceback(
    tmp_path: Path,
) -> None:
    host = _Host(alive_for=1, kill_error=OSError(5, "input/output error"))
    outcome = host.drain(_pidfile(tmp_path))
    assert outcome.code == 1
    assert "input/output error" in outcome.message


def test_a_negative_timeout_is_a_usage_error(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["drain", "--config", str(_config(tmp_path)), "--timeout", "-1"]
    )
    assert result.exit_code == 2
