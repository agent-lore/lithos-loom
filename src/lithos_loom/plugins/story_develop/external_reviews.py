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

import dataclasses
import logging
import re
from collections.abc import Mapping, Sequence
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
from .conflict_resolve import fence
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
    "ack_section",
    "claims_nothing_to_change",
    "external_intake_reviews",
    "fetch_external_findings",
    "finding_from_activity",
    "findings_to_handoff_text",
    "outcomes_after_loop",
    "parse_coder_acks",
    "resolve_ack_history",
    "pr_number_from_spec",
    "render_external_context",
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
    evidence), ``nothing_to_remediate`` (triage found no claim to act on —
    the "finding" was an approval; ``detail`` = why. No thread is answered:
    a reviewer who said "LGTM" is not told their approval was processed),
    ``fixed`` / ``disputed`` / ``reverted`` / ``no_change_needed``
    (#380: not a defect — the coder agrees nothing should change) (the coder's per-id
    acknowledgement in its FINAL handoff, ``detail`` = its one-line response
    — ``reverted`` is a fix a later round undid, #387: the reviewer and the
    story's acceptance criteria disagree, an operator decision), or
    ``unaddressed`` (no validated claim — the loop stopped early, the coder
    never acknowledged the id in its final handoff, or the tree never moved).
    The epilogue only *asserts* a fix in a thread reply when the branch was
    actually pushed; dispositions here are claims.

    ``note`` (#399): how the disposition was read when the coder's final
    acknowledgement and an earlier round's disagree — a final NO CHANGE
    NEEDED over a round-1 FIXED, an omitted id an earlier round had fixed.
    Shown where the operator reads the run (the outcome finding, the CLI
    summary), never on the reviewer's thread.
    """

    finding_id: str
    finding: ExternalFinding
    disposition: str
    detail: str = ""
    note: str = ""


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

    The verdicts are defined by the TREE, not by the round (#399: lens #87's
    coder read "as of this handoff" as "what I did this round", wrote NO
    CHANGE NEEDED over a fix it had made in round 1, and the thread was
    answered "Not changed"). A fixed id stays FIXED in every later handoff;
    NO CHANGE NEEDED means the finding was never a defect. The panel's own
    findings reuse the same-looking ids (a panel ``f-001`` beside the
    external ``f-001``) and belong in ``## Findings``, never here.
    """
    ids = ", ".join(finding_ids)
    return f"""
## External-finding acknowledgements (required, every round)

The findings above come from EXTERNAL reviewers on the PR's own threads, and
each thread is answered from your LAST handoff. In addition to the normal
format, EVERY handoff you write in this run MUST contain a
`{ACK_SECTION.lstrip("# ")}` section (header exactly `{ACK_SECTION}`) with
exactly one line per finding id — every one of: {ids} — stating the state of
that finding IN THE TREE as of this handoff:

- f-001: FIXED — <one line: what the fix is, and where>
- f-002: DISPUTED — <one line: why the finding is wrong>
- f-003: REVERTED — <one line: why you undid a fix from an earlier round>
- f-004: NO CHANGE NEEDED — <one line: why the finding was never a defect>

The verdict describes the TREE, not this round's work:

- FIXED: a fix for this finding is in the tree now, whichever round made it.
  An id you fixed in an earlier round stays FIXED in every later handoff —
  restate what the fix is and where (the thread is answered from your last
  handoff's line). Never downgrade it because you touched nothing for it
  this round.
- REVERTED: a fix was made and is no longer in the tree (a reviewer holds it
  contradicts the acceptance criteria, say) — never FIXED, and never NO
  CHANGE NEEDED: the operator decides between the two contracts, not you.
- NO CHANGE NEEDED: the finding was never a defect and nothing was ever
  changed for it — an approval verdict, a description of the intended
  behaviour, something already in the tree before this run. It does NOT mean
  "nothing further this round".
- DISPUTED: the claim is wrong.

The ids here are the EXTERNAL findings' ids only. The review panel's own
findings use the same-looking ids (its f-001 is not this f-001) and are
answered in `## Findings` — never attach the panel's work, or any other work,
to an external id. An id you omit is treated as NOT addressed and its thread
gets no answer — never omit one silently, and repeat the section in every
round.
"""


def ack_section(text: str) -> str:
    """The coder handoff's ``## External findings`` section verbatim (header
    included), or ``""``. The round-1 reviewer prompt renders the coder's
    ``## Summary`` paragraph only, so the panel judging a no-change claim
    (PR #396 review) is shown the claim itself by appending this."""
    section = _ACK_SECTION_RE.search(text)
    if section is None:
        return ""
    return section.group(0).strip()


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
    nothing_to_remediate: Mapping[str, str] | None = None,
    loop_approved: bool = False,
    tree_changed: bool | None = None,
    missing_ack_detail: str | Mapping[str, str] = "",
    notes: Mapping[str, str] | None = None,
) -> tuple[ExternalOutcome, ...]:
    """Fold triage's verdicts + the coder's per-id claims into per-finding
    outcomes, in the injection order (``id_map`` preserves it).

    *nothing_to_remediate* are the ids triage found to be no claim at all (an
    approval): they never entered the loop, so they are dispositioned from
    the verdict alone, exactly like *rejections*.

    *acks* are the EFFECTIVE acknowledgements — in the epilogue, the final
    round's read against every earlier round's (#399,
    :func:`resolve_ack_history`); *notes* say per id how that reading went
    and ride the outcome unchanged, whatever its disposition.

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
    ``REVERTED`` acknowledgement is ``reverted``. ``no_change_needed`` needs
    the same ``loop_approved`` (PR #396 review: the loop admits a round-1
    no-change claim for review and its gate + panel judge the unchanged
    head; an unapproved claim is ``unaddressed``, never answered as "no
    change needed"). A dispute counts from either channel — a ``DISPUTED``
    acknowledgement line, or the shared ``## Findings`` block contract for an
    id with NO acknowledgement (the block is read in round 1 only, see
    :func:`final_round_outcomes`; the ack channel outranks it). Everything
    else is ``unaddressed`` —
    *missing_ack_detail* is the reason recorded when there is no ack at all
    (one string, or one per id).
    """
    out: list[ExternalOutcome] = []
    nothing = nothing_to_remediate or {}
    for fid, ext in id_map.items():
        if fid in rejections:
            out.append(ExternalOutcome(fid, ext, "rejected", detail=rejections[fid]))
            continue
        if fid in nothing:
            out.append(
                ExternalOutcome(fid, ext, "nothing_to_remediate", detail=nothing[fid])
            )
            continue
        out.append(
            dataclasses.replace(
                _outcome_of(
                    fid,
                    ext,
                    coder_findings.get(fid),
                    acks.get(fid),
                    loop_approved=loop_approved,
                    tree_changed=tree_changed,
                    missing_ack_detail=(
                        missing_ack_detail.get(fid, "")
                        if isinstance(missing_ack_detail, Mapping)
                        else missing_ack_detail
                    ),
                ),
                note=(notes or {}).get(fid, ""),
            )
        )
    return tuple(out)


def _outcome_of(
    fid: str,
    ext: ExternalFinding,
    claim: handoff.Finding | None,
    ack: CoderAck | None,
    *,
    loop_approved: bool,
    tree_changed: bool | None,
    missing_ack_detail: str,
) -> ExternalOutcome:
    """One finding's disposition from its (effective) acknowledgement, the
    round-1 dispute block and the loop's verdict — the rules of
    :func:`outcomes_after_loop`."""
    # The mandated ack channel outranks the round-1 `## Findings` block
    # (opus round 2): the block speaks only for an id with no ack, or
    # the same handoff could admit a no-change claim for review and then
    # read as a formal dispute — a "contradiction" that is not one.
    if (ack is None and claim is not None and claim.status == "disputed") or (
        ack is not None and ack.verdict == "disputed"
    ):
        detail = (
            claim.coder_response
            if claim is not None and claim.coder_response
            else (ack.detail if ack is not None else "")
        )
        return ExternalOutcome(fid, ext, "disputed", detail=detail)
    if ack is not None and ack.verdict == "reverted":
        return ExternalOutcome(fid, ext, "reverted", detail=ack.detail)
    if ack is not None and ack.verdict == "no_change_needed":
        # #380: not a defect (an approval verdict, intended behaviour) —
        # nothing landed and nothing had to. Like `fixed`, a claim the
        # LOOP must have approved (PR #396 review: the coder alone never
        # disposes an external finding — the gate + panel judged the
        # unchanged head, or the claim is unvalidated and no thread is
        # answered with it).
        if loop_approved:
            return ExternalOutcome(fid, ext, "no_change_needed", detail=ack.detail)
        return ExternalOutcome(
            fid,
            ext,
            "unaddressed",
            detail=(
                "the coder acknowledged NO CHANGE NEEDED "
                f"({ack.detail or 'no reason given'}) but the loop "
                "did not approve the unchanged head — the claim is "
                "unvalidated"
            ),
        )
    if ack is not None and ack.verdict == "fixed" and loop_approved:
        if tree_changed is False:
            # round 1 must commit, so an APPROVED loop that ends at the
            # PR head undid what it did — the decision shape, whatever
            # the coder wrote
            return ExternalOutcome(
                fid,
                ext,
                "reverted",
                detail=(
                    "acknowledged FIXED, but the final tree is identical "
                    "to the PR head outside the generated paths — the "
                    "fix was undone"
                ),
            )
        return ExternalOutcome(fid, ext, "fixed", detail=ack.detail)
    detail = ack.detail if ack is not None else missing_ack_detail
    return ExternalOutcome(fid, ext, "unaddressed", detail=detail)


_DECISIVE = ("fixed", "reverted")


def resolve_ack_history(
    history: Sequence[tuple[int, CoderAck | None]],
) -> tuple[CoderAck | None, str]:
    """The effective acknowledgement for one id from its acks across every
    round (``(round_no, ack-or-None)``, ascending), and a note when the
    final round's does not stand on its own (#399).

    The final round's ack is the answer whenever it is decisive —
    ``FIXED`` / ``REVERTED`` / ``DISPUTED`` (#387: a later round that undoes
    a fix says so, and wins). A final ``NO CHANGE NEEDED`` is not: the
    verdict means "never a defect, nothing was ever changed", so an earlier
    ``FIXED`` or ``REVERTED`` contradicts it. Only ONE earlier ack is
    trusted to carry forward: round 1's ``FIXED``, restated ``FIXED`` in
    every round between — round 1 predates the panel (external mode's
    intake is the injected findings alone), so its section can only speak
    of the external ids, whereas a later round's line may describe the
    panel's own same-looking ``f-001`` (lens #87 did exactly that in rounds
    2-3, and said "nothing further this round" in round 4 — the fix in the
    pushed tree is round 1's). Any other disagreement — a later-round
    origin, a revert or dispute or omission in between — is ``None``
    (``unaddressed``: no thread is answered, a false "Fixed in" being the
    one unacceptable outcome) with the note saying what the handoffs said.
    An omitted final ack is ``None`` for the same reason.
    """
    if not history:  # unreachable from the epilogue (final_round >= 1)
        return None, ""
    final_round, final = history[-1]
    if final is not None and final.verdict != "no_change_needed":
        return final, ""
    earlier = [(r, a) for r, a in history[:-1] if a is not None]
    decisive = [(r, a) for r, a in earlier if a.verdict in _DECISIVE]
    if not decisive:
        return final, ""
    said = ", ".join(f"{a.verdict.upper()} in round {r}" for r, a in decisive)
    if final is None:
        return None, f"{said}; no acknowledgement in the round {final_round} handoff"
    unbroken_from_round_one = len(earlier) == final_round - 1 and all(
        a.verdict == "fixed" for _r, a in earlier
    )
    if unbroken_from_round_one:
        return earlier[0][1], (
            f"FIXED in round 1 and every round since; the round {final_round} "
            f"handoff said NO CHANGE NEEDED ({final.detail or 'no reason given'})"
            " — read as round 1's FIXED"
        )
    return None, (
        f"{said}; the round {final_round} handoff said NO CHANGE NEEDED "
        f"({final.detail or 'no reason given'}) — the handoffs disagree and "
        "no one round is trusted: not answered on the thread"
    )


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
    nothing_to_remediate: Mapping[str, str] | None = None,
    surviving_ids: Sequence[str],
) -> tuple[ExternalOutcome, ...]:
    """The converge epilogue's dispositions, read from the coder's
    handoffs of EVERY round — the mandated ``## External findings`` acks,
    the final round's resolved against the earlier ones per id
    (:func:`resolve_ack_history`, #399: lens #87's final "NO CHANGE NEEDED"
    over a round-1 FIXED; #387: lens #84's round 1 said FIXED, round 3
    reverted it, and the threads were answered from round 1 — a decisive
    final ack still wins) plus round 1's ``## Findings`` dispute block —
    and checked against the tree: a run whose final tree equals the PR
    head outside the generated paths undid its fix. The ack section is
    scoped to the injected ids by construction; the ``## Findings`` block
    is read in round 1 only (a later round's belongs to the panel). A
    final round without the section carries no earlier claim forward (the
    safe direction) — the note names what was dropped.
    """
    coder_claims: dict[str, handoff.Finding] = {}
    final_round = max(rounds, 1)
    texts: dict[int, str] = {}
    for round_no in range(1, final_round + 1):
        try:
            texts[round_no] = (
                handoff_dir / handoff.coder_handoff_name(round_no)
            ).read_text(encoding="utf-8")
        except OSError:
            texts[round_no] = ""  # loop died before that round's handoff
    per_round = {r: parse_coder_acks(t, surviving_ids) for r, t in texts.items()}
    acks: dict[str, CoderAck] = {}
    notes: dict[str, str] = {}
    for fid in surviving_ids:
        ack, note = resolve_ack_history(
            [(r, per_round[r].get(fid)) for r in sorted(per_round)]
        )
        if ack is not None:
            acks[fid] = ack
        if note:
            notes[fid] = note
            logger.warning("converge %s: %s: %s", run_id, fid, note)
    missing: dict[str, str] = {}
    text = texts[final_round]
    if text:
        stale = " — an earlier round's claim is not carried forward"
        if not per_round[final_round]:
            # no section at all (or nothing parseable in it)
            reason = f"no acknowledgement in the round {final_round} coder handoff"
            logger.warning("converge %s: %s", run_id, reason)
            missing = {
                fid: reason + (stale if final_round > 1 else "")
                for fid in surviving_ids
            }
        else:
            missing = {
                fid: f"not acknowledged in the round {final_round} coder handoff"
                + (stale if final_round > 1 else "")
                for fid in surviving_ids
                if fid not in per_round[final_round]
            }
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
        nothing_to_remediate=nothing_to_remediate,
        loop_approved=loop_approved,
        tree_changed=tree_changed,
        missing_ack_detail=missing,
        notes=notes,
    )


