"""Project resolution the watcher's dispatchers share.

A ``pr`` gate names its project in ``metadata.project`` when the delivery
payload carried it; otherwise the waiting story does. From the slug the
host's ``[projects.<slug>].repo`` gives the checkout a subprocess runs in,
and the project-context doc carries the per-project dials
(``develop_external_review_converge``, ``develop_merge_gate``) — read
directly here (canonical path, then the smallest ``project-context``-tagged
doc, the same resolution ``daemon_io._fetch_context_metadata`` applies),
kept as a local reader rather than importing the Plugins component into
Subscriptions for one lookup.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from lithos_loom.errors import LithosClientError
from lithos_loom.subscriptions import SubscriptionContext

__all__ = ["origin_repo", "parse_origin", "read_project_flag", "resolve_project_repo"]

# Every remote-url shape `gh` resolves for GitHub: scp-like ssh, ssh:// with
# an optional port, https with an optional user[:token]@ and optional www.
_ORIGIN_RE = re.compile(
    r"^(?:git@github\.com:"
    r"|ssh://[^@/\s]+@github\.com(?::\d+)?/"
    r"|https?://(?:[^@/\s]+@)?(?:www\.)?github\.com/)"
    r"([^/\s]+/[^/\s]+?)(?:\.git)?/?$"
)


def parse_origin(url: str) -> str | None:
    """``owner/name`` from a GitHub remote url (ssh / https / ssh://), or
    ``None`` for anything else."""
    m = _ORIGIN_RE.match(url.strip())
    return None if m is None else m.group(1)


async def origin_repo(path: Path) -> str | None:
    """The checkout's ``origin`` as ``owner/name`` via ``git remote get-url``
    — milliseconds, no network — or ``None`` when it cannot answer (no such
    directory, no origin, not a GitHub url).

    The cheap pre-check both dispatchers run in the sweep before spending
    a budget round or a subprocess on a checkout that is not the gate's
    repo (PR #362 re-review 2). The CLI's ``--expect-repo`` (``gh``,
    redirect-aware) stays the authoritative check inside the run.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(path),
            "remote",
            "get-url",
            "origin",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except (OSError, TimeoutError):
        return None
    if proc.returncode != 0:
        return None
    return parse_origin(out.decode("utf-8", errors="replace"))


async def resolve_project_repo(
    gate: Any,
    story_id: str,
    projects: Mapping[str, Path],
    ctx: SubscriptionContext,
) -> tuple[str, Path] | None:
    """``(slug, repo_path)`` via gate metadata, falling back to the story's
    (gate creation records ``project`` only when the payload carried it).
    ``None`` when no slug is known or the slug is not mapped under
    ``[projects]``."""
    slug = gate.metadata.get("project")
    if not isinstance(slug, str) or not slug:
        slug = None
        try:
            story = await ctx.lithos.task_get(task_id=story_id)
        except LithosClientError:
            story = None
        if story is not None:
            candidate = story.metadata.get("project")
            if isinstance(candidate, str) and candidate:
                slug = candidate
    if slug is None:
        return None
    repo = projects.get(slug)
    return None if repo is None else (slug, repo)


async def read_project_flag(
    slug: str,
    key: str,
    ctx: SubscriptionContext,
    *,
    subsystem: str,
    default: bool = True,
) -> bool | None:
    """A boolean per-project dial from the context doc's metadata.

    Tri-state (PR #346 re-review 2): a READABLE doc with the key absent or
    malformed is *default* (warned when malformed); an UNREADABLE doc returns
    ``None`` — the project may hold an explicit opt-out the caller cannot
    see, and an unknown safety dial must never authorize an autonomous run
    (the caller fails closed and retries).
    """
    meta: Mapping[str, Any] | None = None
    try:
        note = await ctx.lithos.note_read(
            path=f"projects/{slug}/{slug}-project-context.md"
        )
        if note is not None:
            meta = note.metadata
        else:
            candidates = await ctx.lithos.note_list(
                path_prefix=f"projects/{slug}/", tags=["project-context"]
            )
            if candidates:
                meta = min(candidates, key=lambda n: n.path).metadata
    except LithosClientError:
        return None  # unreadable ≠ unset — the caller fails closed
    raw = None if meta is None else meta.get(key)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.strip().lower() in {"true", "false"}:
        return raw.strip().lower() == "true"
    ctx.logger.warning(
        "[Friction] %s: project %r has malformed %s=%r; treating as %s",
        subsystem,
        slug,
        key,
        raw,
        "enabled" if default else "disabled",
    )
    return default
