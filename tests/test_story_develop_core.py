"""Orchestration tests for ``develop()`` (T3: implement → review → fix loop).

Real git/worktree against a temp repo; the containers + turns are monkeypatched
so no Docker or agent is needed. A single fake ``run_turn`` plays both roles and
all rounds, branching on the container name and parsing the round number out of
the prompt, and writing the appropriate handoff files. Reviewer behaviour per
round is scripted via the ``reviews`` list.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from lithos_loom.plugins.story_develop import containers, handoff
from lithos_loom.plugins.story_develop import develop as develop_mod
from lithos_loom.plugins.story_develop import test_gate as test_gate_mod
from lithos_loom.plugins.story_develop.config import DevelopConfig, ReviewerSpec
from lithos_loom.plugins.story_develop.test_gate import GateResult
from lithos_loom.plugins.story_develop.turns import TurnResult

_LGTM = "## Status: LGTM\n## Summary\nLooks correct and complete.\n"
# NEW findings carry a BLANK id — the orchestrator's ledger assigns f-001 etc.
_FINDINGS_MAJOR = (
    "## Status: FINDINGS\n## Summary\nOne issue.\n## Findings\n"
    "- finding_id:\n  severity: major\n  status: open\n"
    '  files: ["greeting.txt:1"]\n  rationale: needs work\n  coder_response:\n'
)
_FINDINGS_MINOR = _FINDINGS_MAJOR.replace("severity: major", "severity: minor")
# A re-review that keeps the (ledger-assigned) first finding open by id.
_FINDINGS_KEEP_F001 = (
    "## Status: FINDINGS\n## Summary\nStill not addressed.\n## Findings\n"
    "- finding_id: f-001\n  severity: major\n  status: open\n"
    '  files: ["greeting.txt:1"]\n  rationale: still needs work\n'
)
_GARBAGE = "this is not a valid handoff at all\n"


def _worktree_from_run_cmd(run_cmd) -> Path:
    # the worktree mount is "<src>:/workspace" (coder) or "<src>:/workspace:ro"
    # (reviewer); the handoff mount "<src>:/workspace/.handoff" must not match.
    for i, arg in enumerate(run_cmd):
        if arg == "-v":
            parts = run_cmd[i + 1].split(":")
            if len(parts) >= 2 and parts[1] == "/workspace":
                return Path(parts[0])
    raise AssertionError("no /workspace mount in run cmd")


def _round_from(prompt: str, kind: str) -> int:
    m = re.search(rf"round_(\d+)_{kind}", prompt)
    assert m is not None, f"no {kind} round marker in prompt:\n{prompt}"
    return int(m.group(1))


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    config: DevelopConfig,
    *,
    coder_ok: bool = True,
    write_source: bool = True,
    write_coder_handoff: bool = True,
    reviews: list[dict] | dict[str, list[dict]] | None = None,
    source_rounds: set[int] | None = None,
    gates: list[bool | str] | None = None,
    coder_results: list[str] | None = None,
    coder_transcript_on_limit: bool = False,
    coder_handoffs: dict[int, str] | None = None,
    skip_coder_handoff_until_nudge: bool = False,
    nudge_fails: bool = False,
) -> dict:
    """Install fake container + turn + gate machinery.

    ``reviews`` scripts the reviewer per round (0-based; the last entry repeats
    for any further rounds). Each entry: ``{text, ok, retry_text, retry_ok}``.
    ``text`` is what the first review turn writes (None = write nothing);
    ``retry_text`` is what the malformed-handoff re-prompt writes.
    ``source_rounds`` limits which rounds the coder writes source in (None =
    every round). ``gates`` scripts the gate result per gate run (last repeats;
    ``True``/``False`` = green/red, ``"error"`` = simulated infra failure); the
    gate only actually runs when the config carries a ``test_command``.

    T5 limit scripting: ``coder_results`` is consumed per coder call (last
    repeats; ``"ok"`` or ``"limit"``); a review entry's ``limit_first: N``
    makes that round's first N reviewer calls fail usage-limited.
    ``coder_transcript_on_limit`` simulates the partial session transcript
    surviving the interruption (drives the resume-vs-fresh retry choice).
    ``_sleep`` is captured into ``state["sleeps"]`` — tests never sleep.
    """
    reviews = reviews if reviews is not None else [{"text": _LGTM}]
    state: dict = {
        "stopped": [],
        "starts": 0,
        "coder_calls": [],
        "coder_prompts": [],
        "review_calls": [],
        "review_prompts": [],
        "review_attempts": {},
        "gate_calls": [],
        "sleeps": [],
        "tools": [],
        "models": [],
        "efforts": [],
        "start_cmds": [],
    }

    def fake_start(run_cmd) -> str:
        state["worktree"] = _worktree_from_run_cmd(run_cmd)
        state["start_cmds"].append(list(run_cmd))
        state["starts"] += 1
        return "cid"

    def _limit_turn(session_id: str) -> TurnResult:
        return TurnResult(
            exit_code=1,
            succeeded=False,
            session_id="",
            result_text="Claude AI usage limit reached|1750000000",
            cost_usd=0.001,
            raw={"is_error": True},
            stderr="",
        )

    def _entry(rnd: int, rev_name: str) -> dict:
        # `reviews` is a list (shared by all reviewers — the single-reviewer
        # style) or a dict keyed by reviewer name (multi-reviewer scripting).
        seq = reviews[rev_name] if isinstance(reviews, dict) else reviews
        return seq[min(rnd - 1, len(seq) - 1)]

    def fake_run_turn(
        *,
        container,
        prompt,
        session_id,
        resume=False,
        timeout,
        tool="claude",
        model=None,
        effort=None,
    ):
        wt = state["worktree"]
        state["tools"].append(tool)
        state["models"].append((container, model))
        state["efforts"].append((container, effort))
        if "-coder" in container:
            # a continuation retry has no round marker; reuse the last round
            if "coder_done" in prompt:
                rnd = _round_from(prompt, "coder_done")
                state["last_coder_round"] = rnd
            else:
                rnd = state.get("last_coder_round", 1)
            state["coder_calls"].append((rnd, resume))
            state["coder_prompts"].append(prompt)
            if coder_results is not None:
                idx = len(state["coder_calls"]) - 1
                action = coder_results[min(idx, len(coder_results) - 1)]
                if action == "limit":
                    if coder_transcript_on_limit:
                        pdir = config.coder_config_dir / "projects" / "-workspace"
                        pdir.mkdir(parents=True, exist_ok=True)
                        (pdir / f"{session_id}.jsonl").write_text("{}\n")
                    return _limit_turn(session_id)
            if write_source and (source_rounds is None or rnd in source_rounds):
                (wt / "greeting.txt").write_text(f"hello round {rnd}\n")
            # #114 salvage: when scripted, the initial coder turn leaves work but
            # no handoff; only the orchestrator's nudge turn writes it.
            is_nudge = "never wrote your handoff" in prompt
            if skip_coder_handoff_until_nudge:
                write_handoff_now = is_nudge
            else:
                write_handoff_now = write_coder_handoff
            if write_handoff_now:
                default = f"## Status: LGTM\n## Summary\nRound {rnd}: did the work.\n"
                text = (coder_handoffs or {}).get(rnd, default)
                (config.handoff_dir / handoff.coder_handoff_name(rnd)).write_text(text)
            # #114: a nudge can write the handoff yet still exit failed — that is
            # NOT a clean recovery, so the round must still fail.
            turn_ok = coder_ok and not (is_nudge and nudge_fails)
            return TurnResult(
                exit_code=0 if turn_ok else 1,
                succeeded=turn_ok,
                session_id=session_id,
                result_text="",
                cost_usd=0.01,
                raw={"is_error": not turn_ok},
                stderr="",
            )
        # reviewer turn — derive WHICH reviewer from the container name
        rev_name = container.split("-review-", 1)[1]
        if "review" in prompt and re.search(r"round_\d+_review", prompt):
            rnd = _round_from(prompt, "review")
            state[f"last_review_round_{rev_name}"] = rnd
        else:  # continuation retry carries no filename marker
            rnd = state.get(f"last_review_round_{rev_name}", 1)
        is_correction = "was not valid" in prompt
        state["review_calls"].append((rnd, resume, is_correction))
        state["review_prompts"].append(prompt)
        entry = _entry(rnd, rev_name)
        attempts = state["review_attempts"].get((rnd, rev_name), 0)
        state["review_attempts"][(rnd, rev_name)] = attempts + 1
        if attempts < entry.get("limit_first", 0):
            return _limit_turn(session_id)
        review_path = config.handoff_dir / handoff.reviewer_handoff_name(rnd, rev_name)
        if is_correction:
            text, ok = entry.get("retry_text"), entry.get("retry_ok", True)
        else:
            text, ok = entry.get("text"), entry.get("ok", True)
        if text is not None:
            review_path.write_text(text)
        return TurnResult(
            exit_code=0 if ok else 1,
            succeeded=ok,
            session_id=session_id,
            result_text="",
            cost_usd=0.02,
            raw={"is_error": not ok},
            stderr="",
        )

    def fake_gate_container(gate_cmd, *, name, command, timeout):
        seq = gates if gates is not None else [True]
        val = seq[min(len(state["gate_calls"]), len(seq) - 1)]
        state["gate_calls"].append(name)
        if isinstance(val, str):  # "error" -> simulated infra failure
            raise RuntimeError("simulated gate infra failure")
        ok = val
        return GateResult(
            command=command,
            exit_code=0 if ok else 1,
            passed=ok,
            output_tail="2 failed, 10 passed" if not ok else "12 passed",
        )

    monkeypatch.setattr(containers, "start_container", fake_start)
    monkeypatch.setattr(
        containers, "stop_container", lambda name: state["stopped"].append(name)
    )
    monkeypatch.setattr(develop_mod, "run_turn", fake_run_turn)
    monkeypatch.setattr(test_gate_mod, "run_gate_container", fake_gate_container)
    monkeypatch.setattr(develop_mod, "_sleep", lambda s: state["sleeps"].append(s))
    return state


@pytest.fixture
def config(tmp_git_repo: Path, tmp_path: Path) -> DevelopConfig:
    cfg_dir = tmp_path / "fake-claude"
    cfg_dir.mkdir()
    return DevelopConfig(
        repo=tmp_git_repo,
        description="Add a greeting file",
        work_dir=tmp_path / "work",
        claude_config_dir=cfg_dir,
    )


def _commit_count_since_base(result) -> int:
    out = subprocess.run(
        ["git", "rev-list", "--count", f"{result.base_sha}..HEAD"],
        cwd=result.worktree,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return int(out or 0)


def test_build_run_cmd_mounts_git_common_dir(config: DevelopConfig) -> None:
    """#109: every agent container gets the linked worktree's shared .git (RO).

    End-to-end lock-in over the real-worktree wiring: guards against the
    ``git_common_dir=`` kwarg silently dropping out of ``_build_run_cmd``.
    """
    from lithos_loom.runner import worktree

    config.worktree_parent.mkdir(parents=True, exist_ok=True)
    wt = worktree.create(
        config.repo, config.base_branch, "t", parent=config.worktree_parent
    )
    _name, cmd = develop_mod._build_run_cmd(
        config,
        agent="coder",
        tool="claude",
        config_dir=config.coder_config_dir,
        wt=wt,
        read_only=False,
    )
    common = worktree.git_common_dir(wt)
    assert f"{common}:{common}:ro" in cmd


# --- happy paths ------------------------------------------------------------


def test_approved_in_round_one_on_lgtm(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    state = _install_fakes(monkeypatch, config, reviews=[{"text": _LGTM}])
    result = develop_mod.develop(config)

    assert result.status == "approved"
    assert result.approved is True and result.succeeded is True
    assert result.rounds == 1
    assert len(result.commits) == 1
    assert result.review is not None and result.review.status == "LGTM"
    # both containers torn down
    assert any("-coder" in n for n in state["stopped"])
    assert any("-review-" in n for n in state["stopped"])
    # only one round of each agent; both started fresh (no resume)
    assert state["coder_calls"] == [(1, False)]
    assert state["review_calls"] == [(1, False, False)]
    # committed file present
    assert (
        subprocess.run(
            ["git", "show", "HEAD:greeting.txt"],
            cwd=result.worktree,
            capture_output=True,
            text=True,
        ).stdout
        == "hello round 1\n"
    )


def test_model_and_effort_threaded_to_agents(
    monkeypatch: pytest.MonkeyPatch, tmp_git_repo: Path, tmp_path: Path
) -> None:
    """#93: each agent's model + reasoning effort reach its run_turn calls."""
    cfg_dir = tmp_path / "fake-claude"
    cfg_dir.mkdir()
    cfg = DevelopConfig(
        repo=tmp_git_repo,
        description="Add a greeting file",
        work_dir=tmp_path / "work",
        claude_config_dir=cfg_dir,
        coder_model="opus",
        coder_effort="xhigh",
        reviewers=(ReviewerSpec(name="code-quality", model="sonnet", effort="high"),),
    )
    state = _install_fakes(monkeypatch, cfg, reviews=[{"text": _LGTM}])
    result = develop_mod.develop(cfg)
    assert result.status == "approved"

    # model + effort threaded per agent through run_turn (per-turn exec flags)
    assert {m for c, m in state["models"] if "-coder" in c} == {"opus"}
    assert {e for c, e in state["efforts"] if "-coder" in c} == {"xhigh"}
    assert {m for c, m in state["models"] if "-review-" in c} == {"sonnet"}
    assert {e for c, e in state["efforts"] if "-review-" in c} == {"high"}


