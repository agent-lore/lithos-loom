"""Persistent dispatch guards for the route-runner.

Two checks decide whether an event may dispatch, both reading state that
survives a daemon restart (unlike the in-process ``_processed_tasks`` dedup):
Lithos's ready frontier (`on_ready_frontier`), and the failed-attempt marker
below.

Failed-attempt markers (6c4423a0):

A story whose plugin run FAILED stays open and unblocked on Lithos's ready
frontier, so without a persistent trace every daemon restart's bootstrap
replay would re-dispatch it at full cost (T1-S7 was developed three times
for $182.52 this way, the third run overrunning completed out-of-band
remediation). The runner therefore records each failure as
``metadata.loom_last_attempt:<route>`` on the task and declines a
**bootstrap-origin** event whose marker records a failed last attempt for
the same route — a restart is a process lifecycle event, not anyone asking
for another run.

One metadata key PER ROUTE (Lithos ``task_update`` merges per top-level
key), so two routes failing on the same task each keep their own guard —
a single shared key would let route B's write erase route A's protection.

Failure must stay retryable. Two retry gestures are guaranteed across a
restart, and the operator contract is exactly these:

1. **Delete the marker key** (``metadata.loom_last_attempt:<route>`` →
   null) — the canonical, always-works signal: no marker, no decline.
2. **Edit the task in ANY way.** The exact guard (#339, requires a
   lithos#415 server): at failure time the runner records the task's
   ``updated_at`` — taken from the marker write's own ``task_update``
   response, because that write is the failure path's last stamp-bumping
   mutation (``finding_post`` and claim/release don't bump; measured
   2026-08-29) — and the decline applies only while the bootstrap
   payload's ``updated_at`` still equals it. Any later mutation —
   metadata-only edits included, and reverted edits (every write bumps,
   so remove+re-add lands on a new stamp) — re-dispatches.

The recorded stamp lives in a loom-local :class:`AttemptStampStore`
(``<work_dir>/route-runner/attempt_stamps/``, beside the SSE cursor), NOT
in the marker: the marker cannot contain the stamp its own write creates,
and a fix-up write would just mint another. When the stamp is missing —
pre-#415 marker, wiped work dir, a server that omits ``updated_at`` — the
guard falls back to the original ``task_fingerprint`` of
title/description/tags, whose two documented blind spots (metadata-only
edits, reverted edits) then apply and the marker-deletion gesture covers
them. A stamp mismatch can also come from another agent's write (e.g. a
watcher mirroring metadata) — that fails OPEN into one re-dispatch, the
pre-guard behaviour, never a stuck task.

A live ``lithos.task.updated`` reaching a runner that has not suppressed
the task in-process, and the T10 resume re-dispatch, bypass the decline by
origin. A later gated delivery clears the marker
(``route_runner._gate_and_release``), and an ``interrupted`` run clears it
too — interrupted's designed recovery IS the restart bootstrap, which a
stale failure marker must not veto.

Reserved namespace: plugins see ``loom_last_attempt:*`` in their
``task.json`` metadata and must not repurpose it (SPECIFICATION §2.2).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

__all__ = [
    "LAST_ATTEMPT_KEY_PREFIX",
    "READY_QUERY_LIMIT",
    "AttemptStampStore",
    "clear_superseded_failure",
    "failed_attempt_for_route",
    "last_attempt_key",
    "on_ready_frontier",
    "record_failed_attempt",
    "release_with_failure",
    "task_fingerprint",
]

logger = logging.getLogger(__name__)

# Per-route task-metadata key prefix; the full key is
# ``loom_last_attempt:<route>`` and its value is
# {"status", "ended_at", "task_fingerprint", "run_id"?}.
LAST_ATTEMPT_KEY_PREFIX = "loom_last_attempt:"


def last_attempt_key(route: str) -> str:
    """The task-metadata key holding ``route``'s last failed attempt."""
    return f"{LAST_ATTEMPT_KEY_PREFIX}{route}"


def task_fingerprint(payload: Mapping[str, Any]) -> str:
    """Fingerprint of the operator-shaped task fields (title, description,
    tags — order-insensitive).

    Stored in the failure marker and compared at bootstrap: a differing
    fingerprint means the task was edited since the failure, which is the
    operator's deliberate-retry gesture, so the decline does not apply.
    Metadata is deliberately excluded — plugins write metadata at will
    (``develop_*``, and the marker itself), and none of that is an operator
    asking for another run. The cost of that exclusion, and of state
    comparison generally (metadata-only edits and reverted edits are
    invisible), is documented in the module docstring: the marker-deletion
    gesture is the contract for those.
    """
    material = json.dumps(
        {
            "title": payload.get("title") or "",
            "description": payload.get("description") or "",
            "tags": sorted(str(t) for t in (payload.get("tags") or ())),
        },
        sort_keys=True,
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16]


