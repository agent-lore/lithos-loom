"""Typed GitHub payload models + pure parsers (extracted from ``github_client``).

The dataclasses here are the domain entities the watcher, the PR-gate
resolver, story-develop's delivery path and the external-review sweep all
share; the ``parse_*`` functions convert raw REST rows into them, and the
``lithos`` body-marker helpers round-trip the ``<!-- lithos:<id> -->`` link.
Everything is pure (no I/O) — the async client lives in
:mod:`lithos_loom.github_client`, which re-exports these names so existing
importers keep working.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

__all__ = [
    "AUTOMATED_REPLY_MARKER",
    "FIXED_REPLY_PREFIX",
    "SILENT_REVIEW_STATES",
    "LOOM_NOTICE_MARKER",
    "GitHubRef",
    "Issue",
    "IssueComment",
    "PullRequest",
    "PullRequestReview",
    "PullRequestReviewComment",
    "apply_marker",
    "parse_github_ref",
    "parse_issue_comment",
    "parse_issues_response",
    "parse_marker",
    "parse_pull_request",
    "parse_pull_request_review",
    "parse_pull_request_review_comment",
    "strip_marker",
    "is_automated_reply",
    "is_landed_fix_reply",
    "is_loom_pr_comment",
    "issue_comment_is_actionable",
    "issue_comment_reply_body",
    "issue_comment_reply_target",
    "review_is_actionable",
]

# Regex matches both canonical and operator-edited shapes:
#   <!-- lithos:abc-123 -->  → canonical
#   <!-- LITHOS:ABC-123 -->  → case-insensitive tolerated
# Captured group 1 is the task id.
_MARKER_RE = re.compile(r"<!--\s*lithos:\s*([A-Za-z0-9_-]+)\s*-->", re.IGNORECASE)

# The one home for the canonical GitHub issue/PR web-URL grammar (ARCH-7). Every
# marker/reconcile path that carries a ``develop_pr_url`` or ``github_issue_url``
# parsed it independently before — six near-identical prefix-splits and regexes
# that could drift on host, trailing path, or numeric id. They are now thin
# adapters over ``parse_github_ref``. Anchored at both ends: a single trailing
# slash is tolerated, any extra path segment (``/pull/82/files``) is not.
_GITHUB_REF_RE = re.compile(
    r"https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/"
    r"(?P<kind>issues|pull)/(?P<number>\d+)/?$"
)


@dataclass(frozen=True)
class GitHubRef:
    """A parsed reference to a GitHub issue or pull request.

    ``repo`` is ``"owner/name"``; ``number`` is the issue/PR number; ``kind`` is
    the canonical singular ``"issue"`` or ``"pull"``. Produced only by
    :func:`parse_github_ref`.
    """

    repo: str
    number: int
    kind: Literal["issue", "pull"]


def parse_github_ref(url: object) -> GitHubRef | None:
    """Parse a canonical ``https://github.com/<owner>/<repo>/(issues|pull)/<n>`` URL.

    Returns a :class:`GitHubRef`, or ``None`` for anything that is not exactly
    that shape — a non-string, a non-github host, a non-https scheme, an unknown
    kind, a trailing path segment, or a non-numeric id. A single trailing slash
    is tolerated and surrounding whitespace is stripped.

    This is the single home for the GitHub issue/PR URL grammar; the per-caller
    helpers (``pr_delivery.parse_issue_ref`` / ``pr_delivery.pr_number_from_url``,
    ``_develop_pr_merge._parse_pr_url``, ``_github_issue_push._resolve_repo_number``)
    are thin adapters that filter on ``kind`` and shape the return their own way.
    """
    if not isinstance(url, str):
        return None
    m = _GITHUB_REF_RE.match(url.strip())
    if m is None:
        return None
    kind: Literal["issue", "pull"] = "issue" if m.group("kind") == "issues" else "pull"
    return GitHubRef(
        repo=f"{m.group('owner')}/{m.group('repo')}",
        number=int(m.group("number")),
        kind=kind,
    )


@dataclass(frozen=True)
class Issue:
    """The slice of GitHub's issue payload the watcher cares about."""

    repo: str
    number: int
    title: str
    body: str
    state: str  # "open" | "closed"
    state_reason: str | None  # "completed" | "not_planned" | None
    labels: tuple[str, ...]
    author: str
    updated_at: datetime
    html_url: str


