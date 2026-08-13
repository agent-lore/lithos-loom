"""Tests for the ``lithos-loom eval review`` command (#183).

``run_case`` (the live, host-only eval) is stubbed; these cover discovery,
case selection, the results table, and the exit code.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lithos_loom.evals.review import cli as eval_cli
from lithos_loom.evals.review.cli import eval_app
from lithos_loom.evals.review.harness import CaseResult
from lithos_loom.evals.review.stats import wilson_interval

runner = CliRunner()

_TOML = """
[case]
id = "{id}"
description = "d"
base = "aaaa"
head = "bbbb"
personas = ["correctness"]
profile = "standard"
acceptance_criteria_file = "ac.md"

[[expected]]
file = "cli/develop.py"
keywords = ["delivery"]
min_severity = "critical"
mechanism = "exits before delivery"
"""


def _make_case(
    cases_dir: Path, case_id: str, tier: str | None = None, artifacts: bool = False
) -> None:
    d = cases_dir / case_id
    d.mkdir(parents=True)
    toml = _TOML.format(id=case_id)
    if tier is not None:
        toml = toml.replace(
            'profile = "standard"', f'profile = "standard"\ntier = "{tier}"'
        )
    if artifacts:
        toml = toml.replace(
            'profile = "standard"',
            'profile = "standard"\nartifacts_dir = "artifacts"\n'
            'artifact_provenance = "captured"',
        )
        art = d / "artifacts"
        art.mkdir()
        (art / "page-800.png").write_bytes(b"\x89PNG...")
    (d / "case.toml").write_text(toml)
    (d / "ac.md").write_text("attach must wait for delivery")


@pytest.fixture
def cases_dir(tmp_path: Path) -> Path:
    d = tmp_path / "cases"
    _make_case(d, "180-attach-delivery", tier="floor")
    _make_case(d, "other-case", tier="frontier")
    return d


@pytest.fixture(autouse=True)
def _default_models(monkeypatch: pytest.MonkeyPatch) -> None:
    # #304: the CLI fails closed when an agent resolves to no explicit model,
    # so the tests provide per-tool defaults; fail-closed tests override this.
    monkeypatch.setattr(
        eval_cli,
        "load_tool_default_models",
        lambda: ({"codex": "gpt-test", "claude": "claude-test"}, ()),
    )


def _stub_run_case(monkeypatch: pytest.MonkeyPatch, *, catch_rate=1.0, passed=True):
    seen = []

    def fake(case, **kwargs):
        n = kwargs.get("k", 5)
        caught = round(catch_rate * n)
        per = tuple([True] * caught + [False] * (n - caught))
        seen.append({"case": case.id, "kwargs": kwargs})
        return CaseResult(
            case_id=case.id,
            n=n,
            catch_rate=catch_rate,
            severity_correctness=1.0,
            false_positive_rate=0.0,
            passed=passed,
            caught_per_sample=per,
            severity_per_sample=per,
            catch_rate_ci=wilson_interval(caught, n),
        )

    monkeypatch.setattr(eval_cli, "run_case", fake)
    return seen


def test_runs_all_cases_and_prints_table(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    seen = _stub_run_case(monkeypatch)
    result = runner.invoke(eval_app, ["review", "--cases-dir", str(cases_dir)])
    assert result.exit_code == 0, result.output
    assert {s["case"] for s in seen} == {"180-attach-delivery", "other-case"}
    assert "180-attach-delivery" in result.output
    # catch-rate surfaced in the table
    assert "100" in result.output or "1.0" in result.output


def test_case_selection(monkeypatch: pytest.MonkeyPatch, cases_dir: Path) -> None:
    seen = _stub_run_case(monkeypatch)
    result = runner.invoke(
        eval_app,
        ["review", "--cases-dir", str(cases_dir), "--case", "180-attach-delivery"],
    )
    assert result.exit_code == 0, result.output
    assert [s["case"] for s in seen] == ["180-attach-delivery"]


def test_k_is_threaded_through(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    seen = _stub_run_case(monkeypatch)
    runner.invoke(
        eval_app,
        ["review", "--cases-dir", str(cases_dir), "--case", "other-case", "-k", "3"],
    )
    assert seen[0]["kwargs"]["k"] == 3


def test_floor_regression_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    # A floor case below the bar is a HARD failure regardless of frontier gains
    # (RH-6): the floor exists purely as a regression gate.
    _stub_run_case(monkeypatch, catch_rate=0.2, passed=False)
    result = runner.invoke(
        eval_app,
        ["review", "--cases-dir", str(cases_dir), "--case", "180-attach-delivery"],
    )
    assert result.exit_code == 1
    assert "REGRESSED" in result.output
    assert "floor: REGRESSED" in result.output
    # a floor-only run has no headline to report
    assert "frontier:" not in result.output


def test_frontier_fail_exits_zero(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    # Frontier cases are EXPECTED to fail while they discriminate — a frontier
    # FAIL is the measurement, not a regression, so it must not gate the exit
    # code (RH-6).
    _stub_run_case(monkeypatch, catch_rate=0.2, passed=False)
    result = runner.invoke(
        eval_app,
        ["review", "--cases-dir", str(cases_dir), "--case", "other-case"],
    )
    assert result.exit_code == 0, result.output
    assert "FAIL" in result.output
    # a frontier-only run has no floor to report
    assert "floor:" not in result.output


def test_unknown_case_errors(monkeypatch: pytest.MonkeyPatch, cases_dir: Path) -> None:
    _stub_run_case(monkeypatch)
    result = runner.invoke(
        eval_app, ["review", "--cases-dir", str(cases_dir), "--case", "nope"]
    )
    assert result.exit_code != 0


def test_judge_on_by_default(monkeypatch: pytest.MonkeyPatch, cases_dir: Path) -> None:
    seen = _stub_run_case(monkeypatch)
    monkeypatch.setattr(eval_cli, "build_agent_judge", lambda **k: "JUDGE")
    runner.invoke(
        eval_app, ["review", "--cases-dir", str(cases_dir), "--case", "other-case"]
    )
    assert seen[0]["kwargs"]["judge"] == "JUDGE"


def test_no_judge_flag_disables_it(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    seen = _stub_run_case(monkeypatch)
    monkeypatch.setattr(eval_cli, "build_agent_judge", lambda **k: "JUDGE")
    runner.invoke(
        eval_app,
        ["review", "--cases-dir", str(cases_dir), "--case", "other-case", "--no-judge"],
    )
    assert seen[0]["kwargs"]["judge"] is None


def test_report_dir_passes_a_sink(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path, tmp_path: Path
) -> None:
    seen = _stub_run_case(monkeypatch)
    monkeypatch.setattr(eval_cli, "build_agent_judge", lambda **k: "JUDGE")
    out = tmp_path / "reports"
    runner.invoke(
        eval_app,
        [
            "review",
            "--cases-dir",
            str(cases_dir),
            "--case",
            "other-case",
            "--report-dir",
            str(out),
        ],
    )
    assert seen[0]["kwargs"]["report_sink"] is not None


def test_no_report_dir_means_no_sink(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    seen = _stub_run_case(monkeypatch)
    monkeypatch.setattr(eval_cli, "build_agent_judge", lambda **k: "JUDGE")
    runner.invoke(
        eval_app, ["review", "--cases-dir", str(cases_dir), "--case", "other-case"]
    )
    assert seen[0]["kwargs"]["report_sink"] is None


def test_report_sink_writes_per_run_files(tmp_path: Path) -> None:
    sink = eval_cli._make_report_sink(tmp_path)
    sink("case-x", "buggy", 0, {"blocking": True})
    f = tmp_path / "case-x" / "buggy-0.json"
    assert f.is_file()
    assert json.loads(f.read_text())["blocking"] is True


def test_table_shows_catch_count_and_ci(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    _stub_run_case(monkeypatch, catch_rate=0.8)
    result = runner.invoke(
        eval_app,
        ["review", "--cases-dir", str(cases_dir), "--case", "180-attach-delivery"],
    )
    assert result.exit_code == 0, result.output
    assert "4/5" in result.output  # caught count out of K, not a bare percentage
    assert "%" in result.output  # the CI range is rendered as a percentage band


def test_summary_json_written_when_report_dir(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path, tmp_path: Path
) -> None:
    _stub_run_case(monkeypatch, catch_rate=0.8)
    monkeypatch.setattr(eval_cli, "build_agent_judge", lambda **k: "JUDGE")
    out = tmp_path / "reports"
    runner.invoke(
        eval_app,
        [
            "review",
            "--cases-dir",
            str(cases_dir),
            "--case",
            "180-attach-delivery",
            "--report-dir",
            str(out),
        ],
    )
    summary = out / "180-attach-delivery" / "summary.json"
    assert summary.is_file()
    data = json.loads(summary.read_text())
    assert data["case"] == "180-attach-delivery"
    assert data["catch_rate"] == 0.8
    assert len(data["caught_per_sample"]) == 5
    assert len(data["catch_rate_ci"]) == 2


def test_no_summary_json_without_report_dir(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path, tmp_path: Path
) -> None:
    _stub_run_case(monkeypatch)
    runner.invoke(
        eval_app,
        ["review", "--cases-dir", str(cases_dir), "--case", "180-attach-delivery"],
    )
    assert not (tmp_path / "180-attach-delivery" / "summary.json").exists()


def _stub_errored(monkeypatch: pytest.MonkeyPatch):
    """run_case returns 18 valid catches + 2 errored samples (k=20)."""

    def fake(case, **kwargs):
        return CaseResult(
            case_id=case.id,
            n=20,
            catch_rate=1.0,
            severity_correctness=1.0,
            false_positive_rate=0.0,
            passed=True,
            caught_per_sample=tuple([True] * 18 + [False] * 2),
            severity_per_sample=tuple([True] * 18 + [False] * 2),
            catch_rate_ci=wilson_interval(18, 18),
            errored_per_sample=tuple([False] * 18 + [True] * 2),
        )

    monkeypatch.setattr(eval_cli, "run_case", fake)


def test_table_shows_errored_count(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    _stub_errored(monkeypatch)
    result = runner.invoke(
        eval_app,
        ["review", "--cases-dir", str(cases_dir), "--case", "180-attach-delivery"],
    )
    assert result.exit_code == 0, result.output
    assert "18/18" in result.output  # denominator is the valid-sample count
    assert "err" in result.output  # errored count surfaced


def test_summary_json_carries_errored(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path, tmp_path: Path
) -> None:
    _stub_errored(monkeypatch)
    out = tmp_path / "reports"
    runner.invoke(
        eval_app,
        [
            "review",
            "--cases-dir",
            str(cases_dir),
            "--case",
            "180-attach-delivery",
            "--report-dir",
            str(out),
        ],
    )
    data = json.loads((out / "180-attach-delivery" / "summary.json").read_text())
    assert data["errored"] == 2
    assert data["n_valid"] == 18
    assert sum(data["errored_per_sample"]) == 2


# ── artifact cases (RH-3): the summary records the measured surface ──


def test_summary_json_carries_artifacts_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # two report dirs are only comparable if each records WHICH surface (diff
    # vs artifact pass) and what was seeded — the RH-7 effective-panel rule
    cases = tmp_path / "cases"
    _make_case(cases, "art-case", tier="frontier", artifacts=True)
    _stub_run_case(monkeypatch)
    out = tmp_path / "reports"
    result = runner.invoke(
        eval_app,
        ["review", "--cases-dir", str(cases), "--report-dir", str(out)],
    )
    assert result.exit_code == 0, result.output
    data = json.loads((out / "art-case" / "summary.json").read_text())
    assert data["artifacts"] == {"n_files": 1, "provenance": "captured"}
    assert "artifact" in result.output  # the running line names the surface


def test_summary_json_omits_artifacts_for_diff_cases(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path, tmp_path: Path
) -> None:
    _stub_run_case(monkeypatch)
    out = tmp_path / "reports"
    runner.invoke(
        eval_app,
        [
            "review",
            "--cases-dir",
            str(cases_dir),
            "--case",
            "180-attach-delivery",
            "--report-dir",
            str(out),
        ],
    )
    data = json.loads((out / "180-attach-delivery" / "summary.json").read_text())
    assert "artifacts" not in data


# ── panel overrides (RH-7): --profile / --reviewer / --reviewer-override ──


def test_bad_override_fails_before_any_run(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    # Fail closed BEFORE paid work: a malformed override must abort with no
    # run_case calls at all.
    seen = _stub_run_case(monkeypatch)
    result = runner.invoke(
        eval_app,
        [
            "review",
            "--cases-dir",
            str(cases_dir),
            "--reviewer-override",
            "corectness.model=x",
        ],
    )
    assert result.exit_code == 2  # the stated pre-run validation contract
    assert seen == []


def test_unknown_profile_fails_before_any_run(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    seen = _stub_run_case(monkeypatch)
    result = runner.invoke(
        eval_app,
        ["review", "--cases-dir", str(cases_dir), "--profile", "thorogh"],
    )
    assert result.exit_code == 2
    assert seen == []


def test_unknown_reviewer_fails_before_any_run(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    seen = _stub_run_case(monkeypatch)
    result = runner.invoke(
        eval_app,
        ["review", "--cases-dir", str(cases_dir), "--reviewer", "corectness"],
    )
    assert result.exit_code == 2
    assert seen == []


def test_gate_only_profile_without_reviewer_fails_before_any_run(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    seen = _stub_run_case(monkeypatch)
    result = runner.invoke(
        eval_app,
        ["review", "--cases-dir", str(cases_dir), "--profile", "minimal"],
    )
    assert result.exit_code == 2
    assert seen == []


def test_capability_crossing_override_fails_before_any_run(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    # Syntactically valid, but correctness runs codex (no effort knob): the
    # requested lever could never fire, so the whole invocation must abort —
    # every case's panel resolves BEFORE the first paid run.
    seen = _stub_run_case(monkeypatch)
    result = runner.invoke(
        eval_app,
        [
            "review",
            "--cases-dir",
            str(cases_dir),
            "--reviewer-override",
            "correctness.effort=xhigh",
        ],
    )
    assert result.exit_code == 2
    assert seen == []


def test_cli_flags_reach_live_review(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    # The composition proof: the CLI-built review_fn must carry the SAME
    # resolved panel it writes to summary.json — otherwise the paid agents
    # could run a different arm than the one recorded.
    captured: dict = {}

    def fake_live_review(
        case, head_sha, *, reviewers=None, profile=None, default_models=None
    ):
        captured["reviewers"] = reviewers
        captured["profile"] = profile
        return {}

    def fake_run_case(case, **kwargs):
        kwargs["review_fn"](case, "deadbeef")
        return CaseResult(
            case_id=case.id,
            n=1,
            catch_rate=1.0,
            severity_correctness=1.0,
            false_positive_rate=0.0,
            passed=True,
            caught_per_sample=(True,),
            severity_per_sample=(True,),
            catch_rate_ci=wilson_interval(1, 1),
        )

    monkeypatch.setattr(eval_cli, "live_review", fake_live_review)
    monkeypatch.setattr(eval_cli, "run_case", fake_run_case)
    result = runner.invoke(
        eval_app,
        [
            "review",
            "--cases-dir",
            str(cases_dir),
            "--case",
            "other-case",
            "--profile",
            "thorough",
            "--reviewer-override",
            "correctness.model=some-model",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["profile"] == "thorough"
    by_name = {s.name: s for s in captured["reviewers"]}
    assert len(by_name) == 5  # thorough's panel reached the execution path
    assert by_name["correctness"].model == "some-model"


def test_override_passes_a_review_fn(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    seen = _stub_run_case(monkeypatch)
    result = runner.invoke(
        eval_app,
        [
            "review",
            "--cases-dir",
            str(cases_dir),
            "--case",
            "other-case",
            "--reviewer-override",
            "correctness.model=some-model",
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen[0]["kwargs"]["review_fn"] is not None


def test_plain_run_passes_resolved_panel_with_default_models(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    # #304: even without override flags the resolved panel (default models
    # applied) must reach the harness — never the personas' model=None specs.
    seen = _stub_run_case(monkeypatch)
    result = runner.invoke(
        eval_app,
        ["review", "--cases-dir", str(cases_dir), "--case", "other-case"],
    )
    assert result.exit_code == 0, result.output
    assert seen[0]["kwargs"]["review_fn"] is not None


def test_missing_default_model_fails_before_any_run(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    # #304 fail-closed: no default for the persona's tool and no override —
    # reject pre-paid with the agent named and the config remedy.
    monkeypatch.setattr(eval_cli, "load_tool_default_models", lambda: ({}, ()))
    seen = _stub_run_case(monkeypatch)
    result = runner.invoke(
        eval_app,
        ["review", "--cases-dir", str(cases_dir), "--case", "other-case"],
    )
    assert result.exit_code == 2
    assert "correctness" in result.output
    assert "[story_develop.default_models]" in result.output
    assert seen == []


def test_override_satisfies_the_model_policy_without_defaults(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    # an explicit --reviewer-override model is the other way to be explicit
    # (--no-judge: with empty defaults the judge would otherwise need one too)
    monkeypatch.setattr(eval_cli, "load_tool_default_models", lambda: ({}, ()))
    seen = _stub_run_case(monkeypatch)
    result = runner.invoke(
        eval_app,
        [
            "review",
            "--cases-dir",
            str(cases_dir),
            "--case",
            "other-case",
            "--no-judge",
            "--reviewer-override",
            "correctness.model=some-model",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(seen) == 1


def test_summary_json_carries_effective_panel(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path, tmp_path: Path
) -> None:
    _stub_run_case(monkeypatch)
    out = tmp_path / "reports"
    runner.invoke(
        eval_app,
        [
            "review",
            "--cases-dir",
            str(cases_dir),
            "--case",
            "180-attach-delivery",
            "--report-dir",
            str(out),
        ],
    )
    data = json.loads((out / "180-attach-delivery" / "summary.json").read_text())
    assert data["profile"] == "standard"
    (reviewer,) = data["panel"]
    assert reviewer["name"] == "correctness"
    assert reviewer["tool"] == "codex"  # the canonical persona's engine
    assert reviewer["model"] == "gpt-test"  # #304: default models applied


def test_summary_json_carries_overridden_panel(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path, tmp_path: Path
) -> None:
    # The effective panel is what makes two report dirs comparable (RH-7).
    _stub_run_case(monkeypatch)
    out = tmp_path / "reports"
    runner.invoke(
        eval_app,
        [
            "review",
            "--cases-dir",
            str(cases_dir),
            "--case",
            "180-attach-delivery",
            "--profile",
            "thorough",
            "--reviewer-override",
            "correctness.model=some-model",
            "--report-dir",
            str(out),
        ],
    )
    data = json.loads((out / "180-attach-delivery" / "summary.json").read_text())
    assert data["profile"] == "thorough"
    by_name = {r["name"]: r for r in data["panel"]}
    assert len(by_name) == 5  # thorough's full persona panel replaced the case's
    assert by_name["correctness"]["model"] == "some-model"  # override wins
    assert by_name["security"]["model"] == "claude-test"  # default fills the rest


# ── tier split (RH-6): floor = regression gate, frontier = headline ──


def test_table_shows_tier_column_and_rollups(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    _stub_run_case(monkeypatch, catch_rate=0.8)
    result = runner.invoke(eval_app, ["review", "--cases-dir", str(cases_dir)])
    assert result.exit_code == 0, result.output
    assert "tier" in result.output
    assert "floor" in result.output
    assert "frontier" in result.output
    # frontier roll-up pools per-sample catches across frontier cases only:
    # here one frontier case at 4/5 (the floor case's 4/5 must NOT pool in)
    assert "frontier: 4/5 pooled catch" in result.output
    assert "floor: OK" in result.output


def test_frontier_rollup_pools_counts_across_cases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The headline must POOL per-sample catches and valid denominators across
    # frontier cases — not average per-case percentages (here pooled 5/8 ≈ 62%
    # vs a percentage-average ≈ 57%) — and errored samples must drop out of the
    # pooled denominator too. The floor case's 5/5 must stay out entirely.
    d = tmp_path / "cases"
    _make_case(d, "floor-case", tier="floor")
    _make_case(d, "frontier-a", tier="frontier")
    _make_case(d, "frontier-b", tier="frontier")

    per_case = {
        # caught, per-sample tuples: floor 5/5; frontier-a 4/5 valid 5;
        # frontier-b 1/3 valid (2 errored) -> pooled frontier 5/8.
        "floor-case": ((True,) * 5, (False,) * 5),
        "frontier-a": ((True, True, True, True, False), (False,) * 5),
        "frontier-b": ((True, False, False, False, False), (False,) * 3 + (True,) * 2),
    }

    def fake(case, **kwargs):
        caught_per_sample, errored_per_sample = per_case[case.id]
        n_valid = 5 - sum(errored_per_sample)
        caught = sum(caught_per_sample)
        return CaseResult(
            case_id=case.id,
            n=5,
            catch_rate=caught / n_valid,
            severity_correctness=1.0,
            false_positive_rate=0.0,
            passed=caught / n_valid >= 0.8,
            caught_per_sample=caught_per_sample,
            severity_per_sample=caught_per_sample,
            catch_rate_ci=wilson_interval(caught, n_valid),
            errored_per_sample=errored_per_sample,
        )

    monkeypatch.setattr(eval_cli, "run_case", fake)
    result = runner.invoke(eval_app, ["review", "--cases-dir", str(d)])
    assert result.exit_code == 0, result.output
    lo, hi = wilson_interval(5, 8)
    assert (
        f"frontier: 5/8 pooled catch (95% CI {lo * 100:.0f}-{hi * 100:.0f}%) "
        "over 2 cases"
    ) in result.output
    assert "floor: OK (1 case at bar)" in result.output


def test_floor_row_reads_ok_not_pass(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    # Floor rows report pass/regressed (RH-6 wording), not the frontier's
    # PASS/FAIL — the row itself says which semantics apply.
    _stub_run_case(monkeypatch, catch_rate=1.0)
    result = runner.invoke(
        eval_app,
        ["review", "--cases-dir", str(cases_dir), "--case", "180-attach-delivery"],
    )
    assert result.exit_code == 0, result.output
    assert "ok" in result.output
    assert "PASS" not in result.output


def test_undeclared_tier_is_treated_as_frontier(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Opt-in floor: a case that hasn't declared a tier must never silently pad
    # the regression floor (or gate the exit code).
    d = tmp_path / "cases"
    _make_case(d, "untiered-case")
    _stub_run_case(monkeypatch, catch_rate=0.2, passed=False)
    result = runner.invoke(eval_app, ["review", "--cases-dir", str(d)])
    assert result.exit_code == 0, result.output
    assert "frontier:" in result.output
    assert "floor:" not in result.output


def _stub_no_valid_samples(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_case returns a result whose every sample errored (n_valid == 0)."""

    def fake(case, **kwargs):
        return CaseResult(
            case_id=case.id,
            n=2,
            catch_rate=0.0,
            severity_correctness=0.0,
            false_positive_rate=0.0,
            passed=False,
            caught_per_sample=(False, False),
            severity_per_sample=(False, False),
            catch_rate_ci=(0.0, 0.0),
            errored_per_sample=(True, True),
        )

    monkeypatch.setattr(eval_cli, "run_case", fake)


