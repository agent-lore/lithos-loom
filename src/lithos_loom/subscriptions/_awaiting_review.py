"""``obsidian-awaiting-review`` subscription handler (#113).

Maintains a dedicated vault note listing open tasks whose PR awaits the
operator's review — those carrying ``metadata.loom_delivered`` +
``metadata.develop_pr_url`` (set by ``story-develop`` delivery under a
``completes_task = false`` route). The operator already lives in Obsidian; this
is a consolidated, always-current pull surface alongside the main task list.

Read-only projection: the note is regenerated from in-memory state on each
relevant event and is never round-tripped by the fs watcher, so — unlike the
task projection — it needs no ``sync_state`` self-write coordination. A
content-hash guard (seeded from disk) skips unchanged writes; the atomic
dot-temp write keeps Obsidian Sync from observing partial files.

Rendered as a plain bullet *reference* list (not ``- [ ]`` checkboxes) so the
Obsidian Tasks plugin never sees a second line carrying the canonical task's id
— the actionable task line lives in ``tasks.md``; this note just links its PR.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lithos_loom.bus import Event
from lithos_loom.config import LoomConfig
from lithos_loom.subscriptions._atomic_write import write_file_atomic

# Events that may add or drop an awaiting-review entry. A delivered task is added
# on created/updated; a terminal event (PR merged → task completed, via the
# github-watcher) removes it. Mirrors the obsidian-projection event split.
_REEVALUATE_EVENTS = frozenset(
    {
        "lithos.task.created",
        "lithos.task.updated",
        "lithos.task.claimed",
        "lithos.task.released",
    }
)
_REMOVAL_EVENTS = frozenset({"lithos.task.completed", "lithos.task.cancelled"})

_PR_NUM_RE = re.compile(r"/pull/(\d+)")


@dataclass(frozen=True)
class _Entry:
    title: str
    pr_url: str
    project: str | None


def _pr_label(pr_url: str) -> str:
    m = _PR_NUM_RE.search(pr_url)
    return f"PR #{m.group(1)}" if m else "PR"


def _render(delivered: dict[str, _Entry]) -> str:
    lines = ["# PRs awaiting review", ""]
    if not delivered:
        lines.append("_No PRs awaiting review._")
        return "\n".join(lines) + "\n"
    # Stable order so unrelated events don't reshuffle the file: project, then
    # title, then id.
    for _tid, e in sorted(
        delivered.items(), key=lambda kv: (kv[1].project or "", kv[1].title, kv[0])
    ):
        proj = f" · #project/{e.project}" if e.project else ""
        lines.append(f"- **{e.title}** — [{_pr_label(e.pr_url)}]({e.pr_url}){proj}")
    return "\n".join(lines) + "\n"


def _hash_existing(path: Path) -> bytes | None:
    """SHA-256 of the note on disk, or ``None`` if absent/unreadable.

    Seeds the dedup guard so a restart whose first event reproduces the existing
    note content writes nothing (no Obsidian Sync churn).
    """
    try:
        return hashlib.sha256(path.read_bytes()).digest()
    except OSError:
        return None


def make_handler(cfg: LoomConfig) -> tuple[Any, Any]:
    """Build the awaiting-review handler + its cold-start reconcile, bound to *cfg*.

    Returns ``(handle, reconcile)``:

    * ``handle(event, ctx)`` — the bus subscription handler (live updates).
    * ``reconcile(lithos)`` — an authoritative startup pass: rebuilds the set
      from a direct ``task_list(status="open")`` and writes the note once. The
      event stream only replays open tasks (+ terminal tasks within the resolved
      window), so a restart with zero open tasks and no recent terminal events
      yields no handler calls — without this, a note written by a previous run
      would persist stale instead of collapsing to the placeholder.

    ``cfg.obsidian_sync`` must be set (the obsidian-sync child's spawn gate
    guarantees this); the note is written to ``vault_path / awaiting_review_file``.
    """
    obs = cfg.obsidian_sync
    if obs is None:
        raise RuntimeError(
            "make_handler called without [obsidian_sync] config; the "
            "supervisor's spawn gate should have prevented this"
        )
    note_path = obs.vault_path / obs.awaiting_review_file
    delivered: dict[str, _Entry] = {}
    last_hash: bytes | None = _hash_existing(note_path)

    def _record(
        task_id: str, title: str, status: str, metadata: Mapping[str, Any]
    ) -> None:
        """Add an open delivered task to the set, or drop it. A terminal task
        (status != "open") or one missing the delivery markers is removed."""
        pr_url = metadata.get("develop_pr_url")
        # isinstance check lives in the `if` so pyright narrows pr_url to str.
        if (
            status == "open"
            and bool(metadata.get("loom_delivered"))
            and isinstance(pr_url, str)
            and pr_url
        ):
            project = metadata.get("project")
            delivered[task_id] = _Entry(
                title=title or task_id,
                pr_url=pr_url,
                project=str(project) if project else None,
            )
        else:
            delivered.pop(task_id, None)

    async def _flush() -> None:
        nonlocal last_hash
        content = _render(delivered)
        content_hash = hashlib.sha256(content.encode("utf-8")).digest()
        if content_hash == last_hash:
            return
        await write_file_atomic(note_path, content)
        last_hash = content_hash

    async def handle(event: Event, ctx: Any) -> None:
        if event.type not in _REEVALUATE_EVENTS and event.type not in _REMOVAL_EVENTS:
            ctx.logger.debug(
                "obsidian-awaiting-review: ignoring event type %s", event.type
            )
            return
        payload = event.payload
        try:
            task_id = str(payload["id"])
        except (KeyError, TypeError) as exc:
            ctx.logger.warning(
                "obsidian-awaiting-review: malformed payload for %s: %r",
                event.type,
                exc,
            )
            return
        if event.type in _REMOVAL_EVENTS:
            # A terminal event always drops the entry, regardless of the
            # status carried in the payload.
            delivered.pop(task_id, None)
        else:
            _record(
                task_id,
                str(payload.get("title") or task_id),
                str(payload.get("status", "")),
                payload.get("metadata") or {},
            )
        await _flush()

    async def reconcile(lithos: Any) -> None:
        """Authoritative cold-start reconcile from the current open tasks.

        Rebuilds the set from ``task_list(status="open")`` and flushes once, so a
        restart that replays no events still collapses a stale note. Live events
        keep it current afterward. Run before the event stream starts so it never
        races the bootstrap replay on the shared state.
        """
        delivered.clear()
        for task in await lithos.task_list(status="open"):
            _record(task.id, task.title, task.status, dict(task.metadata or {}))
        await _flush()

    return handle, reconcile
