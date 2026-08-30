"""External-review ingestion for still-open ``pr`` gates (PRD S2, detection).

A delivered PR sits behind a ``pr`` gate awaiting a human merge. Reviews left
on it in the meantime — Copilot, other bots, humans — were invisible to loom:
the reconcile sweep polled only merge state. This module is the detection half
of PRD S2 (``docs/prd/pr-reconciliation.md``): each sweep of a still-open
gate, read the PR's reviews and inline review comments and surface anything
new as a one-shot ``[ExternalReview]`` finding on the blocked *story* (the
task an operator watches), with a de-dup marker on the *gate* (the task the
sweep re-visits).

De-dup is a pair of **high-water marks** — ``last_review_id`` and
``last_comment_id`` — not a seen-id set: GitHub REST ids are monotonically
increasing, so the marks are bounded and a summary-only review (an
``APPROVED``/``CHANGES_REQUESTED`` with zero inline comments, which comment-id
de-dup cannot represent at all) keys on its review id. The marker is scoped to
the PR url, mirroring ``develop_pr_merge_state``: a replacement PR (fresh url,
fresh id space) re-evaluates from scratch. It is deliberately a **separate
metadata key** from the merge marker so neither can trip the other's skip
logic.

Per-state posting policy (PRD S2): ``CHANGES_REQUESTED`` always posts;
``COMMENTED`` (and any unrecognised state) posts only with a non-empty body;
``APPROVED`` / ``DISMISSED`` advance the marker silently — an approval is not
an operator action item. Inline comments post unless they are thread replies
or loom's own automated replies. Skipped material still advances the marks so
it is never re-fetched or re-considered.

Remediation (injecting trusted findings into ``develop converge``) is the
follow-up slice; this module only detects and reports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from lithos_loom.gates import PrGateSpec
from lithos_loom.github_client import (
    GitHubClient,
    GitHubError,
    PullRequestReview,
    PullRequestReviewComment,
)
from lithos_loom.github_models import (
    is_automated_reply,
    is_landed_fix_reply,
    review_is_actionable,
)
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._findings import post_finding_then_mark, write_marker

__all__ = [
    "EXTERNAL_REVIEW",
    "REVIEW_SEEN_KEY",
    "IngestResult",
    "ingest_external_reviews",
]

# Stable, machine-parseable finding prefix (see AGENTS.md): new external review
# activity (a PR review, or inline review comments) landed on a delivered PR
# that is still awaiting merge behind its `pr` gate.
EXTERNAL_REVIEW = "[ExternalReview]"

# Gate-metadata key holding the ingestion state: {"pr_url", "last_review_id",
# "last_comment_id", "last_comment_at"}. pr_url scopes the marks; the ids are
# the exact de-dup; last_comment_at (ISO) feeds the bounded `since` cursor on
# the comment fetch so a long-lived PR isn't re-paginated every sweep.
REVIEW_SEEN_KEY = "external_review_seen"

# Repo permission levels whose holders' landed-fix replies count as proof —
# the ADR 0011 trust line (allowlisted bots aside, which never post replies).
_TRUSTED_PERMISSIONS = frozenset({"admin", "write"})

# Rendering bounds: a finding is a breadcrumb, not a transcript.
_EXCERPT_CHARS = 160
_MAX_LISTED = 20


@dataclass(frozen=True)
class IngestResult:
    """What one ingestion pass posted, for the remediation dispatcher (slice C).

    ``posted`` is True only when an ``[ExternalReview]`` finding actually
    landed on the story; the actionable lists carry the exact material it
    described so the dispatch decision (trust, own-sha) filters what was
    *reported*, never a re-fetch. Every quiet path — no news, silent states,
    orphan gate, GitHub error — reports ``posted=False`` with empty lists.
    """

    posted: bool = False
    actionable_reviews: list[PullRequestReview] = field(default_factory=list)
    actionable_comments: list[PullRequestReviewComment] = field(default_factory=list)


@dataclass(frozen=True)
class _Seen:
    """The marker's parsed high-water marks (zeros when absent/foreign-url)."""

    last_review_id: int
    last_comment_id: int
    last_comment_at: str | None


