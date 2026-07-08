"""The develop round pipeline's shared injection seam (ARCH-1.S4).

:class:`Services` is the frozen bundle of side-effecting seams the round
machinery calls through instead of reaching module globals directly, so the loop
is unit-testable by constructing a ``Services`` with fakes.

:meth:`Services.live` wires the real module callables — captured when it is
built. Both it and ``develop()``'s own ``_develop_services()`` are constructed at
``develop()`` start, *after* any test applies its ``monkeypatch.setattr`` of
``turns.run_turn`` / ``containers.start_container`` / ``develop_mod.run_turn`` / … ,
so each field binds the patched callable (a patch applied *after* construction is
not observed — nothing does that). ``develop()`` does *not* use ``live()`` yet —
it builds a ``Services`` from its own module globals so the existing
``monkeypatch.setattr(develop_mod, "run_turn"/"_sleep"/…)`` patches keep taking
effect until S8 re-points the tests (see the compat note in :mod:`develop`).

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
        """The concrete production seams — the real module callables. Built at
        ``develop()`` start (once S8 switches to it), *after* any test patch of
        ``turns.run_turn`` / ``containers.*`` is applied, so each field captures
        the patched callable."""
        return cls(
            run_turn=turns.run_turn,
            sleep=time.sleep,
            start_container=containers.start_container,
            stop_container=containers.stop_container,
            run_check_set=check_runner.run_check_set,
        )