# Page size for the `task_ready` membership query (US4). `task_ready` has no
# per-task filter — the runner asks for the frontier and looks for its task on
# it — so the page must be big enough to hold a realistic frontier. The query
# is already narrowed to one route's tags and one project, but that can still
# be large: a decomposed PRD's parallel stories all carry the same trigger tag
# and all become ready at once. The default limit of 50 would quietly truncate
# such a frontier, so ask for far more and treat a full page as undetermined
# rather than as "not ready" (see `on_ready_frontier`).
READY_QUERY_LIMIT = 500


class _GuardClient(Protocol):
    async def task_update(
        self,
        *,
        task_id: str,
        agent: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any: ...

    async def finding_post(
        self, *, task_id: str, summary: str, agent: str | None = None
    ) -> Any: ...

    async def task_release(
        self, *, task_id: str, aspect: str, agent: str | None = None
    ) -> Any: ...

    async def task_ready(
        self,
        *,
        project: str | None = None,
        tags: list[str] | None = None,
        metadata_match: dict[str, Any] | None = None,
        limit: int = 50,
        with_claims: bool = True,
    ) -> Any: ...


async def on_ready_frontier(
    lithos: _GuardClient,
    *,
    task_id: str,
    tags: tuple[str, ...],
    metadata: Mapping[str, Any],
    route: str,
) -> bool:
    """Is ``task_id`` on Lithos's ready frontier for this route? (US4)

    Readiness — every ``blocks`` predecessor completed, no unmet gate, no
    cycle — is computed once, server-side, and shared with every other
    agent. The runner no longer mirrors it from ``metadata.depends_on``.

    ``task_ready`` has no per-task filter, so this is a membership test
    over a frontier narrowed to the route's tags and (when the task
    declares one) its project. A *full* page means the frontier was
    truncated, which makes absence from it meaningless — so that case is
    reported as not-ready-yet rather than trusted, and logged. Deferring
    is the safe direction: the inverse mistake would dispatch a task whose
    blocker is still open, which is exactly what this gate exists to stop.
    """
    project = metadata.get("project")
    ready = await lithos.task_ready(
        tags=list(tags),
        project=project if isinstance(project, str) else None,
        limit=READY_QUERY_LIMIT,
        # Claims never exclude a task from the frontier (collision-safety
        # comes from the runner's atomic claim), so don't pay to fetch them.
        with_claims=False,
    )
    if any(task.id == task_id for task in ready):
        return True
    if len(ready) >= READY_QUERY_LIMIT:
        logger.warning(
            "RouteRunner %s: ready frontier for tags %s hit the %d-task query "
            "limit, so %s's readiness is undetermined — deferring. Raise "
            "READY_QUERY_LIMIT if a frontier this wide is expected.",
            route,
            list(tags),
            READY_QUERY_LIMIT,
            task_id,
        )
    return False


def _fs_slug(value: str, *, max_len: int = 60) -> str:
    """A filename-safe, human-readable rendering of *value* (NOT unique —
    always pair with a digest of the raw value). Strips leading dots so a
    name can neither hide as a dotfile (the vault convention reserves those
    for temp files) nor read as a ``.``/``..`` component."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", value)[:max_len].lstrip(".")
    return slug or "_"


class AttemptStampStore:
    """Loom-local store for each failed attempt's ``updated_at`` stamp (#339).

    One file per ``(route, task)`` under *root*, holding the ISO stamp the
    marker write's ``task_update`` response returned. Loom-local because the
    stamp CANNOT live in the marker — the marker write is what mints it, and
    a second write to fold it in would mint another (the self-reference the
    probe of 2026-08-29 confirmed: every ``task_update`` bumps).

    Same durability class as the runner's SSE cursor, deliberately: survives
    daemon restarts, lost on a work-dir wipe — and a lost stamp only demotes
    the guard to fingerprint semantics, never strands or double-runs a task.
    All operations are best-effort (log + swallow) and synchronous — no
    awaits, so concurrent routes on one event loop cannot interleave a
    read-modify-write.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, route: str, task_id: str) -> Path:
        # Neither half is filename-safe by contract: a route name is any
        # non-empty string (config validates nothing more — "team/story" is
        # legal and would silently break every write via a nonexistent
        # subdir), and a task id arrives off an event payload. Sanitized
        # prefixes keep the store inspectable; the digest of the RAW pair
        # keeps distinct keys distinct however the sanitizer collides them,
        # and confines every name to one component under the root.
        digest = hashlib.sha256(f"{route}\x00{task_id}".encode()).hexdigest()[:12]
        return self.root / f"{_fs_slug(route)}--{_fs_slug(task_id)}--{digest}"

    def record(self, route: str, task_id: str, stamp: str) -> None:
        """Persist *stamp* atomically (tmp + rename, the repo convention)."""
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            final = self._path(route, task_id)
            # Not .with_suffix(): a dot in a sanitized name would make it
            # REPLACE the tail, colliding two keys' staging files.
            tmp = final.with_name(final.name + ".tmp")
            tmp.write_text(stamp, encoding="utf-8")
            os.replace(tmp, final)
        except OSError:
            logger.exception(
                "route %s: recording attempt stamp for %s failed", route, task_id
            )

    def read(self, route: str, task_id: str) -> str | None:
        try:
            return self._path(route, task_id).read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        except OSError:
            logger.exception(
                "route %s: reading attempt stamp for %s failed", route, task_id
            )
            return None

    def clear(self, route: str, task_id: str) -> None:
        try:
            self._path(route, task_id).unlink(missing_ok=True)
        except OSError:
            logger.exception(
                "route %s: clearing attempt stamp for %s failed", route, task_id
            )


