"""Test-only echo child.

Sleeps until SIGTERM; the supervisor's behavioural tests use this as a
stand-in for real category children. Flags exist purely to drive
failure-path tests:

* ``--config PATH`` — accepted to match the supervisor's invocation contract;
  unused.
* ``--crash-after FLOAT`` — exit non-zero after N seconds (simulate crash).
* ``--exit-after FLOAT`` — exit 0 after N seconds on its own (a child that
  leaves without being asked — a crash to the supervisor, exit code aside).
* ``--ignore-sigterm-for FLOAT`` — install a no-op SIGTERM handler for N
  seconds before re-installing the default handler (simulate a slow-to-stop
  child the supervisor must wait on).
* ``--echo-argv`` — write ``sys.argv`` to stderr on startup for argv tests.
* ``--exit-on-drain-after FLOAT`` — on SIGUSR1 (the supervisor's relayed
  drain), exit 0 after N seconds (simulate a child finishing its in-flight
  run before exiting).
* ``--crash-on-drain`` — on SIGUSR1, exit 2 (simulate a crash mid-drain).
* ``--ignore-first-drain`` — the first SIGUSR1 does nothing (a child still
  importing when it landed); the second exits 0.

This module deliberately lives under a leading-underscore name so it is
clearly a private testing artefact, not a public child category.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="lithos_loom.children._echo")
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--crash-after", type=float, default=None)
    parser.add_argument("--exit-after", type=float, default=None)
    parser.add_argument("--ignore-sigterm-for", type=float, default=0.0)
    parser.add_argument("--echo-argv", action="store_true")
    parser.add_argument("--exit-on-drain-after", type=float, default=None)
    parser.add_argument("--crash-on-drain", action="store_true")
    parser.add_argument("--ignore-first-drain", action="store_true")
    return parser.parse_args(argv)


async def _amain(args: argparse.Namespace) -> int:
    if args.echo_argv:
        print(" ".join(sys.argv), file=sys.stderr, flush=True)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    if args.ignore_sigterm_for > 0:
        # On first SIGTERM, defer the actual stop by ``ignore_sigterm_for``
        # seconds. Subsequent SIGTERMs are no-ops. This simulates a child
        # that takes a moment to honour the signal — used by the supervisor's
        # patient-shutdown and force-kill tests.
        def _defer_stop() -> None:
            loop.call_later(args.ignore_sigterm_for, stop_event.set)
            loop.add_signal_handler(signal.SIGTERM, lambda: None)

        loop.add_signal_handler(signal.SIGTERM, _defer_stop)
    else:
        loop.add_signal_handler(signal.SIGTERM, stop_event.set)

    loop.add_signal_handler(signal.SIGINT, stop_event.set)

    exit_code = 0
    if args.crash_on_drain:

        def _crash_on_drain() -> None:
            nonlocal exit_code
            exit_code = 2
            stop_event.set()

        loop.add_signal_handler(signal.SIGUSR1, _crash_on_drain)
    elif args.ignore_first_drain:
        drains = {"n": 0}

        def _second_drain_stops() -> None:
            drains["n"] += 1
            if drains["n"] >= 2:
                stop_event.set()

        loop.add_signal_handler(signal.SIGUSR1, _second_drain_stops)
    elif args.exit_on_drain_after is not None:
        loop.add_signal_handler(
            signal.SIGUSR1,
            lambda: loop.call_later(args.exit_on_drain_after, stop_event.set),
        )

    # Signal that signal handlers are installed and the child is ready.
    # Tests read this line to avoid a race between SIGTERM and handler setup.
    print("ready", file=sys.stderr, flush=True)

    if args.crash_after is not None:

        async def _crash() -> None:
            await asyncio.sleep(args.crash_after)
            sys.exit(2)

        asyncio.create_task(_crash())
    if args.exit_after is not None:
        loop.call_later(args.exit_after, stop_event.set)

    await stop_event.wait()
    return exit_code


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
