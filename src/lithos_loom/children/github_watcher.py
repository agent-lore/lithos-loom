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
from datetime import timedelta
from pathlib import Path

import httpx

from lithos_loom.bus import EventBus
from lithos_loom.config import LogLevel, LoomConfig, load_config
from lithos_loom.github_client import GitHubClient
from lithos_loom.lithos_client import LithosClient
from lithos_loom.sources.github_issue_watcher import GitHubIssueWatcher
from lithos_loom.sources.lithos_event_stream import LithosEventStream
from lithos_loom.sources.lithos_note_stream import LithosNoteStream
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._github_issue_push import (
    EVENT_TYPES as LITHOS_TASK_EVENT_TYPES,
)
from lithos_loom.subscriptions._github_issue_push import (
    make_handler as make_github_issue_push_handler,
)
from lithos_loom.subscriptions._github_issue_sync import (
    EVENT_TYPE as GITHUB_ISSUE_EVENT_TYPE,  # noqa: F401 — re-exported for the child wiring smoke test
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

# Mirror route_runner: httpx logs every HTTP request at INFO — every
# Lithos MCP POST AND every GitHub API GET/PATCH — which drowns out the
# watcher's own per-cycle progress messages. At ``debug`` the operator
# asked for the firehose; otherwise pin to WARNING.
_NOISY_LIBRARY_LOGGERS = ("httpx", "httpx_sse")

logger = logging.getLogger(__name__)


def _configure_logging(level: LogLevel) -> None:
    logging.basicConfig(
        level=_LEVEL_MAP[level],
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if level == "debug":
        for name in _NOISY_LIBRARY_LOGGERS:
            logging.getLogger(name).setLevel(logging.NOTSET)
    else:
        for name in _NOISY_LIBRARY_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)
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
            ctx = SubscriptionContext(
                lithos=lithos,
                logger=logging.getLogger("lithos_loom.subscriptions"),
                agent_id=cfg.orchestrator.agent_id,
            )
            sync_handler = make_github_issue_sync_handler(github)

            async def dispatch_to_sync_handler(event: object) -> None:
                """Inline dispatch path for the GH→Lithos sync handler.

                Bypasses the bus so a queue-full drop can't lose work and
                the watcher source can hold its cursor at the last
                successfully reconciled issue (PR-review finding 1,
                2026-05-30). The handler still posts [Friction] logs
                internally for recoverable problems; any unhandled
                exception bubbles up, the watcher logs it, and the
                cursor freezes — the next poll re-fetches from the
                stuck boundary.
                """
                from lithos_loom.bus import Event as _Event

                assert isinstance(event, _Event)
                await sync_handler(event, ctx)

            watcher = GitHubIssueWatcher(
                github=github,
                lithos=lithos,
                bus=bus,
                poll_interval_seconds=gh_cfg.poll_interval_seconds,
                coord_doc_path=gh_cfg.coord_doc_path,
                agent_id=cfg.orchestrator.agent_id,
                dispatch=dispatch_to_sync_handler,
            )

            # LithosNoteStream feeds the watcher's _refresh_loop so an
            # operator running `project enable-github <slug>` takes
            # effect without a daemon restart.
            note_stream = LithosNoteStream(
                client=lithos,
                bus=bus,
                events_url=events_url,
            )

            # LithosEventStream is the push half: it surfaces
            # task.{completed,cancelled,updated} onto the in-process bus
            # so the push handler can mirror those into the linked GH
            # issue. Replay recently-resolved tasks at bootstrap so a
            # Lithos task that closed (or got renamed) while the watcher
            # was down still gets mirrored to GH on restart. The GH→Lithos
            # polling direction is one-way (it can detect a Lithos task
            # gone terminal but doesn't itself close the GH issue — it
            # only posts a [ReopenRequested] finding on the reverse). The
            # push handler is idempotent (refetches before PATCH), so a
            # too-large window only costs harmless re-checks.
            replay_window = (
                timedelta(days=gh_cfg.resolved_replay_days)
                if gh_cfg.resolved_replay_days > 0
                else None
            )
            event_stream = LithosEventStream(
                client=lithos,
                bus=bus,
                events_url=events_url,
                bootstrap_resolved_window=replay_window,
            )

            push_handler = make_github_issue_push_handler(github)
            push_sub = bus.subscribe(
                event_types=LITHOS_TASK_EVENT_TYPES,
                name="github-issue-push",
                queue_size=512,
            )

            async def consume_push() -> None:
                """Drain the Lithos→GH subscription. Same hand-rolled shape."""
                while True:
                    event = await push_sub.queue.get()
                    try:
                        await push_handler(event, ctx)
                    except Exception:
                        logger.exception(
                            "github-watcher: push subscription handler raised"
                        )

            tasks: list[asyncio.Task[None]] = [
                asyncio.create_task(note_stream.run(), name="lithos-note-stream"),
                asyncio.create_task(event_stream.run(), name="lithos-event-stream"),
                asyncio.create_task(watcher.run(), name="github-issue-watcher"),
                asyncio.create_task(consume_push(), name="github-issue-push-consumer"),
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