@dataclass(frozen=True)
class PullRequest:
    """The pull-request payload two consumers share off the single-PR endpoint.

    ``merged`` is a top-level boolean on the single-PR endpoint
    (``GET /pulls/{n}``) — reliable there, unlike the list endpoint where it
    is absent. ``merged_at`` / ``merge_commit_sha`` are populated only once the
    PR has actually merged. The PR-merge watcher (#87) reads those.

    ``head_sha`` / ``base_ref`` / ``head_ref`` / ``title`` / ``body`` come from
    the same response and drive review-only resolution (``review_resolve``,
    #154 / ARCH-7c) — the head commit to diff, the base branch to merge-base
    against, and the PR's title/body as the default acceptance-criteria source.
    They default to empty so a minimal row (e.g. a list-endpoint slice or an
    older test fixture) still parses; the single-PR endpoint always populates
    them in practice.

    ``head_repo`` / ``base_repo`` are the head/base repo full-names
    (``owner/name``); when they differ the PR head lives on a **fork**, which
    converge (``review_resolve``) reads to refuse pushing to a fork branch under
    origin credentials.
    """

    repo: str
    number: int
    state: str  # "open" | "closed"
    merged: bool
    merged_at: datetime | None
    merge_commit_sha: str | None
    head_sha: str = ""
    base_ref: str = ""
    head_ref: str = ""
    title: str = ""
    body: str = ""
    head_repo: str = ""
    base_repo: str = ""


@dataclass(frozen=True)
class PullRequestReview:
    """A single PR review: the reviewer login + the review-summary body.

    the external-review sweep reads these to detect a bot's review and
    parse its "generated N comments" marker (see ``pr_delivery``).

    ``review_id`` / ``state`` / ``submitted_at`` / ``commit_id`` exist for the
    external-review sweep (PRD S2): a summary-only review — an ``APPROVED`` or
    ``CHANGES_REQUESTED`` with zero inline comments — has no comment id to
    de-dup on, so the review id is its only stable key; ``state`` drives the
    per-state posting policy; ``commit_id`` is the head the reviewer read
    (re-anchor input for remediation)."""

    author: str
    body: str
    review_id: int = 0
    state: str = ""
    submitted_at: datetime | None = None
    commit_id: str = ""


@dataclass(frozen=True)
class PullRequestReviewComment:
    """A single inline review comment on a PR.

    ``line`` falls back to ``original_line`` (GitHub drops ``line`` for comments
    anchored to a since-changed line). ``in_reply_to_id`` is set on thread
    replies — consumers exclude those when collecting root findings.

    For the external-review sweep (PRD S2): ``html_url`` is the thread link
    (operator navigation + reply anchor), ``commit_id`` /
    ``original_commit_id`` are the shas the comment was written against
    (re-anchor input), and ``updated_at`` feeds the sweep's bounded ``since``
    cursor so a long-lived PR's history isn't re-walked every pass."""

    comment_id: int
    author: str
    path: str
    line: int | None
    body: str
    in_reply_to_id: int | None
    html_url: str = ""
    commit_id: str = ""
    original_commit_id: str = ""
    updated_at: datetime | None = None
    pull_request_review_id: int | None = None


@dataclass(frozen=True)
class IssueComment:
    """A comment on the PR's **Conversation** tab (GitHub: an *issue* comment —
    a PR is an issue on that endpoint).

    The third external-review stream (#353). It is the only channel open to
    the PR's own author — GitHub refuses a review from the author, and every
    loom-delivered PR is opened under the operator's login — so the
    operator's verdicts arrive here and nowhere else. No ``path`` / ``line``
    / sha: it reviews the PR, not a commit or a hunk. Its id space is
    separate from inline review comments', so it carries its own
    high-water mark."""

    comment_id: int
    author: str
    body: str
    html_url: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ── Pure helpers ──────────────────────────────────────────────────────


