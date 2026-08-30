"""External review findings → converge's fix loop (PRD S2, slice B).

The fetch + injection seam for ``develop converge --from-github``: pull a
delivered PR's external review material (reviews + inline comments), split it
by the ADR 0011 trust line, and render the trusted findings as a synthetic
``external`` reviewer outcome that seeds converge's coder via
``LoopEntry.intake_reviews`` — bypassing the local-panel intake whose
``already_clean`` short-circuit is exactly the panel that missed the defects
(ADR 0011 decision 1 / 7).

**Trust (decision 8):** allowlisted bot logins and humans with repo
write/admin may seed the coder; everyone else's findings are returned in the
*untrusted* list — reported to the operator, never placed on a prompt path.
An author whose permission cannot be verified is untrusted (fail closed for
the prompt path).

**Suppression parity with the sweep:** a root comment already proven handled
by an *authenticated* landed-fix reply (``github_models.is_landed_fix_reply``
+ the reply author holds write/admin — PR #344 re-reviews 1+2) is excluded,
as is a non-``CHANGES_REQUESTED`` summary review by a handled author, so the
operator-triggered path and the watcher sweep agree on what is still live.

**Severity:** external reviewers state none; every finding enters at
``minor`` (the loop's own panel and gate judge the *result* — the external
reviewer proposes, loom's gate disposes).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from lithos_loom.github_client import GitHubClient, GitHubError
from lithos_loom.github_models import (
    is_automated_reply,
    is_landed_fix_reply,
    parse_github_ref,
    review_is_actionable,
)

from . import handoff
from .findings import FindingLedger
from .github_access import github_call
from .panel import ReviewOutcome

__all__ = [
    "ExternalFinding",
    "ExternalOutcome",
    "GitHubError",  # re-export: the CLI seam catches it without a GitHub-tier import
    "external_intake_reviews",
    "fetch_external_findings",
    "findings_to_handoff_text",
    "outcomes_after_loop",
    "pr_number_from_spec",
]

_TRUSTED_PERMISSIONS = frozenset({"admin", "write"})


@dataclass(frozen=True)
class ExternalFinding:
    """One external review finding, with enough provenance to reply to it.

    ``head_sha`` is the commit the reviewer actually read (load-bearing: a
    finding written against a sha the branch has moved past may already be
    fixed and must be re-anchored, never re-fixed blindly). ``comment_id`` is
    ``None`` for a summary-only review — the reply epilogue can only thread a
    reply onto comment-backed findings.
    """

    author: str
    source: str  # "bot" | "human"
    trusted: bool
    review_id: int | None
    comment_id: int | None
    thread_url: str
    head_sha: str
    path: str = ""
    line: int | None = None
    body: str = ""
    severity: str = "minor"


def fetch_external_findings(
    repo: str, pr_number: int, *, trusted_bots: Sequence[str]
) -> tuple[list[ExternalFinding], list[ExternalFinding]]:
    """Fetch a PR's live external findings, split ``(trusted, untrusted)``.

    One sync bridge call (``github_call``) covering the review + comment
    listings and the per-author permission probes. Raises ``GitHubError`` on
    a listing failure — unlike the retired ``fetch_copilot_comments``, which
    swallowed it to ``[]``, the caller here must be able to distinguish "no
    findings" from "could not look".
    """
    bots = frozenset(trusted_bots)

    async def _op(
        client: GitHubClient,
    ) -> tuple[list[ExternalFinding], list[ExternalFinding]]:
        reviews = await client.list_pull_request_reviews(repo, pr_number)
        comments = await client.list_pull_request_review_comments(repo, pr_number)

        permissions: dict[str, str] = {}

        async def _permission(author: str) -> str:
            cached = permissions.get(author)
            if cached is not None:
                return cached
            try:
                value = await client.get_collaborator_permission(repo, author)
            except Exception:  # noqa: BLE001 — any probe failure = unverified
                value = "none"
            permissions[author] = value
            return value

        # Roots proven handled: an authenticated landed-fix reply (the same
        # two-part proof the sweep applies — PR #344 re-reviews 1+2).
        handled_roots: set[int] = set()
        for c in comments:
            if c.in_reply_to_id is None or not is_landed_fix_reply(c.body):
                continue
            if c.author in bots or await _permission(c.author) in _TRUSTED_PERMISSIONS:
                handled_roots.add(c.in_reply_to_id)
        # Bind suppression to the review that OWNS the handled roots (PR #345
        # review F3) — never to the author across the whole PR, which would
        # hide a later summary re-review behind an ancient fixed root.
        review_roots: dict[int, list[int]] = {}
        for c in comments:
            if c.in_reply_to_id is None and c.pull_request_review_id is not None:
                review_roots.setdefault(c.pull_request_review_id, []).append(
                    c.comment_id
                )
        handled_review_ids = frozenset(
            rid
            for rid, roots in review_roots.items()
            if roots and all(r in handled_roots for r in roots)
        )

        trusted: list[ExternalFinding] = []
        untrusted: list[ExternalFinding] = []

        async def _classify(author: str) -> tuple[str, bool]:
            if author in bots:
                return "bot", True
            return "human", await _permission(author) in _TRUSTED_PERMISSIONS

        for review in reviews:
            if not review_is_actionable(review):
                continue
            # A non-blocking summary review ALL of whose own roots were
            # handled is part of that handled history; a
            # CHANGES_REQUESTED is never suppressed — a reply does not prove
            # the requested changes were accepted.
            if (
                review.state != "CHANGES_REQUESTED"
                and review.review_id in handled_review_ids
            ):
                continue
            source, is_trusted = await _classify(review.author)
            finding = ExternalFinding(
                author=review.author,
                source=source,
                trusted=is_trusted,
                review_id=review.review_id,
                comment_id=None,
                thread_url=(
                    f"https://github.com/{repo}/pull/{pr_number}"
                    f"#pullrequestreview-{review.review_id}"
                ),
                head_sha=review.commit_id,
                body=review.body,
            )
            (trusted if is_trusted else untrusted).append(finding)

        for c in comments:
            if c.in_reply_to_id is not None or is_automated_reply(c.body):
                continue
            if c.comment_id in handled_roots:
                continue
            source, is_trusted = await _classify(c.author)
            finding = ExternalFinding(
                author=c.author,
                source=source,
                trusted=is_trusted,
                review_id=None,
                comment_id=c.comment_id,
                thread_url=c.html_url,
                head_sha=c.commit_id or c.original_commit_id,
                path=c.path,
                line=c.line,
                body=c.body,
            )
            (trusted if is_trusted else untrusted).append(finding)

        return trusted, untrusted

    return github_call(_op)


def findings_to_handoff_text(
    findings: Sequence[ExternalFinding], *, current_head_sha: str
) -> str:
    """Render external findings as a synthetic review handoff.

    Generalises the retired inline round's ``comments_to_handoff_text``:
    blank ids (the ``external`` ledger assigns them), author attribution in
    the rationale, and — when a finding was written against an older sha — a
    re-anchor note telling the coder to verify it still applies before
    changing anything (never re-fix blindly).
    """
    lines = [
        "## Status: FINDINGS",
        "## Summary",
        f"{len(findings)} external review finding(s) fetched from the PR.",
        "## Findings",
    ]
    for f in findings:
        rationale = f"[{f.author}] " + " ".join(f.body.split())
        if f.head_sha and f.head_sha != current_head_sha:
            rationale += (
                f" (written against {f.head_sha[:12]}, older than the current "
                f"head — verify it still applies before changing anything)"
            )
        lines += [
            "- finding_id:",
            f"  severity: {f.severity}",
            "  status: open",
        ]
        if f.path:
            loc = f"{f.path}:{f.line}" if f.line else f.path
            lines.append(f'  files: ["{loc}"]')
        lines.append(f"  rationale: {rationale}")
    return "\n".join(lines) + "\n"


def external_intake_reviews(
    findings: Sequence[ExternalFinding], *, current_head_sha: str
) -> tuple[list[ReviewOutcome], dict[str, ExternalFinding]]:
    """Build the synthetic intake that seeds converge's coder, plus the
    ``finding_id → ExternalFinding`` map the reply epilogue threads back on.

    The inline round's recipe: render → ``parse_review_handoff`` → a fresh
    ``FindingLedger("external")`` assigns canonical ids — bound positionally
    to their source findings (``zip(strict=True)``, the id↔thread binding).
    """
    text = findings_to_handoff_text(findings, current_head_sha=current_head_sha)
    parsed = handoff.parse_review_handoff(text)
    ledger = FindingLedger("external")
    canonical = ledger.apply_review(parsed, 1)
    id_map = {f.finding_id: ext for f, ext in zip(canonical, findings, strict=True)}
    severities = [f.severity for f in canonical if f.is_open]
    outcome = ReviewOutcome(
        reviewer="external",
        status="FINDINGS",
        passed=False,
        max_severity=handoff.max_severity(severities),
        findings=canonical,
        cost_usd=0.0,
    )
    return [outcome], id_map


@dataclass(frozen=True)
class ExternalOutcome:
    """What happened to one injected external finding, for the reply epilogue.

    ``disposition``: ``rejected`` (triage refuted it, ``detail`` = the cited
    evidence), ``fixed`` / ``disputed`` (the coder's round-1 claim, ``detail``
    = its response), or ``unaddressed`` (no coder claim recorded — e.g. the
    loop stopped early). The epilogue only *asserts* a fix in a thread reply
    when the branch was actually pushed; dispositions here are claims.
    """

    finding_id: str
    finding: ExternalFinding
    disposition: str
    detail: str = ""


def outcomes_after_loop(
    id_map: dict[str, ExternalFinding],
    rejections: dict[str, str],
    coder_findings: dict[str, handoff.Finding],
    *,
    loop_approved: bool = False,
) -> tuple[ExternalOutcome, ...]:
    """Fold triage rejections + the coder's claims into per-finding outcomes,
    in the injection order (``id_map`` preserves it).

    The coder's handoff contract (PR #345 review F1) puts a ``## Findings``
    block in only for **disputes** — a conforming successful fix leaves no
    parsed finding at all. So ``fixed`` is derived from the LOOP's approval:
    a non-rejected, non-disputed finding in an approved run was addressed
    (the panel + gate accepted the tree containing its remediation); in an
    unapproved run it stays ``unaddressed`` — never a false ``fixed``.
    """
    out: list[ExternalOutcome] = []
    for fid, ext in id_map.items():
        if fid in rejections:
            out.append(ExternalOutcome(fid, ext, "rejected", detail=rejections[fid]))
            continue
        claim = coder_findings.get(fid)
        if claim is not None and claim.status == "disputed":
            out.append(
                ExternalOutcome(fid, ext, "disputed", detail=claim.coder_response)
            )
            continue
        detail = claim.coder_response if claim is not None else ""
        disposition = "fixed" if loop_approved else "unaddressed"
        out.append(ExternalOutcome(fid, ext, disposition, detail=detail))
    return tuple(out)


def pr_number_from_spec(change_spec: str) -> int | None:
    """PR number from a converge change spec (``142`` / ``#142`` / a PR URL)."""
    raw = change_spec.strip().lstrip("#")
    if raw.isdigit():
        return int(raw)
    ref = parse_github_ref(change_spec)
    if ref is not None and ref.kind == "pull":
        return ref.number
    return None
