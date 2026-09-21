"""The repository half of ``lithos-loom develop deliver`` (see :mod:`cli.deliver`).

Everything the hand delivery does against git and ``gh``: classify the branch
against ``origin`` and push it append-only, resolve the repository every gh
call is pinned to, and adopt *this branch's own* open PR or open one. The
Lithos half is :mod:`cli._deliver_lithos`; this module knows nothing about
stories or gates, so the two halves can be read (and tested) apart.

Two guards live here:

* **Append-only.** A diverged remote ref is refused, never forced — the
  divergence may be a collaborator's commit. The branch always travels as a
  fully-qualified refspec, so a ref legitimately named ``--receive-pack=…``
  cannot be re-read by git as an option naming a program to run (CWE-88).
* **Adopt only what is ours.** ``gh pr list --head`` filters on the head
  *branch name* alone, so a PR opened from a fork with the same branch name is
  indistinguishable from ours. Adoption additionally requires a same-repository
  head at the exact sha we pushed, onto the base this delivery targets — nobody
  else can produce a PR whose head is our commit — and every gh call is pinned
  with ``--repo`` to the ``origin`` the branch was pushed to (gh's own
  inference resolves a *fork* checkout to its parent). Every one of those
  checks fails **closed**: a field GitHub did not report is unknown
  provenance, never ours.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from lithos_loom.cli._deliver_lithos import DeliverRefused
from lithos_loom.plugins.story_develop.github_access import (
    OpenPullRequest,
    default_base_branch,
    list_open_prs_for_branch,
)
from lithos_loom.plugins.story_develop.pr_delivery import create_pr
from lithos_loom.subscriptions._project_settings import parse_origin

__all__ = [
    "PUSH_CREATE",
    "PUSH_DIVERGED",
    "PUSH_FAST_FORWARD",
    "PUSH_UP_TO_DATE",
    "RemoteState",
    "adoptable",
    "open_or_adopt",
    "origin_repo_name",
    "push_branch",
    "remote_state",
]

# How the local branch stands against `origin` (step 1).
PUSH_CREATE = "create"
PUSH_UP_TO_DATE = "up_to_date"
PUSH_FAST_FORWARD = "fast_forward"
PUSH_DIVERGED = "diverged"


@dataclass(frozen=True)
class RemoteState:
    """How ``origin``'s copy of the branch stands against the local one."""

    action: str  # PUSH_CREATE | PUSH_UP_TO_DATE | PUSH_FAST_FORWARD | PUSH_DIVERGED
    local_sha: str
    remote_sha: str  # "" when the remote ref does not exist


