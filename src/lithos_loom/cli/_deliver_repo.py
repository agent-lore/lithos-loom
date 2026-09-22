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

from lithos_loom.cli._deliver_lithos import DeliverRefused, DeliverUncertain
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
    "PRPlan",
    "adoptable",
    "delivered_pr_head",
    "pr_plan",
    "open_or_adopt",
    "origin_repo_name",
    "push_branch",
    "run_git",
    "remote_sha",
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


def run_git(
    repo: Path, args: list[str], *, timeout: int = 300
) -> subprocess.CompletedProcess[str]:
    """Every git call this module makes — one seam, so a test can stand in for
    the whole of git (a lost push response, a refused push) without reaching
    for a private name."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def local_sha(repo: Path, branch: str) -> str:
    """The branch's sha in *repo*. Raises :class:`DeliverRefused` if absent."""
    proc = run_git(repo, ["rev-parse", "--verify", f"refs/heads/{branch}"], timeout=120)
    if proc.returncode != 0:
        raise DeliverRefused(
            f"branch {branch!r} does not exist in {repo} — the run's commits are "
            "not in this checkout (a different repo, or the branch was deleted); "
            "nothing was pushed"
        )
    return proc.stdout.strip()


def remote_state(repo: Path, branch: str) -> RemoteState:
    """Classify the push (step 1) without writing anything."""
    local = local_sha(repo, branch)
    remote = remote_sha(repo, branch)
    if not remote:
        return RemoteState(action=PUSH_CREATE, local_sha=local, remote_sha="")
    if remote == local:
        return RemoteState(action=PUSH_UP_TO_DATE, local_sha=local, remote_sha=remote)
    anc = run_git(
        repo, ["merge-base", "--is-ancestor", remote, local], timeout=120
    ).returncode
    action = PUSH_FAST_FORWARD if anc == 0 else PUSH_DIVERGED
    return RemoteState(action=action, local_sha=local, remote_sha=remote)


def remote_sha(repo: Path, branch: str) -> str:
    """``origin``'s sha for *branch*, or ``""`` when the ref does not exist.

    ``ls-remote`` patterns tail-match, so the fully-qualified ref is queried
    and the returned ref name matched exactly — a bare branch name would also
    match an unrelated ``a/<branch>``.
    """
    dst = f"refs/heads/{branch}"
    ls = run_git(repo, ["ls-remote", "--heads", "origin", dst], timeout=120)
    if ls.returncode != 0:
        raise DeliverRefused(f"git ls-remote origin {dst} failed: {ls.stderr.strip()}")
    for line in ls.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == dst:
            return parts[0]
    return ""


def _set_upstream(repo: Path, branch: str) -> str | None:
    """Point the local branch at ``origin/<branch>`` (the ``push -u`` half).

    The push itself sends a pinned object refspec, which cannot carry ``-u``'s
    meaning, so the tracking config is written directly — no network, and it
    lands whether the ref was created now or already existed.

    **Genuinely best-effort, and reported.** It runs AFTER the push, so a
    read-only or locked ``.git/config`` must neither propagate an exception
    (the caller would then classify a delivery whose branch IS on ``origin``
    as "nothing written") nor pass silently as though the documented ``push
    -u`` contract had been met. Every failure — a nonzero ``git config``, a
    timeout, a missing binary — becomes one note the caller prints as
    ``[Friction]``; the delivery itself stands, since only the operator's
    later ``git status`` depends on this.
    """
    try:
        for args in (
            ["config", f"branch.{branch}.remote", "origin"],
            ["config", f"branch.{branch}.merge", f"refs/heads/{branch}"],
        ):
            proc = run_git(repo, args, timeout=120)
            if proc.returncode != 0:
                return (
                    f"the branch is pushed, but `git {' '.join(args)}` failed "
                    f"({proc.stderr.strip() or f'exit {proc.returncode}'}), so "
                    f"{branch} has no upstream set — `git branch --set-upstream-to "
                    f"origin/{branch} {branch}` finishes it"
                )
    except (OSError, subprocess.SubprocessError) as exc:
        return (
            f"the branch is pushed, but its upstream could not be set ({exc}); "
            f"`git branch --set-upstream-to origin/{branch} {branch}` finishes it"
        )
    return None


