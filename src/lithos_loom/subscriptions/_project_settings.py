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
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lithos_loom.errors import LithosClientError
from lithos_loom.subscriptions import SubscriptionContext

__all__ = [
    "OriginRead",
    "origin_read",
    "origin_repo",
    "parse_origin",
    "project_count",
    "read_project_flag",
    "read_project_metadata",
    "resolve_project_repo",
]

logger = logging.getLogger(__name__)

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


@dataclass(frozen=True)
class OriginRead:
    """What the sweep learned about a mapped checkout's ``origin``.

    ``repo`` is ``owner/name`` when it resolved; otherwise ``reason`` says
    why not — ``missing`` (no such directory / not a git checkout),
    ``no_origin`` (a checkout without an origin remote), ``unparseable``
    (an origin that is not a GitHub url this reads), ``error`` (git did not
    answer in time). "Cannot resolve" is a refusal with a reason the
    operator acts on, never permission to dispatch (PR #362 re-review 3).
    """

    repo: str | None
    reason: str = "ok"


async def origin_read(path: Path) -> OriginRead:
    """The checkout's ``origin`` via ``git remote get-url`` — milliseconds,
    no network — as an :class:`OriginRead`.

    The cheap pre-check both dispatchers run in the sweep before spending
    a budget round or a subprocess on a checkout that is not the gate's
    repo (PR #362 re-review 2). The CLI's ``--expect-repo`` (``gh``,
    redirect-aware) stays the authoritative check inside the run.
    """
    if not (path / ".git").exists() and not path.is_dir():
        return OriginRead(None, "missing")
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(path),
            "remote",
            "get-url",
            "origin",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=10)
    except (OSError, TimeoutError):
        return OriginRead(None, "error")
    if proc.returncode != 0:
        text = err.decode("utf-8", errors="replace").lower()
        if "not a git repository" in text:
            return OriginRead(None, "missing")
        return OriginRead(None, "no_origin")
    repo = parse_origin(out.decode("utf-8", errors="replace"))
    return OriginRead(repo, "ok" if repo is not None else "unparseable")


async def origin_repo(path: Path) -> str | None:
    """:func:`origin_read`'s ``repo`` alone (``None`` when unresolvable)."""
    return (await origin_read(path)).repo


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


async def read_project_metadata(lithos: Any, slug: str) -> Mapping[str, Any] | None:
    """The project-context doc's metadata — the home of every per-project
    dial. Canonical path first, then the smallest ``project-context``-tagged
    doc under the project (the resolution ``daemon_io._fetch_context_metadata``
    applies). ``{}`` when no doc exists (readable, keyless); ``None`` when
    Lithos could not be read — the two are different answers to a caller
    that must fail closed."""
    try:
        note = await lithos.note_read(path=f"projects/{slug}/{slug}-project-context.md")
        if note is not None:
            return note.metadata
        candidates = await lithos.note_list(
            path_prefix=f"projects/{slug}/", tags=["project-context"]
        )
    except LithosClientError:
        return None
    if candidates:
        return min(candidates, key=lambda n: n.path).metadata
    return {}


def project_count(
    meta: Mapping[str, Any],
    key: str,
    default: int,
    *,
    subsystem: str,
    slug: str,
) -> int:
    """A non-negative integer per-project dial from already-read metadata.
    Absent → *default*; present but not a non-negative ``int`` (``bool``
    rejected) → *default* with a warning."""
    raw = meta.get(key)
    if raw is None:
        return default
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    logger.warning(
        "[Friction] %s: project %r has malformed %s=%r; using %d",
        subsystem,
        slug,
        key,
        raw,
        default,
    )
    return default


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
    meta = await read_project_metadata(ctx.lithos, slug)
    if meta is None:
        return None  # unreadable ≠ unset — the caller fails closed
    raw = meta.get(key)
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