def _git(
    repo: Path, args: list[str], *, timeout: int = 300
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def local_sha(repo: Path, branch: str) -> str:
    """The branch's sha in *repo*. Raises :class:`DeliverRefused` if absent."""
    proc = _git(repo, ["rev-parse", "--verify", f"refs/heads/{branch}"], timeout=120)
    if proc.returncode != 0:
        raise DeliverRefused(
            f"branch {branch!r} does not exist in {repo} — the run's commits are "
            "not in this checkout (a different repo, or the branch was deleted); "
            "nothing was pushed"
        )
    return proc.stdout.strip()


def remote_state(repo: Path, branch: str) -> RemoteState:
    """Classify the push (step 1) without writing anything.

    ``ls-remote`` patterns tail-match, so the fully-qualified ref is queried
    and the returned ref name matched exactly — a bare branch name would also
    match an unrelated ``a/<branch>``.
    """
    local = local_sha(repo, branch)
    dst = f"refs/heads/{branch}"
    ls = _git(repo, ["ls-remote", "--heads", "origin", dst], timeout=120)
    if ls.returncode != 0:
        raise DeliverRefused(f"git ls-remote origin {dst} failed: {ls.stderr.strip()}")
    remote = ""
    for line in ls.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == dst:
            remote = parts[0]
            break
    if not remote:
        return RemoteState(action=PUSH_CREATE, local_sha=local, remote_sha="")
    if remote == local:
        return RemoteState(action=PUSH_UP_TO_DATE, local_sha=local, remote_sha=remote)
    anc = _git(
        repo, ["merge-base", "--is-ancestor", remote, local], timeout=120
    ).returncode
    action = PUSH_FAST_FORWARD if anc == 0 else PUSH_DIVERGED
    return RemoteState(action=action, local_sha=local, remote_sha=remote)


def push_branch(repo: Path, branch: str, state: RemoteState) -> None:
    """Push *branch* to ``origin`` — append-only; a diverged ref is refused.

    ``develop deliver`` never rewrites a remote branch: the divergence may be
    a collaborator's commit, and re-developing is the operator's other choice.
    """
    if state.action == PUSH_DIVERGED:
        raise DeliverRefused(
            f"origin/{branch} has diverged from the local branch "
            f"(origin {state.remote_sha[:12]}, local {state.local_sha[:12]}): the "
            "remote carries commits this branch does not. Refusing to push — "
            "`develop deliver` is append-only and never force-pushes. Reconcile "
            "the branch by hand, or complete the needs-human gate to re-develop "
            "the story"
        )
    if state.action == PUSH_UP_TO_DATE:
        return
    # Push the **object** the classification was made against, not the symbolic
    # ref: a local process that advances or rewrites the branch between the
    # classification and the push would otherwise have us send a commit nobody
    # decided was append-only, and then report the measured sha as delivered.
    # (Also a fully-qualified refspec, never a bare positional: a ref
    # legitimately named `--receive-pack=…` would be read by git as an option
    # naming a program to run — CWE-88, the same reason the reads above
    # fully-qualify.) A remote that moved under us still fails closed: git
    # rejects a non-fast-forward, and we never pass --force.
    proc = _git(repo, ["push", "origin", f"{state.local_sha}:refs/heads/{branch}"])
    if proc.returncode != 0:
        raise DeliverRefused(f"git push failed: {proc.stderr.strip()}")


def origin_repo_name(repo: Path) -> str:
    """``owner/name`` of the checkout's ``origin`` — the repository every
    ``gh`` call in this command is pinned to.

    ``origin`` is where step 1 pushes, so it is the only repository this
    delivery can legitimately be about. Letting ``gh`` infer the target from
    the checkout's remotes (its own default for a **fork** checkout is the
    *parent*) would open the PR, request the review and build the ``pr`` gate
    against a repository the branch was never pushed to — and widen the
    adoption search to that repository's open PRs. Resolved once, passed to
    every call as ``--repo``.
    """
    proc = _git(repo, ["remote", "get-url", "origin"], timeout=120)
    if proc.returncode != 0:
        raise DeliverRefused(
            f"{repo} has no `origin` remote ({proc.stderr.strip()}); there is "
            "nowhere to push the branch or open the PR"
        )
    name = parse_origin(proc.stdout)
    if name is None:
        raise DeliverRefused(
            f"{repo}'s origin ({proc.stdout.strip()!r}) is not a GitHub "
            "repository url; `develop deliver` opens GitHub PRs"
        )
    return name


def adoptable(
    candidates: Sequence[OpenPullRequest], *, head_sha: str, base: str
) -> tuple[OpenPullRequest | None, str]:
    """Pick the open PR that is *ours* to adopt, or say why none is (pure).

    ``gh pr list --head`` filters on the head **branch name** alone, so a PR
    opened from a fork whose branch carries the same name matches exactly like
    a same-repo one — and adopting it would point the `pr` gate, merge
    tracking, review ingestion and the story's eventual completion at a third
    party's work while retiring the story's escalation. So a candidate is ours
    only when it is same-repo, its head is the sha we just pushed, and its base
    is the base this delivery targets; the head check alone kills the class,
    since nobody else can produce a PR whose head is our commit.

    **Every check fails closed.** A field GitHub did not report reads as
    ``""``/unknown, and unknown provenance is never ours: a verification step
    that silently does not run is the failure this function exists to prevent.
    *base* is the RESOLVED base (``--base`` or the repo's default), never
    ``None`` — skipping the comparison when the operator named no base would
    let an adopted PR merge into a branch ``deliver`` would never have opened
    onto, with the `pr` gate tracking that merge. Returns ``(pr, "")`` or
    ``(None, reason)``.
    """
    for pr in candidates:
        if pr.cross_repository:
            continue
        if pr.head_sha != head_sha:
            continue
        if pr.base_ref != base:
            continue
        return pr, ""
    if not candidates:
        return None, ""
    described = "; ".join(
        f"#{pr.number} head {pr.head_sha[:12] or '(not reported)'}"
        f"{' (fork ' + (pr.head_owner or '?') + ')' if pr.cross_repository else ''}"
        f" → {pr.base_ref or '(not reported)'}"
        for pr in candidates
    )
    return None, (
        f"an open PR already claims this branch name but is not this delivery "
        f"({described}); expected a same-repository PR whose head is "
        f"{head_sha[:12]} and whose base is {base}. Refusing to adopt a PR "
        "that is not this branch's — nothing was gated. Close or rename the "
        "other PR, or deliver from a branch name it does not claim"
    )


def open_or_adopt(
    repo: Path,
    *,
    branch: str,
    repo_name: str,
    base: str | None,
    head_sha: str,
    title: str,
    body: Callable[[], str],
) -> tuple[str, bool]:
    """Step 2: adopt this branch's own open PR, else open one. Returns
    ``(url, adopted)``.

    A ``gh`` failure is a refusal, not a degraded delivery: without an answer
    we cannot tell "no PR yet" from "could not ask", and opening a second PR
    for a branch that already has one is the failure this step exists to
    avoid. Everything after ``create_pr`` returns is the caller's to degrade —
    the PR exists from that moment and its url must never be lost.
    """
    try:
        # Resolved BEFORE the adoption decision: the base a candidate must
        # match is the one this delivery targets, whether the operator named
        # it or the repository's default supplied it.
        resolved_base = base or default_base_branch(repo, repo_name=repo_name)
        candidates = list_open_prs_for_branch(repo, branch, repo_name=repo_name)
        existing, refusal = adoptable(candidates, head_sha=head_sha, base=resolved_base)
        if existing is not None:
            return existing.url, True
        if refusal:
            raise DeliverRefused(refusal)
        return (
            create_pr(
                repo,
                branch=branch,
                base=resolved_base,
                title=title,
                # built lazily: an adopted PR needs no body, and composing one
                # costs a run-dir read
                body=body(),
                repo_name=repo_name,
            ),
            False,
        )
    except RuntimeError as exc:
        raise DeliverRefused(str(exc)) from exc
