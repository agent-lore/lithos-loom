"""Idempotency-key short-circuit for ``story-develop`` daemon runs (US-18).

A daemon-mode run is keyed by ``--idempotency-key`` (default: the task id from
``task.json``). The first run under a key that ends ``succeeded`` records its
``result.json`` payload in a host-persistent store; a later run under the same
key replays that recorded payload verbatim — no second agent loop, no second
PR — instead of re-developing the task.

**Store location.** One JSON file per key under::

    $LITHOS_LOOM_IDEMPOTENCY_DIR                         # explicit override
    $XDG_STATE_HOME/lithos-loom/story-develop/idempotency
    ~/.local/state/lithos-loom/story-develop/idempotency # default

The file name is ``<sha256(key)>.json`` so an arbitrary key (a task id, or any
operator-supplied string) is always a safe filename. Persistence is
deliberately on disk, not in the per-task ``work_dir`` (which the runner
overwrites on each dispatch), so the short-circuit survives across invocations
and daemon restarts.

**What short-circuits.** Only a record that is itself a *completed/succeeded*
result (``status == "succeeded"`` and ``exit_code == 0``) replays. A failed,
interrupted, or malformed record — even one that merely *claims* success —
is ignored so the task stays retriable. The recorder mirrors this: it writes a
record only for a succeeded run, so a failed/interrupted run leaves no marker.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...plugin_runner import write_result_atomically

if TYPE_CHECKING:
    from collections.abc import Mapping

#: Env override for the idempotency store directory (absolute path).
STORE_DIR_ENV = "LITHOS_LOOM_IDEMPOTENCY_DIR"


def store_dir() -> Path:
    """Resolve the host-persistent idempotency store directory.

    ``$LITHOS_LOOM_IDEMPOTENCY_DIR`` wins; otherwise the XDG state dir
    (``$XDG_STATE_HOME`` or ``~/.local/state``) under
    ``lithos-loom/story-develop/idempotency``.
    """
    override = os.environ.get(STORE_DIR_ENV)
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "lithos-loom" / "story-develop" / "idempotency"


def _record_path(key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return store_dir() / f"{digest}.json"


def _is_completed_record(payload: object) -> bool:
    """True only for a well-formed prior *succeeded* result payload.

    Defensive on purpose (AC4): a record that is not an object, or whose
    ``status`` / ``exit_code`` do not jointly say "completed success", is
    treated as absent so the task re-runs rather than replaying a lie.
    """
    return (
        isinstance(payload, dict)
        and payload.get("status") == "succeeded"
        and payload.get("exit_code") == 0
    )


def lookup_completed(key: str) -> dict[str, Any] | None:
    """Return the recorded result payload for *key* iff it is a completed run.

    Returns ``None`` when no record exists, the record is unreadable/malformed,
    or it is not a completed/succeeded run — in every such case the caller
    should run normally (the task stays retriable).
    """
    path = _record_path(key)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not _is_completed_record(payload):
        return None
    return payload


def record_completion(key: str, payload: Mapping[str, Any]) -> None:
    """Persist *payload* as the completed-run record for *key* (atomic write).

    Only meaningful for a succeeded payload; the caller gates on that. The
    write reuses the plugin runner's temp+fsync+rename helper so a partial
    record is never observable.
    """
    write_result_atomically(_record_path(key), dict(payload))