def test_zero_valid_samples_exits_nonzero_even_for_frontier(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    # All-errored is an infra failure, not a measurement — it must not read as
    # "frontier miss, exit 0" or the harness could silently measure nothing.
    _stub_no_valid_samples(monkeypatch)
    result = runner.invoke(
        eval_app,
        ["review", "--cases-dir", str(cases_dir), "--case", "other-case"],
    )
    assert result.exit_code == 1


def test_summary_json_carries_tier(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path, tmp_path: Path
) -> None:
    _stub_run_case(monkeypatch)
    out = tmp_path / "reports"
    runner.invoke(
        eval_app,
        [
            "review",
            "--cases-dir",
            str(cases_dir),
            "--case",
            "180-attach-delivery",
            "--report-dir",
            str(out),
        ],
    )
    data = json.loads((out / "180-attach-delivery" / "summary.json").read_text())
    assert data["tier"] == "floor"


# ── judge model policy (#305 review finding 4) ─────────────────────────


def test_judge_gets_an_explicit_model(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    # The judge decides whether a finding counts — an unrecorded drifting
    # judge model makes eval scores incomparable even with a pinned panel.
    _stub_run_case(monkeypatch)
    seen: dict = {}

    def fake_judge(**kwargs):
        seen.update(kwargs)
        return "JUDGE"

    monkeypatch.setattr(eval_cli, "build_agent_judge", fake_judge)
    result = runner.invoke(
        eval_app, ["review", "--cases-dir", str(cases_dir), "--case", "other-case"]
    )
    assert result.exit_code == 0, result.output
    assert seen["tool"] == "claude"
    assert seen["model"] == "claude-test"


def test_judge_without_a_default_model_fails_before_any_run(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    monkeypatch.setattr(
        eval_cli, "load_tool_default_models", lambda: ({"codex": "gpt-test"}, ())
    )
    seen = _stub_run_case(monkeypatch)
    result = runner.invoke(
        eval_app, ["review", "--cases-dir", str(cases_dir), "--case", "other-case"]
    )
    assert result.exit_code == 2
    assert "judge" in result.output
    assert seen == []


def test_no_judge_needs_no_judge_model(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    monkeypatch.setattr(
        eval_cli, "load_tool_default_models", lambda: ({"codex": "gpt-test"}, ())
    )
    seen = _stub_run_case(monkeypatch)
    result = runner.invoke(
        eval_app,
        ["review", "--cases-dir", str(cases_dir), "--case", "other-case", "--no-judge"],
    )
    assert result.exit_code == 0, result.output
    assert len(seen) == 1


def test_summary_json_records_the_judge(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path, tmp_path: Path
) -> None:
    _stub_run_case(monkeypatch)
    monkeypatch.setattr(eval_cli, "build_agent_judge", lambda **k: "JUDGE")
    out = tmp_path / "reports"
    runner.invoke(
        eval_app,
        [
            "review",
            "--cases-dir",
            str(cases_dir),
            "--case",
            "other-case",
            "--report-dir",
            str(out),
        ],
    )
    data = json.loads((out / "other-case" / "summary.json").read_text())
    assert data["judge"] == {"tool": "claude", "model": "claude-test"}


def test_summary_json_judge_null_when_disabled(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path, tmp_path: Path
) -> None:
    _stub_run_case(monkeypatch)
    out = tmp_path / "reports"
    runner.invoke(
        eval_app,
        [
            "review",
            "--cases-dir",
            str(cases_dir),
            "--case",
            "other-case",
            "--no-judge",
            "--report-dir",
            str(out),
        ],
    )
    data = json.loads((out / "other-case" / "summary.json").read_text())
    assert data["judge"] is None


def test_unsupported_judge_tool_fails_before_any_run(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    # #305 review round 2: a configured default for an unknown tool key is
    # accepted (forward compat), so the model check alone would let an
    # unsupported --judge-tool through — and it would only crash when the
    # first finding reached the judge, AFTER paid reviewer runs.
    monkeypatch.setattr(
        eval_cli,
        "load_tool_default_models",
        lambda: ({"opencode": "some-model", "codex": "gpt-test"}, ()),
    )
    seen = _stub_run_case(monkeypatch)
    result = runner.invoke(
        eval_app,
        [
            "review",
            "--cases-dir",
            str(cases_dir),
            "--case",
            "other-case",
            "--judge-tool",
            "opencode",
        ],
    )
    assert result.exit_code == 2
    assert "opencode" in result.output
    assert seen == []
