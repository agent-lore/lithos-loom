"""RouteRunner — claim-bound bus subscriber that runs plugins.

A special subscriber type sitting on the in-process :class:`EventBus`.
It listens for ``lithos.task.created`` / ``lithos.task.released`` events
whose tags match the route's ``RouteMatch.tags``, claims the task via
Lithos, runs the configured plugin subprocess, and applies the resulting
status:

* ``status="succeeded"`` → ``task_complete`` (releases all claims)
* ``status="failed"`` → ``task_release`` + ``[BlockerFailed]`` finding
* ``status="interrupted"`` → ``task_release`` (no finding — operator
  signal, not an error). When the result also carries a ``resume`` block
  (``resume_after`` timestamp — e.g. a story-develop run checkpointed on a
  provider usage limit), the runner schedules an in-process re-dispatch:
  at ``resume_after`` it re-checks the task is still open, drops it from
  the dedup set, and re-claims + re-runs. Bounded by
  ``MAX_RESUMES_PER_TASK``; the schedule is in-memory only (a daemon
  restart re-bootstraps open tasks anyway).

The runner is instantiated directly by the route-runner child entry
point (one runner per route) — it does **not** go through the
``lithos_loom.subscriptions`` entry-point registry, because routes have
distinct semantics (claim-bound, plugin-driven) from the generic fire-
and-forget subscriptions that registry serves. Routes and subscriptions
share an internal type but are distinct TOML stanzas.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lithos_loom.bus import Event, EventBus, Subscription
from lithos_loom.config import RouteConfig
from lithos_loom.errors import LithosClientError, PluginContractError
from lithos_loom.plugin_runner import run_plugin

__all__ = ["PluginRunFn", "RouteRunner"]

logger = logging.getLogger(__name__)


PluginRunFn = Callable[..., Awaitable[Mapping[str, Any]]]
"""Signature ``run_plugin`` exposes; injectable for tests."""


_HANDLED_EVENT_TYPES = (
    "lithos.task.created",
    "lithos.task.released",
)

# Re-dispatch budget for `interrupted` results carrying a `resume` block.
# Each resume re-runs the full plugin (container spin-up + agent spend), so
# a run that keeps hitting its provider limit must not retry unbounded —
# after this many resumes the task is left open with a [Friction] finding
# for the operator. Distinct from the failure retry budget (issue #11):
# resume is "try again after the limit lifts", not "retry a failure".
MAX_RESUMES_PER_TASK = 3


@dataclass
class RouteRunner:
    """One claim-bound subscriber per route.

    Attributes
    ----------
    route:
        The route configuration this runner serves. ``route.match.tags``
        becomes the bus filter; ``route.command`` is the plugin template;
        ``route.max_runtime_seconds`` caps each plugin invocation.
    bus:
        The in-process bus this runner subscribes against.
    lithos:
        A live :class:`lithos_loom.lithos_client.LithosClient` (or any
        object that quacks like one for tests).
    agent_id:
        The Lithos agent identity used for ``task_claim`` / ``task_renew``
        / ``task_release`` / ``task_complete`` / ``finding_post``.
    work_dir_base:
        Per-task staging directories are created at
        ``work_dir_base / <task_id>``.
    renew_interval_seconds:
        How often the renewer task calls ``task_renew``. Should be less
        than the claim TTL; defaults to 60s, matching the claim default.
    retain_failed_workdirs:
        When ``True`` (default), the work dir is left behind on plugin
        failure for operator inspection.
    plugin_runner:
        Injectable subprocess-runner. Defaults to the real
        :func:`lithos_loom.plugin_runner.run_plugin`. Tests inject an
        ``AsyncMock`` to bypass real subprocess work.
    """

    route: RouteConfig
    bus: EventBus
    lithos: Any
    agent_id: str
    work_dir_base: Path
    renew_interval_seconds: float = 60.0
    retain_failed_workdirs: bool = True
    plugin_runner: PluginRunFn = field(default=run_plugin)

    def __post_init__(self) -> None:
        self._subscription: Subscription = self.bus.subscribe(
            event_types=_HANDLED_EVENT_TYPES,
            match={"tags": list(self.route.match.tags)},
            name=f"route-runner-{self.route.name}",
        )
        # Tasks this runner has successfully claimed. Future events for
        # the same id are skipped — without this, multiple stale events
        # queued for the same open task would each run the plugin,
        # relying on Lithos's claim_failed envelope for safety. Real
        # Lithos enforces it, but the runner should too.
        #
        # Important: this set is also what suppresses re-attempts after a
        # plugin failure. When the plugin fails we release the claim and
        # post a [BlockerFailed] finding; Lithos then emits
        # lithos.task.released; that event hits this dedup check and is
        # silently skipped. The effect is "fail once per task per daemon
        # process" — deliberate, to avoid tight retry loops when a plugin
        # is deterministically broken. A proper retry budget lives in
        # follow-up issue #11. The lost-claim-race path below
        # (claim_failed) deliberately does NOT add to this set, so a
        # subsequent released event there does re-attempt the claim.
        self._processed_tasks: set[str] = set()
        # T10: pending usage-limit re-dispatches (task id → sleeper task)
        # and how many resumes each task has consumed. In-memory only:
        # a daemon restart loses the schedule, but the event-stream
        # bootstrap re-surfaces open tasks on startup anyway.
        self._resume_tasks: dict[str, asyncio.Task[None]] = {}
        self._resume_counts: dict[str, int] = {}

    @property
    def subscription(self) -> Subscription:
        return self._subscription

    async def run(self) -> None:
        """Drain the subscription forever. Cancellable."""
        while True:
            event = await self._subscription.queue.get()
            try:
                await self._handle(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "RouteRunner %s: unhandled error processing %s",
                    self.route.name,
                    event.type,
                )

    # ── handle ────────────────────────────────────────────────────────

    async def _handle(self, event: Event) -> None:
        payload = event.payload
        task_id = str(payload.get("id") or "")
        if not task_id:
            return
        if payload.get("status") != "open":
            return  # nothing to do for terminal-state observations
        if task_id in self._processed_tasks:
            logger.debug(
                "RouteRunner %s: skipping stale event for already-processed %s",
                self.route.name,
                task_id,
            )
            return

        depends_on = payload.get("metadata", {}).get("depends_on") or []
        if depends_on and not await self._deps_satisfied(depends_on):
            logger.info(
                "RouteRunner %s: deferring %s — dependencies not complete",
                self.route.name,
                task_id,
            )
            return

        try:
            await self.lithos.task_claim(
                task_id=task_id, aspect=self.route.name, agent=self.agent_id
            )
        except LithosClientError as exc:
            if exc.code == "claim_failed":
                # Another runner won the race. Don't add to processed —
                # if they release the claim, the lithos.task.released
                # event will land here again and we'll re-attempt. This
                # is the only path where released triggers a re-claim;
                # for the won-claim-then-plugin-fail path, see the
                # comment on _processed_tasks above (issue #11).
                logger.debug(
                    "RouteRunner %s: lost claim race for %s",
                    self.route.name,
                    task_id,
                )
                return
            raise

        # Claim succeeded; remember so duplicate queued events for the same
        # task ID are skipped rather than racing into a second plugin run.
        self._processed_tasks.add(task_id)
        logger.info("RouteRunner %s: claimed %s", self.route.name, task_id)
        await self._run_claimed_task(task_id, payload)

    async def _deps_satisfied(self, dep_ids: list[str]) -> bool:
        # Use task_get (post-lithos#294) — only ``.status`` is read here,
        # so skipping claim serialization with the dedicated single-task
        # endpoint is a small per-call efficiency win, plus we get an
        # explicit task_not_found envelope instead of an empty-list miss.
        for dep_id in dep_ids:
            status = await self.lithos.task_get(task_id=dep_id)
            if status is None or status.status != "completed":
                return False
        return True

    # ── claimed-task lifecycle ────────────────────────────────────────

    async def _run_claimed_task(self, task_id: str, payload: Mapping[str, Any]) -> None:
        work_dir = self.work_dir_base / task_id
        work_dir.mkdir(parents=True, exist_ok=True)
        task_json_path = work_dir / "task.json"
        result_file = work_dir / "result.json"
        task_json_path.write_text(json.dumps({"task": dict(payload)}))

        renew_task = asyncio.create_task(
            self._renew_loop(task_id), name=f"renew-{task_id}"
        )
        succeeded = False
        try:
            try:
                result = await self.plugin_runner(
                    command=self.route.command,
                    task_json_path=task_json_path,
                    work_dir=work_dir,
                    result_file=result_file,
                    max_runtime_seconds=self.route.max_runtime_seconds,
                )
            except PluginContractError as exc:
                await self._release_with_finding(
                    task_id,
                    f"plugin contract violation: {exc}",
                )
            except TimeoutError as exc:
                await self._release_with_finding(
                    task_id,
                    f"plugin exceeded max runtime: {exc}",
                )
            else:
                succeeded = await self._apply_result(task_id, result, payload)
        finally:
            renew_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renew_task
            # Cleanup runs on every exit path — success, plugin failure,
            # contract violation, timeout, even cancellation. The flag
            # decides whether failed dirs are retained for inspection.
            self._cleanup_work_dir(work_dir, success=succeeded)

    async def _renew_loop(self, task_id: str) -> None:
        while True:
            await asyncio.sleep(self.renew_interval_seconds)
            try:
                await self.lithos.task_renew(
                    task_id=task_id, aspect=self.route.name, agent=self.agent_id
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "RouteRunner %s: renew failed for %s",
                    self.route.name,
                    task_id,
                )
                # Don't crash; next renewal attempt may succeed. If the
                # claim has fully expired, the next operation will fail
                # cleanly and the [BlockerFailed] finding will surface.

    async def _apply_result(
        self,
        task_id: str,
        result: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> bool:
        """Apply the plugin's result. Returns ``True`` iff the task succeeded."""
        status = result.get("status")
        if status == "succeeded":
            await self.lithos.task_complete(task_id=task_id, agent=self.agent_id)
            logger.info("RouteRunner %s: completed %s", self.route.name, task_id)
            return True
        if status == "failed":
            err = result.get("error") or {}
            err_msg = err.get("message") if isinstance(err, dict) else None
            await self._release_with_finding(
                task_id,
                f"plugin reported failure: {err_msg or 'no error message'}",
            )
            return False
        if status == "interrupted":
            # Release the claim either way: a shutdown signal frees the task
            # for a future run; a usage-limit checkpoint must not hold the
            # claim across the (potentially hours-long) wait. No
            # [BlockerFailed] finding — neither case is an error.
            with contextlib.suppress(Exception):
                await self.lithos.task_release(
                    task_id=task_id,
                    aspect=self.route.name,
                    agent=self.agent_id,
                )
            logger.info(
                "RouteRunner %s: released %s (plugin interrupted)",
                self.route.name,
                task_id,
            )
            # T10: a `resume` block makes the interruption retryable — the
            # plugin says WHEN a re-run is expected to succeed.
            resume = result.get("resume")
            if isinstance(resume, Mapping):
                await self._maybe_schedule_resume(task_id, payload, resume)
            return False
        await self._release_with_finding(
            task_id,
            f"plugin returned unknown status {status!r}",
        )
        return False

    # ── usage-limit re-dispatch (T10) ─────────────────────────────────

    async def _maybe_schedule_resume(
        self,
        task_id: str,
        payload: Mapping[str, Any],
        resume: Mapping[str, Any],
    ) -> None:
        raw = resume.get("resume_after")
        try:
            resume_after = datetime.fromisoformat(str(raw))
        except ValueError:
            logger.warning(
                "RouteRunner %s: %s has unparseable resume_after %r; not "
                "re-dispatching",
                self.route.name,
                task_id,
                raw,
            )
            return
        if resume_after.tzinfo is None:
            resume_after = resume_after.replace(tzinfo=UTC)
        used = self._resume_counts.get(task_id, 0)
        if used >= MAX_RESUMES_PER_TASK:
            logger.warning(
                "RouteRunner %s: %s exhausted its resume budget (%d); leaving open",
                self.route.name,
                task_id,
                MAX_RESUMES_PER_TASK,
            )
            with contextlib.suppress(Exception):
                await self.lithos.finding_post(
                    task_id=task_id,
                    summary=(
                        f"[Friction] route {self.route.name}: usage-limited run "
                        f"resume budget exhausted ({MAX_RESUMES_PER_TASK} "
                        "re-dispatches); the task stays open for the operator"
                    ),
                    agent=self.agent_id,
                )
            return
        self._resume_counts[task_id] = used + 1
        existing = self._resume_tasks.get(task_id)
        if existing is not None and not existing.done():
            existing.cancel()
        delay = max(0.0, (resume_after - datetime.now(UTC)).total_seconds())
        logger.info(
            "RouteRunner %s: scheduling re-dispatch of %s in %.0fs "
            "(resume %d/%d, at %s)",
            self.route.name,
            task_id,
            delay,
            used + 1,
            MAX_RESUMES_PER_TASK,
            resume_after.isoformat(timespec="seconds"),
        )
        sleeper = asyncio.create_task(
            self._resume_dispatch(task_id, payload, delay),
            name=f"resume-{task_id}",
        )
        self._resume_tasks[task_id] = sleeper
        sleeper.add_done_callback(lambda _t: self._resume_tasks.pop(task_id, None))

    async def _resume_dispatch(
        self, task_id: str, payload: Mapping[str, Any], delay: float
    ) -> None:
        """Sleep until ``resume_after``, then re-claim + re-run the task."""
        try:
            await asyncio.sleep(delay)
            task = await self.lithos.task_get(task_id=task_id)
            if task is None or task.status != "open":
                logger.info(
                    "RouteRunner %s: %s no longer open at resume time; dropping",
                    self.route.name,
                    task_id,
                )
                return
            # Drop the dedup entry so _handle's claim path re-runs; the
            # synthetic event reuses the original payload (the task body is
            # re-read by the plugin from task.json either way).
            self._processed_tasks.discard(task_id)
            await self._handle(
                Event(
                    type="loom.route.resume",
                    timestamp=datetime.now(UTC),
                    payload=payload,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "RouteRunner %s: re-dispatch of %s failed",
                self.route.name,
                task_id,
            )

    async def _release_with_finding(self, task_id: str, detail: str) -> None:
        summary = f"[BlockerFailed] route {self.route.name}: {detail}"
        logger.info(
            "RouteRunner %s: releasing %s with finding: %s",
            self.route.name,
            task_id,
            detail,
        )
        try:
            await self.lithos.finding_post(
                task_id=task_id, summary=summary, agent=self.agent_id
            )
        except Exception:
            logger.exception(
                "RouteRunner %s: finding_post failed for %s",
                self.route.name,
                task_id,
            )
        try:
            await self.lithos.task_release(
                task_id=task_id, aspect=self.route.name, agent=self.agent_id
            )
        except Exception:
            logger.exception(
                "RouteRunner %s: task_release failed for %s",
                self.route.name,
                task_id,
            )

    def _cleanup_work_dir(self, work_dir: Path, *, success: bool) -> None:
        if success or not self.retain_failed_workdirs:
            with contextlib.suppress(OSError):
                shutil.rmtree(work_dir)
