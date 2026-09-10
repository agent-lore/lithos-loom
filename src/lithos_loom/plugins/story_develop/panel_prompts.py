"""The reviewer panel's prompts — briefs, severity calibration and the
per-round prompt renderer.

Split out of :mod:`.panel` (at the module budget) so the prompt shape has one
home: round 1 renders ``reviewer_round.md``, later rounds
``reviewer_rereview.md``, an artifact pass ``reviewer_artifacts.md``; the
usage-limit reseed (``reviewer_reseed.md``, rendered in :mod:`.panel`) shares
:func:`context_block`. Every variant carries the optional ``review_context``
block (PRD S5: a conflict resolution's merge shape — the conflicted paths
and both parents).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...runner import git
from . import handoff
from .check_set import CheckSetResult, render_check_summary
from .config import DevelopConfig
from .gate_findings import GateLedger
from .handoff import render_prompt
from .sandbox_facts import for_prompt as _sandbox_section

if TYPE_CHECKING:
    from .config import ReviewerSpec

__all__ = [
    "SEVERITY_CALIBRATION",
    "artifact_reviewer_brief",
    "context_block",
    "reviewer_brief",
    "round_prompt",
]


def context_block(review_context: str) -> str:
    """The ``{review_context}`` slot's value: the block padded onto its own
    lines, or nothing — every reviewer template (round, re-review, artifact
    pass, reseed) renders it the same way."""
    return f"\n{review_context}\n" if review_context else ""


# Shared severity rubric injected into every reviewer prompt (#137, ADR 0003 §8)
# so the panel calibrates the same way; the orchestrator then applies each
# reviewer's per-persona ``block_threshold`` to decide what actually blocks.
SEVERITY_CALIBRATION = """## Severity calibration

Give each finding the severity the orchestrator will weigh against this
reviewer's threshold. Calibrate consistently across the panel:

- **critical** — a security vulnerability, a data-loss risk, or a correctness
  defect that breaks an acceptance criterion.
- **major** — a real bug or a significant quality / maintainability problem that
  should be fixed before merge.
- **minor** — a style, naming, or low-impact maintainability nit; recorded but
  usually non-blocking.

Assign the honest severity — do not inflate to force a block or deflate to dodge
one. The threshold decision is the orchestrator's, not yours."""


def reviewer_brief(spec) -> str:
    """The optional per-reviewer focus paragraph + lane discipline for its prompts.

    A focused persona (``system_prompt`` set) is told to stay strictly in its
    dimension so the panel does not produce N overlapping general reviews. The
    generalist default (no ``system_prompt``) is unchanged — empty string.
    """
    if not spec.system_prompt:
        return ""
    return (
        f"\n## Your focus\n\n{spec.system_prompt}\n\n"
        "**Stay strictly within this focus.** Record only findings in this "
        "dimension — another reviewer owns the rest; do not report outside your "
        "lane.\n"
    )


def artifact_reviewer_brief(spec) -> str:
    """The reviewer's responsibility on the ARTIFACT pass (#308 review).

    Deliberately **not** the code-review brief, and this is a documented
    exception to ``ReviewerSpec.system_prompt`` — see below, it was measured.

    Those briefs narrow hard: "judge only what this change does to the
    dependencies — nothing else", "find ways this change can be abused —
    nothing else", plus the stay-in-your-lane rule :func:`reviewer_brief`
    appends; ``dependency-hygiene`` adds "if it adds or bumps no dependency, a
    quick LGTM is the right answer". On a pass about screenshots that is a
    licence to rubber-stamp, so injecting them handed every persona but
    ``correctness`` two incompatible orders.

    Re-attaching the brief as an *additive* lens with its narrowing language
    explicitly suspended was tried, because dropping it discards a project pool's
    configured speciality (#308 review 2). Measured on ``lens22-artifact-prewrap``
    with ``dependency-hygiene``, K=3 — the catch never moved, and the clean
    render is the only thing that changed:

    ==================================  ======  ==========================
    artifact brief                      catch   known-good runs that BLOCK
    ==================================  ======  ==========================
    mandate only (this)                 3/3     0/3
    + brief re-attached, suspended       3/3     3/3  (1-4 findings each)
    + brief re-attached, scope-guarded   3/3     2/3  (1 finding each)
    ==================================  ======  ==========================

    A reviewer holding approval on correct pages is a broken gate, and the
    residue is exactly the sharpened-eye-turned-noise failure mode: on the
    *fixed* render it filed "the top-level heading has no space above it".
    A speciality worth having on this surface (accessibility, brand,
    design-system) is better served by a reviewer that *is* one than by a
    dependency reviewer wearing its hat — the artifact panel's composition is
    RH-9's question. Engine, model and block threshold still vary per persona.
    """
    return (
        "\n## Your focus\n\n"
        f"You are the **{spec.name}** reviewer. This is not a code review: on "
        "this pass you are the only reviewer looking at these rendered pages, "
        "so **every rendering defect is yours to report** — whichever code "
        "dimension it would otherwise belong to, and however much it reads as "
        "styling. That widens what you look **for**, not what counts as a "
        "defect: report what a user would experience as wrong on the page in "
        "front of you, never work this change did not claim to do.\n"
    )


