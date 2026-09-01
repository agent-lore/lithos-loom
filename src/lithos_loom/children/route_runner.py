"""Subprocess child that runs the bus + LithosEventStream + RouteRunners.

Spawned by the :class:`~lithos_loom.supervisor.Supervisor` per the
``route-runner`` :class:`~lithos_loom.supervisor.CategorySpec`. Owns one
:class:`~lithos_loom.bus.EventBus`, one
:class:`~lithos_loom.sources.lithos_event_stream.LithosEventStream`
consuming Lithos's ``/events`` SSE channel, one
:class:`~lithos_loom.subscriptions.route_runner.RouteRunner` per
configured route, and — for the needs-human escalation convention
(b91177d2) — one
:class:`~lithos_loom.subscriptions.escalation_resolver.EscalationResolver`
(re-dispatch nudge when the operator completes a loom-raised gate) plus the
:class:`~lithos_loom.notifications.Notifier` the runners fire when they raise
one. Runs until SIGTERM/SIGINT.

Invocation contract (set by the supervisor):

    python -m lithos_loom.children.route_runner --config <path>
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Sequence

import httpx

from lithos_loom.bus import EventBus
from lithos_loom.children import _boot
from lithos_loom.config import LoomConfig, load_config
from lithos_loom.cursor_store import CursorStore
from lithos_loom.github_client import GitHubClient, GitHubError
from lithos_loom.lithos_client import LithosClient
from lithos_loom.notifications import Notifier
from lithos_loom.sources.lithos_event_stream import LithosEventStream
from lithos_loom.subscriptions.escalation_resolver import EscalationResolver
from lithos_loom.subscriptions.route_runner import RouteRunner

logger = logging.getLogger(__name__)


async def _build_notifier(cfg: LoomConfig, http: httpx.AsyncClient) -> Notifier:
    """The push sinks for needs-human gates, from ``[notifications]``.

    The GitHub mention sink needs an operator login (``[story_develop]
    .operator_github_login``, #113) AND a working ``gh auth token``; either
    missing stands the sink down with a log line rather than failing the
    child — the gate + finding still land, only the push is lost.
    """
    notifications = cfg.notifications
    login = cfg.story_develop.operator_github_login if cfg.story_develop else None
    github: GitHubClient | None = None
    if notifications.github_mention and login:
        try:
            github = await GitHubClient.create(http=http)
        except GitHubError as exc:
            logger.warning(
                "route-runner child: github_mention notifications disabled — %s", exc
            )
    elif notifications.github_mention:
        logger.info(
            "route-runner child: github_mention notifications need "
            "[story_develop].operator_github_login; standing down"
        )
    return Notifier(
        desktop_toast=notifications.desktop_toast,
        command=notifications.on_needs_human,
        github_login=login if github is not None else None,
        github=github,
    )


async def _amain(cfg: LoomConfig) -> int:
    if not cfg.routes:
        logger.info("route-runner child: no routes configured; exiting cleanly")
        return 0

    bus = EventBus()
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    _boot.install_stop_signals(loop, stop_event.set)

    cursor_store = CursorStore(
        cfg.orchestrator.work_dir / "route-runner" / "sse_cursors.json"
    )

    async with (
        LithosClient(
            cfg.orchestrator.lithos_url, agent_id=cfg.orchestrator.agent_id
        ) as lithos,
        httpx.AsyncClient(timeout=30.0) as http,
    ):
        events_url = cfg.orchestrator.lithos_url.rstrip("/") + "/events"
        source = LithosEventStream(
            client=lithos,
            bus=bus,
            events_url=events_url,
            cursor_store=cursor_store,
            cursor_name="task-events",
        )
        notifier = await _build_notifier(cfg, http)
        project_repos = {slug: pc.repo for slug, pc in cfg.projects.items()}
        runners = [
            RouteRunner(
                route=route,
                bus=bus,
                lithos=lithos,
                agent_id=cfg.orchestrator.agent_id,
                work_dir_base=cfg.orchestrator.work_dir,
                retain_failed_workdirs=cfg.orchestrator.retain_failed_workdirs,
                project_repos=project_repos,
                notifier=notifier,
            )
            for route in cfg.routes
        ]
        resolver = EscalationResolver(
            bus=bus, lithos=lithos, agent_id=cfg.orchestrator.agent_id
        )
        logger.info(
            "route-runner child: starting event-stream + %d route runners (%s) "
            "+ escalation resolver",
            len(runners),
            ", ".join(r.route.name for r in runners),
        )

        tasks: list[asyncio.Task[None]] = [
            asyncio.create_task(source.run(), name="lithos-event-stream"),
            *(
                asyncio.create_task(r.run(), name=f"route-{r.route.name}")
                for r in runners
            ),
            asyncio.create_task(resolver.run(), name="escalation-resolver"),
        ]

        try:
            await stop_event.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _boot.parse_child_args("lithos_loom.children.route_runner", argv)
    # Load config first so we know what level to configure. Any
    # ConfigError that escapes here surfaces via Python's default
    # last-resort stderr handler before logging is up.
    cfg = load_config(args.config)
    _boot.configure_logging(cfg.orchestrator.log_level)
    try:
        return asyncio.run(_amain(cfg))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