def _parse_iso(s: str) -> datetime:
    """GitHub stamps timestamps as ``2026-05-29T12:00:00Z``. Make them tz-aware."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def parse_issues_response(payload: list[dict[str, Any]], *, repo: str) -> list[Issue]:
    """Convert a GitHub ``/issues`` response into typed Issues, dropping PRs.

    Pull requests appear in the same endpoint with a ``pull_request`` field
    set. D53 requires they be filtered out so the subscription handler never
    sees them.
    """
    issues: list[Issue] = []
    for row in payload:
        if "pull_request" in row:
            continue
        issues.append(
            Issue(
                repo=repo,
                number=int(row["number"]),
                title=str(row["title"]),
                body=str(row.get("body") or ""),
                state=str(row["state"]),
                state_reason=row.get("state_reason"),
                labels=tuple(lbl["name"] for lbl in row.get("labels") or ()),
                author=str((row.get("user") or {}).get("login", "")),
                updated_at=_parse_iso(str(row["updated_at"])),
                html_url=str(row.get("html_url", "")),
            )
        )
    return issues


def parse_pull_request(row: dict[str, Any], *, repo: str) -> PullRequest:
    """Convert a GitHub ``GET /pulls/{n}`` response row into a typed PullRequest."""
    merged_at_raw = row.get("merged_at")
    sha = row.get("merge_commit_sha")
    head = row.get("head") or {}
    base = row.get("base") or {}
    return PullRequest(
        repo=repo,
        number=int(row["number"]),
        state=str(row["state"]),
        merged=bool(row.get("merged", False)),
        merged_at=_parse_iso(str(merged_at_raw)) if merged_at_raw else None,
        merge_commit_sha=str(sha) if sha else None,
        head_sha=str(head.get("sha") or ""),
        base_ref=str(base.get("ref") or ""),
        head_ref=str(head.get("ref") or ""),
        title=str(row.get("title") or ""),
        body=str(row.get("body") or ""),
        head_repo=str((head.get("repo") or {}).get("full_name") or ""),
        base_repo=str((base.get("repo") or {}).get("full_name") or ""),
    )


def parse_pull_request_review(row: dict[str, Any]) -> PullRequestReview:
    submitted_raw = row.get("submitted_at")
    return PullRequestReview(
        author=str((row.get("user") or {}).get("login", "")),
        body=str(row.get("body") or ""),
        review_id=int(row.get("id") or 0),
        state=str(row.get("state") or ""),
        submitted_at=_parse_iso(str(submitted_raw)) if submitted_raw else None,
        commit_id=str(row.get("commit_id") or ""),
    )


def parse_pull_request_review_comment(row: dict[str, Any]) -> PullRequestReviewComment:
    updated_raw = row.get("updated_at")
    return PullRequestReviewComment(
        comment_id=int(row["id"]),
        author=str((row.get("user") or {}).get("login", "")),
        path=str(row.get("path") or ""),
        # ``line`` is null for comments anchored to a since-changed line;
        # GitHub then exposes the position via ``original_line``.
        line=row.get("line") or row.get("original_line"),
        body=str(row.get("body") or ""),
        in_reply_to_id=row.get("in_reply_to_id"),
        html_url=str(row.get("html_url") or ""),
        commit_id=str(row.get("commit_id") or ""),
        original_commit_id=str(row.get("original_commit_id") or ""),
        updated_at=_parse_iso(str(updated_raw)) if updated_raw else None,
        pull_request_review_id=row.get("pull_request_review_id"),
    )


def parse_issue_comment(row: dict[str, Any]) -> IssueComment:
    created_raw = row.get("created_at")
    updated_raw = row.get("updated_at")
    return IssueComment(
        comment_id=int(row["id"]),
        author=str((row.get("user") or {}).get("login", "")),
        body=str(row.get("body") or ""),
        html_url=str(row.get("html_url") or ""),
        created_at=_parse_iso(str(created_raw)) if created_raw else None,
        updated_at=_parse_iso(str(updated_raw)) if updated_raw else None,
    )


def parse_marker(body: str | None) -> str | None:
    """Extract the task id from a ``<!-- lithos:<id> -->`` marker, if present.

    Tolerant of placement (top/bottom of body) and case (the writer emits
    canonical lowercase ``lithos:`` but the parser accepts both).
    """
    if not body:
        return None
    match = _MARKER_RE.search(body)
    if match is None:
        return None
    return match.group(1)


def apply_marker(body: str | None, task_id: str) -> str:
    """Return ``body`` with a canonical marker appended at the end.

    If a marker is already present (anywhere), it is removed first so the
    canonical form lands at the body's tail. This both fixes operator
    placement drift over time and prevents duplicate markers.
    """
    text = body or ""
    text = _MARKER_RE.sub("", text).rstrip()
    canonical = f"<!-- lithos:{task_id} -->"
    if not text:
        return canonical
    return f"{text}\n\n{canonical}"


def strip_marker(body: str | None) -> str:
    """Return ``body`` with any ``<!-- lithos:<id> -->`` marker removed.

    Slice 7.2 mirrors GH issue body → Lithos task description. The Loom-
    managed marker is bookkeeping noise from the operator's perspective
    and must not bleed into the projected task surface, so it is stripped
    before comparison + write.
    """
    if not body:
        return ""
    return _MARKER_RE.sub("", body).strip()


# ── loom's PR-reply vocabulary + the external-review policy ───────────
#
# Single-sourced here (Foundation) because three consumers need them and no
# two share a tier-safe import path otherwise: story-develop's delivery
# (produces the replies), the github-watcher sweep (must not re-ingest them,
# and may suppress roots they prove handled), and the converge external-
# findings fetch (same suppression, plugin-side).

# Every automated PR thread reply loom posts ends with this marker.
AUTOMATED_REPLY_MARKER = "_(automated reply by story-develop)_"

# The one reply head that proves a fix actually LANDED (a pushed commit).
# The marker also rides on "A fix was prepared but NOT pushed …" (red
# regression gate) and "Not changed — …" (coder pushback) replies, where the
# root comment is still unresolved — those must never count as proof.
FIXED_REPLY_PREFIX = "Fixed in "

# Every loom-authored conversation comment that is NOT a reply — today the
# route-runner's ``[NeedsHuman]`` @mention — ends with this marker. Both it and
# the reply marker are posted under the operator's ``gh`` login, i.e. a
# write/admin human: without a marker the conversation stream (#353) would
# ingest loom's own notices as trusted review material.
LOOM_NOTICE_MARKER = "_(automated notice by lithos-loom)_"

# The head of the notices posted before the marker existed (b91177d2 slice A
# shipped 2026-09-01; the marker landed with #353) — recognised so a PR that
# already carries one is not re-ingested after the upgrade.
_LEGACY_NOTICE_HEAD = "[NeedsHuman] loom stopped on"

# Review states recorded but never actionable: an approval is not an operator
# action item, and a dismissal has already had its say.
SILENT_REVIEW_STATES = frozenset({"APPROVED", "DISMISSED"})

# The reply line that names the conversation comment a loom reply answers —
# the conversation-stream twin of ``in_reply_to_id`` (which issue comments do
# not have). Anchored to this exact line so a coder's prose quoting some other
# comment's url can never be read as the target.
_ISSUE_COMMENT_REPLY_LINE = "_(replying to {url})_"
_ISSUE_COMMENT_REPLY_RE = re.compile(
    r"^_\(replying to \S*#issuecomment-(\d+)\)_[ \t]*$", re.MULTILINE
)


def is_automated_reply(body: str) -> bool:
    """True for loom's own automated PR replies (never re-ingested)."""
    return AUTOMATED_REPLY_MARKER in body


