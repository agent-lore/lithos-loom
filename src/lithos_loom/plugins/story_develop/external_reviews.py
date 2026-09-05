"""External review findings → converge's fix loop (PRD S2, slice B).

The fetch + injection seam for ``develop converge --from-github``: pull a
delivered PR's external review material (reviews + inline comments +
Conversation-tab comments, #353), split it by the ADR 0011 trust line, and
render the trusted findings as a synthetic ``external`` reviewer outcome that
seeds converge's coder via ``LoopEntry.intake_reviews`` — bypassing the
local-panel intake whose
``already_clean`` short-circuit is exactly the panel that missed the defects
(ADR 0011 decision 1 / 7).

**Trust (decision 8):** allowlisted bot logins and humans with repo
write/admin may seed the coder; everyone else's findings are returned in the
*untrusted* list — reported to the operator, never placed on a prompt path.
An author whose permission cannot be verified is untrusted (fail closed for
the prompt path).

**Suppression parity with the sweep (#355):** what is still live — the
per-stream actionability rules and the authenticated landed-fix proof (PR
#344 re-reviews 1+2, PR #345 F3) — is decided by the shared
:mod:`lithos_loom.github_review_activity`, so the operator-triggered path and
the watcher sweep cannot disagree.

**Severity:** external reviewers state none; every finding enters at
``minor`` (the loop's own panel and gate judge the *result* — the external
reviewer proposes, loom's gate disposes).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from lithos_loom.github_client import GitHubClient, GitHubError
from lithos_loom.github_models import issue_comment_reply_body, parse_github_ref
from lithos_loom.github_review_activity import ExternalReviewActivity, ReviewStream
from lithos_loom.github_review_streams import (
    AuthorTrust,
    ReplyMode,
    actionable,
    adapter_for,
    fetch_activity,
    proven_handled,
)

from . import handoff
from .findings import FindingLedger
from .github_access import github_call
from .panel import ReviewOutcome

__all__ = [
    "CoderAck",
    "ExternalFinding",
    "ExternalOutcome",
    "GitHubError",  # re-export: the CLI seam catches it without a GitHub-tier import
    "ReplyMode",  # re-export: the CLI epilogue routes on it (no GitHub-tier import)
    "issue_comment_reply_body",  # re-export: same reason, for the reply epilogue
    "ack_instruction",
    "external_intake_reviews",
    "fetch_external_findings",
    "finding_from_activity",
    "findings_to_handoff_text",
    "outcomes_after_loop",
    "parse_coder_acks",
    "pr_number_from_spec",
]


@dataclass(frozen=True)
class ExternalFinding:
    """One external review finding, with enough provenance to reply to it.

    ``head_sha`` is the commit the reviewer actually read (load-bearing: a
    finding written against a sha the branch has moved past may already be
    fixed and must be re-anchored, never re-fixed blindly; empty for a
    conversation comment, which reviews the PR, not a hunk). ``stream`` +
    ``activity_id`` are the row's identity; ``reply_mode`` is the reply
    capability its stream's adapter chose (PR #356 re-review) — the epilogue
    routes on the mode, never on the stream, so a new stream picks an
    existing capability in its adapter row and is answered without touching
    the epilogue.
    """

    author: str
    source: str  # "bot" | "human"
    trusted: bool
    stream: ReviewStream
    activity_id: int
    reply_mode: ReplyMode
    thread_url: str
    head_sha: str
    path: str = ""
    line: int | None = None
    body: str = ""
    severity: str = "minor"


def finding_from_activity(
    a: ExternalReviewActivity, *, source: str, trusted: bool
) -> ExternalFinding:
    """The intake's finding for one normalised row (#355): identity and the
    reply capability come from the row and its stream's adapter."""
    return ExternalFinding(
        author=a.author,
        source=source,
        trusted=trusted,
        stream=a.stream,
        activity_id=a.activity_id,
        reply_mode=adapter_for(a.stream).reply_mode,
        thread_url=a.url,
        head_sha=a.head_sha,
        path=a.path,
        line=a.line,
        body=a.body,
    )


def fetch_external_findings(
    repo: str, pr_number: int, *, trusted_bots: Sequence[str]
) -> tuple[list[ExternalFinding], list[ExternalFinding]]:
    """Fetch a PR's live external findings, split ``(trusted, untrusted)``.

    One sync bridge call (``github_call``) covering the three stream listings
    and the per-author permission probes. Raises ``GitHubError`` on a listing
    failure — unlike the retired ``fetch_copilot_comments``, which swallowed
    it to ``[]``, the caller here must be able to distinguish "no findings"
    from "could not look". What is still live is decided by the same shared
    rules the watcher sweep applies (:mod:`lithos_loom.github_review_activity`).
    """

    async def _op(
        client: GitHubClient,
    ) -> tuple[list[ExternalFinding], list[ExternalFinding]]:
        activities = await fetch_activity(client, repo, pr_number)

        async def permission_of(author: str) -> str:
            return await client.get_collaborator_permission(repo, author)

        trust = AuthorTrust(permission_of, bots=trusted_bots)
        handled = await proven_handled(activities, trust)

        trusted: list[ExternalFinding] = []
        untrusted: list[ExternalFinding] = []
        for a in actionable(activities, handled):
            source, is_trusted = await trust.source(a.author)
            finding = finding_from_activity(a, source=source, trusted=is_trusted)
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
    evidence), ``fixed`` / ``disputed`` (the coder's per-id acknowledgement,
    ``detail`` = its one-line response), or ``unaddressed`` (no validated
    claim — the loop stopped early, or the coder never acknowledged the id).
    The epilogue only *asserts* a fix in a thread reply when the branch was
    actually pushed; dispositions here are claims.
    """

    finding_id: str
    finding: ExternalFinding
    disposition: str
    detail: str = ""


@dataclass(frozen=True)
class CoderAck:
    """One line of the coder's ``## External findings`` acknowledgement."""

    verdict: str  # "fixed" | "disputed"
    detail: str = ""