def test_below_threshold_findings_pass_immediately(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    _install_fakes(monkeypatch, config, reviews=[{"text": _FINDINGS_MINOR}])
    result = develop_mod.develop(config)
    assert result.status == "approved"  # minor < major threshold
    assert result.rounds == 1
    assert result.review is not None and result.review.max_severity == "minor"
    assert result.review.passed is True


def test_findings_then_fix_then_approved(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    state = _install_fakes(
        monkeypatch,
        config,
        reviews=[{"text": _FINDINGS_MAJOR}, {"text": _LGTM}],
    )
    result = develop_mod.develop(config)

    assert result.status == "approved"
    assert result.rounds == 2
    assert len(result.commits) == 2  # a commit per round (distinct content)
    # round 2 resumed BOTH sessions — the headline session-persistence proof
    assert state["coder_calls"] == [(1, False), (2, True)]
    assert state["review_calls"] == [(1, False, False), (2, True, False)]
    assert result.review is not None and result.review.status == "LGTM"


# --- bounded termination ----------------------------------------------------


def test_max_rounds_stops_unapproved(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    cfg = DevelopConfig(
        repo=config.repo,
        description=config.description,
        work_dir=config.work_dir,
        claude_config_dir=config.claude_config_dir,
        max_rounds=2,
    )
    _install_fakes(
        monkeypatch,
        cfg,
        reviews=[{"text": _FINDINGS_MAJOR}, {"text": _FINDINGS_KEEP_F001}],
    )
    result = develop_mod.develop(cfg)

    assert result.status == "max_rounds"
    assert result.succeeded is False
    assert result.rounds == 2
    assert len(result.commits) == 2
    assert result.review is not None and result.review.status == "FINDINGS"
    assert result.review.passed is False
    assert "max_rounds" in result.message


def test_state_json_failure_reason_none_for_max_rounds(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    # #188/#192 review: a max_rounds run DID run rounds, so the offline `attach`
    # summary must NOT show the "no rounds ran" sentinel. max_rounds describes
    # itself, so state.json records no failure_reason for it (only the genuine
    # failure statuses carry one).
    cfg = DevelopConfig(
        repo=config.repo,
        description=config.description,
        work_dir=config.work_dir,
        claude_config_dir=config.claude_config_dir,
        max_rounds=2,
    )
    _install_fakes(
        monkeypatch,
        cfg,
        reviews=[{"text": _FINDINGS_MAJOR}, {"text": _FINDINGS_KEEP_F001}],
    )
    result = develop_mod.develop(cfg)
    assert result.status == "max_rounds" and result.rounds == 2  # rounds ran
    data = json.loads((cfg.run_dir / "state.json").read_text())
    assert data["failure_reason"] is None  # not the stale "no rounds ran"


# --- malformed / failed review handling -------------------------------------


def test_malformed_review_is_reprompted_and_recovers(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    state = _install_fakes(
        monkeypatch, config, reviews=[{"text": _GARBAGE, "retry_text": _LGTM}]
    )
    result = develop_mod.develop(config)
    assert result.status == "approved"  # the re-prompt fixed it
    # the correction was a resumed turn on the same reviewer session
    assert state["review_calls"] == [(1, False, False), (1, True, True)]


def test_review_invalid_when_never_well_formed(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    _install_fakes(
        monkeypatch, config, reviews=[{"text": _GARBAGE, "retry_text": _GARBAGE}]
    )
    result = develop_mod.develop(config)
    assert result.status == "failed"
    assert result.review is not None and result.review.status == "invalid"
    assert result.review.passed is False


def test_review_invalid_when_turn_fails_even_with_parseable_file(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    # A failed reviewer turn that left a *valid* handoff must NOT be accepted.
    _install_fakes(monkeypatch, config, reviews=[{"text": _LGTM, "ok": False}])
    result = develop_mod.develop(config)
    assert result.status == "failed"
    assert result.review is not None and result.review.status == "invalid"


def test_review_invalid_when_retry_turn_fails(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    _install_fakes(
        monkeypatch,
        config,
        reviews=[{"text": _GARBAGE, "retry_text": _LGTM, "retry_ok": False}],
    )
    result = develop_mod.develop(config)
    assert result.status == "failed"
    assert result.review is not None and result.review.status == "invalid"


# --- coder failure modes (no review, no commit) -----------------------------


def test_failed_when_coder_turn_fails(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    state = _install_fakes(monkeypatch, config, coder_ok=False)
    result = develop_mod.develop(config)
    assert result.status == "failed"
    assert result.review is None  # never got to review
    assert state["review_calls"] == []
    assert result.commits == []
    assert _commit_count_since_base(result) == 0


def test_failed_when_coder_makes_no_commit(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    _install_fakes(monkeypatch, config, write_source=False)
    result = develop_mod.develop(config)
    assert result.status == "failed"
    assert result.review is None
    assert _commit_count_since_base(result) == 0


# --- coder handoff salvage (#114) -------------------------------------------


def test_coder_reprompted_to_write_handoff_recovers(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    """#114: a clean coder turn that left work but no handoff is nudged once;
    the nudge writes the handoff and the round proceeds to review + approval."""
    state = _install_fakes(
        monkeypatch,
        config,
        write_source=True,
        skip_coder_handoff_until_nudge=True,
        reviews=[{"text": _LGTM}],
    )
    result = develop_mod.develop(config)

    assert result.status == "approved"
    assert result.rounds == 1
    # initial coder turn + the resume nudge, both for round 1
    assert state["coder_calls"] == [(1, False), (1, True)]
    assert "never wrote your handoff" in state["coder_prompts"][1]
    # the salvaged work was committed and the reviewer ran
    assert _commit_count_since_base(result) == 1
    assert state["review_calls"]


def test_coder_no_handoff_and_no_changes_fails_without_reprompt(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    """#114: with no uncommitted work there is nothing to salvage, so the coder
    is NOT re-prompted — the round fails on the first (only) turn."""
    state = _install_fakes(
        monkeypatch,
        config,
        write_source=False,
        write_coder_handoff=False,
    )
    result = develop_mod.develop(config)

    assert result.status == "failed"
    assert "no coder handoff file" in result.message
    assert state["coder_calls"] == [(1, False)]  # no wasted nudge turn
    assert state["review_calls"] == []


def test_coder_reprompt_still_no_handoff_fails(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    """#114: the salvage nudge is one-shot — if the coder still writes no
    handoff after the nudge, the round fails as before (after exactly one
    re-prompt)."""
    state = _install_fakes(
        monkeypatch,
        config,
        write_source=True,
        write_coder_handoff=False,  # never writes, even on the nudge
    )
    result = develop_mod.develop(config)

    assert result.status == "failed"
    assert "no coder handoff file" in result.message
    assert state["coder_calls"] == [(1, False), (1, True)]  # nudged exactly once
    assert state["review_calls"] == []


def test_coder_nudge_writes_handoff_but_fails_does_not_recover(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    """#114: recovery is judged on the NUDGE's own outcome. A nudge that writes
    the handoff but then exits failed is not a clean recovery — the round fails
    (it does not proceed to commit/gate/review on a failed turn)."""
    state = _install_fakes(
        monkeypatch,
        config,
        write_source=True,
        skip_coder_handoff_until_nudge=True,  # the nudge writes the handoff...
        nudge_fails=True,  # ...but the nudge turn exits failed
    )
    result = develop_mod.develop(config)

    assert result.status == "failed"
    assert "coder turn failed" in result.message
    assert state["coder_calls"] == [(1, False), (1, True)]
    assert state["review_calls"] == []
    assert _commit_count_since_base(result) == 0


# --- artifacts --------------------------------------------------------------


def test_conversation_log_written_per_round(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    _install_fakes(
        monkeypatch,
        config,
        reviews=[{"text": _FINDINGS_MAJOR}, {"text": _LGTM}],
    )
    result = develop_mod.develop(config)
    assert result.conversation_log is not None
    log = result.conversation_log.read_text()
    assert "## Round 1" in log and "## Round 2" in log
    # both the coder's and the reviewer's handoffs are inlined
    assert "Coder" in log and f"Reviewer [{config.reviewer}]" in log
    # handoff bodies are blockquoted so their own "## Status" headings don't
    # become siblings of the log's "## Round N" structure
    assert "> ## Status:" in log
    assert "\n## Status:" not in log


# --- test gate (T4) ---------------------------------------------------------


def _gated_config(config: DevelopConfig, **overrides) -> DevelopConfig:
    from dataclasses import replace

    return replace(config, test_command="fake-tests", **overrides)


def test_gate_skipped_when_no_command_detected(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    # fixture repo has no Makefile/pytest markers -> detection finds nothing
    state = _install_fakes(monkeypatch, config)
    result = develop_mod.develop(config)
    assert result.status == "approved"
    assert result.test_gate is None
    assert state["gate_calls"] == []


def test_gate_green_recorded_on_approval(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    cfg = _gated_config(config)
    state = _install_fakes(monkeypatch, cfg, gates=[True])
    result = develop_mod.develop(cfg)
    assert result.status == "approved"
    assert result.test_gate is not None and result.test_gate.passed
    assert result.test_gate.command == "fake-tests"
    assert "test gate GREEN" in result.message
    assert len(state["gate_calls"]) == 1
    # the gate output artifact is preserved per round
    assert (cfg.gate_dir / "round_01" / "output.txt").is_file()


def test_gate_red_blocking_loops_and_feeds_coder(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    # #140: the default `standard` profile makes `test` required, so a RED test gate
    # blocks approval with no `block_on_red` knob (removed).
    cfg = _gated_config(config)
    state = _install_fakes(monkeypatch, cfg, gates=[False, True])
    result = develop_mod.develop(cfg)

    # round 1: review LGTM but gate RED -> blocked; round 2: gate GREEN -> approved
    assert result.status == "approved"
    assert result.rounds == 2
    assert len(state["gate_calls"]) == 2
    # the round-2 coder prompt carried the gate failure + its output tail
    r2_prompt = state["coder_prompts"][1]
    assert "Independent test gate (FAILED)" in r2_prompt
    assert "2 failed, 10 passed" in r2_prompt
    assert result.test_gate is not None and result.test_gate.passed


def test_gate_red_blocking_exhausts_rounds(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    cfg = _gated_config(config, max_rounds=2)
    _install_fakes(monkeypatch, cfg, gates=[False])
    result = develop_mod.develop(cfg)
    assert result.status == "max_rounds"
    assert result.succeeded is False
    assert "test gate RED" in result.message


def test_gate_not_rerun_without_new_commit(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    # round 1 commits (gate runs); round 2 the coder only disputes (no commit,
    # no new tree) -> the gate must not re-run.
    cfg = _gated_config(config)
    state = _install_fakes(
        monkeypatch,
        cfg,
        reviews=[{"text": _FINDINGS_MAJOR}, {"text": _LGTM}],
        source_rounds={1},
        gates=[True],
    )
    result = develop_mod.develop(cfg)
    assert result.status == "approved"
    assert result.rounds == 2
    assert len(state["gate_calls"]) == 1  # only the round-1 commit was gated


def test_gate_infra_error_clears_stale_red(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    # Round 1: gate RED (required test floor) -> blocked despite LGTM review.
    # Round 2: NEW commit but the gate errors (infra) -> the stale round-1 RED
    # must NOT stand in for this commit; with no gate result the review's pass
    # approves the run (the gate is an independent check, not a dependency).
    cfg = _gated_config(config)
    state = _install_fakes(monkeypatch, cfg, gates=[False, "error"])
    result = develop_mod.develop(cfg)

    assert result.status == "approved"
    assert result.rounds == 2
    assert len(state["gate_calls"]) == 2  # round 2 did attempt its own gate
    assert result.test_gate is None  # no result for the approved commit
    assert "test gate" not in result.message  # no stale verdict reported


def test_gate_disabled_by_config(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    from dataclasses import replace

    cfg = replace(config, test_command="fake-tests", test_gate=False)
    state = _install_fakes(monkeypatch, cfg, gates=[True])
    result = develop_mod.develop(cfg)
    assert result.test_gate is None
    assert state["gate_calls"] == []


def test_auto_format_pass_commits_separately_before_review(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    """#134/ADR §4: after the coder's commit, the formatter runs in the sandbox and
    its change lands as a SEPARATE commit, so the gate + reviewers see the formatted
    tree. Here the fake formatter rewrites the coder's file; the run must end with two
    commits (coder + auto-format), the latter holding the reformatted content."""
    from lithos_loom.plugins.story_develop import autoformat as autoformat_mod

    monkeypatch.setattr(
        autoformat_mod, "resolve_formatters", lambda config, wt: ["ruff format"]
    )
    _install_fakes(monkeypatch, config, reviews=[{"text": _LGTM}])

    def fake_formatter(gate_cmd, *, name, command, timeout):
        # A real formatter rewrites the ISOLATED export (the /workspace mount), not the
        # live worktree; the host applies a successful run's result back.
        for i, arg in enumerate(gate_cmd):
            if arg == "-v":
                host, _, mount = gate_cmd[i + 1].rpartition(":")
                if mount == "/workspace":
                    (Path(host) / "greeting.txt").write_text("HELLO ROUND 1\n")
        return GateResult(command=command, exit_code=0, passed=True, output_tail="")

    monkeypatch.setattr(test_gate_mod, "run_gate_container", fake_formatter)

    result = develop_mod.develop(config)

    assert result.status == "approved"
    # Two commits on the branch: the coder's round commit, then the auto-format commit.
    assert _commit_count_since_base(result) == 2
    subj = subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        cwd=result.worktree,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert "auto-format" in subj
    assert (Path(result.worktree) / "greeting.txt").read_text() == "HELLO ROUND 1\n"


def test_auto_format_pass_noop_leaves_single_commit(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    """A markerless fixture repo resolves no formatters, so the pass never runs a
    container and the round keeps exactly the coder's single commit."""
    state = _install_fakes(monkeypatch, config, reviews=[{"text": _LGTM}])

    def _boom(*a, **k):
        raise AssertionError("no formatter should run for a markerless repo")

    monkeypatch.setattr(test_gate_mod, "run_gate_container", _boom)

    result = develop_mod.develop(config)

    assert result.status == "approved"
    assert _commit_count_since_base(result) == 1
    assert state["gate_calls"] == []


def test_candidate_checks_run_only_on_the_approval_candidate(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    """#140/ADR §4: fast checks run every round for tight coder feedback; the
    expensive candidate-staged checks run only on the approval candidate (the
    round that would otherwise pass), not on every churning commit."""
    from lithos_loom.plugins.story_develop.check_set import (
        Check,
        CheckResult,
        CheckSetResult,
    )

    fast = Check("lint", "ruff check --x", "informational", "fast")
    candidate = Check("dep-audit", "pip-audit --x", "informational", "candidate")
    monkeypatch.setattr(
        develop_mod, "build_check_set", lambda config, wt: (fast, candidate)
    )
    calls: list[tuple[int, tuple[str, ...]]] = []

    def fake_run_check_set(config, wt, sha, round_no, checks, gate_ledger=None):
        calls.append((round_no, tuple(c.name for c in checks)))
        return CheckSetResult(
            tuple(
                CheckResult(
                    c,
                    "ran",
                    GateResult(
                        command=c.command, exit_code=0, passed=True, output_tail="ok"
                    ),
                )
                for c in checks
            )
        )

    monkeypatch.setattr(develop_mod, "_run_check_set", fake_run_check_set)
    # Round 1 FINDINGS (not approved) then round 2 LGTM (approved): proves the
    # fast gate runs both rounds while the candidate gate fires only at approval.
    _install_fakes(
        monkeypatch, config, reviews=[{"text": _FINDINGS_MAJOR}, {"text": _LGTM}]
    )
    result = develop_mod.develop(config)
    assert result.status == "approved"
    assert result.rounds == 2
    fast_rounds = sorted(r for r, names in calls if names == ("lint",))
    candidate_calls = [(r, names) for r, names in calls if names == ("dep-audit",)]
    assert fast_rounds == [1, 2]  # every round
    assert candidate_calls == [(2, ("dep-audit",))]  # approval candidate only


def test_required_adapter_red_exit_without_findings_blocks_as_failed_run(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    """#167 floor-liveness: a *required* adapter check (ruff `lint`) that exits RED
    with NO ledger finding FAILED TO RUN — adapters use `--exit-zero`, so a red exit
    is a spawn/crash, not findings, and must BLOCK, not pass on the empty ledger. (A
    red exit *with* a finding still defers to severity: the tool ran.) With no commit
    that can fix an unrunnable tool, the run stalls."""
    from lithos_loom.plugins.story_develop.check_set import (
        Check,
        CheckResult,
        CheckSetResult,
    )

    lint = Check("lint", "ruff check --x", "required", "fast")
    monkeypatch.setattr(develop_mod, "build_check_set", lambda config, wt: (lint,))

    def fake_run_check_set(config, wt, sha, round_no, checks, gate_ledger=None):
        # RED exit with an EMPTY ledger: the adapter failed to run, so the floor blocks
        # via the liveness rule (#167), not the severity read.
        return CheckSetResult(
            tuple(
                CheckResult(
                    c,
                    "ran",
                    GateResult(
                        command=c.command, exit_code=1, passed=False, output_tail="x"
                    ),
                )
                for c in checks
            )
        )

    monkeypatch.setattr(develop_mod, "_run_check_set", fake_run_check_set)
    _install_fakes(monkeypatch, config, reviews=[{"text": _LGTM}], source_rounds={1})
    result = develop_mod.develop(config)
    assert result.status == "stalled"
    assert result.succeeded is False


def test_required_candidate_red_exit_blocks_approval(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    """#140 floor: a *required* no-adapter candidate check (e.g. `coverage`) that runs
    RED on the approval candidate blocks approval even when reviewers LGTM. With no new
    commit to fix it, the run stalls rather than sealing approval over a blocking
    floor — the candidate stage gains teeth (previously informational-only)."""
    from lithos_loom.plugins.story_develop.check_set import (
        Check,
        CheckResult,
        CheckSetResult,
    )

    fast = Check("lint", "ruff check --x", "required", "fast")
    candidate = Check("coverage", "coverage report", "required", "candidate")
    monkeypatch.setattr(
        develop_mod, "build_check_set", lambda config, wt: (fast, candidate)
    )

    def fake_run_check_set(config, wt, sha, round_no, checks, gate_ledger=None):
        return CheckSetResult(
            tuple(
                CheckResult(
                    c,
                    "ran",
                    GateResult(
                        command=c.command,
                        exit_code=0 if c.name != "coverage" else 1,
                        passed=c.name != "coverage",
                        output_tail="x",
                    ),
                )
                for c in checks
            )
        )

    monkeypatch.setattr(develop_mod, "_run_check_set", fake_run_check_set)
    # LGTM from round 1, but the coder commits only in round 1 -> the blocking required
    # candidate can never be fixed -> the stall guard terminates the run.
    _install_fakes(monkeypatch, config, reviews=[{"text": _LGTM}], source_rounds={1})
    result = develop_mod.develop(config)
    assert result.status == "stalled"
    assert result.succeeded is False


# --- termination guards (T7) ---------------------------------------------------

_CODER_DISPUTE = (
    "## Status: LGTM\n## Summary\nI disagree with f-001; see dispute.\n"
    "## Findings\n- finding_id: f-001\n  severity: major\n  status: disputed\n"
    "  coder_response: the current behaviour is intentional and documented\n"
)
_UNKNOWN_ID_FINDINGS = (
    "## Status: FINDINGS\n## Summary\nIssue.\n## Findings\n"
    "- finding_id: f-999\n  severity: major\n  status: open\n  rationale: x\n"
)


def test_stall_guard_stops_on_unchanged_blocking_findings(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    # Coder commits every round, but the reviewer keeps the SAME finding open
    # by id: rounds 2 and 3 have identical blocking signatures -> stalled.
    _install_fakes(
        monkeypatch,
        config,
        reviews=[{"text": _FINDINGS_MAJOR}, {"text": _FINDINGS_KEEP_F001}],
    )
    result = develop_mod.develop(config)
    assert result.status == "stalled"
    assert result.rounds == 3  # strikes at r2 and r3
    assert "stalled" in result.message
    assert result.succeeded is False
    # stalled is reason-bearing: the reason reaches state.json.
    reason = json.loads((config.run_dir / "state.json").read_text())["failure_reason"]
    assert reason and "stalled" in reason


def test_stall_guard_stops_on_empty_round_commits(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    # The coder stops changing code after round 1 while findings stay open:
    # two commit-less rounds -> stalled, even though statuses keep moving.
    _install_fakes(
        monkeypatch,
        config,
        reviews=[{"text": _FINDINGS_MAJOR}, {"text": _FINDINGS_KEEP_F001}],
        source_rounds={1},
    )
    result = develop_mod.develop(config)
    assert result.status == "stalled"
    assert result.rounds == 3


def test_stall_guard_resets_on_progress(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    # r2: same signature (strike); r3: reviewer accepts f-001 but raises a NEW
    # finding (signature changes -> strike resets); r4: LGTM -> approved.
    fix_then_new = (
        "## Status: FINDINGS\n## Summary\nOld fixed; new issue.\n## Findings\n"
        "- finding_id: f-001\n  severity: major\n  status: fixed\n"
        "- finding_id:\n  severity: major\n  status: open\n  rationale: new\n"
    )
    _install_fakes(
        monkeypatch,
        config,
        reviews=[
            {"text": _FINDINGS_MAJOR},
            {"text": _FINDINGS_KEEP_F001},
            {"text": fix_then_new},
            {"text": _LGTM},
        ],
    )
    result = develop_mod.develop(config)
    assert result.status == "approved"
    assert result.rounds == 4


def test_dispute_deadlock_stops_with_breadcrumb(
    monkeypatch: pytest.MonkeyPatch,
    config: DevelopConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # r1: finding raised. r2: coder formally disputes; reviewer keeps blocking
    # (disputed-block #1). r3: reviewer blocks again (#2) -> dispute deadlock.
    _install_fakes(
        monkeypatch,
        config,
        reviews=[{"text": _FINDINGS_MAJOR}, {"text": _FINDINGS_KEEP_F001}],
        coder_handoffs={2: _CODER_DISPUTE, 3: _CODER_DISPUTE},
    )
    with caplog.at_level("WARNING"):
        result = develop_mod.develop(config)
    assert result.status == "disputed"
    assert result.rounds == 3
    assert "dispute deadlock" in result.message and "f-001" in result.message
    assert any("[ReviewDispute]" in r.message for r in caplog.records)
    # disputed is reason-bearing: the breadcrumb reaches state.json.
    reason = json.loads((config.run_dir / "state.json").read_text())["failure_reason"]
    assert reason and "dispute deadlock" in reason


def test_cost_ceiling_stops_run(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    from dataclasses import replace

    # fake costs: coder 0.01/turn, reviewer 0.02/turn -> r1 total 0.03
    cfg = replace(config, max_cost_usd=0.025)
    _install_fakes(monkeypatch, cfg, reviews=[{"text": _FINDINGS_MAJOR}])
    result = develop_mod.develop(cfg)
    assert result.status == "cost_exceeded"
    assert result.rounds == 1
    assert "cost ceiling" in result.message
    # cost_exceeded is reason-bearing: the exact POST-review (J) reason reaches
    # state.json. Same template as the PRE-review (D) site — see
    # test_cost_ceiling_stops_before_reviews_when_coder_exceeds.
    reason = json.loads((cfg.run_dir / "state.json").read_text())["failure_reason"]
    assert re.fullmatch(
        r"round \d+: cost ceiling reached \(\$\d+\.\d\d >= \$\d+\.\d\d\)", reason
    )


def test_approval_beats_cost_ceiling_in_the_same_round(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    from dataclasses import replace

    # The round's reviews push spend past the ceiling AND all pass: approval
    # wins. The ceiling stops FURTHER spend on unfinished work; the spend has
    # already happened, and discarding a finished approved branch protects
    # nothing. (Deliberate precedence — see the develop() comment.)
    cfg = replace(config, max_cost_usd=0.025)  # coder 0.01 + review 0.02 = 0.03
    _install_fakes(monkeypatch, cfg, reviews=[{"text": _LGTM}])
    result = develop_mod.develop(cfg)
    assert result.status == "approved"
    assert result.total_cost_usd >= 0.025


def test_cost_ceiling_stops_before_reviews_when_coder_exceeds(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    from dataclasses import replace

    # The coder phase alone crosses the ceiling -> stop BEFORE spending on
    # reviews (no reviewer calls at all).
    cfg = replace(config, max_cost_usd=0.01)
    state = _install_fakes(monkeypatch, cfg, reviews=[{"text": _LGTM}])
    result = develop_mod.develop(cfg)
    assert result.status == "cost_exceeded"
    assert state["review_calls"] == []
    # Pin the PRE-review (D) reason text; it shares the exact template with the
    # POST-review (J) site (test_cost_ceiling_stops_run), so the round-pipeline
    # refactor cannot diverge the two cost-ceiling messages.
    reason = json.loads((cfg.run_dir / "state.json").read_text())["failure_reason"]
    assert re.fullmatch(
        r"round \d+: cost ceiling reached \(\$\d+\.\d\d >= \$\d+\.\d\d\)", reason
    )


def test_lifecycle_unknown_id_is_reprompted_and_recovers(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    # The reviewer invents an id -> lifecycle validation rejects -> the same
    # session is re-prompted with the correction and writes a valid review.
    state = _install_fakes(
        monkeypatch,
        config,
        reviews=[{"text": _UNKNOWN_ID_FINDINGS, "retry_text": _LGTM}],
    )
    result = develop_mod.develop(config)
    assert result.status == "approved"
    corrections = [c for (_, _, c) in state["review_calls"] if c]
    assert len(corrections) == 1  # exactly one correction re-prompt


def test_lifecycle_dropped_id_never_fixed_fails(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    # r2 review drops the open f-001 (new blank-id finding instead) and the
    # correction retry repeats the mistake -> invalid -> failed.
    _install_fakes(
        monkeypatch,
        config,
        reviews=[
            {"text": _FINDINGS_MAJOR},
            {"text": _FINDINGS_MAJOR, "retry_text": _FINDINGS_MAJOR},
        ],
    )
    result = develop_mod.develop(config)
    assert result.status == "failed"
    assert "invalid" in result.message


def test_rereview_prompt_carries_open_findings_ledger(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    state = _install_fakes(
        monkeypatch,
        config,
        reviews=[{"text": _FINDINGS_MAJOR}, {"text": _LGTM}],
    )
    result = develop_mod.develop(config)
    assert result.status == "approved"
    r2_prompt = state["review_prompts"][1]
    assert "Your open findings" in r2_prompt
    assert "finding_id: f-001" in r2_prompt  # the ledger-assigned id


def test_develop_rejects_bad_max_cost(tmp_git_repo: Path, tmp_path: Path) -> None:
    from dataclasses import replace

    base = DevelopConfig(
        repo=tmp_git_repo,
        description="x",
        work_dir=tmp_path / "work",
        claude_config_dir=tmp_path / "fake-claude",
    )
    with pytest.raises(ValueError, match="max_cost_usd"):
        develop_mod.develop(replace(base, max_cost_usd=0))


# --- multi-reviewer panel (T6) ------------------------------------------------


def _panel_config(config: DevelopConfig, *specs) -> DevelopConfig:
    from dataclasses import replace

    return replace(config, reviewers=tuple(specs))


def test_panel_both_lgtm_approved_round_one(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    from lithos_loom.plugins.story_develop.config import ReviewerSpec

    cfg = _panel_config(
        config,
        ReviewerSpec(name="code-quality", block_threshold="major"),
        ReviewerSpec(
            name="security",
            block_threshold="minor",
            system_prompt="Hunt for injection, authz and secret handling issues.",
        ),
    )
    state = _install_fakes(
        monkeypatch,
        cfg,
        reviews={"code-quality": [{"text": _LGTM}], "security": [{"text": _LGTM}]},
    )
    result = develop_mod.develop(cfg)

    assert result.status == "approved"
    assert result.rounds == 1
    assert [r.reviewer for r in result.reviews] == ["code-quality", "security"]
    assert all(r.passed for r in result.reviews)
    assert state["starts"] == 3  # coder + 2 reviewer containers
    # each reviewer wrote its own handoff file
    assert (cfg.handoff_dir / "round_01_review_code-quality.md").is_file()
    assert (cfg.handoff_dir / "round_01_review_security.md").is_file()
    # the security reviewer's prompt carried its focus brief
    security_prompt = state["review_prompts"][1]
    assert "Your focus" in security_prompt and "injection" in security_prompt
    # conversation log includes both panel members
    assert result.conversation_log is not None
    log = result.conversation_log.read_text()
    assert "Reviewer [code-quality]" in log and "Reviewer [security]" in log


def test_panel_mixed_claude_and_codex_reviewers(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    """#94: a heterogeneous panel (codex + claude reviewers) — each reviewer's
    container is built for its own tool, and the tool is threaded to each turn.
    """
    from lithos_loom.plugins.story_develop.config import ReviewerSpec

    cfg = _panel_config(
        config,
        ReviewerSpec(name="code-quality", tool="codex"),
        ReviewerSpec(name="security", tool="claude"),
    )
    state = _install_fakes(
        monkeypatch,
        cfg,
        reviews={"code-quality": [{"text": _LGTM}], "security": [{"text": _LGTM}]},
    )
    result = develop_mod.develop(cfg)

    assert result.status == "approved"
    assert state["starts"] == 3  # coder + 2 reviewers
    # both tools reached the exec layer (coder=claude + the two reviewers)
    assert "codex" in state["tools"] and "claude" in state["tools"]
    # each reviewer container was built for its own tool
    cq_cmd = next(
        c for c in state["start_cmds"] if "review-code-quality" in " ".join(c)
    )
    sec_cmd = next(c for c in state["start_cmds"] if "review-security" in " ".join(c))
    assert "CODEX_HOME=/codex_home" in cq_cmd
    assert "CLAUDE_CONFIG_DIR=/claude_config" in sec_cmd
    # state.json records both reviewers
    data = json.loads((cfg.run_dir / "state.json").read_text())
    assert set(data["reviewers"]) == {"code-quality", "security"}


def test_panel_per_reviewer_thresholds(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    from lithos_loom.plugins.story_develop.config import ReviewerSpec

    # The SAME minor finding: code-quality (threshold major) passes;
    # security (threshold minor) blocks -> round 2 needed.
    cfg = _panel_config(
        config,
        ReviewerSpec(name="code-quality", block_threshold="major"),
        ReviewerSpec(name="security", block_threshold="minor"),
    )
    state = _install_fakes(
        monkeypatch,
        cfg,
        reviews={
            "code-quality": [{"text": _FINDINGS_MINOR}, {"text": _LGTM}],
            "security": [{"text": _FINDINGS_MINOR}, {"text": _LGTM}],
        },
    )
    result = develop_mod.develop(cfg)

    assert result.status == "approved"
    assert result.rounds == 2
    # round 1 verdicts: same severity, different thresholds
    # (final_reviews only holds round 2; check via the coder's fix prompt)
    fix_prompt = state["coder_prompts"][1]
    # consolidated findings, labelled per reviewer, ids qualified
    assert "From the code-quality reviewer" in fix_prompt
    assert "From the security reviewer" in fix_prompt
    assert "[security/f-001]" in fix_prompt


def test_panel_approval_requires_all_pass_same_round(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    from lithos_loom.plugins.story_develop.config import ReviewerSpec

    # cq blocks r1 / passes r2+; security passes r1, blocks r2, passes r3.
    # Approval must only land in round 3 when BOTH pass together.
    cfg = _panel_config(
        config,
        ReviewerSpec(name="cq", block_threshold="major"),
        ReviewerSpec(name="security", block_threshold="minor"),
    )
    _install_fakes(
        monkeypatch,
        cfg,
        reviews={
            "cq": [{"text": _FINDINGS_MAJOR}, {"text": _LGTM}, {"text": _LGTM}],
            "security": [{"text": _LGTM}, {"text": _FINDINGS_MINOR}, {"text": _LGTM}],
        },
    )
    result = develop_mod.develop(cfg)
    assert result.status == "approved"
    assert result.rounds == 3


def test_panel_invalid_reviewer_fails_run(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    from lithos_loom.plugins.story_develop.config import ReviewerSpec

    cfg = _panel_config(
        config,
        ReviewerSpec(name="cq"),
        ReviewerSpec(name="security"),
    )
    _install_fakes(
        monkeypatch,
        cfg,
        reviews={
            "cq": [{"text": _LGTM}],
            "security": [{"text": _GARBAGE, "retry_text": _GARBAGE}],
        },
    )
    result = develop_mod.develop(cfg)
    assert result.status == "failed"
    assert "security" in result.message and "invalid" in result.message


def test_panel_duplicate_names_rejected(tmp_git_repo: Path, tmp_path: Path) -> None:
    from lithos_loom.plugins.story_develop.config import ReviewerSpec

    cfg = DevelopConfig(
        repo=tmp_git_repo,
        description="x",
        work_dir=tmp_path / "work",
        reviewers=(ReviewerSpec(name="cq"), ReviewerSpec(name="cq")),
        claude_config_dir=tmp_path / "fake-claude",
    )
    with pytest.raises(ValueError, match="duplicate reviewer names"):
        develop_mod.develop(cfg)


# --- usage limits (T5) -------------------------------------------------------


def test_coder_limit_pauses_and_retries_fresh(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    # No transcript survived -> the retry re-issues the ORIGINAL prompt fresh.
    state = _install_fakes(monkeypatch, config, coder_results=["limit", "ok"])
    result = develop_mod.develop(config)
    assert result.status == "approved"
    assert state["sleeps"] == [300]  # one poll-interval pause (5 min default)
    assert [r for r, _ in state["coder_calls"]] == [1, 1]  # same round, twice
    assert state["coder_calls"][1][1] is False  # fresh, not resumed
    assert "coder_done" in state["coder_prompts"][1]  # original prompt re-sent
    # the failed turn was captured as a G4 fixture
    fixture = config.failures_dir / "round_01_coder.json"
    assert "usage limit" in fixture.read_text()


def test_coder_limit_resumes_when_transcript_survived(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    state = _install_fakes(
        monkeypatch,
        config,
        coder_results=["limit", "ok"],
        coder_transcript_on_limit=True,
    )
    result = develop_mod.develop(config)
    assert result.status == "approved"
    assert state["coder_calls"][1][1] is True  # resumed the SAME session
    assert "interrupted by a provider usage limit" in state["coder_prompts"][1]


def test_coder_limit_budget_exhausted_interrupts(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    from dataclasses import replace

    cfg = replace(config, max_pause_minutes=0)
    state = _install_fakes(monkeypatch, cfg, coder_results=["limit"])
    result = develop_mod.develop(cfg)
    assert result.status == "interrupted"
    assert result.succeeded is False
    assert "INTERRUPTED" in result.message and "usage-limited" in result.message
    assert state["sleeps"] == []  # zero budget -> no pointless wait
    state_file = json.loads((cfg.run_dir / "state.json").read_text())
    assert state_file["status"] == "interrupted"
    assert state_file["coder_session"]  # resume handle preserved
    # interrupted is reason-bearing: the "why" reaches state.json (the offline
    # `attach` summary), not just the status.
    assert (
        state_file["failure_reason"] and "usage-limited" in state_file["failure_reason"]
    )


def test_reviewer_limit_pauses_when_no_fallback(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    state = _install_fakes(
        monkeypatch, config, reviews=[{"text": _LGTM, "limit_first": 1}]
    )
    result = develop_mod.develop(config)
    assert result.status == "approved"
    assert state["sleeps"] == [300]
    assert len(state["review_calls"]) == 2  # limited attempt + retry
    assert state["starts"] == 2  # no container replacement happened


def test_reviewer_limit_switches_to_fallback_tool(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    from dataclasses import replace

    # codex is natively supported (#94) — no _tool_supported monkeypatch needed.
    cfg = replace(config, reviewer_fallback_chain=("codex",))
    state = _install_fakes(
        monkeypatch, cfg, reviews=[{"text": _LGTM, "limit_first": 1}]
    )
    result = develop_mod.develop(cfg)

    assert result.status == "approved"
    assert state["sleeps"] == []  # switch, not pause
    assert state["starts"] == 3  # coder + reviewer + replacement reviewer
    # the replacement was reseeded: fresh session, takeover prompt with the
    # handoff-history payload
    reseed = state["review_prompts"][1]
    assert "taking over" in reseed
    assert "acceptance criteria" in reseed.lower()
    # the switched tool is actually threaded through to the exec layer
    assert state["tools"][-1] == "codex"
    # the replacement container was REBUILT for codex (CODEX_HOME, not the
    # original claude env) — #94
    assert "CODEX_HOME=/codex_home" in state["start_cmds"][-1]
    assert "CLAUDE_CONFIG_DIR=/claude_config" not in state["start_cmds"][-1]
    state_file = json.loads((cfg.run_dir / "state.json").read_text())
    assert state_file["reviewers"][cfg.reviewer]["tool"] == "codex"


def test_reviewer_limit_skips_unsupported_fallback_and_pauses(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    from dataclasses import replace

    # An unsupported tool is in the chain (claude + codex run; opencode does
    # not, #94) -> fall through to the pause path rather than a broken switch.
    cfg = replace(config, reviewer_fallback_chain=("opencode",))
    state = _install_fakes(
        monkeypatch, cfg, reviews=[{"text": _LGTM, "limit_first": 1}]
    )
    result = develop_mod.develop(cfg)
    assert result.status == "approved"
    assert state["sleeps"] == [300]
    assert state["starts"] == 2  # no replacement container


def test_reviewer_limit_budget_exhausted_interrupts(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    from dataclasses import replace

    cfg = replace(config, max_pause_minutes=0)
    _install_fakes(monkeypatch, cfg, reviews=[{"text": _LGTM, "limit_first": 99}])
    result = develop_mod.develop(cfg)
    assert result.status == "interrupted"
    assert "reviewer usage-limited" in result.message


def test_duplicate_primary_in_fallback_chain_does_not_self_switch(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    from dataclasses import replace

    # --reviewer-fallback claude --reviewer-fallback codex: the duplicated
    # primary must not trap the reviewer in a claude->claude self-switch loop
    # that never reaches codex.
    cfg = replace(config, reviewer_fallback_chain=("claude", "codex"))
    monkeypatch.setattr(develop_mod, "_tool_supported", lambda t: True)
    state = _install_fakes(
        monkeypatch, cfg, reviews=[{"text": _LGTM, "limit_first": 1}]
    )
    result = develop_mod.develop(cfg)

    assert result.status == "approved"
    assert state["starts"] == 3  # exactly ONE replacement, straight to codex
    assert state["tools"][-1] == "codex"
    state_file = json.loads((cfg.run_dir / "state.json").read_text())
    assert state_file["reviewers"][cfg.reviewer]["tool"] == "codex"


def test_develop_rejects_bad_pause_poll(tmp_git_repo: Path, tmp_path: Path) -> None:
    from dataclasses import replace

    base = DevelopConfig(
        repo=tmp_git_repo,
        description="x",
        work_dir=tmp_path / "work",
        claude_config_dir=tmp_path / "fake-claude",
    )
    with pytest.raises(ValueError, match="pause_poll_minutes"):
        develop_mod.develop(replace(base, pause_poll_minutes=0))
    with pytest.raises(ValueError, match="max_pause_minutes"):
        develop_mod.develop(replace(base, max_pause_minutes=-1))


def test_state_json_written_on_success(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    _install_fakes(monkeypatch, config)
    result = develop_mod.develop(config)
    data = json.loads((config.run_dir / "state.json").read_text())
    assert data["status"] == "approved"
    assert data["branch"] == result.branch
    assert data["rounds"] == result.rounds
    # Review-metadata record (ADR 0003 §11): the durable run-state carries the
    # SAME profile + panel + findings-by-severity written to Lithos metadata, so
    # the local record is sufficient for outcome correlation.
    assert data["review_profile"] == config.review_profile
    assert data["review_panel"] == [r.reviewer for r in result.reviews]
    assert data["findings_by_severity"] == develop_mod.findings_by_severity(
        result.reviews
    )
    # #188: an approved run carries no failure reason (the field is for the
    # terminal summary on a non-approved exit).
    assert data["failure_reason"] is None


def test_state_json_records_failure_reason_on_failure(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    # #188: the failure reason `develop()` builds must reach `state.json` so the
    # offline `attach` summary can show *why* a run failed, not just "failed".
    _install_fakes(monkeypatch, config, write_source=False)  # coder makes no commit
    result = develop_mod.develop(config)
    assert result.status == "failed"
    data = json.loads((config.run_dir / "state.json").read_text())
    assert data["failure_reason"] == "round 1: coder produced no commit"


# --- validation -------------------------------------------------------------


def test_develop_rejects_invalid_reviewer_name(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    cfg = DevelopConfig(
        repo=tmp_git_repo,
        description="x",
        work_dir=tmp_path / "work",
        reviewer="code quality",  # space -> invalid container/path/filename
        claude_config_dir=tmp_path / "fake-claude",
    )
    with pytest.raises(ValueError, match="invalid reviewer name"):
        develop_mod.develop(cfg)


def test_develop_rejects_unsupported_coder(tmp_git_repo: Path, tmp_path: Path) -> None:
    # claude + codex are supported (#94); anything else is rejected up front.
    cfg = DevelopConfig(
        repo=tmp_git_repo,
        description="x",
        work_dir=tmp_path / "work",
        coder="opencode",
        claude_config_dir=tmp_path / "fake-claude",
    )
    with pytest.raises(ValueError, match="unsupported coder tool"):
        develop_mod.develop(cfg)


def test_develop_rejects_bad_max_rounds(tmp_git_repo: Path, tmp_path: Path) -> None:
    cfg = DevelopConfig(
        repo=tmp_git_repo,
        description="x",
        work_dir=tmp_path / "work",
        max_rounds=0,
        claude_config_dir=tmp_path / "fake-claude",
    )
    with pytest.raises(ValueError, match="max_rounds"):
        develop_mod.develop(cfg)


# --- resume_after surface (T10) ----------------------------------------------


def test_interrupted_coder_run_carries_resume_after(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    """An interrupted run exposes WHEN to retry — on the result AND in
    state.json (the daemon re-dispatch surface). The fake's epoch hint is in
    the past (= noise), so the fixed fallback delay applies."""
    from dataclasses import replace
    from datetime import UTC, datetime, timedelta

    cfg = replace(config, max_pause_minutes=0)
    _install_fakes(monkeypatch, cfg, coder_results=["limit"])
    before = datetime.now(UTC)
    result = develop_mod.develop(cfg)
    assert result.status == "interrupted"
    assert result.resume_after is not None
    delta = result.resume_after - before
    assert timedelta(minutes=55) < delta < timedelta(minutes=65)
    state_file = json.loads((cfg.run_dir / "state.json").read_text())
    assert state_file["resume_after"] == result.resume_after.isoformat(
        timespec="seconds"
    )


def test_interrupted_reviewer_run_carries_resume_after(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    from dataclasses import replace

    cfg = replace(config, max_pause_minutes=0)
    _install_fakes(monkeypatch, cfg, reviews=[{"text": _LGTM, "limit_first": 99}])
    result = develop_mod.develop(cfg)
    assert result.status == "interrupted"
    assert result.resume_after is not None
    # The panel-path resume_after (1745) reaches state.json too, not only the
    # coder-path one — the daemon re-dispatch surface needs both.
    state_file = json.loads((cfg.run_dir / "state.json").read_text())
    assert state_file["resume_after"] == result.resume_after.isoformat(
        timespec="seconds"
    )


def test_non_interrupted_run_has_no_resume_after(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    _install_fakes(monkeypatch, config)
    result = develop_mod.develop(config)
    assert result.status == "approved"
    assert result.resume_after is None
    state_file = json.loads((config.run_dir / "state.json").read_text())
    assert state_file["resume_after"] is None


def test_resume_after_uses_provider_reset_hint_when_future() -> None:
    """A parseable FUTURE epoch sentinel becomes the resume time verbatim."""
    from datetime import UTC, datetime

    from lithos_loom.plugins.story_develop.develop import _resume_after_from
    from lithos_loom.plugins.story_develop.turns import TurnResult

    future_epoch = int(datetime.now(UTC).timestamp()) + 7200  # +2h
    turn = TurnResult(
        exit_code=1,
        succeeded=False,
        session_id="",
        result_text=f"Claude AI usage limit reached|{future_epoch}",
        cost_usd=0.0,
        raw={"is_error": True},
        stderr="",
    )
    resumed = _resume_after_from(turn)
    assert int(resumed.timestamp()) == future_epoch


# --- ARCH-1.S1: exit-path characterisation net -------------------------------
# Pins the develop() exits the existing suite left uncovered, so the round/phase
# seam refactor (ARCH-1.S6) cannot silently drop one. The cost-ceiling message
# text, the state.json reason-bearing filter, and the reviewer resume_after
# round-trip are pinned inline on their existing terminal tests above.


def test_develop_rejects_unsupported_reviewer_tool(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    # The reviewer-tool validation branch (distinct from the coder-tool one): a
    # reviewer whose engine is neither claude nor codex is rejected up front,
    # before any container starts.
    from lithos_loom.plugins.story_develop.config import ReviewerSpec

    cfg = DevelopConfig(
        repo=tmp_git_repo,
        description="x",
        work_dir=tmp_path / "work",
        reviewers=(ReviewerSpec(name="sec", tool="opencode"),),
        claude_config_dir=tmp_path / "fake-claude",
    )
    with pytest.raises(ValueError, match=r"unsupported tool .* for reviewer"):
        develop_mod.develop(cfg)


def test_uncaught_exception_propagates_and_tears_down_containers(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    """A phase raising mid-loop propagates out of develop() while the ``finally``
    still stops every started container, and the epilogue (conversation log /
    state.json / DevelopResult) is skipped. Pins the try/finally teardown
    contract the round-pipeline refactor must preserve — the only exit the
    existing suite never exercised."""
    state = _install_fakes(monkeypatch, config)

    def boom(*args, **kwargs):
        raise RuntimeError("boom in panel")

    # run_panel_round is called directly in the loop body with no local handler,
    # so it reaches only the outer try/finally — a clean injection point that is
    # past both container-start calls.
    monkeypatch.setattr(develop_mod, "run_panel_round", boom)

    with pytest.raises(RuntimeError, match="boom in panel"):
        develop_mod.develop(config)

    # finally tore down every container that was started (coder + reviewer(s)).
    assert state["starts"] >= 2
    assert len(state["stopped"]) == state["starts"]
    # epilogue skipped: no durable run state is written on an uncaught exception.
    assert not (config.run_dir / "state.json").exists()


def test_candidate_stage_dedups_per_committed_sha(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    """#140 dedup: the expensive candidate stage runs at most once per committed
    tree. When the approval branch is re-entered across rounds on the SAME
    ``gated_sha`` (reviews keep passing but a required candidate blocks and no new
    commit lands), the candidate checks are NOT re-run — pins
    ``candidate_ran_for_sha != gated_sha``. Without the guard the candidate would
    fire every round."""
    from dataclasses import replace

    from lithos_loom.plugins.story_develop.check_set import (
        Check,
        CheckResult,
        CheckSetResult,
    )

    fast = Check("lint", "ruff check --x", "required", "fast")
    candidate = Check("coverage", "coverage report", "required", "candidate")
    monkeypatch.setattr(
        develop_mod, "build_check_set", lambda config, wt: (fast, candidate)
    )
    calls: list[tuple[int, tuple[str, ...]]] = []

    def fake_run_check_set(config, wt, sha, round_no, checks, gate_ledger=None):
        calls.append((round_no, tuple(c.name for c in checks)))
        return CheckSetResult(
            tuple(
                CheckResult(
                    c,
                    "ran",
                    GateResult(
                        command=c.command,
                        # coverage (candidate) is RED and required -> blocks
                        # approval; lint (fast) is green so only the candidate
                        # holds the run.
                        exit_code=0 if c.name != "coverage" else 1,
                        passed=c.name != "coverage",
                        output_tail="x",
                    ),
                )
                for c in checks
            )
        )

    monkeypatch.setattr(develop_mod, "_run_check_set", fake_run_check_set)
    # LGTM every round (approval branch entered each round), but the coder commits
    # ONLY in round 1 (source_rounds={1}), so gated_sha never changes: rounds 2/3
    # re-enter approval on the same sha. The blocking candidate can never be
    # fixed -> the run stalls.
    cfg = replace(config, max_rounds=3)
    _install_fakes(monkeypatch, cfg, reviews=[{"text": _LGTM}], source_rounds={1})
    result = develop_mod.develop(cfg)

    assert result.status == "stalled"
    candidate_calls = [(r, n) for r, n in calls if n == ("coverage",)]
    fast_calls = [(r, n) for r, n in calls if n == ("lint",)]
    # candidate ran exactly once (round 1), not on the same-sha re-entries.
    assert candidate_calls == [(1, ("coverage",))]
    # the fast gate likewise runs only on the round that produced a new commit.
    assert fast_calls == [(1, ("lint",))]