def _read_seen(gate: Any, pr_url: str) -> _Seen:
    raw = gate.metadata.get(REVIEW_SEEN_KEY)
    if not isinstance(raw, dict) or raw.get("pr_url") != pr_url:
        # Absent, malformed, or recorded for a *different* PR (the story was
        # re-developed into a replacement) — start from scratch; the old PR's
        # id space says nothing about the new one's.
        return _Seen(0, 0, None)
    review_id = raw.get("last_review_id")
    comment_id = raw.get("last_comment_id")
    comment_at = raw.get("last_comment_at")
    return _Seen(
        last_review_id=review_id if isinstance(review_id, int) else 0,
        last_comment_id=comment_id if isinstance(comment_id, int) else 0,
        last_comment_at=comment_at if isinstance(comment_at, str) else None,
    )


def _since(seen: _Seen) -> datetime | None:
    """The stored boundary minus a one-second overlap.

    GitHub's ``since`` is strictly-after with second precision (PR #344
    review, finding 1): a comment that becomes visible in the same second as
    the stored maximum would be excluded forever — the id high-water mark
    cannot rescue a row that is never returned. Querying one second early
    re-fetches at most a second's worth of rows, and the id mark drops the
    repeats.
    """
    if seen.last_comment_at is None:
        return None
    try:
        boundary = datetime.fromisoformat(seen.last_comment_at)
    except ValueError:
        return None  # corrupt cursor → unbounded fetch; ids still de-dup
    return boundary - timedelta(seconds=1)


def _comment_posts(
    comment: PullRequestReviewComment, handled_roots: frozenset[int]
) -> bool:
    if comment.in_reply_to_id is not None:
        return False  # thread replies ride on their root comment
    if comment.comment_id in handled_roots:
        return False  # the inline round already remediated + replied to it
    return not is_automated_reply(comment.body)


def _fixed_reply_candidates(
    comments: list[PullRequestReviewComment],
) -> list[tuple[int, str]]:
    """``(root_id, reply_author)`` pairs whose reply *claims* a landed fix.

    A claim, not proof: the marker and the ``Fixed in`` head are public body
    strings any commenter can copy. :func:`_proven_handled` authenticates the
    author before the claim may suppress anything.
    """
    return [
        (c.in_reply_to_id, c.author)
        for c in comments
        if c.in_reply_to_id is not None and is_landed_fix_reply(c.body)
    ]


async def _proven_handled(
    candidates: list[tuple[int, str]],
    repo: str,
    github: GitHubClient,
    ctx: SubscriptionContext,
) -> frozenset[int]:
    """Root-comment ids proven handled by an authenticated landed-fix reply.

    Backfill guard (PR #344 review, finding 2): until the inline Copilot
    round is retired (slice D), delivery remediates root comments, pushes the
    fix and replies — all *before* the ``pr`` gate exists. A markerless
    gate's first sweep would otherwise re-report that already-handled history
    as fresh ``[ExternalReview]`` findings. On later sweeps this is naturally
    inert: handled roots sit below the id high-water mark anyway.

    Proof has two halves, both required:

    - **Landed** (PR #344 re-review 1): only the ``Fixed in <sha>`` reply
      shape counts. Held-back (red gate) and "Not changed" replies carry the
      same ``AUTOMATED_MARKER`` while the root is still unresolved — and
      "Addressed" (fixed, no sha) records no landed commit.
    - **Authenticated** (PR #344 re-review 2): the reply's author must hold
      write/admin on the repo — the ADR 0011 trust line, and the identity
      loom's own replies post under (the operator's ``gh`` login). Body
      strings are forgeable; without this, any outside commenter could hide a
      trusted reviewer's root forever by copying the two tokens. One
      permission call per unseen author; a probe failure counts as untrusted
      (fail closed for *suppression*: a duplicate report is recoverable, a
      hidden root is not).
    """
    handled: set[int] = set()
    author_trusted: dict[str, bool] = {}
    for root_id, author in candidates:
        trusted = author_trusted.get(author)
        if trusted is None:
            try:
                permission = await github.get_collaborator_permission(repo, author)
            except GitHubError as exc:
                ctx.logger.warning(
                    "[Friction] external-reviews: permission probe for reply "
                    "author %r on %s failed (%s: %s); treating their "
                    "landed-fix replies as unproven this sweep",
                    author,
                    repo,
                    type(exc).__name__,
                    exc,
                )
                permission = "none"
            trusted = permission in _TRUSTED_PERMISSIONS
            author_trusted[author] = trusted
        if trusted:
            handled.add(root_id)
    return frozenset(handled)


