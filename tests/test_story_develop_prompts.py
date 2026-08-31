"""Guardrails on the packaged coder prompts (regression for lithos-loom#114).

The coder runs in a single non-interactive ``claude -p`` turn. Prior failure
mode: the coder backgrounded a slow test suite and ended its turn waiting for
async continuation, so it never wrote the handoff and the round failed despite
completed work. The prompts must keep telling the coder (a) it has one turn and
must never background-and-wait, and (b) the orchestrator runs an objective test
gate, so it need not run the full suite itself.
"""

from __future__ import annotations

import pytest

from lithos_loom.plugins.story_develop.config import ReviewerSpec
from lithos_loom.plugins.story_develop.develop import _coder_handoff_nudge
from lithos_loom.plugins.story_develop.handoff import load_prompt
from lithos_loom.plugins.story_develop.panel import (
    SEVERITY_CALIBRATION,
    _reviewer_brief,
)


@pytest.mark.parametrize(
    "name", ["coder_init.md", "coder_fix.md", "converge_coder_init.md"]
)
def test_coder_prompt_forbids_background_and_defers_tests(name: str) -> None:
    text = load_prompt(name).lower()
    # single non-interactive turn + no background-and-wait
    assert "non-interactive turn" in text
    assert "never background" in text
    # the objective gate covers tests, so the coder needn't run the full suite
    assert "objective test gate" in text


def test_converge_prompt_carries_intent_transfer_and_slots() -> None:
    # The converge cold-start prompt fixes a PR the coder did NOT author, so it
    # must steer intent reconstruction (read the PR + commit log + code first),
    # keep the dispute escape, and expose the render slots converge fills.
    raw = load_prompt("converge_coder_init.md")
    text = " ".join(raw.lower().split())
    assert "did not author" in text
    assert "commit history" in text or "commit log" in text
    assert "dispute" in text
    # do not redesign — satisfy the author's intent
    assert "do not redesign" in text or "not redesign" in text
    for slot in (
        "{acceptance_criteria}",
        "{commit_log}",
        "{findings}",
        "{gate_summary}",
        "{handoff_file}",
        # external mode's per-id acknowledgement contract (PR #345 re-review
        # 1); rendered empty on the local-panel path
        "{external_ack}",
    ):
        assert slot in raw


def test_coder_init_drops_run_the_suite_instruction() -> None:
    # The old instruction ("run it and note the result") is what pushed the
    # agent to background a slow suite; it must not return.
    assert "run it and note the result" not in load_prompt("coder_init.md")


def test_coder_init_carries_plan_first_and_pragmatic_test_discipline() -> None:
    # The implement turn must steer the coder to understand + plan before
    # editing, then add tests that actually protect the new behaviour — without
    # turning into dogmatic ceremony testing.
    # normalise wrapping so phrase checks don't hinge on line breaks
    text = " ".join(load_prompt("coder_init.md").lower().split())
    assert "plan-first" in text
    assert "smallest change" in text
    # pragmatic test-first: a test that fails without the change, but not dogma
    assert "fail without your change" in text
    assert "pragmatic" in text
    # ...and the coder must RUN that targeted fast test (red->green), not merely
    # write it — the core of test-first, scoped to the fast test so the #114
    # full-suite/background guardrail still holds (#153 review).
    assert "run that targeted fast test" in text


def test_coder_fix_keeps_regression_test_discipline() -> None:
    # The fix turn carries the FULL discipline: understand + plan before editing
    # (not just "smallest change" + a regression test), so round-2+ coders get
    # the same plan-first guidance the init turn does.
    text = " ".join(load_prompt("coder_fix.md").lower().split())
    assert "understand before you change" in text
    assert "plan before you edit" in text
    assert "regression test" in text
    assert "smallest change" in text
    # the regression test must be RUN (red->green), not merely written (#153 review)
    assert "run that targeted fast test" in text


def test_coder_handoff_nudge_asks_only_for_the_handoff() -> None:
    # The #114 salvage re-prompt: when the coder left work but no handoff, the
    # one-shot nudge names that round's handoff file and forbids any further
    # backgrounded/awaited work — the implementation is already done.
    nudge = _coder_handoff_nudge(1)
    assert "round_01_coder_done.md" in nudge
    assert "synchronously" in nudge
    assert "background" in nudge
    # the stable marker the orchestrator's salvage path is recognised by
    assert "never wrote your handoff" in nudge


# --- reviewer prompt discipline + severity calibration (#137) ----------------


@pytest.mark.parametrize("name", ["reviewer_round.md", "reviewer_rereview.md"])
def test_reviewer_templates_carry_the_severity_calibration_slot(name: str) -> None:
    assert "{severity_calibration}" in load_prompt(name)


@pytest.mark.parametrize("name", ["reviewer_round.md", "reviewer_rereview.md"])
def test_reviewer_templates_require_mechanical_ac_to_evidence_mapping(
    name: str,
) -> None:
    # #208: holistic "judge whether it meets the AC" lets unmet criteria slip
    # through under single-pass variance / coder-summary anchoring (influx #239 →
    # PR #242 escape). Both reviewer prompts must force a *mechanical* per-criterion
    # AC -> evidence checklist, where a criterion with no implementing evidence is
    # itself a finding (not satisfied by the coder's claim that it is done).
    text = " ".join(load_prompt(name).lower().split())
    assert "acceptance criteri" in text
    assert "one by one" in text
    assert "evidence" in text
    # An unmet criterion is a finding in its own right.
    assert "unmet" in text


