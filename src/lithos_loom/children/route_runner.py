"""Subprocess child that runs the bus + LithosEventStream + RouteRunners.

Spawned by the :class:`~lithos_loom.supervisor.Supervisor` per the
``route-runner`` :class:`~lithos_loom.supervisor.CategorySpec`. Owns one
:class:`~lithos_loom.bus.EventBus`, one
:class:`~lithos_loom.sources.lithos_event_stream.LithosEventStream`
consuming Lithos's ``/events`` SSE channel, and one
:class:`~lithos_loom.subscriptions.route_runner.RouteRunner` per
configured route. Runs until SIGTERM/SIGINT.

Invocation contract (set by the supervisor):

    python -m lithos_loom.children.route_runner --config <path>
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from lithos_loom.bus import EventBus
from lithos_loom.config import LoomConfig, load_config
from lithos_loom.lithos_client import LithosClient
from lithos_loom.sources.lithos_event_stream import LithosEventStream
from lithos_loom.subscriptions.route_runner import RouteRunner

logger = logging.getLogger(__name__)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="lithos_loom.children.route_runner")
    parser.add_argument("--config", type=Path, default=None)
    return parser.parse_args(argv)


async def _amain(cfg: LoomConfig) -> int:
    if not cfg.routes:
        logger.info("route-runner child: no routes configured; exiting cleanly")
        return 0

    bus = EventBus()
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    async with LithosClient(
        cfg.orchestrator.lithos_url, agent_id=cfg.orchestrator.agent_id
    ) as lithos:
        events_url = cfg.orchestrator.lithos_url.rstrip("/") + "/events"
        source = LithosEventStream(
            client=lithos,
            bus=bus,
            events_url=events_url,
        )
        runners = [
            RouteRunner(
                route=route,
                bus=bus,
                lithos=lithos,
                agent_id=cfg.orchestrator.agent_id,
                work_dir_base=cfg.orchestrator.work_dir,
                retain_failed_workdirs=cfg.orchestrator.retain_failed_workdirs,
            )
            for route in cfg.routes
        ]
        logger.info(
            "route-runner child: starting event-stream + %d route runners (%s)",
            len(runners),
            ", ".join(r.route.name for r in runners),
        )

        tasks: list[asyncio.Task[None]] = [
            asyncio.create_task(source.run(), name="lithos-event-stream"),
            *(
                asyncio.create_task(r.run(), name=f"route-{r.route.name}")
                for r in runners
            ),
        ]

        try:
            await stop_event.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx / httpx-sse log every HTTP request at INFO — one POST per
    # MCP tool call (claim, complete, renew, finding_post…) plus the
    # SSE GET. Demote to WARNING so the operator can read the source +
    # subscriber lifecycle without grepping past per-call noise.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx_sse").setLevel(logging.WARNING)
    cfg = load_config(args.config)
    try:
        return asyncio.run(_amain(cfg))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
