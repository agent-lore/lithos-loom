"""Shared retry-with-backoff primitive for bus-draining consumers.

Both the declared-subscription consumer (:class:`SubscriptionRunner`) and
the github-watcher's hand-wired ``consume_push`` consumer retry a handler
call with exponential/linear backoff, then drop the event on persistent
failure. This module owns that control flow once (#237); each caller
supplies its own :class:`~lithos_loom.config.RetryPolicy`, per-attempt
logging, and give-up action — a Lithos ``[Friction]`` finding for the
runner, a logged drop for the watcher.

Only the retry *loop* is shared. Consumer wiring stays as it is: per
ADR 0007 the github-watcher is deliberately hand-wired (inline dispatch
with an 8192-deep queue + a periodic reconcile backstop), not routed
through ``build_runners``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from lithos_loom.config import RetryPolicy


async def run_with_retry(
    operation: Callable[[], Awaitable[None]],
    policy: RetryPolicy,
    *,
    on_attempt_failed: Callable[[int, Exception, float | None], None] | None = None,
    on_give_up: Callable[[BaseException | None], Awaitable[None]],
) -> None:
    """Run ``operation``, retrying up to ``policy.attempts`` times.

    On success, returns immediately. ``asyncio.CancelledError`` always
    propagates and is never retried — cancellation aborts the loop.

    On each failed attempt, ``on_attempt_failed`` (when given) is called
    with the zero-based attempt index, the exception, and the delay before
    the next attempt — ``None`` when this was the final attempt. When the
    delay is non-``None`` the loop then sleeps it.

    After the final attempt fails, ``on_give_up`` is awaited with the last
    exception and the call returns. A handler ``Exception`` is never
    re-raised: give-up (drop-and-continue) is the terminal behaviour, so a
    persistently-failing event doesn't kill the consumer task.
    """
    last_exc: BaseException | None = None
    for attempt in range(policy.attempts):
        try:
            await operation()
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_exc = exc
            is_final = attempt >= policy.attempts - 1
            next_delay = None if is_final else _backoff_delay(policy, attempt)
            if on_attempt_failed is not None:
                on_attempt_failed(attempt, exc, next_delay)
            if next_delay is not None:
                await asyncio.sleep(next_delay)
    await on_give_up(last_exc)


def _backoff_delay(policy: RetryPolicy, attempt: int) -> float:
    """Delay before the *next* attempt, given the just-failed ``attempt``."""
    if policy.backoff == "linear":
        delay = policy.initial_delay_seconds * (attempt + 1)
    else:  # exponential
        delay = policy.initial_delay_seconds * (2**attempt)
    return min(delay, policy.max_delay_seconds)
