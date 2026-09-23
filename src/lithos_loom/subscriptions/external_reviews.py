"""External-review ingestion for still-open ``pr`` gates (PRD S2, detection).

A delivered PR sits behind a ``pr`` gate awaiting a human merge. Reviews left
on it in the meantime — Copilot, other bots, humans — were invisible to loom:
the reconcile sweep polled only merge state. This module is the detection half
of PRD S2 (``docs/prd/pr-reconciliation.md``): each sweep of a still-open
gate, read the PR's reviews, inline review comments and Conversation-tab
comments and surface anything new as a one-shot ``[ExternalReview]`` finding
on the blocked *story* (the task an operator watches), with a de-dup marker
on the *gate* (the task the sweep re-visits).

De-dup is a triple of **high-water marks** — ``last_review_id``,
``last_comment_id`` and ``last_issue_comment_id`` — not a seen-id set: GitHub
REST ids are monotonically increasing, so the marks are bounded and a
summary-only review (an ``APPROVED``/``CHANGES_REQUESTED`` with zero inline
comments, which comment-id de-dup cannot represent at all) keys on its review
id. The three streams have separate id spaces, hence three marks. The marker
is scoped to the PR url, mirroring ``develop_pr_merge_state``: a replacement
PR (fresh url, fresh id space) re-evaluates from scratch. It is deliberately a
**separate metadata key** from the merge marker so neither can trip the
other's skip logic.

Per-state posting policy (PRD S2): ``CHANGES_REQUESTED`` always posts;
``COMMENTED`` (and any unrecognised state) posts only with a non-empty body;
``DISMISSED`` advances the marker silently — a dismissal has had its say.
Inline comments post unless they are thread replies or loom's own automated
replies. Conversation comments (#353 — the only channel open to the PR's own
author, i.e. the operator on every loom-delivered PR) post unless empty or
loom-authored (a reply or a ``[NeedsHuman]`` notice, both posted under the
operator's login). Skipped material still advances the marks so it is never
re-fetched or re-considered.

**An approval is not a finding.** A row that asks for nothing — an
``APPROVED`` review with no inline comments of its own, a "No findings /
LGTM / ready to merge" comment — is classified as an ``approval``
(:func:`~lithos_loom.github_review_streams.dispositions`): recorded on the
marks like any other activity, reported on the story with the ``approval``
disposition, and NEVER placed in the dispatchable batch. Remediation run
827cedf8 on lens #100 spent a coder turn and a full panel pass to learn what
Dave's "No findings. Ready to merge." already said, and deferred that PR's
merge-gate behind it. A comment that approves *and* asks ("LGTM, but rename
X") is a finding, not an approval.

Remediation (injecting trusted findings into ``develop converge``) is the
follow-up slice; this module only detects and reports.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from lithos_loom.gates import PrGateSpec
from lithos_loom.github_client import GitHubClient, GitHubError
from lithos_loom.github_review_activity import ExternalReviewActivity, ReviewStream
from lithos_loom.github_review_streams import (
    STREAM_ADAPTERS,
    AuthorTrust,
    dispositions,
    fetch_activity,
    proven_handled,
    render_row,
)
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._findings import post_finding_then_mark, write_marker

__all__ = [
    "APPROVAL_NOTE",
    "EXTERNAL_REVIEW",
    "UNVERIFIED_APPROVAL_NOTE",
    "REVIEW_SEEN_KEY",
    "IngestResult",
    "PendingMarkerProvider",
    "ingest_external_reviews",
]

# Stable, machine-parseable finding prefix (see AGENTS.md): new external review
# activity (a PR review, inline review comments, or Conversation-tab comments)
# landed on a delivered PR that is still awaiting merge behind its `pr` gate.
EXTERNAL_REVIEW = "[ExternalReview]"

# Gate-metadata key holding the ingestion state: {"pr_url", "last_review_id",
# "last_comment_id", "last_comment_at", "last_issue_comment_id",
# "last_issue_comment_at"}. pr_url scopes the marks; the ids are the exact
# de-dup; the *_at stamps (ISO) feed the bounded `since` cursor on each
# comment fetch so a long-lived PR isn't re-paginated every sweep.
REVIEW_SEEN_KEY = "external_review_seen"

# A finding is a breadcrumb, not a transcript: rows listed per stream, capped.
_MAX_LISTED = 20

# The `approval` disposition, stated on the finding when the whole batch asks
# for nothing: an approval is not a finding, so no coder, no panel and no
# remediation round are spent on it (the sibling of 8cfa3184).
APPROVAL_NOTE = (
    "disposition: approval — this activity carries no actionable finding, so "
    "nothing was remediated and no remediation round was reserved or spent"
)

# The same disposition when NO approval in the batch came from an author loom
# could verify as a repo write/admin collaborator (PR #425 review, security
# f-003). Anyone who can comment on a public PR can leave an approval, and
# this line is read while deciding to merge — loom must not lend a drive-by
# account its own voice. Bots are labelled this way too: the allowlist that
# trusts one lives in the remediation settings, which detection cannot see.
UNVERIFIED_APPROVAL_NOTE = (
    "disposition: approval — this activity carries no actionable finding, but "
    "no approval in it comes from an author loom can verify as a repo "
    "write/admin collaborator: it is recorded, not evidence the PR is ready. "
    "Nothing was dispatched and no remediation round was reserved or spent"
)


@dataclass(frozen=True)
class IngestResult:
    """What one ingestion pass posted, for the remediation dispatcher (slice C).

    ``posted`` is True only when an ``[ExternalReview]`` finding actually
    landed on the story; ``actionable`` carries the exact rows it described
    (every stream, in finding order) so the dispatch decision (trust,
    own-sha) filters what was *reported*, never a re-fetch. Every quiet path
    — no news, silent states, orphan gate, GitHub error — reports
    ``posted=False`` with an empty batch.

    ``approvals`` are the rows that ask for nothing (an APPROVED review, a
    "No findings / LGTM" comment). They are reported with the ``approval``
    disposition and are NOT in ``actionable``: a batch of nothing but
    approvals posts, dispatches nothing and spends no budget round.

    ``failed`` (PR #348 re-review 1) separates "nothing to report" from a
    RETRYABLE failure — a GitHub listing error, or a batch whose finding /
    de-dup mark did not land. On the still-open path the next sweep retries
    either way; on the MERGED path the caller must not resolve the gate over
    a failed final observation (there is no next sweep after resolution).
    """

    posted: bool = False
    failed: bool = False
    actionable: list[ExternalReviewActivity] = field(default_factory=list)
    approvals: list[ExternalReviewActivity] = field(default_factory=list)

    def count(self, stream: ReviewStream) -> int:
        return sum(1 for a in self.actionable if a.stream is stream)


# The pending-trigger provider's shape: the actionable batch, in finding order.
PendingMarkerProvider = Callable[
    [list[ExternalReviewActivity]], Awaitable[Mapping[str, Any] | None]
]


@dataclass(frozen=True)
class _Mark:
    """One stream's parsed high-water mark (zeros when absent/foreign-url)."""

    last_id: int = 0
    last_at: str | None = None


def _read_seen(gate: Any, pr_url: str) -> dict[ReviewStream, _Mark]:
    raw = gate.metadata.get(REVIEW_SEEN_KEY)
    if not isinstance(raw, dict) or raw.get("pr_url") != pr_url:
        # Absent, malformed, or recorded for a *different* PR (the story was
        # re-developed into a replacement) — start from scratch; the old PR's
        # id space says nothing about the new one's.
        return {adapter.stream: _Mark() for adapter in STREAM_ADAPTERS}
    seen: dict[ReviewStream, _Mark] = {}
    for adapter in STREAM_ADAPTERS:
        last_id = raw.get(adapter.mark_id_key)
        last_at = raw.get(adapter.mark_at_key) if adapter.mark_at_key else None
        seen[adapter.stream] = _Mark(
            last_id=last_id if isinstance(last_id, int) else 0,
            last_at=last_at if isinstance(last_at, str) else None,
        )
    return seen


def _since(stamp: str | None) -> datetime | None:
    """A stored boundary minus a one-second overlap.

    GitHub's ``since`` is strictly-after with second precision (PR #344
    review, finding 1): a comment that becomes visible in the same second as
    the stored maximum would be excluded forever — the id high-water mark
    cannot rescue a row that is never returned. Querying one second early
    re-fetches at most a second's worth of rows, and the id mark drops the
    repeats. One cursor per stream that has a ``since``.
    """
    if stamp is None:
        return None
    try:
        boundary = datetime.fromisoformat(stamp)
    except ValueError:
        return None  # corrupt cursor → unbounded fetch; ids still de-dup
    return boundary - timedelta(seconds=1)


def _listed(
    rows: list[ExternalReviewActivity], render: Callable[[ExternalReviewActivity], str]
) -> tuple[list[str], int]:
    """Rendered lines per stream, each capped; plus how many were elided."""
    lines: list[str] = []
    hidden = 0
    for adapter in STREAM_ADAPTERS:  # listed per stream, each capped
        of_stream = [a for a in rows if a.stream is adapter.stream]
        lines.extend(render(a) for a in of_stream[:_MAX_LISTED])
        hidden += max(0, len(of_stream) - _MAX_LISTED)
    return lines, hidden


def _approval_renderer(
    verified: Mapping[str, bool],
) -> Callable[[ExternalReviewActivity], str]:
    """Render an approval row, tagged with its disposition AND whether loom
    could verify the author (security f-003): the approval of an account that
    merely *can* comment on the PR must not read like a collaborator's."""

    def render(a: ExternalReviewActivity) -> str:
        tag = "approval" if verified.get(a.author) else "approval, unverified author"
        return f"- [{tag}] {render_row(a).removeprefix('- ')}"

    return render


def _render_summary(
    pr_url: str,
    batch: list[ExternalReviewActivity],
    *,
    approvals: list[ExternalReviewActivity] | None = None,
    verified: Mapping[str, bool] | None = None,
    story_id: str,
    gate_id: str,
    extra_note: str | None = None,
    post_merge: bool = False,
) -> str:
    lines = [f"{EXTERNAL_REVIEW} new review activity on delivered PR {pr_url}:"]
    rendered, hidden = _listed(batch, render_row)
    lines.extend(rendered)
    verified = verified or {}
    rendered, elided = _listed(approvals or [], _approval_renderer(verified))
    lines.extend(rendered)
    hidden += elided
    if hidden:
        lines.append(f"- …and {hidden} more (see the PR)")
    if not batch:
        # Nothing but approvals: say so where the operator reads it, and say
        # that no round was spent (the whole point of the disposition) — but
        # only claim "no actionable finding" in loom's own voice when an
        # author loom can verify said it.
        lines.append(
            APPROVAL_NOTE
            if any(verified.get(a.author) for a in approvals or ())
            else UNVERIFIED_APPROVAL_NOTE
        )
    if extra_note:
        lines.append(extra_note)
    if post_merge:
        # PR #348 re-review 3: this record is written on the MERGED path — an
        # instruction to review before merging would be impossible to follow.
        lines.append(
            "the PR was already merged when this activity was first observed "
            "— recorded for audit; automated remediation is no longer "
            "possible, follow up on the merged changes manually"
        )
    else:
        lines.append(
            f"story {story_id} remains blocked on gate {gate_id}; review the "
            f"PR before merging"
        )
    return "\n".join(lines)


def _new_marker(
    pr_url: str,
    seen: Mapping[ReviewStream, _Mark],
    activities: list[ExternalReviewActivity],
) -> dict[str, Any]:
    """Advance every stream's marks over EVERYTHING fetched — including
    replies, silent states and loom's own automated replies / notices — so
    skipped material is never re-fetched (the `since` cursors) or
    re-considered (the ids)."""
    marker: dict[str, Any] = {"pr_url": pr_url}
    for adapter in STREAM_ADAPTERS:
        rows = [a for a in activities if a.stream is adapter.stream]
        mark = seen[adapter.stream]
        marker[adapter.mark_id_key] = max(
            mark.last_id, *(a.activity_id for a in rows), 0
        )
        if adapter.mark_at_key is None:
            continue
        stamps = [a.updated_at for a in rows if a.updated_at is not None]
        if stamps:
            marker[adapter.mark_at_key] = max(stamps).isoformat()
        elif mark.last_at is not None:
            marker[adapter.mark_at_key] = mark.last_at
    return marker


def _reply_author_trust(
    spec: PrGateSpec, github: GitHubClient, ctx: SubscriptionContext
) -> AuthorTrust:
    """The sweep's trust: write/admin humans only (bots never post replies, and
    the bot allowlist lives in the remediation settings this module cannot
    see), a probe failure logged as friction and treated as unverified.

    Two readers, one probe budget per batch: the landed-fix proof, and the
    label on an approval row (security f-003).
    """

    def on_error(author: str, exc: Exception) -> None:
        ctx.logger.warning(
            "[Friction] external-reviews: permission probe for "
            "author %r on %s failed (%s: %s); treating their "
            "landed-fix replies as unproven and their approvals as "
            "unverified this sweep",
            author,
            spec.repo,
            type(exc).__name__,
            exc,
        )

    async def permission_of(author: str) -> str:
        return await github.get_collaborator_permission(spec.repo, author)

    return AuthorTrust(permission_of, on_error=on_error)


async def ingest_external_reviews(
    gate: Any,
    spec: PrGateSpec,
    story_id: str | None,
    github: GitHubClient,
    ctx: SubscriptionContext,
    *,
    extra_note: str | None = None,
    post_merge: bool = False,
    pending_marker_for: PendingMarkerProvider | None = None,
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
    going over budget never blinds the operator). ``pending_marker_for``
    (PR #346 re-reviews 1+3) is the dispatcher's pending-trigger provider:
    called with the actionable batch just before the marker write — so
    dispatchability (trust, own-sha) is decided BEFORE any durable state —
    and its entry, if any, is folded into the SAME ``task_update`` as the
    seen marks. The marks consume the batch, so its dispatch debt must
    become durable in the same write or not at all (a failed combined write
    retries the whole batch next sweep). ``post_merge`` (PR #348 re-reviews
    1+3) marks the merged-path final observation: the rendered record says
    the activity was discovered after merge, and the caller reads
    ``IngestResult.failed`` to defer gate resolution instead of losing the
    record. Returns the posted batch for the
    dispatcher — see :class:`IngestResult`.
    """
    seen = _read_seen(gate, spec.pr_url)
    try:
        activities = await fetch_activity(
            github,
            spec.repo,
            spec.pr_number,
            since={stream: _since(mark.last_at) for stream, mark in seen.items()},
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
        return IngestResult(failed=True)

    new = [a for a in activities if a.activity_id > seen[a.stream].last_id]
    if not new:
        return IngestResult()  # idle PR: no fetch produced news, write nothing

    # Handled-ness is proven over the whole fetched history (roots below the
    # marks still vouch for a review above them); the decision is on `new`.
    trust = _reply_author_trust(spec, github, ctx)
    handled = await proven_handled(activities, trust)
    batch, approvals = dispositions(new, handled, context=activities)
    marker: dict[str, Any] = {
        REVIEW_SEEN_KEY: _new_marker(spec.pr_url, seen, activities)
    }

    if not batch and not approvals:
        # Only silent material (dismissals, replies, our own automated
        # replies):
        # advance the marks so it is never re-walked, post nothing. The marker
        # IS this material's durable record, so a failed write is a retryable
        # failure (PR #348 re-review round 3) — the merged-path caller must
        # defer resolution over it, not lose the observation forever.
        marked = await write_marker(
            ctx, task_id=gate.id, marker=marker, subsystem="external-reviews"
        )
        return IngestResult(failed=not marked)

    if story_id is None:
        # Orphan gate (no waiter edge): nothing to post the finding on — the
        # marker is the only record here too, so its write result propagates
        # the same way (and the "recorded" claim is only logged when true).
        marked = await write_marker(
            ctx, task_id=gate.id, marker=marker, subsystem="external-reviews"
        )
        ctx.logger.warning(
            "[Friction] external-reviews: gate %s has no waiter; review "
            "activity on %s %s",
            gate.id,
            spec.pr_url,
            "recorded but not posted"
            if marked
            else "NOT recorded (marker write failed; will retry)",
        )
        return IngestResult(failed=not marked)

    # One probe per unseen approval author, on the batch's own cached trust:
    # the row's label and the note's voice depend on it (security f-003).
    verified = {a.author: await trust.is_trusted(a.author) for a in approvals}
    if batch and pending_marker_for is not None:
        # An approvals-only batch never parks a dispatch trigger: there is
        # nothing to remediate, so no round may be owed for it.
        pending = await pending_marker_for(batch)
        if pending:
            marker.update(pending)
    landed = await post_finding_then_mark(
        ctx,
        task_id=story_id,
        summary=_render_summary(
            spec.pr_url,
            batch,
            approvals=approvals,
            verified=verified,
            story_id=story_id,
            gate_id=gate.id,
            extra_note=extra_note,
            post_merge=post_merge,
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
        # would double-dispatch when it re-posts). PR #348 re-review 1:
        # ``failed`` lets the merged-path caller defer resolution too.
        return IngestResult(failed=True)
    counts = Counter(a.stream for a in batch)
    ctx.logger.info(
        "external-reviews: posted %s for %s (%s%s) on story %s",
        EXTERNAL_REVIEW,
        spec.pr_url,
        ", ".join(
            f"{counts[adapter.stream]} {adapter.label}(s)"
            for adapter in STREAM_ADAPTERS
        ),
        f", {len(approvals)} approval(s)" if approvals else "",
        story_id,
    )
    return IngestResult(posted=True, actionable=batch, approvals=approvals)
