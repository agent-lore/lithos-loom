"""Subprocess child that hosts the github-issue-watcher runtime.

Spawned by the :class:`~lithos_loom.supervisor.Supervisor` whenever
the loaded config carries a ``[github_watcher]`` table with
``enabled = true``. The supervisor gate is presence + enabled; this
child is responsible for everything below that line.

Single source + single subscription action. No allow-list filtering
because the watcher subscription is auto-wired here (not declared in
``[[subscriptions]]``) — the operator just flips the gate on and the
child sources, subscribes, and runs.

Invocation contract (set by the supervisor)::

    python -m lithos_loom.children.github_watcher --config <path>
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

import httpx

from lithos_loom.bus import EventBus
from lithos_loom.config import LogLevel, LoomConfig, load_config
from lithos_loom.github_client import GitHubClient
from lithos_loom.lithos_client import LithosClient
from lithos_loom.sources.github_issue_watcher import GitHubIssueWatcher
from lithos_loom.sources.lithos_note_stream import LithosNoteStream
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._github_issue_sync import (
    EVENT_TYPE as GITHUB_ISSUE_EVENT_TYPE,
)
from lithos_loom.subscriptions._github_issue_sync import (
    make_handler as make_github_issue_sync_handler,
)

_LEVEL_MAP: dict[LogLevel, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

logger = logging.getLogger(__name__)


def _configure_logging(level: LogLevel) -> None:
    logging.basicConfig(
        level=_LEVEL_MAP[level],
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Same noise suppression as obsidian-sync — the MCP SDK logs a
    # full traceback every Lithos reconnect.
    logging.getLogger("mcp.client.sse").setLevel(logging.CRITICAL)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="lithos_loom.children.github_watcher")
    parser.add_argument("--config", type=Path, default=None)
    return parser.parse_args(argv)


async def _amain(cfg: LoomConfig) -> int:
    """Body of the child. Returns the exit code."""
    if cfg.github_watcher is None or not cfg.github_watcher.enabled:
        # Defensive: the supervisor gate is the same condition. If we
        # land here, config drift removed the gate underneath us.
        logger.error(
            "github-watcher spawned without [github_watcher] enabled=true; exiting"
        )
        return 1

    gh_cfg = cfg.github_watcher
    logger.info(
        "github-watcher child started; poll_interval=%ds coord_doc=%s",
        gh_cfg.poll_interval_seconds,
        gh_cfg.coord_doc_path,
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[int] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)
            installed.append(sig)

    try:
        bus = EventBus()
        events_url = cfg.orchestrator.lithos_url.rstrip("/") + "/events"
        async with (
            httpx.AsyncClient(timeout=30.0) as http,
            LithosClient(
                cfg.orchestrator.lithos_url, agent_id=cfg.orchestrator.agent_id
            ) as lithos,
        ):
            github = await GitHubClient.create(http=http)
            watcher = GitHubIssueWatcher(
                github=github,
                lithos=lithos,
                bus=bus,
                poll_interval_seconds=gh_cfg.poll_interval_seconds,
                coord_doc_path=gh_cfg.coord_doc_path,
                agent_id=cfg.orchestrator.agent_id,
            )

            # LithosNoteStream feeds the watcher's _refresh_loop so an
            # operator running `project enable-github <slug>` takes
            # effect without a daemon restart.
            note_stream = LithosNoteStream(
                client=lithos,
                bus=bus,
                events_url=events_url,
            )

            ctx = SubscriptionContext(
                lithos=lithos,
                logger=logging.getLogger("lithos_loom.subscriptions"),
                agent_id=cfg.orchestrator.agent_id,
            )
            sync_handler = make_github_issue_sync_handler(github)
            sub = bus.subscribe(
                event_types=(GITHUB_ISSUE_EVENT_TYPE,),
                name="github-issue-sync",
                queue_size=512,
            )

            async def consume() -> None:
                """Drain the bus subscription, dispatch one event at a time.

                Hand-rolled rather than using ``SubscriptionRunner`` because
                that takes a ``SubscriptionConfig`` (TOML-driven retry policy)
                we don't have here — the watcher action isn't declared in
                ``[[subscriptions]]``; it's auto-wired by this child. The
                handler swallows its own errors as [Friction] logs.
                """
                while True:
                    event = await sub.queue.get()
                    try:
                        await sync_handler(event, ctx)
                    except Exception:
                        logger.exception("github-watcher: subscription handler raised")

            tasks: list[asyncio.Task[None]] = [
                asyncio.create_task(note_stream.run(), name="lithos-note-stream"),
                asyncio.create_task(watcher.run(), name="github-issue-watcher"),
                asyncio.create_task(consume(), name="github-issue-sync-consumer"),
            ]
            try:
                await stop_event.wait()
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        for sig in installed:
            with contextlib.suppress(NotImplementedError):
                loop.remove_signal_handler(sig)
        logger.info("github-watcher child stopping")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    cfg = load_config(args.config)
    _configure_logging(cfg.orchestrator.log_level)
    try:
        return asyncio.run(_amain(cfg))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