def failed_attempt_for_route(
    metadata: Mapping[str, Any], route: str
) -> Mapping[str, Any] | None:
    """The task's last-attempt marker, iff it records a FAILURE for ``route``."""
    last = metadata.get(last_attempt_key(route))
    if isinstance(last, Mapping) and last.get("status") == "failed":
        return last
    return None


def declines_bootstrap_replay(
    metadata: Mapping[str, Any],
    route: str,
    payload: Mapping[str, Any],
    *,
    task_id: str,
    stamps: AttemptStampStore | None = None,
) -> bool:
    """True iff a bootstrap replay of this payload must be declined (logged).

    Declines when ``route``'s last attempt failed AND the task is unchanged
    since that failure. "Unchanged" is decided by the strongest evidence
    available:

    - **Exact (#339):** a recorded ``updated_at`` stamp in *stamps* plus a
      stamp on the payload — decline iff equal. ANY later mutation
      (metadata-only and reverted edits included) mismatches and
      dispatches.
    - **Fallback:** fingerprint match over title/description/tags — a
      marker without a fingerprint (e.g. from a partial write) is treated
      as unchanged: fail closed. Applies when either stamp is missing
      (pre-#415 marker or server, wiped store).

    An edited task dispatches: the edit is the deliberate-retry gesture.
    """
    last = failed_attempt_for_route(metadata, route)
    if last is None:
        return False
    recorded_stamp = stamps.read(route, task_id) if stamps is not None else None
    payload_stamp = payload.get("updated_at")
    if recorded_stamp is not None and isinstance(payload_stamp, str) and payload_stamp:
        if payload_stamp != recorded_stamp:
            return False
        logger.info(
            "RouteRunner %s: declining bootstrap replay of %s — last attempt "
            "failed (%s) and the task's updated_at is unchanged since (no "
            "edit of any kind). Edit the task to retry, or delete "
            "metadata.%s.",
            route,
            task_id,
            last.get("ended_at"),
            last_attempt_key(route),
        )
        return True
    recorded = last.get("task_fingerprint")
    if recorded is not None and recorded != task_fingerprint(payload):
        return False
    logger.info(
        "RouteRunner %s: declining bootstrap replay of %s — last attempt "
        "failed (%s) and the task's title/description/tags are unchanged "
        "since. Delete metadata.%s to retry (always works — metadata-only "
        "or reverted edits are not detected), or edit the task to a new "
        "state.",
        route,
        task_id,
        last.get("ended_at"),
        last_attempt_key(route),
    )
    return True