def round_prompt(
    config: DevelopConfig,
    rstate: Any,
    *,
    round_no: int,
    fork: str,
    wt: Path,
    check_set: CheckSetResult | None,
    gate_ledger: GateLedger,
    coder_summary: str,
    artifacts_note: str,
    artifact_pass: bool,
    review_context: str = "",
) -> tuple[str, bool, str | None]:
    """Render one reviewer's prompt for this round: ``(prompt, resume, review
    file override)``. *rstate* is the panel's ``ReviewerState`` (its ``spec``,
    ``ledger`` and last ``outcome`` are read)."""
    spec: ReviewerSpec = rstate.spec
    name = spec.name
    sandbox = _sandbox_section(config.image, for_coder=False)
    gate_summary = render_check_summary(
        check_set, for_coder=False, gate_ledger=gate_ledger
    )
    context = context_block(review_context)
    if artifact_pass:
        # #283 (PR #291 review): a panel-only pass shown the artifacts the
        # candidate checks collected AFTER this round's regular review —
        # its own handoff file, so the round's review is never clobbered.
        review_file = handoff.reviewer_handoff_name(round_no, f"{name}_artifacts")
        prompt = render_prompt(
            handoff.load_prompt("reviewer_artifacts.md"),
            reviewer=name,
            reviewer_brief=artifact_reviewer_brief(spec),
            sandbox_facts=sandbox,
            round_no=str(round_no),
            acceptance_criteria=config.effective_acceptance_criteria,
            base_sha=fork[:12],
            artifacts_note=artifacts_note,
            gate_summary=gate_summary,
            review_context=context,
            severity_calibration=SEVERITY_CALIBRATION,
            review_file=review_file,
        )
        # Resume only a session a prior round actually minted: in develop the
        # pass follows this round's regular review (resume, unchanged); in
        # review-only artifact-only mode (RH-3) the panel is fresh, and
        # resume=True would hand `claude --resume` a session that does not
        # exist, failing every turn before it starts.
        return prompt, rstate.outcome is not None, review_file
    if round_no == 1:
        prompt = render_prompt(
            handoff.load_prompt("reviewer_round.md"),
            reviewer=name,
            reviewer_brief=reviewer_brief(spec),
            sandbox_facts=sandbox,
            acceptance_criteria=config.effective_acceptance_criteria,
            coder_summary=coder_summary,
            base_sha=fork[:12],
            diff_stat=git.diff_stat(wt, fork),
            gate_summary=gate_summary,
            artifacts_note=artifacts_note,
            review_context=context,
            severity_calibration=SEVERITY_CALIBRATION,
            review_file=handoff.reviewer_handoff_name(1, name),
        )
        return prompt, False, None
    prompt = render_prompt(
        handoff.load_prompt("reviewer_rereview.md"),
        reviewer=name,
        reviewer_brief=reviewer_brief(spec),
        sandbox_facts=sandbox,
        round_no=str(round_no),
        acceptance_criteria=config.effective_acceptance_criteria,
        base_sha=fork[:12],
        coder_handoff_file=handoff.coder_handoff_name(round_no),
        open_findings=rstate.ledger.render_open(),
        diff_stat=git.diff_stat(wt, fork),
        gate_summary=gate_summary,
        artifacts_note=artifacts_note,
        review_context=context,
        severity_calibration=SEVERITY_CALIBRATION,
        review_file=handoff.reviewer_handoff_name(round_no, name),
    )
    return prompt, True, None