def nothing_to_change(outcomes: Sequence[ExternalOutcome]) -> bool:
    """#380: every injected finding was refuted by triage, was no claim at
    all (``nothing_to_remediate`` — an approval), or was dispositioned
    ``no_change_needed`` — which the loop APPROVED (an unapproved claim reads
    ``unaddressed``, so this is never true on the coder's word alone) — the
    run had nothing to do, so a loop that committed nothing is
    ``already_clean`` (reported, not remediated), not a failure. False when
    there is no external finding at all."""
    return bool(outcomes) and all(
        o.disposition in ("rejected", "nothing_to_remediate", "no_change_needed")
        for o in outcomes
    )


def claims_nothing_to_change(
    handoff_dir: Path, round_no: int, finding_ids: Sequence[str]
) -> bool:
    """PR #396 review (High): whether the round's coder handoff claims that
    EVERY injected finding needs no change — the one shape in which a
    round-1 coder may commit nothing and still be reviewed: the loop admits
    the empty round (``LoopEntry.no_change_claim``) and its gate + panel
    judge the claim at the unchanged head. Anything short of that — a
    ``FIXED`` over an unchanged tree, a ``DISPUTED``, an omitted id, no
    section, no handoff, nothing injected — is not a reviewable claim, and
    round 1's no-commit exit stands.
    """
    if not finding_ids:
        return False
    try:
        text = (handoff_dir / handoff.coder_handoff_name(round_no)).read_text(
            encoding="utf-8"
        )
    except OSError:
        return False
    acks = parse_coder_acks(text, finding_ids)
    return all(
        (ack := acks.get(fid)) is not None and ack.verdict == "no_change_needed"
        for fid in finding_ids
    )


