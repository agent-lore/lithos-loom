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

from lithos_loom.plugins.story_develop.handoff import load_prompt


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
