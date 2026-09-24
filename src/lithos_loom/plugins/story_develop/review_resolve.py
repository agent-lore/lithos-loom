"""Resolve a review-only change spec into a concrete ``base..head`` (#154).

Review-only mode runs the panel + gate against a change that *already exists*.
This module turns the operator's argument — an explicit ``base..head`` range, a
local branch / ref, or a GitHub PR number / URL — into a :class:`ResolvedChange`
the orchestrator can materialise a worktree at.

The subprocess ``git`` / ``gh`` calls live behind thin module-level wrappers so
the resolution logic is unit-testable without a network round-trip.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from lithos_loom.github_client import PullRequest

from ...runner import git
from .github_access import github_call, repo_name_with_owner
from .publish_text import MAX_EXCERPT_CHARS, publish_line

# A PR argument: ``#142``, bare ``142``, or a GitHub PR URL ending ``/pull/142``.
_PR_URL_REPO_RE = re.compile(r"github\.com/([^/\s#?]+/[^/\s#?]+)/pull/\d+\b")
_PR_URL_RE = re.compile(r"/pull/(\d+)\b")
_PR_HASH_RE = re.compile(r"^#?(\d+)$")

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedChange:
    """A concrete change to review: the ``base..head`` commit pair + intent.

    ``head_ref`` is a human label for the change (branch / ref / ``#PR``).
    ``title`` / ``body`` carry the PR's title and description when the spec was a
    PR (empty for a bare range / branch) — the body is the default
    acceptance-criteria source for a PR review.

    ``head_branch`` is the PR's raw pushable branch name (empty for a range /
    branch spec); ``is_fork`` is set when the PR head lives on a fork. converge
    reads both to push fixes back to the PR branch, and to refuse a fork PR it
    cannot push to under origin credentials.

    ``is_closed`` is the PR's state (closed, merged or not); ``is_merged`` that
    it landed. ``is_merged`` is a FLAG here, not
    a refusal: reviewing a merged PR is a legitimate read-only operation, so only
    converge — which would push fixes that could never land — acts on it.

    ``base_ref`` names the LIVE base the change lands on (``origin/main`` for a
    PR, the base branch for a local branch, the typed base of a range) so a
    base merge during a converge run moves the diff base with it (S5c). Empty
    when the operator forced the base: an explicit sha is the base, full stop.
    """

    base_sha: str
    head_sha: str
    head_ref: str
    base_ref: str = ""
    title: str = ""
    body: str = ""
    head_branch: str = ""
    is_fork: bool = False
    is_merged: bool = False
    is_closed: bool = False


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


# A PR head's first fetch into a fresh checkout can be big; the daemon's
# 60 s base-branch budget is too tight for it (#392 review, L6).
PR_FETCH_TIMEOUT_SECONDS = 300.0

# Transport failures the intake fetch hits TRANSIENTLY — the daemon's ssh agent
# or the network blinking for a second. One `Could not read from remote
# repository.` used to cost a whole conflict-resolve run and leave the PR
# `behind` until the next daemon boot (#431), so these are retried; anything
# else (a ref that does not exist, a permission denial) is answered first time.
_TRANSIENT_FETCH_SIGNS = (
    "could not read from remote repository",
    "connection reset",
    "timed out",
    "early eof",
    "kex_exchange",
)
# Three attempts, ~1 s then ~2 s apart: a hiccup costs seconds, not a restart.
FETCH_ATTEMPTS = 3
FETCH_RETRY_BACKOFF_SECONDS = 1.0


class FetchFailedError(RuntimeError):
    """The intake fetch failed — an infrastructure failure, not a verdict.

    #377's contract, applied to intake (#431): an auth / transport / spawn
    failure says nothing about the change, so the caller ends the run
    ``infra_failed`` with a :attr:`host_action` naming what to fix on the host
    and writes its ``--json`` record — never an uncaught exception whose
    traceback reaches a story's ``[Friction]`` as the "output tail". Raised
    after :data:`FETCH_ATTEMPTS` attempts on a transport failure, on the first
    answer for anything else.

    ``problem`` is git's own ``fatal:`` line — text the ORIGIN host and the
    local ssh client author (an ssh banner, a ``remote:`` line), so it is
    stripped of anything that could render as something else and bounded
    before it reaches an operator's terminal or a Lithos finding (review
    security f-001, CWE-117 / CWE-150 / CWE-770).
    """

    def __init__(self, *, refspecs: Sequence[str], problem: str) -> None:
        self.refspecs = tuple(refspecs)
        self.problem = publish_line(problem, limit=MAX_EXCERPT_CHARS)
        super().__init__(
            f"git fetch origin {' '.join(self.refspecs)} failed: {self.problem}"
        )

    @property
    def host_action(self) -> str:
        """What to fix on the host — git's own ``fatal:`` line plus where to
        look for it (the operator reading the story sees "SSH fetch failed",
        not a traceback fragment)."""
        return (
            f"the intake fetch of {' '.join(self.refspecs)} from origin failed "
            f"({self.problem}) — check the daemon's SSH agent (SSH_AUTH_SOCK, "
            "`ssh-add -l`) and its network access to origin, then re-run"
        )


def _transient_fetch_failure(detail: str) -> bool:
    """Does git's WHOLE stderr carry a transport sign? (review f-001: the one
    reported line is a collapse — ``error: RPC failed …`` hides the
    ``fatal: early EOF`` under it.)"""
    lowered = detail.lower()
    return any(sign in lowered for sign in _TRANSIENT_FETCH_SIGNS)


def _git_fetch(repo: Path, *refspecs: str) -> None:
    """Fetch the PR head + base with the daemon's tolerances (#392): a lost
    ref-lock race against a concurrent fetch of the same moved base (the
    sweep's merge-gate probe or a remediation converge beside a story-develop
    worktree cut, #390) is retried once, a hung transport is killed with its
    process group, and no credential prompt can block
    (``GIT_TERMINAL_PROMPT=0`` — on the operator's own ``develop review`` an
    https helper that would have prompted now fails plainly; use ``gh auth`` /
    ssh).

    A **transport** failure on top of that is retried up to
    :data:`FETCH_ATTEMPTS` times with a short backoff (#431), and a failure
    that persists raises :class:`FetchFailedError` — which every intake
    surface maps to an ``infra_failed`` run, never a crash. The retries share
    ONE :data:`PR_FETCH_TIMEOUT_SECONDS` budget: the fast signs the retry
    exists for come back in milliseconds, while a hung origin would otherwise
    hold a dispatcher's single-flight slot for attempts × the timeout (review
    security f-003)."""
    deadline = time.monotonic() + PR_FETCH_TIMEOUT_SECONDS
    problem = git.FetchProblem(f"timed out after {PR_FETCH_TIMEOUT_SECONDS:.0f}s")
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break  # the whole intake fetch gets one timeout, retries included
        problem = git.fetch_problem(repo, refspecs, timeout=remaining)
        if not problem.reason:
            return
        if attempt == FETCH_ATTEMPTS or not _transient_fetch_failure(problem.detail):
            break
        backoff = FETCH_RETRY_BACKOFF_SECONDS * attempt
        logger.warning(
            "git: intake fetch of %s failed transiently (%s); retrying in "
            "%.0fs (attempt %d/%d)",
            " ".join(refspecs),
            problem.reason,
            backoff,
            attempt + 1,
            FETCH_ATTEMPTS,
        )
        time.sleep(backoff)
    raise FetchFailedError(refspecs=refspecs, problem=problem.fatal_line)


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


class RepoMismatchError(RuntimeError):
    """The checkout's ``origin`` is not the repository the caller expected.

    A PR number resolves against the LOCAL checkout's origin, never against
    any URL — so an autonomous caller that maps a project slug to a checkout
    (the watcher's dispatchers, PRD S2 / S3) would act on ``owner/other#N``
    if that mapping were stale. Raised before any GitHub call or fetch.
    """

    def __init__(self, *, expected: str, actual: str) -> None:
        super().__init__(
            f"checkout origin is {actual!r}, not the expected {expected!r}"
        )
        self.expected = expected
        self.actual = actual


def resolve_change(
    repo: Path,
    spec: str,
    *,
    base_branch: str = "main",
    base_override: str | None = None,
    allow_fork: bool = True,
    expect_repo: str | None = None,
) -> ResolvedChange:
    """Resolve *spec* into a :class:`ResolvedChange`.

    *spec* is one of: a GitHub PR (``#142`` / ``142`` / a PR URL), an explicit
    ``base..head`` ref range, or a single local ref / branch (whose base is its
    merge-base with *base_branch*). *base_override* forces the base sha for the
    range / branch forms. With ``allow_fork=False`` a fork PR is answered from
    GitHub's own metadata **before** anything is fetched — ``is_fork`` set,
    ``base_sha`` empty — so a caller that must never pull a third-party head
    into the operator's checkout (merge-gate, PRD S3) can refuse it cleanly.
    With ``expect_repo`` (``owner/name``) a PR spec is pinned to that
    repository: the checkout's origin is compared first and a mismatch raises
    :class:`RepoMismatchError` before anything is fetched (PR #362 review F2).
    """
    number = _parse_pr_number(spec)
    if number is not None:
        return _resolve_pr(
            repo,
            number,
            base_override=base_override,
            allow_fork=allow_fork,
            expect_repo=expect_repo,
            spec=spec,
        )

    if ".." in spec:
        base_ref, _, head_ref = spec.partition("..")
        return ResolvedChange(
            base_sha=_rev_parse(repo, base_override or base_ref),
            head_sha=_rev_parse(repo, head_ref),
            head_ref=head_ref,
            base_ref="" if base_override else base_ref,
        )

    head_sha = _rev_parse(repo, spec)
    if base_override is not None:
        base_sha = _rev_parse(repo, base_override)
        live_base = ""
    else:
        base_sha = _merge_base(repo, base_branch, spec)
        live_base = base_branch
    return ResolvedChange(
        base_sha=base_sha, head_sha=head_sha, head_ref=spec, base_ref=live_base
    )


def _resolve_pr(
    repo: Path,
    number: str,
    *,
    base_override: str | None,
    allow_fork: bool = True,
    expect_repo: str | None = None,
    spec: str = "",
) -> ResolvedChange:
    if expect_repo is not None:
        # The URL's own repository, when the spec is a URL (the number is
        # what selects the PR, so a URL naming another repo must not be
        # quietly resolved in the checkout's) — then the checkout's origin.
        # The same lenient shape _parse_pr_number accepts (http, www., a
        # trailing path, a fragment) — the canonical ref parser is stricter
        # and would silently skip the check for those forms (self-review).
        url_repo = _PR_URL_REPO_RE.search(spec)
        candidates = [url_repo.group(1)] if url_repo is not None else []
        candidates.append(repo_name_with_owner(repo))
        for candidate in candidates:
            if candidate.strip().lower() != expect_repo.strip().lower():
                raise RepoMismatchError(expected=expect_repo, actual=candidate)
    pr = _gh_pr_view(repo, number)
    head_sha = pr.head_sha
    base_ref_name = pr.base_ref
    is_fork = bool(pr.head_repo and pr.base_repo and pr.head_repo != pr.base_repo)
    if is_fork and not allow_fork:
        return ResolvedChange(
            base_sha="",
            head_sha=head_sha,
            head_ref=f"#{number} ({pr.head_ref})".strip(),
            title=pr.title,
            body=pr.body,
            head_branch=pr.head_ref,
            is_fork=True,
            is_merged=pr.merged,
            is_closed=pr.state == "closed",
        )
    # Fetch the PR head (works for forks too) and the base branch so both
    # commits are local before we materialise a worktree / diff against them.
    # The base by EXPLICIT refspec (#390's lesson, #392 review M3): a
    # checkout whose remote.origin.fetch is narrowed would otherwise report
    # success and leave origin/<base> stale — and the S5c RangeBase below
    # would hand the panel a range that includes work already on the base.
    _git_fetch(
        repo,
        f"pull/{number}/head",
        f"+refs/heads/{base_ref_name}:refs/remotes/origin/{base_ref_name}",
    )
    if base_override:
        base_sha = _rev_parse(repo, base_override)
        live_base = ""
    else:
        # Derive the PR's true diff base as the merge-base of the base branch
        # and the head (what GitHub diffs) rather than the PR object's base ref
        # OID — using the merge-base, not the base branch tip, avoids spurious
        # deletions when the base branch advanced after the PR was cut (this is
        # also why not requesting the base OID at all sidesteps #207). The base
        # branch was just fetched, so its tip is local at origin/<base>.
        base_sha = _merge_base(repo, f"origin/{base_ref_name}", head_sha)
        live_base = f"origin/{base_ref_name}"
    return ResolvedChange(
        base_sha=base_sha,
        head_sha=head_sha,
        base_ref=live_base,
        head_ref=f"#{number} ({pr.head_ref})".strip(),
        title=pr.title,
        body=pr.body,
        head_branch=pr.head_ref,
        is_fork=is_fork,
        is_merged=pr.merged,
        is_closed=pr.state == "closed",
    )
