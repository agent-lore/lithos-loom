"""story-develop's access to GitHub: the typed-client bridge + gh conveniences.

story-develop's plugin core is synchronous (the same reason ``lithos_io`` /
``daemon_io`` exist — see their module docstrings), so its GitHub PR access
bridges the async :class:`~lithos_loom.github_client.GitHubClient` through
``asyncio.run``. The REST-shaped PR ops (list reviews / comments, request
reviewers, post comments, fetch a PR's refs) go through :func:`github_call`
onto the typed client, sharing the watcher family's error hierarchy, rate-limit
retry, and pagination (ARCH-7c). The genuinely gh-CLI-shaped conveniences that
resolve the *local* checkout — the origin's ``owner/repo`` here, PR create /
branch push in ``pr_delivery`` — stay subprocess: the REST API can't resolve a
working tree's remote, so they aren't REST-shaped. "Two adapters (typed HTTP +
gh CLI) at one seam is fine; two seams is not."
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from lithos_loom.github_client import GitHubClient

# The single-injected-client timeout, matching GitHubClient.create's own
# fallback (github_client.py). Each call is short-lived: one client, one
# ``gh auth token`` resolution.
_HTTP_TIMEOUT = 30.0


def github_call[T](op: Callable[[GitHubClient], Awaitable[T]]) -> T:
    """Run one GitHub REST operation against a typed client, synchronously.

    Bridges the async :class:`GitHubClient` into the sync plugin core via
    ``asyncio.run`` (same pattern as ``lithos_io`` / ``daemon_io`` for the
    async LithosClient). Constructs a short-lived client — one ``gh auth
    token`` resolution + one ``httpx.AsyncClient`` — runs ``op`` against it,
    and lets the typed error hierarchy (``GitHubError`` / ``GitHubAuthError``
    / ``GitHubRepoNotFoundError`` / ...) propagate to the sync caller, which
    catches what it needs.
    """

    async def _run_op() -> T:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as http:
            client = await GitHubClient.create(http=http)
            return await op(client)

    return asyncio.run(_run_op())


def repo_name_with_owner(repo: Path) -> str:
    """``owner/repo`` of the local checkout's ``origin`` remote, via ``gh``.

    A genuine gh convenience: it resolves the remote from the working tree,
    which the REST API cannot do (you must already know ``owner/repo`` to call
    it). Shared by ``pr_delivery`` (delivery) and ``review_resolve`` (PR-number
    review specs), so it lives here rather than in either. Raises on failure.
    """
    proc = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh repo view failed: {proc.stderr.strip()}")
    return proc.stdout.strip()