async def record_failed_attempt(
    lithos: _GuardClient,
    *,
    task_id: str,
    route: str,
    agent: str,
    payload: Mapping[str, Any],
    run_id: str | None = None,
    stamps: AttemptStampStore | None = None,
) -> None:
    """Best-effort persist the failed attempt on the task.

    Called BEFORE the claim release so the record exists by the time the task
    is externally visible as unclaimed. A Lithos hiccup here must never mask
    the ``[BlockerFailed]`` finding or the release, so failures are logged
    and swallowed. ``task_update`` metadata is an additive per-key merge, so
    this cannot clobber ``develop_status``, ``pr_gate_id``, or any
    plugin-written key.

    On a lithos#415 server the update response returns the write's own
    ``updated_at`` — the stamp of the failure path's LAST bumping mutation
    (``finding_post`` before it and ``task_release`` after it don't bump) —
    which is recorded in *stamps* for the exact edited-since comparison. A
    missing return (older server) just leaves the fingerprint fallback.
    """
    marker: dict[str, Any] = {
        "status": "failed",
        "ended_at": datetime.now(UTC).isoformat(),
        "task_fingerprint": task_fingerprint(payload),
    }
    if run_id:
        marker["run_id"] = run_id
    key = last_attempt_key(route)
    try:
        stamp = await lithos.task_update(
            task_id=task_id,
            agent=agent,
            metadata={key: marker},
        )
    except Exception:
        logger.exception("route %s: recording %s on %s failed", route, key, task_id)
        return
    if stamps is None:
        return
    if isinstance(stamp, datetime):
        stamps.record(route, task_id, stamp.isoformat())
    elif isinstance(stamp, str) and stamp:
        stamps.record(route, task_id, stamp)
    else:
        # No stamp from this write (pre-#415 server): drop any stamp a
        # PREVIOUS failure left, or it would speak for this one — a stale
        # stamp always mismatches, which would nullify the guard into a
        # dispatch on every restart. Absent stamp = fingerprint fallback.
        stamps.clear(route, task_id)


async def release_with_failure(
    lithos: _GuardClient,
    *,
    task_id: str,
    route: str,
    agent: str,
    detail: str,
    payload: Mapping[str, Any],
    run_id: Any = None,
    stamps: AttemptStampStore | None = None,
) -> None:
    """The whole failure-path release: marker (+stamp), finding, release.

    Order is load-bearing for the exact guard: the marker write is the LAST
    stamp-bumping mutation (``finding_post`` and ``task_release`` after it
    don't bump — measured 2026-08-29), so the stamp it returns still equals
    the task's ``updated_at`` when the dust settles. Every step is
    best-effort and independently logged — a Lithos hiccup on one must not
    mask the others.
    """
    summary = f"[BlockerFailed] route {route}: {detail}"
    logger.info("RouteRunner %s: releasing %s with finding: %s", route, task_id, detail)
    await record_failed_attempt(
        lithos,
        task_id=task_id,
        route=route,
        agent=agent,
        payload=payload,
        run_id=run_id if isinstance(run_id, str) else None,
        stamps=stamps,
    )
    try:
        await lithos.finding_post(task_id=task_id, summary=summary, agent=agent)
    except Exception:
        logger.exception("RouteRunner %s: finding_post failed for %s", route, task_id)
    try:
        await lithos.task_release(task_id=task_id, aspect=route, agent=agent)
    except Exception:
        logger.exception("RouteRunner %s: task_release failed for %s", route, task_id)


async def clear_superseded_failure(
    lithos: _GuardClient,
    *,
    task_id: str,
    route: str,
    agent: str,
    payload: Mapping[str, Any],
    stamps: AttemptStampStore | None = None,
) -> None:
    """Best-effort per-key delete of ``route``'s failed-attempt marker, iff
    the dispatch-time ``payload`` carried one (no round trip otherwise).

    Used when an ``interrupted`` run supersedes an earlier failure: the
    restart bootstrap is interrupted's designed recovery path, and a stale
    failure marker must not veto it. Failures are logged and swallowed —
    same contract as :func:`record_failed_attempt`. The local stamp is
    cleared alongside the marker (a stamp without a marker is inert, but a
    stale one must never outlive its marker into a future failure).
    """
    if failed_attempt_for_route(payload.get("metadata") or {}, route) is None:
        return
    key = last_attempt_key(route)
    try:
        await lithos.task_update(
            task_id=task_id,
            agent=agent,
            metadata={key: None},
        )
    except Exception:
        logger.exception("route %s: clearing %s on %s failed", route, key, task_id)
        return
    if stamps is not None:
        stamps.clear(route, task_id)