def _excerpt(body: str) -> str:
    line = " ".join(body.strip().split())
    if len(line) > _EXCERPT_CHARS:
        return line[: _EXCERPT_CHARS - 1] + "…"
    return line


def _render_summary(
    pr_url: str,
    reviews: list[PullRequestReview],
    comments: list[PullRequestReviewComment],
    *,
    story_id: str,
    gate_id: str,
    extra_note: str | None = None,
) -> str:
    lines = [f"{EXTERNAL_REVIEW} new review activity on delivered PR {pr_url}:"]
    for review in reviews[:_MAX_LISTED]:
        state = review.state or "COMMENTED"
        at = f", at {review.commit_id[:12]}" if review.commit_id else ""
        excerpt = _excerpt(review.body)
        tail = f": {excerpt}" if excerpt else ""
        lines.append(f"- review by {review.author} ({state}{at}){tail}")
    for comment in comments[:_MAX_LISTED]:
        loc = f"{comment.path}:{comment.line}" if comment.line else comment.path
        url = f" ({comment.html_url})" if comment.html_url else ""
        lines.append(
            f"- comment by {comment.author} on {loc}: {_excerpt(comment.body)}{url}"
        )
    hidden = max(0, len(reviews) - _MAX_LISTED) + max(0, len(comments) - _MAX_LISTED)
    if hidden:
        lines.append(f"- …and {hidden} more (see the PR)")
    if extra_note:
        lines.append(extra_note)
    lines.append(
        f"story {story_id} remains blocked on gate {gate_id}; review the PR "
        f"before merging"
    )
    return "\n".join(lines)


def _new_marker(
    pr_url: str,
    seen: _Seen,
    reviews: list[PullRequestReview],
    comments: list[PullRequestReviewComment],
) -> dict[str, Any]:
    """Advance the marks over EVERYTHING fetched — including replies, silent
    states and loom's own automated replies — so skipped material is never
    re-fetched (the `since` cursor) or re-considered (the ids)."""
    marker: dict[str, Any] = {
        "pr_url": pr_url,
        "last_review_id": max(seen.last_review_id, *(r.review_id for r in reviews), 0),
        "last_comment_id": max(
            seen.last_comment_id, *(c.comment_id for c in comments), 0
        ),
    }
    stamps = [c.updated_at for c in comments if c.updated_at is not None]
    if stamps:
        marker["last_comment_at"] = max(stamps).isoformat()
    elif seen.last_comment_at is not None:
        marker["last_comment_at"] = seen.last_comment_at
    return marker


