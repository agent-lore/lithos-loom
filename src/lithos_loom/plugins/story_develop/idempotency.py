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
result replays. Three gates, all required (AC4): it claims success
(``status == "succeeded"`` and ``exit_code == 0``); it validates against the
full plugin ``result.json`` schema (``docs/result-schema.json``) — so a record
missing required fields like ``schema_version`` / ``task_id``, or otherwise
violating the contract, is NOT replayable even when it claims success (replaying
it would hand the runner an invalid result and the task would not stay cleanly
retriable); and it is bound to the task being run (``task_id`` matches), so one
task's result is never replayed into another's. A failed, interrupted, or
malformed record is ignored so the task stays retriable. The recorder mirrors
this: it writes a record only for a succeeded run, so a failed/interrupted run
leaves no marker.

**Trust boundary.** The store is plain JSON at operator-home privilege; it is
trusted exactly as far as the operator's own state dir. A local process able to
write ``$LITHOS_LOOM_IDEMPOTENCY_DIR`` (or the default XDG state dir) can plant
a record, so that directory is a trust boundary — the task-id binding above
blocks cross-task replay (a planted record only affects the task it names), but
the store offers no cryptographic integrity. Treat write access to it as
equivalent to write access to the develop pipeline's outputs.

**Retention.** One tiny record per *distinct* key, pruned to a bound
(``LITHOS_LOOM_IDEMPOTENCY_MAX_RECORDS``, default 10000) newest-by-mtime on each
write. Evicting an old record is safe — a later dispatch under an evicted key
simply re-runs (the task stays retriable) — so the store cannot grow without
limit. Locking out concurrent in-flight runs is an explicit out-of-scope
follow-up (US-14 / US-37); pruning is not a substitute for that.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...plugin_runner import (
    PluginContractError,
    _validate_result_schema,
    write_result_atomically,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

#: Env override for the idempotency store directory (absolute path).
STORE_DIR_ENV = "LITHOS_LOOM_IDEMPOTENCY_DIR"
#: Env override for the store size bound (newest-by-mtime records kept).
MAX_RECORDS_ENV = "LITHOS_LOOM_IDEMPOTENCY_MAX_RECORDS"
#: Default store size bound when the env override is unset/invalid.
DEFAULT_MAX_RECORDS = 10000


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


def _max_records() -> int:
    raw = os.environ.get(MAX_RECORDS_ENV)
    if raw is None:
        return DEFAULT_MAX_RECORDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_RECORDS
    return value if value > 0 else DEFAULT_MAX_RECORDS


def _record_path(key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return store_dir() / f"{digest}.json"


def _is_completed_record(payload: object) -> bool:
    """True only for a well-formed prior *succeeded* result payload.

    Defensive on purpose (AC4): a record that is not an object, whose
    ``status`` / ``exit_code`` do not jointly say "completed success", or that
    fails full ``result.json`` schema validation (e.g. missing ``schema_version``
    / ``task_id``) is treated as absent so the task re-runs rather than replaying
    an invalid or lying record.
    """
    if not (
        isinstance(payload, dict)
        and payload.get("status") == "succeeded"
        and payload.get("exit_code") == 0
    ):
        return False
    try:
        _validate_result_schema(payload)
    except PluginContractError:
        return False
    return True


def lookup_completed(
    key: str, *, expected_task_id: str | None = None
) -> dict[str, Any] | None:
    """Return the recorded result payload for *key* iff it is replayable.

    Replayable means: the record exists, is a schema-valid completed/succeeded
    run, AND (when *expected_task_id* is given) is bound to that task. Returns
    ``None`` in every other case — no record, unreadable/malformed, not a
    completed run, or a task-id mismatch — so the caller runs normally and the
    task stays retriable. ``expected_task_id=None`` skips the binding check
    (store-level callers/tests that don't have a task in hand).
    """
    path = _record_path(key)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not _is_completed_record(payload):
        return None
    # Bind the record to the task it is replayed for (CWE-345): never replay one
    # task's result into another's result.json, whether from a reused
    # --idempotency-key or a tampered store. _is_completed_record already proved
    # payload is a dict with a string task_id (schema-required).
    if expected_task_id is not None and payload.get("task_id") != expected_task_id:
        return None
    return payload


def _prune(keep: int) -> None:
    """Bound the store: keep the *keep* newest records (by mtime), drop the rest.

    Best-effort and never raises — eviction is safe because a later dispatch
    under an evicted key just re-runs. A file vanishing mid-sweep (a concurrent
    prune) is swallowed.
    """
    store = store_dir()
    try:
        records = sorted(store.glob("*.json"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    excess = len(records) - keep
    for path in records[: max(0, excess)]:
        with contextlib.suppress(OSError):
            path.unlink()


def record_completion(key: str, payload: Mapping[str, Any]) -> None:
    """Persist *payload* as the completed-run record for *key* (atomic write).

    Only meaningful for a succeeded payload; the caller gates on that. The
    write reuses the plugin runner's temp+fsync+rename helper so a partial
    record is never observable, then prunes the store back to its size bound.
    """
    write_result_atomically(_record_path(key), dict(payload))
    _prune(_max_records())
