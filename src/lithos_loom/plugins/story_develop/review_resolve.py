"""Resolve a review-only change spec into a concrete ``base..head`` (#154).

Review-only mode runs the panel + gate against a change that *already exists*.
This module turns the operator's argument — an explicit ``base..head`` range, a
local branch / ref, or a GitHub PR number / URL — into a :class:`ResolvedChange`
the orchestrator can materialise a worktree at.

The subprocess ``git`` / ``gh`` calls live behind thin module-level wrappers so
the resolution logic is unit-testable without a network round-trip.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from lithos_loom.github_client import PullRequest

from .github_access import github_call, repo_name_with_owner

# A PR argument: ``#142``, bare ``142``, or a GitHub PR URL ending ``/pull/142``.
_PR_URL_RE = re.compile(r"/pull/(\d+)\b")
_PR_HASH_RE = re.compile(r"^#?(\d+)$")


@dataclass(frozen=True)
class ResolvedChange:
    """A concrete change to review: the ``base..head`` commit pair + intent.

    ``head_ref`` is a human label for the change (branch / ref / ``#PR``).
    ``title`` / ``body`` carry the PR's title and description when the spec was a
    PR (empty for a bare range / branch) — the body is the default
    acceptance-criteria source for a PR review.
    """

    base_sha: str
    head_sha: str
    head_ref: str
    title: str = ""
    body: str = ""


def _run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip()


def _rev_parse(repo: Path, ref: str) -> str:
    """Resolve *ref* to a full commit sha (raises on an unknown ref)."""
    return _run_git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")


def _merge_base(repo: Path, a: str, b: str) -> str:
    return _run_git(repo, "merge-base", a, b)


def _git_fetch(repo: Path, *refspecs: str) -> None:
    _run_git(repo, "fetch", "origin", *refspecs)


def _gh_pr_view(repo: Path, number: str) -> PullRequest:
    """Fetch PR #*number* via the typed GitHub client (raises if not found).

    ``gh pr view <number>`` resolved the PR against the LOCAL checkout — it
    never consulted any URL's owner/repo, just the working tree's origin
    remote. Preserve that exactly: resolve ``owner/repo`` from the tree (a gh
    convenience the REST API can't do), then fetch that repo's PR #*number*.
    """
    owner_repo = repo_name_with_owner(repo)
    pr = github_call(lambda c: c.get_pull_request(owner_repo, int(number)))
    if pr is None:
        raise RuntimeError(f"PR #{number} not found in {owner_repo}")
    return pr


def _parse_pr_number(spec: str) -> str | None:
    """Return the PR number if *spec* is a PR reference, else None."""
    url = _PR_URL_RE.search(spec)
    if url is not None:
        return url.group(1)
    m = _PR_HASH_RE.match(spec.strip())
    return m.group(1) if m is not None else None


def resolve_change(
    repo: Path,
    spec: str,
    *,
    base_branch: str = "main",
    base_override: str | None = None,
) -> ResolvedChange:
    """Resolve *spec* into a :class:`ResolvedChange`.

    *spec* is one of: a GitHub PR (``#142`` / ``142`` / a PR URL), an explicit
    ``base..head`` ref range, or a single local ref / branch (whose base is its
    merge-base with *base_branch*). *base_override* forces the base sha for the
    range / branch forms.
    """
    number = _parse_pr_number(spec)
    if number is not None:
        return _resolve_pr(repo, number, base_override=base_override)

    if ".." in spec:
        base_ref, _, head_ref = spec.partition("..")
        return ResolvedChange(
            base_sha=_rev_parse(repo, base_override or base_ref),
            head_sha=_rev_parse(repo, head_ref),
            head_ref=head_ref,
        )

    head_sha = _rev_parse(repo, spec)
    if base_override is not None:
        base_sha = _rev_parse(repo, base_override)
    else:
        base_sha = _merge_base(repo, base_branch, spec)
    return ResolvedChange(base_sha=base_sha, head_sha=head_sha, head_ref=spec)


def _resolve_pr(
    repo: Path, number: str, *, base_override: str | None
) -> ResolvedChange:
    pr = _gh_pr_view(repo, number)
    head_sha = pr.head_sha
    base_ref_name = pr.base_ref
    # Fetch the PR head (works for forks too) and the base branch so both
    # commits are local before we materialise a worktree / diff against them.
    _git_fetch(repo, f"pull/{number}/head", base_ref_name)
    if base_override:
        base_sha = _rev_parse(repo, base_override)
    else:
        # Derive the PR's true diff base as the merge-base of the base branch
        # and the head (what GitHub diffs) rather than the PR object's base ref
        # OID — using the merge-base, not the base branch tip, avoids spurious
        # deletions when the base branch advanced after the PR was cut (this is
        # also why not requesting the base OID at all sidesteps #207). The base
        # branch was just fetched, so its tip is local at origin/<base>.
        base_sha = _merge_base(repo, f"origin/{base_ref_name}", head_sha)
    return ResolvedChange(
        base_sha=base_sha,
        head_sha=head_sha,
        head_ref=f"#{number} ({pr.head_ref})".strip(),
        title=pr.title,
        body=pr.body,
    )