def test_round1_template_offers_the_full_base_head_diff() -> None:
    # The architecture persona needs the cumulative change in round 1, not just
    # the latest commit, so round 1 now injects base_sha and offers base..HEAD.
    text = load_prompt("reviewer_round.md")
    assert "{base_sha}" in text
    assert "{base_sha}..HEAD" in text


def test_severity_calibration_defines_all_three_levels() -> None:
    cal = SEVERITY_CALIBRATION.lower()
    assert "critical" in cal
    assert "major" in cal
    assert "minor" in cal


def test_reviewer_brief_adds_focus_discipline_when_a_focus_is_set() -> None:
    spec = ReviewerSpec(name="security", system_prompt="Find injection bugs.")
    brief = _reviewer_brief(spec)
    assert "Find injection bugs." in brief
    assert "Stay strictly within this focus" in brief


def test_reviewer_brief_is_empty_for_the_generalist_default() -> None:
    # The zero-config code-quality reviewer has no focus; its prompt is unchanged.
    assert _reviewer_brief(ReviewerSpec(name="code-quality")) == ""


def test_reviewer_templates_carry_the_artifacts_note_slot() -> None:
    # #283 slice 2: collected gate artifacts (rendered-page screenshots) are
    # enumerated into BOTH reviewer prompts — round one and re-review — so a
    # panel evaluating a web UI sees pages, not just the diff.
    assert "{artifacts_note}" in load_prompt("reviewer_round.md")
    assert "{artifacts_note}" in load_prompt("reviewer_rereview.md")


def test_every_agent_template_carries_the_sandbox_facts_slot() -> None:
    """SC-1: the measured environment reaches EVERY agent, both families.

    A coder that never sees the list can still claim a tool is absent, and a
    reviewer that never sees it has no means to refuse the claim — which is
    exactly what happened on lens T1-S11. The reseed template is included
    deliberately: it is a FRESH session with no history, so it needs the facts
    most, and it is the one prompt that historically got left out of shared
    sections (it carries no gate summary, artifacts note or severity block).
    """
    for name in (
        "reviewer_round.md",
        "reviewer_rereview.md",
        "reviewer_reseed.md",
        "reviewer_artifacts.md",
        "coder_init.md",
        "coder_fix.md",
        "converge_coder_init.md",
        "external_triage.md",
    ):
        assert "{sandbox_facts}" in load_prompt(name), name


# ── the artifact pass's semantics (RH-1 / #308 review) ────────────────────────
# The pass returned ZERO findings over 20+ samples until these instructions
# existed, so they are load-bearing behaviour, not prose. Each assertion below
# pins a diagnosed cause of that blindness.


def test_artifact_prompt_leads_with_rendering_fidelity() -> None:
    # the breakage-shaped checklist had no bucket for "renders, but renders
    # wrong" — reviewers enumerated it back verbatim and LGTM'd
    body = load_prompt("reviewer_artifacts.md")
    assert "Rendering fidelity" in body
    assert "internally inconsistent" in body


def test_artifact_prompt_invites_the_source_cross_check() -> None:
    # "do not re-litigate the diff" forbade the one move that makes a visual
    # anomaly decidable: open the rule that produces it
    body = load_prompt("reviewer_artifacts.md")
    assert "do not re-litigate" not in body.lower()
    assert "find the\n   rule responsible" in body or "rule responsible" in body


def test_artifact_prompt_keeps_a_finding_reportable_without_a_known_cause() -> None:
    # #308 review (finding 2): demanding an identified source rule for EVERY
    # finding suppresses real defects with no localisable cause (a broken
    # asset, a failed script) or invites fabricated attribution
    body = load_prompt("reviewer_artifacts.md")
    assert "cannot be localised" in body
    assert "**Report those too**" in body
    assert "when you identified one" in body


def test_artifact_prompt_accounts_for_a_capped_listing() -> None:
    # #308 review (finding 3): render_artifacts_note caps at 12 files per check
    # and 36 overall, so "open every image listed" understates the job
    body = load_prompt("reviewer_artifacts.md")
    assert "capped" in body
    assert "+N more" in body
    assert "List each directory" in body


def test_artifact_prompt_carries_no_approval_prime() -> None:
    # the preamble used to assert the panel had already approved this change —
    # priming, and counterfactual in the eval's artifact-only mode
    body = load_prompt("reviewer_artifacts.md").lower()
    assert "you approved" not in body
    assert "review of this work passed" not in body


def test_artifact_prompt_treats_the_criteria_as_a_floor() -> None:
    body = load_prompt("reviewer_artifacts.md")
    assert "floor, not a\n   ceiling" in body or "floor, not a ceiling" in body


def test_artifact_prompt_guards_against_taste_findings() -> None:
    # the counterweight to the above: a sharper eye must not become a
    # trigger-happy one (the known-good arm measures this for real)
    body = load_prompt("reviewer_artifacts.md")
    assert "not what you would have designed differently" in body


def test_external_triage_prompt_defaults_to_act_and_demands_evidence() -> None:
    """S5a's load-bearing steering (PRD): triage may reject only with cited
    code evidence, defaults to PROCEED when unsure, and never fixes anything
    itself — a prompt re-tune that loses any of these regresses the
    over-suppression guard."""
    raw = load_prompt("external_triage.md")
    text = " ".join(raw.lower().split())
    assert "when in doubt, proceed" in text
    assert "reject a claim **only**" in text or "reject" in text and "cite" in text
    assert "read-only" in text
    assert "you do not fix anything" in text
    assert "a reject without evidence is treated as proceed" in text
    assert "{findings}" in raw and "{handoff_file}" in raw
    assert "non-interactive turn" in text and "never background" in text