# The dedicated handoff section the external-mode coder prompt mandates
# (PR #345 re-review 1). Distinct from `## Findings` (the shared dispute
# contract) so `parse_review_handoff`'s exact "findings" section key never
# sees it, and scoped parsing below never reads the Summary's per-id prose
# ("- f-001: fixed the guard") as an acknowledgement.
ACK_SECTION = "## External findings"

_ACK_SECTION_RE = re.compile(
    r"^##[ \t]*External findings[ \t]*:?[ \t]*$(?P<body>.*?)(?=^##[ \t]|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
# One ack per LINE (the same anchoring rule as the triage verdict regex — an
# unanchored pattern would let one line's detail swallow the next).
_ACK_RE = re.compile(
    r"^[ \t]*-[ \t]*(?P<fid>f-\d+)[ \t]*:[ \t]*(?P<verdict>FIXED|DISPUTED)"
    r"[ \t]*(?:[—–:-]+[ \t]*(?P<detail>.*\S))?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


def ack_instruction(finding_ids: Sequence[str]) -> str:
    """The prompt block that makes the coder's per-id acknowledgement a hard
    contract, appended to the external-mode round-1 coder prompt.

    Every injected id is named explicitly so the coder cannot conform while
    silently dropping one — an omitted id parses to no ack and the finding
    lands ``unaddressed`` (its thread gets no "Fixed in" reply).
    """
    ids = ", ".join(finding_ids)
    return f"""
## External-finding acknowledgements (required)

The findings above come from EXTERNAL reviewers on the PR's own threads, and
each thread is answered from your handoff. In addition to the normal format,
your handoff MUST contain a `{ACK_SECTION.lstrip("# ")}` section (header
exactly `{ACK_SECTION}`) with exactly one line per finding id — every one of:
{ids} — stating what you did:

- f-001: FIXED — <one line: what you changed, and where>
- f-002: DISPUTED — <one line: why the finding is wrong>

Use FIXED only for a finding you actually resolved in the code this turn. An
id you omit is treated as NOT addressed and its thread gets no answer — never
omit one silently.
"""


def parse_coder_acks(text: str, finding_ids: Sequence[str]) -> dict[str, CoderAck]:
    """Parse the coder handoff's ``## External findings`` acknowledgements.

    Only lines inside that dedicated section count — the mandated per-id
    Summary prose never does. Ids outside *finding_ids* are ignored; a missing
    section returns ``{}`` (every finding then ``unaddressed`` — the safe
    direction: an unparseable handoff can under-claim, never over-claim).
    """
    section = _ACK_SECTION_RE.search(text)
    if section is None:
        return {}
    known = set(finding_ids)
    acks: dict[str, CoderAck] = {}
    for line in _ACK_RE.finditer(section.group("body")):
        fid = line.group("fid")
        if fid not in known:
            continue
        acks[fid] = CoderAck(
            verdict=line.group("verdict").lower(),
            detail=(line.group("detail") or "").strip(),
        )
    return acks


def outcomes_after_loop(
    id_map: dict[str, ExternalFinding],
    rejections: dict[str, str],
    coder_findings: dict[str, handoff.Finding],
    acks: dict[str, CoderAck],
    *,
    loop_approved: bool = False,
) -> tuple[ExternalOutcome, ...]:
    """Fold triage rejections + the coder's per-id claims into per-finding
    outcomes, in the injection order (``id_map`` preserves it).

    ``fixed`` requires BOTH halves of the evidence (PR #345 re-review 1): the
    coder's explicit ``FIXED`` acknowledgement for that id (*acks*, from the
    mandated ``## External findings`` section — the loop's approval alone is
    evidence the TREE passed, not evidence of each disposition, so a silent
    partial fix must never earn a per-thread claim) AND ``loop_approved``
    (the panel + gate accepted the tree the acknowledgement is about — an
    acked fix in an unapproved loop was never validated). A dispute counts
    from either channel: the shared ``## Findings`` block contract, or a
    ``DISPUTED`` acknowledgement line. Everything else is ``unaddressed``.
    """
    out: list[ExternalOutcome] = []
    for fid, ext in id_map.items():
        if fid in rejections:
            out.append(ExternalOutcome(fid, ext, "rejected", detail=rejections[fid]))
            continue
        claim = coder_findings.get(fid)
        ack = acks.get(fid)
        if (claim is not None and claim.status == "disputed") or (
            ack is not None and ack.verdict == "disputed"
        ):
            detail = (
                claim.coder_response
                if claim is not None and claim.coder_response
                else (ack.detail if ack is not None else "")
            )
            out.append(ExternalOutcome(fid, ext, "disputed", detail=detail))
            continue
        if ack is not None and ack.verdict == "fixed" and loop_approved:
            out.append(ExternalOutcome(fid, ext, "fixed", detail=ack.detail))
            continue
        detail = ack.detail if ack is not None else ""
        out.append(ExternalOutcome(fid, ext, "unaddressed", detail=detail))
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
