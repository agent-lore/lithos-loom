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

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

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

from ...runner import git
from . import handoff
from .findings import FindingLedger
from .github_access import github_call
from .panel import ReviewOutcome

logger = logging.getLogger(__name__)

__all__ = [
    "CoderAck",
    "ExternalFinding",
    "ExternalOutcome",
    "GitHubError",  # re-export: the CLI seam catches it without a GitHub-tier import
    "ReplyMode",  # re-export: the CLI epilogue routes on it (no GitHub-tier import)
    "ReviewStream",  # re-export: the S8 triage eval builds findings through this seam
    "adapter_for",  # re-export: same seam — the stream's reply capability
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
    evidence), ``fixed`` / ``disputed`` / ``reverted`` / ``no_change_needed``
    (#380: not a defect — the coder agrees nothing should change) (the coder's per-id
    acknowledgement in its FINAL handoff, ``detail`` = its one-line response
    — ``reverted`` is a fix a later round undid, #387: the reviewer and the
    story's acceptance criteria disagree, an operator decision), or
    ``unaddressed`` (no validated claim — the loop stopped early, the coder
    never acknowledged the id in its final handoff, or the tree never moved).
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

    verdict: str  # "fixed" | "disputed" | "reverted" | "no_change_needed"
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
    r"^[ \t]*-[ \t]*(?P<fid>f-\d+)[ \t]*:[ \t]*"
    r"(?P<verdict>FIXED|DISPUTED|REVERTED|NO[ \t_]+CHANGE[ \t_]+NEEDED)"
    r"[ \t]*(?:[—–:-]+[ \t]*(?P<detail>.*\S))?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


def ack_instruction(finding_ids: Sequence[str]) -> str:
    """The prompt block that makes the coder's per-id acknowledgement a hard
    contract, appended to EVERY external-mode coder prompt (round 1's cold
    start and each fix round — #387: the threads are answered from the
    FINAL handoff, so a later round that undoes a fix must say so).

    Every injected id is named explicitly so the coder cannot conform while
    silently dropping one — an omitted id parses to no ack and the finding
    lands ``unaddressed`` (its thread gets no "Fixed in" reply).
    """
    ids = ", ".join(finding_ids)
    return f"""
## External-finding acknowledgements (required, every round)

The findings above come from EXTERNAL reviewers on the PR's own threads, and
each thread is answered from your LAST handoff. In addition to the normal
format, EVERY handoff you write in this run MUST contain a
`{ACK_SECTION.lstrip("# ")}` section (header exactly `{ACK_SECTION}`) with
exactly one line per finding id — every one of: {ids} — stating the state of
that finding AS OF THIS HANDOFF:

- f-001: FIXED — <one line: what you changed, and where>
- f-002: DISPUTED — <one line: why the finding is wrong>
- f-003: REVERTED — <one line: why you undid a fix from an earlier round>
- f-004: NO CHANGE NEEDED — <one line: why there is nothing to change>

Use FIXED only for a finding whose fix is in the tree NOW. A fix you undid
this round (a reviewer holds it contradicts the acceptance criteria, say) is
REVERTED, never FIXED — the operator decides between the two contracts, not
you. NO CHANGE NEEDED is for a finding that is not a defect at all — an
approval verdict, a description of the intended behaviour, something already
in the tree — where you agree with the reviewer that nothing should change
(DISPUTED is for a claim you say is wrong). An id you omit is treated as NOT
addressed and its thread gets no answer — never omit one silently, and repeat
the section in every round.
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
        verdict = re.sub(r"[ \t_]+", "_", line.group("verdict").strip().lower())
        acks[fid] = CoderAck(
            verdict=verdict,
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
    tree_changed: bool | None = None,
    missing_ack_detail: str = "",
) -> tuple[ExternalOutcome, ...]:
    """Fold triage rejections + the coder's per-id claims into per-finding
    outcomes, in the injection order (``id_map`` preserves it).

    ``fixed`` requires BOTH halves of the evidence (PR #345 re-review 1): the
    coder's explicit ``FIXED`` acknowledgement for that id (*acks*, from the
    mandated ``## External findings`` section of its FINAL handoff — the
    loop's approval alone is evidence the TREE passed, not evidence of each
    disposition, so a silent partial fix must never earn a per-thread claim)
    AND ``loop_approved`` (the panel + gate accepted the tree the
    acknowledgement is about — an acked fix in an unapproved loop was never
    validated) AND, when known, a tree that MOVED (*tree_changed*, #387: an
    approved run whose final tree equals the PR head outside the generated
    paths undid its own fix — ``reverted``, whatever the handoff says). A
    ``REVERTED`` acknowledgement is ``reverted``. A dispute counts from
    either channel — the ``## Findings`` block only in round 1, see
    :func:`final_round_outcomes`:
    the shared ``## Findings`` block contract, or a ``DISPUTED``
    acknowledgement line. Everything else is ``unaddressed`` —
    *missing_ack_detail* is the reason recorded when there is no ack at all.
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
        if ack is not None and ack.verdict == "reverted":
            out.append(ExternalOutcome(fid, ext, "reverted", detail=ack.detail))
            continue
        if ack is not None and ack.verdict == "no_change_needed":
            # #380: not a defect (an approval verdict, intended behaviour) —
            # nothing landed and nothing had to; no approval is needed to
            # report that the coder agreed with the reviewer
            out.append(ExternalOutcome(fid, ext, "no_change_needed", detail=ack.detail))
            continue
        if ack is not None and ack.verdict == "fixed" and loop_approved:
            if tree_changed is False:
                # round 1 must commit, so an APPROVED loop that ends at the
                # PR head undid what it did — the decision shape, whatever
                # the coder wrote
                out.append(
                    ExternalOutcome(
                        fid,
                        ext,
                        "reverted",
                        detail=(
                            "acknowledged FIXED, but the final tree is identical "
                            "to the PR head outside the generated paths — the "
                            "fix was undone"
                        ),
                    )
                )
                continue
            out.append(ExternalOutcome(fid, ext, "fixed", detail=ack.detail))
            continue
        detail = ack.detail if ack is not None else missing_ack_detail
        out.append(ExternalOutcome(fid, ext, "unaddressed", detail=detail))
    return tuple(out)


def final_round_outcomes(
    *,
    handoff_dir: Path,
    run_id: str,
    rounds: int,
    loop_approved: bool,
    worktree: Path,
    head_sha: str,
    generated_paths: Sequence[str],
    id_map: dict[str, ExternalFinding],
    rejections: dict[str, str],
    surviving_ids: Sequence[str],
) -> tuple[ExternalOutcome, ...]:
    """The converge epilogue's dispositions, read from the coder's FINAL
    handoff (#387: lens #84's round 1 said FIXED, round 3 reverted it, and
    the threads were answered from round 1) — the mandated ``## External
    findings`` acks plus any ``## Findings`` dispute block — and checked
    against the tree: a run whose final tree equals the PR head outside the
    generated paths undid its fix. The ack section is scoped to the injected
    ids by construction; the ``## Findings`` block is read in round 1 only
    (a later round's belongs to the panel). A final round without the
    section carries no earlier claim forward (the safe direction).
    """
    coder_claims: dict[str, handoff.Finding] = {}
    acks: dict[str, CoderAck] = {}
    final_round = max(rounds, 1)
    coder_path = handoff_dir / handoff.coder_handoff_name(final_round)
    try:
        text = coder_path.read_text(encoding="utf-8")
    except OSError:
        text = ""  # loop died before that round's handoff → unaddressed
    missing = ""
    if text:
        acks = parse_coder_acks(text, surviving_ids)
        if not acks and final_round > 1:
            missing = (
                f"no acknowledgement in the round {final_round} coder handoff — "
                "an earlier round's claim is not carried forward"
            )
            logger.warning("converge %s: %s", run_id, missing.split(" — ")[0])
        if final_round == 1:
            # Round 1's `## Findings` block can only name the injected ids.
            # A later round's is the panel's dispute contract, whose ids
            # are minted independently (a panel f-001 beside the external
            # f-001) — never read as a claim about an external id.
            try:
                parsed = handoff.parse_review_handoff(text)
                coder_claims = {f.finding_id: f for f in parsed.findings}
            except ValueError:
                pass  # unparseable handoff: acks (line-scoped) may still hold
    try:
        tree_changed: bool | None = git.tree_differs(
            worktree, head_sha, "HEAD", exclude=generated_paths
        )
    except (RuntimeError, OSError) as exc:
        # the claim then stands on the acknowledgement alone, as before
        logger.warning("converge %s: could not read the final tree: %s", run_id, exc)
        tree_changed = None
    return outcomes_after_loop(
        id_map,
        rejections,
        coder_claims,
        acks,
        loop_approved=loop_approved,
        tree_changed=tree_changed,
        missing_ack_detail=missing,
    )


def nothing_to_change(outcomes: Sequence[ExternalOutcome]) -> bool:
    """#380: every injected finding was refuted by triage or dispositioned
    ``no_change_needed`` by the coder — the run had nothing to do, so a loop
    that committed nothing is ``already_clean`` (reported, not remediated),
    not a failure. False when there is no external finding at all."""
    return bool(outcomes) and all(
        o.disposition in ("rejected", "no_change_needed") for o in outcomes
    )


def undecided_note(outcomes: Sequence[ExternalOutcome]) -> str:
    """The status-line suffix for a converged run that left an external
    finding ``reverted`` (#387) — the tree converged, the decision did not."""
    undecided = [o.finding_id for o in outcomes if o.disposition == "reverted"]
    if not undecided:
        return ""
    return (
        f"; external {', '.join(undecided)} REVERTED — operator decision needed "
        "(the review vs the story's acceptance criteria)"
    )


def pr_number_from_spec(change_spec: str) -> int | None:
    """PR number from a converge change spec (``142`` / ``#142`` / a PR URL)."""
    raw = change_spec.strip().lstrip("#")
    if raw.isdigit():
        return int(raw)
    ref = parse_github_ref(change_spec)
    if ref is not None and ref.kind == "pull":
        return ref.number
    return None