async def ingest_external_reviews(
    gate: Any,
    spec: PrGateSpec,
    story_id: str | None,
    github: GitHubClient,
    ctx: SubscriptionContext,
    *,
    extra_note: str | None = None,
) -> IngestResult:
    """Ingest new review activity on one still-open gate's PR. Never raises.

    Called from :func:`.._develop_pr_merge.reconcile_pr_gate`'s still-open
    branch, which has already fetched the PR (proving it exists) and resolved
    the gate's waiting story. A transient GitHub failure logs and returns with
    the marker untouched — the whole batch retries next sweep. The
    finding-then-mark ordering (via :func:`.._findings.post_finding_then_mark`)
    means a crash between the two costs at most one duplicate finding.

    ``extra_note`` is appended to the finding body (S5b: the remediation
    dispatcher states budget exhaustion *inside* the detection breadcrumb, so
    going over budget never blinds the operator). Returns the posted batch for
    the dispatcher — see :class:`IngestResult`.
    """
    seen = _read_seen(gate, spec.pr_url)
    try:
        reviews = await github.list_pull_request_reviews(spec.repo, spec.pr_number)
        comments = await github.list_pull_request_review_comments(
            spec.repo, spec.pr_number, since=_since(seen)
        )
    except GitHubError as exc:
        ctx.logger.warning(
            "[Friction] external-reviews: fetching reviews for %s#%d "
            "(gate %s) failed (%s: %s); will retry next sweep",
            spec.repo,
            spec.pr_number,
            gate.id,
            type(exc).__name__,
            exc,
        )
        return IngestResult()

    new_reviews = [r for r in reviews if r.review_id > seen.last_review_id]
    new_comments = [c for c in comments if c.comment_id > seen.last_comment_id]
    if not new_reviews and not new_comments:
        return IngestResult()  # idle PR: no fetch produced news, write nothing

    handled_roots = await _proven_handled(
        _fixed_reply_candidates(comments), spec.repo, github, ctx
    )
    # A non-blocking summary review ALL of whose own roots the inline round
    # already remediated is part of that same handled history — suppressing
    # it keeps the round's Copilot review out of the first sweep. Bound to
    # the review that OWNS the handled roots (PR #345 review F3) — an
    # author-wide rule would hide a later summary re-review behind an
    # ancient fixed root. Narrow on purpose: CHANGES_REQUESTED always posts
    # (review_is_actionable) — a reply does not prove the requested changes
    # were accepted.
    review_roots: dict[int, list[int]] = {}
    for c in comments:
        if c.in_reply_to_id is None and c.pull_request_review_id is not None:
            review_roots.setdefault(c.pull_request_review_id, []).append(c.comment_id)
    handled_review_ids = frozenset(
        rid
        for rid, roots in review_roots.items()
        if roots and all(r in handled_roots for r in roots)
    )
    actionable_reviews = [
        r
        for r in new_reviews
        if review_is_actionable(r)
        and not (r.state != "CHANGES_REQUESTED" and r.review_id in handled_review_ids)
    ]
    actionable_comments = [c for c in new_comments if _comment_posts(c, handled_roots)]
    marker = {REVIEW_SEEN_KEY: _new_marker(spec.pr_url, seen, reviews, comments)}

    if not actionable_reviews and not actionable_comments:
        # Only silent material (approvals, replies, our own automated replies):
        # advance the marks so it is never re-walked, post nothing.
        await write_marker(
            ctx, task_id=gate.id, marker=marker, subsystem="external-reviews"
        )
        return IngestResult()

    if story_id is None:
        # Orphan gate (no waiter edge): nothing to post the finding on.
        await write_marker(
            ctx, task_id=gate.id, marker=marker, subsystem="external-reviews"
        )
        ctx.logger.warning(
            "[Friction] external-reviews: gate %s has no waiter; review "
            "activity on %s recorded but not posted",
            gate.id,
            spec.pr_url,
        )
        return IngestResult()

    landed = await post_finding_then_mark(
        ctx,
        task_id=story_id,
        summary=_render_summary(
            spec.pr_url,
            actionable_reviews,
            actionable_comments,
            story_id=story_id,
            gate_id=gate.id,
            extra_note=extra_note,
        ),
        marker=marker,
        subsystem="external-reviews",
        retry_hint="will retry next sweep",
        marker_task_id=gate.id,
    )
    if not landed:
        # PR #346 review F4: the finding or the de-dup mark did not land —
        # the batch retries next sweep and must NOT dispatch remediation now
        # (either the operator breadcrumb is missing, or an unmarked batch
        # would double-dispatch when it re-posts).
        return IngestResult()
    ctx.logger.info(
        "external-reviews: posted %s for %s (%d review(s), %d comment(s)) on story %s",
        EXTERNAL_REVIEW,
        spec.pr_url,
        len(actionable_reviews),
        len(actionable_comments),
        story_id,
    )
    return IngestResult(
        posted=True,
        actionable_reviews=actionable_reviews,
        actionable_comments=actionable_comments,
    )