def is_landed_fix_reply(body: str) -> bool:
    """True for the reply shape that proves a fix landed (see the constants).

    Callers that use this to *suppress* another comment must additionally
    authenticate the reply's author — both tokens are public body strings any
    commenter can copy (PR #344 re-review 2).
    """
    return is_automated_reply(body) and body.startswith(FIXED_REPLY_PREFIX)


def is_loom_pr_comment(body: str) -> bool:
    """True for any conversation comment loom itself posted (a reply or a
    notice, marked or legacy-shaped) — never re-ingested (#353)."""
    return (
        AUTOMATED_REPLY_MARKER in body
        or LOOM_NOTICE_MARKER in body
        or _LEGACY_NOTICE_HEAD in body
    )


def issue_comment_is_actionable(comment: IssueComment) -> bool:
    """The conversation-stream policy (#353): a non-empty body from anyone
    but loom. There is no review state to key on and no thread structure —
    every human comment on the conversation is a potential verdict."""
    return bool(comment.body.strip()) and not is_loom_pr_comment(comment.body)


def issue_comment_reply_body(reply: str, target_url: str) -> str:
    """Wrap a per-finding reply for the conversation tab, naming its target.

    The reply head stays FIRST (``is_landed_fix_reply`` keys on the body's
    start) and the target line comes last, so the same reply text proves the
    same things whether threaded inline or posted on the conversation.
    """
    return f"{reply}\n\n{_ISSUE_COMMENT_REPLY_LINE.format(url=target_url)}"


def issue_comment_reply_target(body: str) -> int | None:
    """The conversation comment id a loom reply answers, or ``None``."""
    match = _ISSUE_COMMENT_REPLY_RE.search(body)
    return int(match.group(1)) if match is not None else None


def review_is_actionable(review: PullRequestReview) -> bool:
    """The per-state external-review policy (PRD S2).

    ``CHANGES_REQUESTED`` is always actionable; ``APPROVED`` / ``DISMISSED``
    never are; ``COMMENTED`` — and any state GitHub adds later — only with a
    non-empty body (conservative for unknown states, silent-drop only for the
    two known non-actionable ones).
    """
    if review.state == "CHANGES_REQUESTED":
        return True
    if review.state in SILENT_REVIEW_STATES:
        return False
    return bool(review.body.strip())