def push_branch(repo: Path, branch: str, state: RemoteState) -> str | None:
    """Push *branch* to ``origin`` — append-only; a diverged ref is refused.

    ``develop deliver`` never rewrites a remote branch: the divergence may be
    a collaborator's commit, and re-developing is the operator's other choice.

    Returns a note when the push landed but the local tracking config did not
    (:func:`_set_upstream`), else ``None``. A failure of the push ITSELF is a
    :class:`DeliverRefused`; nothing after the branch is on ``origin`` may
    raise, or the caller would report a committed push as nothing written.
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
        return None
    # Push the **object** the classification was made against, not the symbolic
    # ref: a local process that advances or rewrites the branch between the
    # classification and the push would otherwise have us send a commit nobody
    # decided was append-only, and then report the measured sha as delivered.
    # (Also a fully-qualified refspec, never a bare positional: a ref
    # legitimately named `--receive-pack=…` would be read by git as an option
    # naming a program to run — CWE-88, the same reason the reads above
    # fully-qualify.) A remote that moved under us still fails closed: git
    # rejects a non-fast-forward, and we never pass --force.
    proc = run_git(repo, ["push", "origin", f"{state.local_sha}:refs/heads/{branch}"])
    if proc.returncode != 0:
        # A push can COMMIT and still report failure — the remote applied the
        # update and the response was lost. Ask the remote what it holds before
        # calling this "nothing was written": exiting 1 on a ref that now
        # exists would send the operator looking for a branch that is already
        # there, and would hide a delivery half-done.
        try:
            landed = remote_sha(repo, branch)
        except (DeliverRefused, OSError, subprocess.SubprocessError) as exc:
            # The push may have landed and the read that would settle it is
            # unavailable too. "Nothing was written" is the one thing that
            # cannot be asserted here, so say so and let the caller report a
            # possibly-committed partial instead of a clean refusal.
            raise DeliverUncertain(
                f"git push reported failure ({proc.stderr.strip()}) and the "
                f"remote could not then be read ({exc}), so it is not known "
                f"whether {state.local_sha[:12]} reached origin/{branch}. "
                "Nothing else was attempted; re-run when the remote answers — "
                "the push is append-only, so a second run is safe either way"
            ) from exc
        if landed != state.local_sha:
            _refuse_unless_the_push_may_have_landed(
                repo, branch, state=state, landed=landed, stderr=proc.stderr.strip()
            )
    return _set_upstream(repo, branch)


def _refuse_unless_the_push_may_have_landed(
    repo: Path, branch: str, *, state: RemoteState, landed: str, stderr: str
) -> None:
    """Classify a failed push whose remote is now at neither the pre-push sha
    nor ours — raising unless the evidence says the push landed after all.

    "The ref is not at my sha" is not "my push wrote nothing": between the
    failed push and this read another actor can append to the same branch, and
    then the ref holds a THIRD sha with ours in its history. Exiting 1 there
    claims nothing was written about a commit that is on ``origin``, leaves
    the PR unopened and the story ungated, and sends every retry into the
    diverged refusal.

    So the three answers are kept apart:

    * the ref is **exactly where it was** (or still absent) — the push is a
      proven non-landing, and the refusal is the truth;
    * the ref moved and **contains** our commit — the push landed (or someone
      else carried it); the caller goes on and the head read-back in step 2
      reports what the PR now delivers;
    * anything else — the remote does not hold our commit, or containment
      could not be read at all. Neither is "nothing was written", so it is a
      partial the operator re-runs, never a clean refusal.
    """
    if landed == state.remote_sha:
        # untouched since the classification: the push really did not land
        raise DeliverRefused(f"git push failed: {stderr}")
    contained = _remote_contains(repo, branch, state.local_sha)
    if contained is True:
        return
    before = state.remote_sha[:12] or "(absent)"
    raise DeliverUncertain(
        f"git push reported failure ({stderr}) and origin/{branch} is now at "
        f"{landed[:12] or '(absent)'} — neither the {before} it held before "
        f"nor the {state.local_sha[:12]} this delivery sent, so "
        "another actor moved the branch and it is not known whether the push "
        + (
            "landed first (it is not in the branch's history now). "
            if contained is False
            else "landed (the branch's history could not be read). "
        )
        + "Nothing else was attempted; reconcile the branch and re-run — the "
        "push is append-only, so a second run is safe either way"
    )


def _remote_contains(repo: Path, branch: str, sha: str) -> bool | None:
    """Whether ``origin/<branch>`` has *sha* in its history — ``None`` when
    that cannot be established.

    The remote tip may be a commit this checkout has never seen, so the branch
    is fetched first (a read: it writes only ``FETCH_HEAD`` locally). Every
    failure answers ``None`` rather than ``False``: "I could not look" must
    not be reported as "your commit is not there".
    """
    try:
        fetch = run_git(repo, ["fetch", "--quiet", "origin", f"refs/heads/{branch}"])
        if fetch.returncode != 0:
            return None
        anc = run_git(
            repo, ["merge-base", "--is-ancestor", sha, "FETCH_HEAD"], timeout=120
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if anc.returncode == 0:
        return True
    return False if anc.returncode == 1 else None


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
    proc = run_git(repo, ["remote", "get-url", "origin"], timeout=120)
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
    candidates: Sequence[OpenPullRequest],
    *,
    head_sha: str,
    base: str,
    moves_to_ours: str = "",
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

    *moves_to_ours* is the PREVIEW's extra: a PR's head is whatever
    ``origin/<branch>`` points at, so a delivery that will fast-forward that
    ref carries the PR's head with it. Asked before the push (``--dry-run``),
    a candidate sitting at the CURRENT remote sha is therefore ours the moment
    step 1 lands — refusing it would make the preview disagree with the real
    invocation by construction. The real step 2 asks *after* the push and
    passes ``""``, so nothing is ever adopted on a projection that has not
    happened.
    """
    ours = {head_sha, *([moves_to_ours] if moves_to_ours else [])}
    for pr in candidates:
        if pr.cross_repository:
            continue
        if pr.head_sha not in ours:
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
    expected = f"{head_sha[:12]}"
    if moves_to_ours:
        expected += f" (or {moves_to_ours[:12]}, which the push would advance)"
    return None, (
        f"an open PR already claims this branch name but is not this delivery "
        f"({described}); expected a same-repository PR whose head is "
        f"{expected} and whose base is {base}. Refusing to adopt a PR "
        "that is not this branch's — nothing was gated. Close or rename the "
        "other PR, or deliver from a branch name it does not claim"
    )


