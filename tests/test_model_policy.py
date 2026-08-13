"""Tests for the explicit per-agent model policy (#304).

Every agent invocation (coder + each panel reviewer, develop AND eval) must
resolve to an explicit model — the sandbox image CLIs' builtin defaults are
invisible, drift with image rebuilds, and made eval arms incomparable (#303).
``model_policy`` is the shared seam: fill from ``[story_develop.default_models]``,
then fail closed on anything still unset.
"""

from __future__ import annotations

import pytest

from lithos_loom.plugins.story_develop.config import ReviewerSpec
from lithos_loom.plugins.story_develop.model_policy import (
    active_model,
    apply_panel_default_models,
    missing_agent_models,
    missing_fallback_models,
    require_agent_models,
)

# ── apply_panel_default_models ─────────────────────────────────────────


def test_apply_fills_only_unset_models_keyed_by_tool() -> None:
    panel = (
        ReviewerSpec(name="correctness", tool="codex"),
        ReviewerSpec(name="security", tool="claude", model="pinned"),
    )
    out = apply_panel_default_models(panel, {"codex": "gpt-x", "claude": "fable"})
    assert out[0].model == "gpt-x"
    assert out[1].model == "pinned"  # explicit pin wins over the default


def test_apply_leaves_tool_without_default_unset() -> None:
    panel = (ReviewerSpec(name="correctness", tool="codex"),)
    out = apply_panel_default_models(panel, {"claude": "fable"})
    assert out[0].model is None


def test_apply_empty_mapping_is_noop_identity() -> None:
    panel = (ReviewerSpec(name="correctness", tool="codex"),)
    assert apply_panel_default_models(panel, {}) == panel


# ── missing_agent_models ───────────────────────────────────────────────


def test_missing_names_each_agent_with_its_tool() -> None:
    panel = (
        ReviewerSpec(name="correctness", tool="codex"),
        ReviewerSpec(name="security", tool="claude", model="fable"),
    )
    missing = missing_agent_models(panel=panel, coder="claude", coder_model=None)
    joined = " ".join(missing)
    assert "coder" in joined and "claude" in joined
    assert "correctness" in joined and "codex" in joined
    assert "security" not in joined


def test_missing_empty_when_everything_pinned() -> None:
    panel = (ReviewerSpec(name="security", tool="claude", model="fable"),)
    assert missing_agent_models(panel=panel, coder="codex", coder_model="gpt-x") == ()


def test_missing_without_coder_checks_panel_only() -> None:
    # review-only surfaces (eval, develop review/converge) have no coder
    panel = (ReviewerSpec(name="correctness", tool="codex"),)
    missing = missing_agent_models(panel=panel)
    assert len(missing) == 1
    assert "correctness" in missing[0]


# ── require_agent_models ───────────────────────────────────────────────


def test_require_raises_with_agents_and_remedy() -> None:
    panel = (ReviewerSpec(name="correctness", tool="codex"),)
    with pytest.raises(ValueError) as exc:
        require_agent_models(panel=panel, where="eval review")
    msg = str(exc.value)
    assert "correctness" in msg and "codex" in msg
    assert "eval review" in msg
    # actionable: the message names the config key to set
    assert "[story_develop.default_models]" in msg


def test_require_passes_when_all_explicit() -> None:
    panel = (ReviewerSpec(name="correctness", tool="codex", model="gpt-x"),)
    require_agent_models(
        panel=panel, coder="claude", coder_model="fable", where="develop"
    )


# ── active_model (#305 review finding 1) ───────────────────────────────


def test_active_model_uses_spec_model_for_primary_tool() -> None:
    spec = ReviewerSpec(name="security", tool="claude", model="fable")
    assert active_model(spec, "claude", {"codex": "gpt-x"}) == "fable"


def test_active_model_uses_tool_default_after_engine_switch() -> None:
    # a claude model string must NEVER reach `codex -m` — the fallback engine
    # draws from ITS tool's default, not the primary spec pin
    spec = ReviewerSpec(name="security", tool="claude", model="fable")
    assert active_model(spec, "codex", {"codex": "gpt-x"}) == "gpt-x"


def test_active_model_missing_fallback_default_is_none() -> None:
    spec = ReviewerSpec(name="security", tool="claude", model="fable")
    assert active_model(spec, "codex", {}) is None


# ── fallback-chain coverage (#305 review finding 1) ────────────────────


def test_missing_fallback_models_names_uncovered_chain_tools() -> None:
    panel = (
        ReviewerSpec(
            name="security", tool="claude", model="fable", fallback_chain=("codex",)
        ),
    )
    missing = missing_fallback_models(panel, {})
    assert len(missing) == 1
    assert "security" in missing[0] and "codex" in missing[0]


def test_missing_fallback_models_empty_when_defaults_cover_chain() -> None:
    panel = (
        ReviewerSpec(
            name="security", tool="claude", model="fable", fallback_chain=("codex",)
        ),
    )
    assert missing_fallback_models(panel, {"codex": "gpt-x"}) == ()


def test_missing_fallback_models_ignores_self_and_unsupported_tools() -> None:
    # the runtime skips unsupported chain entries with a warning, so requiring
    # a model for one would demand config for a tool that can never run
    panel = (
        ReviewerSpec(
            name="security",
            tool="claude",
            model="fable",
            fallback_chain=("claude", "opencode"),
        ),
    )
    assert missing_fallback_models(panel, {}) == ()


def test_require_agent_models_covers_fallback_chains() -> None:
    panel = (
        ReviewerSpec(
            name="security", tool="claude", model="fable", fallback_chain=("codex",)
        ),
    )
    with pytest.raises(ValueError, match="codex"):
        require_agent_models(panel=panel, default_models={}, where="develop")
    # covered chain passes
    require_agent_models(
        panel=panel, default_models={"codex": "gpt-x"}, where="develop"
    )
