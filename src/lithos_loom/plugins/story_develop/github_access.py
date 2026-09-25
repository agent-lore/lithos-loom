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
import json
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from lithos_loom.github_client import GitHubClient, GitHubError
from lithos_loom.github_models import PullRequest

# Re-exported at this seam (as ``external_reviews`` re-exports ``GitHubError``
# for the same reason): a CLI command that fetches a PR through
# :func:`github_call` must be able to name what it gets back and what it
# catches without taking a GitHub-tier import of its own — the layering the
# import-linter contract and the component-edge budget both describe.
__all__ = [
    "GitHubError",
    "OpenPullRequest",
    "PullRequest",
    "default_base_branch",
    "github_call",
    "list_open_prs_for_branch",
    "repo_name_with_owner",
]

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


def _gh(args: list[str], *, cwd: Path, timeout: int = 120):
    """One ``gh`` read against a checkout (the seam tests monkeypatch)."""
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=timeout
    )


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


def default_base_branch(repo: Path, *, repo_name: str | None = None) -> str:
    """The default branch of the checkout's ``origin`` repository, via ``gh``.

    The same gh-CLI shape as :func:`repo_name_with_owner` — it answers from
    the working tree's remote, which the REST API cannot do without already
    knowing ``owner/repo``. Used by ``develop deliver`` to pick the PR base
    when the operator names none (a story-develop run's own base branch is a
    run-time config value, not something the stopped run left on disk).
    *repo_name* pins the repository explicitly (``gh repo view owner/name``)
    — for a fork checkout gh's own default is the *parent*, whose default
    branch is not necessarily the one the branch was pushed alongside.
    Raises on failure.
    """
    proc = subprocess.run(
        [
            "gh",
            "repo",
            "view",
            *([repo_name] if repo_name else []),
            "--json",
            "defaultBranchRef",
            "-q",
            ".defaultBranchRef.name",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh repo view failed: {proc.stderr.strip()}")
    name = proc.stdout.strip()
    if not name:
        raise RuntimeError("gh repo view returned no default branch")
    return name


@dataclass(frozen=True)
class OpenPullRequest:
    """An open PR ``gh`` reports for a head branch, with the fields an adopter
    must verify before treating it as ours.

    ``gh pr list --head`` filters on the head **branch name** only, so a PR
    opened from a *fork* whose branch carries the same name matches exactly
    like a same-repo one. Adopting such a PR would point the whole
    PR-maintenance machine (and the story's `pr` gate) at a third party's
    work, so the identity fields ride along and the caller checks them.
    """

    number: int
    url: str
    head_sha: str
    cross_repository: bool
    base_ref: str
    head_owner: str


def list_open_prs_for_branch(
    repo: Path, branch: str, *, repo_name: str | None = None
) -> list[OpenPullRequest]:
    """Every open PR whose head branch is *branch*, with its identity fields.

    The adopt half of an idempotent delivery (``develop deliver``): a second
    invocation must find the PR the first one opened rather than opening a
    duplicate. gh-CLI-shaped like :func:`create_pr` — it resolves the head
    ref against a remote, which the REST API cannot do without already
    knowing ``owner/repo`` and the head's owner. *repo_name* pins the
    repository (``--repo owner/name``) instead of letting ``gh`` infer it
    from the checkout's remotes / default-repo state. Raises on a gh failure
    (the caller must not read "could not ask" as "no PR").
    """
    proc = _gh(
        [
            "gh",
            "pr",
            "list",
            *(["--repo", repo_name] if repo_name else []),
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "number,url,headRefOid,isCrossRepository,baseRefName,headRepositoryOwner",
        ],
        cwd=repo,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh pr list failed: {proc.stderr.strip()}")
    try:
        rows = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gh pr list returned no JSON: {proc.stdout!r}") from exc
    found: list[OpenPullRequest] = []
    for row in rows if isinstance(rows, list) else []:
        number, url = row.get("number"), row.get("url")
        if not (isinstance(number, int) and isinstance(url, str) and url):
            continue
        owner = row.get("headRepositoryOwner")
        found.append(
            OpenPullRequest(
                number=number,
                url=url,
                head_sha=str(row.get("headRefOid") or ""),
                # absent / non-bool reads as cross-repository: unknown
                # provenance is never adopted (fail closed)
                cross_repository=row.get("isCrossRepository") is not False,
                base_ref=str(row.get("baseRefName") or ""),
                head_owner=str(
                    owner.get("login") if isinstance(owner, dict) else owner or ""
                ),
            )
        )
    return found
