"""The develop round pipeline's shared injection seam (ARCH-1.S4).

:class:`Services` is the frozen bundle of side-effecting seams the round
machinery calls through instead of reaching module globals directly, so the loop
is unit-testable by constructing a ``Services`` with fakes.

:meth:`Services.live` wires the real modules with **call-time** attribute
lookups, so a ``monkeypatch`` of ``turns.run_turn`` / ``containers.start_container``
/ … is honoured even after the ``Services`` instance is built. ``develop()`` does
*not* use ``live()`` yet — it constructs a ``Services`` from its own module
globals so the existing ``monkeypatch.setattr(develop_mod, "run_turn"/"_sleep"/…)``
patches keep taking effect until S8 re-points the tests (see the compat note in
:mod:`develop`).

S4 introduces the seam and threads it through
:func:`agent_session.turn_with_limit_pauses` (which reads ``run_turn`` +
``sleep``); S6 grows this module into the round/phase pipeline that consumes the
rest.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from . import check_runner, containers, turns
from .check_set import CheckSetResult
from .turns import TurnResult


@dataclass(frozen=True)
class Services:
    """The side-effecting seams the round pipeline depends on, injected so the
    loop is testable with fakes (ARCH-1.S4).

    ``run_turn`` and ``sleep`` are consumed by
    :func:`agent_session.turn_with_limit_pauses` today; ``start_container`` /
    ``stop_container`` / ``run_check_set`` are wired now for the S6 phase
    pipeline.
    """

    run_turn: Callable[..., TurnResult]
    sleep: Callable[[float], None]
    start_container: Callable[[Sequence[str]], str]
    stop_container: Callable[[str], None]
    run_check_set: Callable[..., CheckSetResult | None]

    @classmethod
    def live(cls) -> Services:
        """Wire the real modules. Each field defers its module-attr lookup to
        call time, so monkeypatching ``turns.run_turn`` / ``containers.*`` is
        still honoured after this ``Services`` is constructed."""
        return cls(
            run_turn=lambda **kw: turns.run_turn(**kw),
            sleep=lambda seconds: time.sleep(seconds),
            start_container=lambda cmd: containers.start_container(cmd),
            stop_container=lambda name: containers.stop_container(name),
            run_check_set=lambda *a, **k: check_runner.run_check_set(*a, **k),
        )