@dataclass(frozen=True)
class PRPlan:
    """What step 2 would do, decided from reads alone.

    *base* is the resolved base (``--base``, else the repository's default);
    exactly one of *existing* (adopt it) / *refusal* (a same-name PR that is
    not ours) is set, and neither means "open a new PR onto *base*".
    """

    base: str
    existing: OpenPullRequest | None
    refusal: str
    projected: bool = False
    """The adoption depends on a push that has not happened yet — the PR is at
    ``origin``'s current sha and this delivery's fast-forward will carry it to
    the delivered one. Only ever set for the ``--dry-run`` preview."""


def pr_plan(
    repo: Path,
    *,
    branch: str,
    repo_name: str,
    base: str | None,
    head_sha: str,
    moves_to_ours: str = "",
) -> PRPlan:
    """The READ-ONLY half of step 2: resolve the base and the adoption
    decision, writing nothing.

    Shared with ``--dry-run`` on purpose. A preview that names the base as
    "the repo's default branch" and the PR step as "adopt or open" has
    resolved neither: the real invocation asks GitHub for both and can refuse
    outright at the second. One function, the same three reads in the same
    order, so the plan the operator approves is the decision the delivery
    takes (`feedback-extract-shared-no-duplicate-impl`).

    Sharing the function is not enough on its own, because the preview asks
    **before** step 1 and the real step 2 asks after it: an open PR for a
    branch whose remote ref this delivery is about to fast-forward is reported
    at the OLD sha now and at the delivered one then. *moves_to_ours* carries
    that push into the decision (see :func:`adoptable`), so the two agree
    without a race being involved at all.
    """
    try:
        # Resolved BEFORE the adoption decision: the base a candidate must
        # match is the one this delivery targets, whether the operator named
        # it or the repository's default supplied it.
        resolved_base = base or default_base_branch(repo, repo_name=repo_name)
        candidates = list_open_prs_for_branch(repo, branch, repo_name=repo_name)
    except RuntimeError as exc:
        raise DeliverRefused(str(exc)) from exc
    existing, refusal = adoptable(
        candidates,
        head_sha=head_sha,
        base=resolved_base,
        moves_to_ours=moves_to_ours,
    )
    return PRPlan(
        base=resolved_base,
        existing=existing,
        refusal=refusal,
        projected=existing is not None and existing.head_sha != head_sha,
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
    plan = pr_plan(
        repo, branch=branch, repo_name=repo_name, base=base, head_sha=head_sha
    )
    resolved_base = plan.base
    if plan.existing is not None:
        return plan.existing.url, True
    if plan.refusal:
        raise DeliverRefused(plan.refusal)
    try:
        try:
            return (
                create_pr(
                    repo,
                    branch=branch,
                    base=resolved_base,
                    title=title,
                    # built lazily: an adopted PR needs no body, and composing
                    # one costs a run-dir read
                    body=body(),
                    repo_name=repo_name,
                ),
                False,
            )
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            # A create can COMMIT and still report failure — GitHub opened the
            # PR and the response was lost. The same ambiguity the push handles
            # by re-reading the remote: ask again before concluding that no PR
            # exists, or the command leaves an open, UNGATED PR behind while
            # reporting that it opened none (and, on an already-equal remote,
            # calls the whole delivery "nothing written").
            recovered, asked = _created_despite_the_error(
                repo,
                branch=branch,
                repo_name=repo_name,
                head_sha=head_sha,
                base=resolved_base,
            )
            if recovered is not None:
                return recovered, True
            if not asked:
                # The re-ask failed too: a PR may exist. Never a refusal.
                raise DeliverUncertain(
                    f"gh pr create failed ({exc}) and the open-PR list could "
                    "not then be read, so it is not known whether a PR was "
                    "opened for this branch. Re-run when gh answers — an "
                    "existing PR at this head is adopted, not duplicated"
                ) from exc
            raise
    except RuntimeError as exc:
        raise DeliverRefused(str(exc)) from exc


def delivered_pr_head(repo: Path, *, branch: str, repo_name: str, pr_url: str) -> str:
    """The revision GitHub reports behind *pr_url* NOW, or ``""`` if it could
    not be read.

    The one fact this command cannot derive from its own inputs. ``gh pr
    create`` opens a PR from a head **branch**, never from a sha — GitHub's
    API takes no revision — so between the push and the create another actor
    can advance ``origin/<branch>`` and the PR opens at *their* commit while
    the body this delivery composed describes ours. The adopt path has the
    same check-then-act shape around ``gh pr list``. Nothing closes that
    window; reading the head back after the fact is what turns it from an
    unnoticed false claim into a stated one — so the caller compares this
    against the sha it pushed, and withdraws the approval claim (and says so)
    when they differ.

    ``""`` is "not answered", never "no head": a gh failure, or a PR that is
    no longer in the open list, must not read as a mismatch.
    """
    try:
        candidates = list_open_prs_for_branch(repo, branch, repo_name=repo_name)
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return ""
    for pr in candidates:
        if pr.url == pr_url:
            return pr.head_sha
    return ""


def _created_despite_the_error(
    repo: Path, *, branch: str, repo_name: str, head_sha: str, base: str
) -> tuple[str | None, bool]:
    """The PR a failed ``gh pr create`` opened anyway — ``(url, answered)``.

    Re-asks first with exactly the adoption rule of the first pass —
    same-repo, our head, our base — so the PR recovered on that branch is one
    this delivery could have adopted outright. *answered* separates the two
    ways of getting no url: GitHub said there is no such PR (``(None, True)``
    — the create really failed), and the re-ask could not be made at all
    (``(None, False)`` — a PR may exist, and the caller must not report an
    absence it never established).

    **A moved head is not proof of no write.** A PR's head is whatever
    ``origin/<branch>`` points at, so an actor appending to the branch between
    the create and this read leaves our own new PR reporting THEIR sha. The
    exact rule would then answer "no such PR" about a PR that exists, open and
    ungated, and every retry would refuse. So the fallback is the widest match
    that is still provably about this delivery: the caller has just tried to
    open a PR **for this branch** and saw none a moment earlier, so a
    same-repository PR on this branch, onto this base, is that one (or a
    concurrent one for the same branch, which delivers the same commits — the
    fork class, which is what the head check exists for, stays excluded).
    Nothing rests on the recovered head: the caller reads it back in step 2b,
    reports the delivery partial and withdraws any approval claim.
    """
    try:
        candidates = list_open_prs_for_branch(repo, branch, repo_name=repo_name)
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return None, False
    found, _ = adoptable(candidates, head_sha=head_sha, base=base)
    if found is not None:
        return found.url, True
    moved = next(
        (
            pr
            for pr in candidates
            if not pr.cross_repository and pr.base_ref == base and pr.head_sha
        ),
        None,
    )
    return (moved.url if moved is not None else None), True
