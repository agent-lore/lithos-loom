"""PR delivery + the Copilot review round (T9, PRD decision #9).

After the panel approves, the trusted HOST-side plugin process (never an
agent container — agents have no push credentials) pushes the branch, opens a
PR via ``gh``, and runs one **Copilot round**:

    push + open PR -> request Copilot -> poll for its review
      -> translate inline comments into a synthetic review handoff
         (Copilot is a reviewer DATA SOURCE, not a panel member — it can only
         review a PR, which exists only after approval)
      -> ONE coder fix round on the resumed session (no panel re-review)
      -> T4 test gate on the fix commit; RED => the fix is NOT pushed
      -> reply to each Copilot comment thread ("Fixed in <sha> — ..." /
         "Not changed — ..." for disputes), marked as automated.

No Copilot re-request — one round is the bound; a re-trigger loop is future
work. A Copilot timeout degrades gracefully: the PR stands as approved.

Layered like :mod:`containers` / :mod:`test_gate`: pure builders up top,
thin ``gh`` / ``git`` wrappers at the bottom (monkeypatched in tests).
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lithos_loom.github_client import parse_github_ref

from . import containers, engines, handoff, run_outcome, turns
from .agent_session import build_run_cmd
from .check_runner import run_delivery_test_gate
from .findings import FindingLedger
from .rounds import commit_round

logger = logging.getLogger(__name__)

COPILOT_LOGIN = "copilot-pull-request-reviewer[bot]"
# Copilot's reviewer slug for the requested_reviewers POST (the display name
# "Copilot" silently no-ops — learned the hard way on this repo).
COPILOT_REVIEWER = "copilot-pull-request-reviewer[bot]"
AUTOMATED_MARKER = "_(automated reply by story-develop)_"
DEFAULT_COPILOT_TIMEOUT = 600  # seconds; observed turnaround is ~2-4 min
COPILOT_POLL_SECONDS = 15


@dataclass(frozen=True)
class CopilotComment:
    """One Copilot inline comment, as fetched from the PR."""

    comment_id: int
    path: str
    line: int | None
    body: str


@dataclass(frozen=True)
class DeliveryOutcome:
    """What the delivery phase did (for the summary + Lithos posting)."""

    pr_url: str
    pr_number: int
    pushed: bool = True
    copilot_requested: bool = False
    copilot_reviewed: bool = False
    copilot_settled: bool = True
    """Whether all the comments Copilot's summary claimed actually materialised
    within the wait budget. ``False`` means the round was INCOMPLETE — some
    comments hadn't appeared in time and may be unaddressed (the #91
    comment-lag race); the operator should review the PR or re-trigger."""
    comments_count: int = 0
    fix_committed: bool = False
    fix_pushed: bool = False
    fix_gate_verdict: str | None = None  # GREEN | RED | TIMEOUT | None (no gate)
    replies_posted: int = 0
    fix_sha: str | None = None  # the (pushed) fix commit
    extra_cost_usd: float = 0.0  # the Copilot fix turn's spend
    notes: tuple[str, ...] = field(default=())


# --- pure builders ------------------------------------------------------------


def parse_issue_ref(github_issue_url: str) -> tuple[str, int] | None:
    """``https://github.com/o/r/issues/42`` -> ``("o/r", 42)``; None if not.

    Thin adapter over :func:`~lithos_loom.github_client.parse_github_ref`,
    filtered to issue (not PR) refs.
    """
    ref = parse_github_ref(github_issue_url)
    if ref is None or ref.kind != "issue":
        return None
    return ref.repo, ref.number


def closes_line(github_issue_url: str | None, pr_repo: str) -> str:
    """The ``Closes …`` line linking the PR to its source issue, or ``""``.

    Same-repo issues use the short ``#N`` form; cross-repo issues need the
    full ``owner/repo#N`` form for GitHub's closing keywords to bind.
    """
    if not github_issue_url:
        return ""
    ref = parse_issue_ref(github_issue_url)
    if ref is None:
        return ""
    repo, number = ref
    if repo.lower() == pr_repo.lower():
        return f"Closes #{number}"
    return f"Closes {repo}#{number}"


def build_pr_body(
    *,
    description: str,
    acceptance_criteria: str | None,
    reviews_summary: str,
    rounds: int,
    gate_verdict: str | None,
    cost_usd: float,
    task_id: str | None,
    issue_closes: str = "",
) -> str:
    """The generated PR body: provenance + verdicts, not the whole log."""
    parts = ["## What", "", description.strip(), ""]
    if issue_closes:
        parts += [issue_closes, ""]
    if acceptance_criteria:
        parts += ["## Acceptance criteria", "", acceptance_criteria.strip(), ""]
    parts += [
        "## Review",
        "",
        f"- verdicts: {reviews_summary}",
        f"- rounds: {rounds}",
    ]
    if gate_verdict:
        parts.append(f"- test gate: {gate_verdict}")
    parts.append(f"- agent cost: ${cost_usd:.2f}")
    if task_id:
        parts.append(f"- Lithos task: `{task_id}`")
    parts += [
        "",
        "Per-round commits are intentional (the dialogue history); "
        "squash-merge keeps main clean.",
        "",
        "🤖 Generated by lithos-loom story-develop",
    ]
    return "\n".join(parts)


def comments_to_handoff_text(comments: list[CopilotComment]) -> str:
    """Render Copilot's inline comments as a synthetic review handoff.

    Findings carry blank ids (the ``copilot`` ledger assigns them) and
    ``minor`` severity (Copilot expresses no severity; the round is
    non-gating either way — the PR is already open).
    """
    lines = [
        "## Status: FINDINGS",
        "## Summary",
        f"Copilot left {len(comments)} inline comment(s) on the PR.",
        "## Findings",
    ]
    for c in comments:
        loc = f"{c.path}:{c.line}" if c.line else c.path
        rationale = " ".join(c.body.split())  # one line; parser-safe
        lines += [
            "- finding_id:",
            "  severity: minor",
            "  status: open",
            f'  files: ["{loc}"]',
            f"  rationale: {rationale}",
        ]
    return "\n".join(lines) + "\n"


def reply_body(
    *,
    fixed: bool,
    sha: str | None,
    coder_response: str,
    held_back_verdict: str | None = None,
) -> str:
    """The per-thread reply: fix reference, held-back notice, or pushback.

    *held_back_verdict* covers the committed-but-not-pushed case (RED
    regression gate): the code DID change, so "Not changed" would be
    misleading — say what happened instead.
    """
    response = coder_response.strip() or "(no further detail given)"
    if held_back_verdict is not None:
        head = (
            f"A fix was prepared but NOT pushed — the regression test gate "
            f"came back {held_back_verdict} on the fix commit (see the PR "
            f"comment). Intended change: {response}"
        )
    elif fixed and sha:
        head = f"Fixed in {sha[:10]} — {response}"
    elif fixed:
        head = f"Addressed — {response}"
    else:
        head = f"Not changed — {response}"
    return f"{head}\n\n{AUTOMATED_MARKER}"


# --- thin gh / git wrappers (monkeypatched in tests) ---------------------------


def _run(
    args: list[str], *, cwd: Path, timeout: int = 120
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=timeout
    )


def push_branch(wt: Path, branch: str) -> None:
    """Host-side push of the worktree branch to origin. Raises on failure."""
    proc = _run(["git", "push", "-u", "origin", branch], cwd=wt, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f"git push failed: {proc.stderr.strip()}")


def repo_name_with_owner(wt: Path) -> str:
    """``owner/repo`` of the worktree's origin (via gh)."""
    proc = _run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        cwd=wt,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh repo view failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def create_pr(wt: Path, *, branch: str, base: str, title: str, body: str) -> str:
    """Open the PR; returns its URL. Raises on failure."""
    proc = _run(
        [
            "gh",
            "pr",
            "create",
            "--head",
            branch,
            "--base",
            base,
            "--title",
            title,
            "--body",
            body,
        ],
        cwd=wt,
        timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh pr create failed: {proc.stderr.strip()}")
    url = proc.stdout.strip().splitlines()[-1].strip()
    if not url.startswith("http"):
        raise RuntimeError(f"gh pr create returned no URL: {proc.stdout!r}")
    return url


def pr_number_from_url(url: str) -> int:
    """Extract the PR number from a canonical GitHub PR URL; raise if it can't.

    Thin adapter over :func:`~lithos_loom.github_client.parse_github_ref`; the
    hard failure (vs. ``None``) is deliberate — delivery has a real PR URL here.
    """
    ref = parse_github_ref(url)
    if ref is None or ref.kind != "pull":
        raise RuntimeError(f"cannot parse PR number from {url!r}")
    return ref.number


def request_copilot(wt: Path, repo: str, pr_number: int) -> bool:
    """Request the Copilot reviewer; False (logged) on failure — non-fatal."""
    proc = _run(
        [
            "gh",
            "api",
            "-X",
            "POST",
            f"repos/{repo}/pulls/{pr_number}/requested_reviewers",
            "-f",
            f"reviewers[]={COPILOT_REVIEWER}",
        ],
        cwd=wt,
    )
    if proc.returncode != 0:
        logger.warning(
            "story-develop: requesting Copilot on %s#%d failed: %s",
            repo,
            pr_number,
            proc.stderr.strip(),
        )
        return False
    return True


def request_operator_review(wt: Path, repo: str, pr_number: int, login: str) -> str:
    """Request *login* as a reviewer on the PR; assign them if they authored it.

    Best-effort and non-fatal (mirrors :func:`request_copilot`): never raises,
    logs on failure. GitHub forbids requesting review from the PR *author* (HTTP
    422) — the common case when loom runs under the operator's own ``gh`` auth —
    so on that error we fall back to *assigning* the PR to them, which is allowed
    for self and still fires a native GitHub notification.

    Returns ``"review_requested"``, ``"assigned"``, or ``"failed"``.
    """
    proc = _run(
        [
            "gh",
            "api",
            "-X",
            "POST",
            f"repos/{repo}/pulls/{pr_number}/requested_reviewers",
            "-f",
            f"reviewers[]={login}",
        ],
        cwd=wt,
    )
    if proc.returncode == 0:
        return "review_requested"

    stderr = proc.stderr.strip()
    # Only the specific self-author 422 ("Review cannot be requested from pull
    # request author") falls back to assigning. Every other failure — a bad
    # login, a non-collaborator, repo policy, or any other 422 — is a real
    # failure the operator should see, NOT silently downgraded to an assignee.
    if "pull request author" not in stderr:
        logger.warning(
            "story-develop: requesting review from %s on %s#%d failed: %s",
            login,
            repo,
            pr_number,
            stderr,
        )
        return "failed"

    assigned = _run(
        [
            "gh",
            "api",
            "-X",
            "POST",
            f"repos/{repo}/issues/{pr_number}/assignees",
            "-f",
            f"assignees[]={login}",
        ],
        cwd=wt,
    )
    if assigned.returncode == 0:
        return "assigned"
    logger.warning(
        "story-develop: assigning %s to %s#%d failed: %s",
        login,
        repo,
        pr_number,
        assigned.stderr.strip(),
    )
    return "failed"


_GENERATED_RE = re.compile(r"generated (\d+|no) comments?", re.IGNORECASE)


def copilot_expected_comments(wt: Path, repo: str, pr_number: int) -> int | None:
    """The comment count Copilot's review summary claims, or status markers.

    Returns ``None`` while no Copilot review exists yet; ``-1`` when a review
    exists but its body doesn't state a count (treat as "unknown, expect
    some"); otherwise the stated count (``"no comments"`` -> 0).
    """
    proc = _run(["gh", "api", f"repos/{repo}/pulls/{pr_number}/reviews"], cwd=wt)
    if proc.returncode != 0:
        return None
    try:
        reviews: list[dict[str, Any]] = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    for r in reviews:
        if (r.get("user") or {}).get("login") != COPILOT_LOGIN:
            continue
        m = _GENERATED_RE.search(str(r.get("body") or ""))
        if m is None:
            return -1
        token = m.group(1)
        return 0 if token == "no" else int(token)  # noqa: S105 — "token" is a parsed Copilot-marker value (a count or "no"), not a credential
    return None


def fetch_copilot_comments(wt: Path, repo: str, pr_number: int) -> list[CopilotComment]:
    """Copilot's top-level inline comments on the PR (replies excluded)."""
    proc = _run(["gh", "api", f"repos/{repo}/pulls/{pr_number}/comments"], cwd=wt)
    if proc.returncode != 0:
        return []
    try:
        raw: list[dict[str, Any]] = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    out: list[CopilotComment] = []
    for c in raw:
        if (c.get("user") or {}).get("login") != COPILOT_LOGIN:
            continue
        if c.get("in_reply_to_id"):  # thread replies are not findings
            continue
        out.append(
            CopilotComment(
                comment_id=int(c["id"]),
                path=str(c.get("path") or ""),
                line=c.get("line") or c.get("original_line"),
                body=str(c.get("body") or ""),
            )
        )
    return out


def wait_for_copilot(
    wt: Path,
    repo: str,
    pr_number: int,
    *,
    timeout: int,
    poll_seconds: int = COPILOT_POLL_SECONDS,
) -> int | None:
    """Poll until Copilot's review lands; its expected comment count, or
    ``None`` on timeout (see :func:`copilot_expected_comments`)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        expected = copilot_expected_comments(wt, repo, pr_number)
        if expected is not None:
            return expected
        time.sleep(min(poll_seconds, max(1, deadline - time.monotonic())))
    return None


def fetch_copilot_comments_settled(
    wt: Path,
    repo: str,
    pr_number: int,
    *,
    expected: int,
    grace_seconds: int = 180,
    poll_seconds: int = 5,
    settle_seconds: int = 15,
) -> tuple[list[CopilotComment], bool]:
    """Fetch Copilot's inline comments, waiting for them to MATERIALISE.

    Returns ``(comments, settled)``. *settled* is ``True`` only when the wait
    ended because the comments actually arrived and the count stabilised —
    ``False`` when it ended at the deadline (so the caller can flag the round
    incomplete rather than silently treat a partial/empty result as complete).

    Copilot's inline comments lag its review summary by a variable amount
    (a first-poll empty list is the norm, not the signal — this raced in the
    first T9 dogfood run and again at 90 s grace).  *expected* comes from the
    review body: 0 returns immediately (settled); a positive count waits until
    that many are visible AND the count stabilises; -1 (unknown) waits for the
    count to stabilise after any appear.  The grace window bounds the wait.

    "Stabilised" means the count has not changed for *settle_seconds*.
    This prevents returning before late-arriving comments materialise —
    the original 90 s window with an immediate return on threshold hit
    was the root cause of the recurrence.
    """
    if expected == 0:
        return fetch_copilot_comments(wt, repo, pr_number), True
    deadline = time.monotonic() + grace_seconds
    comments: list[CopilotComment] = []
    prev_count = 0
    settled_at: float | None = None  # monotonic time the count last changed
    while True:
        comments = fetch_copilot_comments(wt, repo, pr_number)
        now = time.monotonic()
        if len(comments) != prev_count:
            prev_count = len(comments)
            settled_at = now  # (re)start the settle clock

        threshold_hit = (expected > 0 and len(comments) >= expected) or (
            expected < 0 and len(comments) > 0
        )
        stable = settled_at is not None and now - settled_at >= settle_seconds
        if threshold_hit and stable:
            return comments, True

        if now >= deadline:
            return comments, False
        time.sleep(min(poll_seconds, max(1, deadline - now)))


def post_thread_reply(
    wt: Path, repo: str, pr_number: int, comment_id: int, body: str
) -> bool:
    proc = _run(
        [
            "gh",
            "api",
            "-X",
            "POST",
            f"repos/{repo}/pulls/{pr_number}/comments/{comment_id}/replies",
            "-f",
            f"body={body}",
        ],
        cwd=wt,
    )
    if proc.returncode != 0:
        logger.warning(
            "story-develop: reply to comment %d failed: %s",
            comment_id,
            proc.stderr.strip(),
        )
        return False
    return True


def post_pr_comment(wt: Path, pr_number: int, body: str) -> bool:
    proc = _run(["gh", "pr", "comment", str(pr_number), "--body", body], cwd=wt)
    if proc.returncode != 0:
        logger.warning("story-develop: PR comment failed: %s", proc.stderr.strip())
        return False
    return True


# --- orchestration --------------------------------------------------------------

# Overhead beyond the bounded agent phases — the worktree pushes (push_branch
# runs twice, start + fix, each with a 300s timeout) plus the gh API calls (PR
# open, reviewer/Copilot requests, per-comment replies — _run's 120s timeout
# each). A crashed delivery only takes longer to declare dead if this is too
# large, whereas too small re-opens the false-timeout; so it is deliberately
# generous (a healthy run is never timed out).
_DELIVERY_OVERHEAD_SECONDS = 1800


def delivery_budget_seconds(config, *, copilot_timeout: int, coder_timeout: int) -> int:
    """Upper bound on the wall-clock :func:`deliver` can legitimately spend (#189).

    `develop attach` records a delivery deadline from this (via
    :func:`run_outcome.record_delivery_deadline` — the develop-run marker contract
    lives in :mod:`run_outcome`) so it can bound a *crashed* delivery without ever
    timing out a *healthy* slow one. It sums every bounded phase below — **keep in
    sync with deliver() if a phase is added** — each bound maps to the named
    primitive that phase drives (ARCH-1.S7):

    - the Copilot review round — ``copilot_timeout`` (:func:`wait_for_copilot`
      + :func:`fetch_copilot_comments_settled`),
    - the Copilot fix coder turn — ``coder_timeout`` (the one-shot
      ``turns.run_turn`` in :func:`_deliver_after_open`),
    - the regression gate on the fix commit — ``config.test_timeout``
      (:func:`check_runner.run_delivery_test_gate`),
    - plus push / PR / gh overhead (a flat margin).

    That attach's no-deadline fallback (``run_outcome.DELIVERY_FALLBACK_SECONDS``)
    stays above this budget — so it can't false-fire on a healthy default-config
    run — is enforced by the executed invariant
    ``test_delivery_fallback_exceeds_the_full_default_delivery_budget``, not prose.
    """
    return (
        copilot_timeout
        + coder_timeout
        + config.test_timeout
        + _DELIVERY_OVERHEAD_SECONDS
    )


def deliver_guarded(
    config,
    result,
    *,
    open_pr: bool,
    no_copilot: bool,
    copilot_timeout: int,
    coder_timeout: int,
    github_issue_url: str | None,
    task_id: str | None,
) -> tuple[DeliveryOutcome | None, str | None]:
    """Guarded PR delivery for an approved run — shared daemon/standalone seam.

    The single develop→deliver seam both ``__main__`` entry points call, so the
    #189 / #194 delivery contract is enforced identically on both surfaces instead
    of living in two hand-kept copies (the asymmetry ARCH-1.S3 fixes: standalone
    used to complete the task + exit 0 on a failed delivery). Returns
    ``(delivery, error)``:

    - ``(None, None)`` — nothing to deliver (``open_pr`` off, or the run wasn't
      approved). The caller renders "skipped" / nothing.
    - ``(DeliveryOutcome, None)`` — delivered.
    - ``(None, reason)`` — ``deliver()`` raised before a PR opened (#194). An
      approved dialogue with no PR is NOT a clean success, so the reason is
      returned for the caller to map to a non-success (daemon: ``result.json``
      failed + ``EXIT_FAILED``; standalone: skip ``--complete-on-approval`` + exit
      non-zero). The failure is recorded in the run's private ``delivery.json``
      (:func:`run_outcome.record_delivery_failure`) so ``develop attach`` reports
      it terminally rather than waiting out the #189 deadline.

    Records the #189 delivery deadline before delivery starts, so attach can bound
    a crashed/orphaned delivery without timing out a healthy slow one. The marker
    writers live in :mod:`run_outcome` (ARCH-3.R2); this only calls them.
    """
    if not (open_pr and result.approved):
        return None, None
    run_outcome.record_delivery_deadline(
        config.run_dir,
        budget_seconds=delivery_budget_seconds(
            config, copilot_timeout=copilot_timeout, coder_timeout=coder_timeout
        ),
    )
    try:
        delivery = deliver(
            config,
            result,
            no_copilot=no_copilot,
            copilot_timeout=copilot_timeout,
            coder_timeout=coder_timeout,
            github_issue_url=github_issue_url,
            task_id=task_id,
        )
    except Exception as exc:  # delivery failure must not sink the run (#194)
        logger.exception("story-develop PR delivery failed")
        reason = str(exc)
        run_outcome.record_delivery_failure(config.run_dir, reason=reason)
        return None, reason
    return delivery, None


def deliver(
    config,
    result,
    *,
    no_copilot: bool = False,
    copilot_timeout: int = DEFAULT_COPILOT_TIMEOUT,
    coder_timeout: int = 3600,
    github_issue_url: str | None = None,
    task_id: str | None = None,
) -> DeliveryOutcome:
    """Push the approved branch and open the PR, then run the Copilot round.

    Once :func:`create_pr` returns, the PR exists — so any later failure must NOT
    lose its url. The post-open work is delegated to :func:`_deliver_after_open`,
    and a raise there degrades to a delivered-with-notes outcome **carrying the
    url** (#192) rather than propagating — which would otherwise strand the
    operator with an approved run and no PR in the `attach` summary.
    """
    wt: Path = result.worktree
    notes: list[str] = []

    push_branch(wt, result.branch)
    repo = repo_name_with_owner(wt)
    title = config.description.strip().splitlines()[0][:90]
    body = build_pr_body(
        description=config.description,
        acceptance_criteria=config.acceptance_criteria,
        reviews_summary=" ".join(f"[{r.reviewer}]={r.status}" for r in result.reviews),
        rounds=result.rounds,
        gate_verdict=result.test_gate.verdict if result.test_gate else None,
        cost_usd=result.total_cost_usd,
        task_id=task_id,
        issue_closes=closes_line(github_issue_url, repo),
    )
    pr_url = create_pr(
        wt, branch=result.branch, base=config.base_branch, title=title, body=body
    )
    pr_number = pr_number_from_url(pr_url)
    logger.info("story-develop %s: opened PR %s", config.run_id, pr_url)

    try:
        return _deliver_after_open(
            config,
            result,
            wt=wt,
            repo=repo,
            title=title,
            pr_url=pr_url,
            pr_number=pr_number,
            notes=notes,
            no_copilot=no_copilot,
            copilot_timeout=copilot_timeout,
            coder_timeout=coder_timeout,
        )
    except Exception as exc:
        # The PR is already open; never strand its url on a later failure (#192).
        logger.warning(
            "story-develop %s: delivery did not finish after opening %s: %s",
            config.run_id,
            pr_url,
            exc,
        )
        return DeliveryOutcome(
            pr_url=pr_url,
            pr_number=pr_number,
            notes=(*notes, f"delivery did not finish after opening the PR: {exc}"),
        )


def _deliver_after_open(
    config,
    result,
    *,
    wt: Path,
    repo: str,
    title: str,
    pr_url: str,
    pr_number: int,
    notes: list[str],
    no_copilot: bool,
    copilot_timeout: int,
    coder_timeout: int,
) -> DeliveryOutcome:
    """The post-PR-open delivery work: notify, the Copilot round, the fix turn +
    regression gate, and the per-thread replies. Separated so :func:`deliver` can
    guarantee the PR url survives any failure here (#192).

    Drives the shared story-develop primitives directly (ARCH-1.S7) —
    :func:`agent_session.build_run_cmd`, :func:`handoff.render_prompt` /
    :func:`handoff.render_findings`, :func:`rounds.commit_round`, and
    :func:`check_runner.run_delivery_test_gate` — rather than re-implementing the
    coder round inline or reaching develop's private aliases through a lazy import.
    """
    # #113: notify the operator their PR awaits review (native GitHub
    # notification). Best-effort; the note threads into every return below.
    if config.notify_github_login:
        notified = request_operator_review(
            wt, repo, pr_number, config.notify_github_login
        )
        if notified == "review_requested":
            notes.append(f"requested review from @{config.notify_github_login}")
        elif notified == "assigned":
            notes.append(
                f"assigned PR to @{config.notify_github_login} "
                "(review can't be self-requested on your own PR)"
            )
        else:
            notes.append(f"could not notify @{config.notify_github_login} of the PR")

    if no_copilot:
        return DeliveryOutcome(pr_url=pr_url, pr_number=pr_number, notes=tuple(notes))

    requested = request_copilot(wt, repo, pr_number)
    if not requested:
        notes.append("Copilot review request failed; respond manually")
        return DeliveryOutcome(pr_url=pr_url, pr_number=pr_number, notes=tuple(notes))
    # copilot_timeout is the whole-round budget: the review summary wait AND
    # the comment-materialisation wait share it.
    copilot_deadline = time.monotonic() + copilot_timeout
    expected = wait_for_copilot(wt, repo, pr_number, timeout=copilot_timeout)
    if expected is None:
        notes.append(
            f"Copilot review not received within {copilot_timeout}s; "
            "respond manually when it lands"
        )
        return DeliveryOutcome(
            pr_url=pr_url,
            pr_number=pr_number,
            copilot_requested=True,
            notes=tuple(notes),
        )

    # Bound the comment-settle by the REMAINING copilot budget — the #91 fix.
    # Comments lag the summary by a variable amount; a flat 90/180s window
    # silently missed late arrivals. Give materialisation whatever of
    # copilot_timeout wait_for_copilot left, and NOTHING more — copilot_timeout
    # is a hard cap on the whole round. A near-exhausted budget just means the
    # round is flagged incomplete (below), which is honest.
    settle_budget = max(0, int(copilot_deadline - time.monotonic()))
    comments, settled = fetch_copilot_comments_settled(
        wt, repo, pr_number, expected=expected, grace_seconds=settle_budget
    )
    # `settled` is authoritative (the fetch reports whether the comments
    # actually arrived + stabilised, vs hit the deadline). A non-settle means
    # the round is INCOMPLETE, not all-clear (#91): the operator must be told,
    # not left thinking Copilot was happy.
    if not settled:
        if expected > 0:
            notes.append(
                f"Copilot review did not settle: expected {expected} comment(s), "
                f"{len(comments)} arrived within {settle_budget}s — the rest may "
                "be unaddressed; review the PR or re-trigger Copilot"
            )
        else:
            notes.append(
                "Copilot review's comment stream did not stabilise within "
                f"{settle_budget}s ({len(comments)} seen) — there may be more; "
                "review the PR or re-trigger Copilot"
            )
    if not comments:
        # Nothing to fix this round. With expected>0 and none materialised this
        # is a deferral (flagged above + copilot_settled=False), not an
        # all-clear; with expected==0 it is a genuine clean review.
        return DeliveryOutcome(
            pr_url=pr_url,
            pr_number=pr_number,
            copilot_requested=True,
            copilot_reviewed=True,
            copilot_settled=settled,
            notes=tuple(notes),
        )

    # --- synthetic copilot review -> ledger ids -> ONE coder fix round ------
    fix_round = result.rounds + 1
    synthetic = comments_to_handoff_text(comments)
    parsed = handoff.parse_review_handoff(synthetic)
    ledger = FindingLedger("copilot")
    canonical = ledger.apply_review(parsed, fix_round)
    id_to_comment = {f.finding_id: c for f, c in zip(canonical, comments, strict=True)}
    # keep the synthetic review on disk: conversation-log + audit parity
    synthetic_path = config.handoff_dir / handoff.reviewer_handoff_name(
        fix_round, "copilot"
    )
    synthetic_path.write_text(synthetic, encoding="utf-8")

    coder_handoff = handoff.coder_handoff_name(fix_round)
    prompt = handoff.render_prompt(
        handoff.load_prompt("copilot_fix.md"),
        pr_url=pr_url,
        acceptance_criteria=config.effective_acceptance_criteria,
        findings=handoff.render_findings(canonical),
        handoff_file=coder_handoff,
    )
    name, run_cmd = build_run_cmd(
        config,
        agent="coder",
        engine=engines.get_engine(config.coder),
        config_dir=config.coder_config_dir,
        wt=wt,
        read_only=False,
    )
    try:
        containers.start_container(run_cmd)
        # Same coder, same session resumed — it must run on the SAME tool (#94)
        # + model + reasoning effort the develop loop used (#93), not silently
        # revert to the agent default while finalizing the branch.
        # ``result.coder_session`` is the live handle (the codex thread_id, or
        # the claude uuid).
        #
        # Deliberately a bare ``turns.run_turn`` — no usage-limit pause / tool-switch
        # reaction and no #114 salvage nudge, unlike develop()'s
        # ``agent_session.turn_with_limit_pauses``. This one-shot fix turn just
        # fails cleanly (below) if the coder is usage-limited; whether it should
        # gain the limit reaction is tracked as a follow-up, not decided here.
        turn = turns.run_turn(
            container=name,
            prompt=prompt,
            session_id=result.coder_session,
            resume=True,
            timeout=coder_timeout,
            engine=engines.get_engine(config.coder),
            model=config.coder_model,
            effort=config.coder_effort,
        )
    finally:
        containers.stop_container(name)

    extra_cost = turn.cost_usd
    if not turn.succeeded:
        notes.append(
            f"Copilot fix turn failed (exit {turn.exit_code}); comments left "
            "unanswered — respond manually"
        )
        post_pr_comment(
            wt,
            pr_number,
            "story-develop attempted an automated response to Copilot's "
            f"review but the coder turn failed.\n\n{AUTOMATED_MARKER}",
        )
        return DeliveryOutcome(
            pr_url=pr_url,
            pr_number=pr_number,
            copilot_requested=True,
            copilot_reviewed=True,
            copilot_settled=settled,
            comments_count=len(comments),
            extra_cost_usd=extra_cost,
            notes=tuple(notes),
        )

    # per-finding coder responses (fixed/disputed + public one-liner)
    responses: dict[str, Any] = {}
    coder_path = config.handoff_dir / coder_handoff
    try:
        coder_parsed = handoff.parse_review_handoff(
            coder_path.read_text(encoding="utf-8")
        )
        responses = {f.finding_id: f for f in coder_parsed.findings}
    except Exception:  # tolerant: replies degrade gracefully
        notes.append("coder handoff unreadable; replies use generic text")

    new_sha = commit_round(wt, f"story-develop copilot round: {title}")
    fix_committed = new_sha is not None

    # regression gate on the fix commit; RED => do NOT push the fix
    fix_pushed = False
    gate_verdict: str | None = None
    if fix_committed:
        # The delivery-side regression gate on the fix commit: test-only,
        # ledger-less — the intentional delivery-vs-develop divergence lives in
        # check_runner.run_delivery_test_gate (its docstring carries the rationale).
        gate = run_delivery_test_gate(config, wt, new_sha, fix_round)
        gate_verdict = gate.verdict if gate else None
        if gate is None or gate.passed:
            push_branch(wt, result.branch)
            fix_pushed = True
        else:
            notes.append(
                "Copilot fix NOT pushed: test gate "
                f"{gate.verdict} on the fix commit (kept locally in the worktree)"
            )
            post_pr_comment(
                wt,
                pr_number,
                "story-develop prepared a fix for Copilot's comments but the "
                f"regression test gate came back {gate.verdict}, so it was NOT "
                f"pushed. The candidate fix is in the run worktree.\n\n"
                f"{AUTOMATED_MARKER}",
            )

    # per-thread replies
    replies = 0
    for fid, comment in id_to_comment.items():
        f = responses.get(fid)
        disputed = f is not None and f.status == "disputed"
        coder_response = f.coder_response if f is not None else ""
        held_back = (
            gate_verdict
            if (not disputed and fix_committed and not fix_pushed)
            else None
        )
        body_text = reply_body(
            fixed=not disputed and fix_pushed,
            sha=new_sha if fix_pushed else None,
            coder_response=coder_response,
            held_back_verdict=held_back,
        )
        if post_thread_reply(wt, repo, pr_number, comment.comment_id, body_text):
            replies += 1

    # Audit parity: the conversation log must include the Copilot exchange.
    _append_copilot_round_to_log(
        config, fix_round=fix_round, pr_url=pr_url, coder_handoff=coder_handoff
    )

    return DeliveryOutcome(
        pr_url=pr_url,
        pr_number=pr_number,
        copilot_requested=True,
        copilot_reviewed=True,
        copilot_settled=settled,
        comments_count=len(comments),
        fix_committed=fix_committed,
        fix_pushed=fix_pushed,
        fix_gate_verdict=gate_verdict,
        replies_posted=replies,
        fix_sha=new_sha if fix_pushed else None,
        extra_cost_usd=extra_cost,
        notes=tuple(notes),
    )


def _append_copilot_round_to_log(
    config, *, fix_round: int, pr_url: str, coder_handoff: str
) -> None:
    """Append the Copilot review + the coder's response to conversation.md.

    ``develop()`` wrote the log before delivery started; without this append
    the shipped log would omit the whole Copilot exchange.
    """
    log_path = config.run_dir / run_outcome.CONVERSATION_LOG
    review_name = handoff.reviewer_handoff_name(fix_round, "copilot")
    section = handoff.render_log_section(
        config.handoff_dir,
        f"## Copilot round ({pr_url})",
        [("Reviewer [copilot]", review_name), ("Coder", coder_handoff)],
    )
    with open(log_path, "a", encoding="utf-8") as fh:
        # leading newline separates the appended section from develop()'s log body
        fh.write("\n" + "\n".join(section))
