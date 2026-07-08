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


@pytest.mark.parametrize("name", ["coder_init.md", "coder_fix.md"])
def test_coder_prompt_forbids_background_and_defers_tests(name: str) -> None:
    text = load_prompt(name).lower()
    # single non-interactive turn + no background-and-wait
    assert "non-interactive turn" in text
    assert "never background" in text
    # the objective gate covers tests, so the coder needn't run the full suite
    assert "objective test gate" in text


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