def render_external_context(findings: Mapping[str, ExternalFinding]) -> str:
    """The panel's context in external mode (PR #396 review): the injected
    findings by the id the coder saw — author, location, body, fenced so
    reviewer prose cannot become prompt prose — and the rule that the coder's
    per-id acknowledgement is a claim for the panel to verify: a ``NO CHANGE
    NEEDED`` or ``DISPUTED`` a reviewer disagrees with is a finding of theirs.
    Without it the panel would review the PR blind to what was claimed, and a
    round-1 no-change claim (admitted for review with nothing committed)
    would rest on the coder's word."""
    if not findings:
        return ""
    entries = []
    for fid, f in findings.items():
        if f.path:
            where = f"{f.path}:{f.line}" if f.line else f.path
        else:
            where = "the PR as a whole (a conversation comment)"
        entries.append(f"{fid} — [{f.author}] at {where}:\n{' '.join(f.body.split())}")
    body = "\n\n".join(entries)
    body_fence = fence(body)
    return "\n".join(
        [
            "## External review findings under remediation",
            "",
            (
                "This run was dispatched to act on findings raised by EXTERNAL "
                "reviewers on the PR's own threads, injected under these ids:"
            ),
            "",
            body_fence,
            body,
            body_fence,
            "",
            (
                "The coder's handoff acknowledges each id in its `## External "
                "findings` section — FIXED, DISPUTED, REVERTED or NO CHANGE NEEDED "
                "— and every acknowledgement is a claim for you to verify against "
                "the tree, never a verdict: the external reviewer proposes, this "
                "panel disposes. A NO CHANGE NEEDED or DISPUTED you disagree with "
                "(the finding names a real defect the tree still carries) is a "
                "finding of yours, with the defect as its rationale; a FIXED you "
                "cannot confirm in the diff is one too. When the coder committed "
                "nothing this round, the change under review is the PR head as it "
                "stands, and the question is exactly whether that claim holds."
            ),
        ]
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
