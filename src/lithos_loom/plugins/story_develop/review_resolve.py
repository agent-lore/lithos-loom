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
from .publish_text import MAX_EXCERPT_CHARS, log_text, publish_line

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
# Each sign is a substring of what GIT / SSH printed (`timed out` catches the
# transport's own `Connection timed out`); loom's watchdog kill —
# `FetchProblem.timed_out`, the failure git never got to diagnose — is retried
# beside them, bounded by the shared budget below rather than excluded (review
# f-005).
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
# Held back from each attempt for each attempt after it, so a first try that
# HANGS for the whole budget cannot leave the retries with nothing to run in
# (review f-005). A transport sign answers in milliseconds — the reserve only
# has to be enough for the next try to reach the remote and be refused — so the
# first attempt still keeps 240s of the 300s for a big PR-head fetch (#392).
FETCH_RETRY_RESERVE_SECONDS = 30.0
# Below this there is no attempt worth making: it would only replace what the
# previous one reported with a 0s timeout of its own.
_MIN_ATTEMPT_SECONDS = 1.0

# `host_action` is published into a `[Friction]` whose sinks cap what a child
# supplies at 300 characters, so the WHOLE action is composed to fit inside that
# cap — and loom's own remediation guidance comes FIRST, so a cap applied
# anywhere downstream can only ever shorten the origin's quote, never the half
# that says what to fix (review f-006).
HOST_ACTION_CHARS = 300
# …and the refspecs inside it are bounded too: the base ref name comes from the
# GitHub API, not from loom.
_REFSPEC_CHARS = (
    80  # leaves the lead room for a full-width quote under HOST_ACTION_CHARS
)
_MIN_QUOTE_CHARS = 40


def _attributed(lead: str, problem: str, *, limit: int) -> str:
    """*lead* — loom's own words — followed by *problem* quoted and attributed
    to the origin, the whole thing inside *limit* characters.

    The one composer for both strings a failed fetch publishes (the run's
    ``message`` and its ``host_action``): loom's half FIRST, so a cap applied
    downstream can only shorten the quotation, and the quotation last, so the
    origin's line can never read as loom's own prose (review security f-004).
    The excerpt is sized to what is left of the budget, so the sinks' own
    300-character cut is a no-op; the final slice is a backstop for a *lead*
    long enough to crowd the quote out, and a plain cut rather than another
    :func:`publish_line` — a second pass would fold the delimiters this puts
    around the excerpt.
    """
    room = limit - len(lead) - 2
    if room < _MIN_QUOTE_CHARS:
        # A lead long enough to crowd the quote gives way ITSELF: cutting the
        # composed string would take the closing delimiter — and the `…` the
        # excerpt had just been given — so loom's trailing clause sat inside
        # the origin's open quotation (remediation review of PR #430).
        lead = publish_line(lead, limit=limit - _MIN_QUOTE_CHARS - 2)
        room = _MIN_QUOTE_CHARS
    return f'{lead}"{publish_line(problem, limit=room)}"'


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
        # The message travels as the run's `message` and both watcher sinks echo
        # it in their own prose, so the attribution belongs HERE too — not only
        # on `host_action` (review security f-004): every sink that quotes a
        # `FetchFailedError` inherits it, and the quote cannot be closed from
        # inside (`publish_line` folds the delimiter).
        what = publish_line(" ".join(self.refspecs), limit=_REFSPEC_CHARS)
        super().__init__(
            _attributed(
                f"git fetch origin {what} failed; origin said: ",
                self.problem,
                limit=HOST_ACTION_CHARS,
            )
        )

    @property
    def host_action(self) -> str:
        """What to fix on the host — where to look, then git's own ``fatal:``
        line **quoted and attributed** (the operator reading the story sees
        "SSH fetch failed", not a traceback fragment).

        The order and the quotes are the point. The line is the ORIGIN host's
        and the local ssh client's text — an ssh pre-auth banner reaches the
        same stderr unprefixed, so a hostile origin can put its own
        ``fatal: …`` line where git's verdict would be. Attributed to its
        author inside ``origin said: "…"`` it can no longer read as loom's own
        diagnosis of what to do (review security f-004), and with loom's
        guidance FIRST a downstream cap cannot leave a `[Friction]` that quotes
        the origin without saying what to fix (review f-006).
        """
        what = publish_line(" ".join(self.refspecs), limit=_REFSPEC_CHARS)
        lead = (
            f"the intake fetch of {what} from origin failed — check the "
            "daemon's SSH agent (SSH_AUTH_SOCK, `ssh-add -l`) and its network "
            "access to origin, then re-run; origin said: "
        )
        # what is left of the sink's budget after loom's own half, so the whole
        # action fits the cap its sinks apply and nothing has to be cut there
        return _attributed(lead, self.problem, limit=HOST_ACTION_CHARS)


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
    :data:`FETCH_ATTEMPTS` times with a short backoff (#431) — the signs are
    matched against git's WHOLE stderr, so ``Connection timed out`` /
    ``Connection reset`` / ``early EOF`` under an ``error: RPC failed …`` line
    are seen, and loom's own watchdog kill (``FetchProblem.timed_out``, which
    git never got to diagnose) is one of them. A failure that persists raises
    :class:`FetchFailedError`, which every intake surface maps to an
    ``infra_failed`` run, never a crash.

    All of it fits ONE :data:`PR_FETCH_TIMEOUT_SECONDS` budget, so three tries
    can never cost three times it and a hung origin cannot hold a dispatcher's
    single-flight slot for multiples of it (review security f-003). Each
    attempt gets what is left **minus a reserve** that keeps a usable slice for
    the attempts after it: without that, a first attempt that hangs for the
    whole budget would leave the retries no time to run in (review f-005).
    Since a transport sign answers in milliseconds, the reserve is small and
    the first attempt keeps nearly the whole budget for a legitimately big
    PR-head fetch (#392)."""
    deadline = time.monotonic() + PR_FETCH_TIMEOUT_SECONDS
    problem = git.FetchProblem(
        f"timed out after {PR_FETCH_TIMEOUT_SECONDS:.0f}s", timed_out=True
    )
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        remaining = deadline - time.monotonic()
        if remaining < _MIN_ATTEMPT_SECONDS:
            break  # nothing useful left of the one budget; keep what we know
        reserved = FETCH_RETRY_RESERVE_SECONDS * (FETCH_ATTEMPTS - attempt)
        allowance = max(remaining - reserved, min(remaining, _MIN_ATTEMPT_SECONDS))
        problem = git.fetch_problem(repo, refspecs, timeout=allowance)
        if not problem.reason:
            return
        backoff = FETCH_RETRY_BACKOFF_SECONDS * attempt
        retryable = (
            attempt < FETCH_ATTEMPTS
            and (problem.timed_out or _transient_fetch_failure(problem.detail))
            and (deadline - time.monotonic()) > backoff + _MIN_ATTEMPT_SECONDS
        )
        if not retryable:
            break
        logger.warning(
            "git: intake fetch of %s failed transiently (%s); retrying in "
            "%.0fs (attempt %d/%d)",
            " ".join(refspecs),
            log_text(problem.reason),
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
