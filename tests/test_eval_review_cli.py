"""Tests for the ``lithos-loom eval review`` command (#183).

``run_case`` (the live, host-only eval) is stubbed; these cover discovery,
case selection, the results table, and the exit code.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lithos_loom.evals.review import app as eval_app_mod
from lithos_loom.evals.review import cli as eval_cli
from lithos_loom.evals.review.case import Expected
from lithos_loom.evals.review.cli import eval_app
from lithos_loom.evals.review.harness import CaseResult
from lithos_loom.evals.review.match import JudgeVerdict, MatchResult, RunScore
from lithos_loom.evals.review.stats import wilson_interval

runner = CliRunner()

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(output: str) -> str:
    """CLI output with styling stripped.

    Rich's option highlighter splits a long flag into separately-styled runs
    (``--max-known-good-block-rate`` renders as ``-`` + ``-max`` +
    ``-known-good-block-rate``, each wrapped in escapes), so a raw substring
    check passes only where colour happens to be off — locally, but not in CI.
    """
    return _ANSI.sub("", output)


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
    cases_dir: Path,
    case_id: str,
    tier: str | None = None,
    artifacts: bool = False,
    known_good_artifacts: bool = False,
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
    if known_good_artifacts:
        toml += (
            '\n[known_good]\nhead = "cccccccc"\n'
            'artifacts_dir = "known-good-artifacts"\n'
        )
        kg = d / "known-good-artifacts"
        kg.mkdir()
        (kg / "page-800.png").write_bytes(b"\x89PNG-fixed")
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
    monkeypatch.setattr(eval_app_mod, "build_agent_judge", lambda **k: "JUDGE")
    runner.invoke(
        eval_app, ["review", "--cases-dir", str(cases_dir), "--case", "other-case"]
    )
    assert seen[0]["kwargs"]["judge"] == "JUDGE"


def test_no_judge_flag_disables_it(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    seen = _stub_run_case(monkeypatch)
    monkeypatch.setattr(eval_app_mod, "build_agent_judge", lambda **k: "JUDGE")
    runner.invoke(
        eval_app,
        ["review", "--cases-dir", str(cases_dir), "--case", "other-case", "--no-judge"],
    )
    assert seen[0]["kwargs"]["judge"] is None


def test_report_dir_passes_a_sink(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path, tmp_path: Path
) -> None:
    seen = _stub_run_case(monkeypatch)
    monkeypatch.setattr(eval_app_mod, "build_agent_judge", lambda **k: "JUDGE")
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
    monkeypatch.setattr(eval_app_mod, "build_agent_judge", lambda **k: "JUDGE")
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
    monkeypatch.setattr(eval_app_mod, "build_agent_judge", lambda **k: "JUDGE")
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


def test_summary_json_records_the_known_good_captures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # RH-1: whether an artifact case measured false positives — and against how
    # many captures — is part of what makes two report dirs comparable
    cases = tmp_path / "cases"
    _make_case(
        cases, "art-case", tier="frontier", artifacts=True, known_good_artifacts=True
    )
    _stub_run_case(monkeypatch)
    out = tmp_path / "reports"
    result = runner.invoke(
        eval_app,
        ["review", "--cases-dir", str(cases), "--report-dir", str(out)],
    )
    assert result.exit_code == 0, result.output
    data = json.loads((out / "art-case" / "summary.json").read_text())
    assert data["artifacts"] == {
        "n_files": 1,
        "provenance": "captured",
        "known_good_n_files": 1,
    }


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

    monkeypatch.setattr(eval_app_mod, "build_agent_judge", fake_judge)
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
    monkeypatch.setattr(eval_app_mod, "build_agent_judge", lambda **k: "JUDGE")
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


# ── noise reporting + the known-good block gate (#310) ────────────────────────


def _noisy_result(
    case_id: str,
    *,
    noisy: tuple[bool, ...] = (True, True, True),
    blocked: tuple[bool, ...] = (True, True, True),
    errored: tuple[bool, ...] = (False, False, False),
) -> CaseResult:
    n = len(noisy)
    valid = [i for i in range(n) if not errored[i]]
    noise_rate = sum(noisy[i] for i in valid) / len(valid) if valid else 0.0
    return CaseResult(
        case_id=case_id,
        n=n,
        catch_rate=1.0,
        severity_correctness=1.0,
        false_positive_rate=0.0,
        passed=True,
        caught_per_sample=(True,) * n,
        severity_per_sample=(True,) * n,
        catch_rate_ci=wilson_interval(n, n),
        false_positive_per_sample=(False,) * n,
        false_positive_errored_per_sample=errored,
        findings_per_sample=(1,) * n,
        blocked_per_sample=(False,) * n,
        known_good_findings_per_sample=tuple(2 if x else 0 for x in noisy),
        known_good_blocked_per_sample=blocked,
        noise_rate=noise_rate,
        noise_rate_ci=wilson_interval(sum(noisy[i] for i in valid), len(valid)),
    )


def _stub_noisy(monkeypatch: pytest.MonkeyPatch, **kwargs):
    monkeypatch.setattr(
        eval_cli, "run_case", lambda case, **_: _noisy_result(case.id, **kwargs)
    )


def test_table_shows_the_noise_cell_beside_fp(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    # The whole point of #310: `fp 0/3` and `noise 3/3` must be readable side by
    # side, or a config that blocks every clean render looks perfect.
    _stub_noisy(monkeypatch)
    result = runner.invoke(
        eval_app, ["review", "--cases-dir", str(cases_dir), "--case", "other-case"]
    )
    assert result.exit_code == 0, result.output
    assert "noise" in result.output
    assert "3/3 blk3" in result.output


def test_table_noise_cell_is_a_dash_without_a_known_good_arm(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    _stub_run_case(monkeypatch)  # no known-good samples at all
    result = runner.invoke(
        eval_app, ["review", "--cases-dir", str(cases_dir), "--case", "other-case"]
    )
    assert result.exit_code == 0, result.output
    assert "blk" not in result.output


def test_summary_json_carries_the_noise_instrumentation(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path, tmp_path: Path
) -> None:
    _stub_noisy(monkeypatch)
    out = tmp_path / "reports"
    result = runner.invoke(
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
    assert result.exit_code == 0, result.output
    payload = json.loads((out / "other-case" / "summary.json").read_text())
    assert payload["noise_rate"] == 1.0
    assert len(payload["noise_rate_ci"]) == 2
    assert payload["known_good_findings_per_sample"] == [2, 2, 2]
    assert payload["known_good_blocked_per_sample"] == [True, True, True]
    assert payload["known_good_blocked"] == 3
    assert payload["findings_per_sample"] == [1, 1, 1]
    assert payload["blocked_per_sample"] == [False, False, False]


def test_known_good_block_gate_is_off_by_default(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    # No baseline exists for the noise numbers yet, so the gate must be opt-in:
    # blocking every known-good run records, it does not fail the run.
    _stub_noisy(monkeypatch)
    result = runner.invoke(
        eval_app, ["review", "--cases-dir", str(cases_dir), "--case", "other-case"]
    )
    assert result.exit_code == 0, result.output


def test_known_good_block_gate_exits_nonzero_when_exceeded(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    _stub_noisy(monkeypatch)
    result = runner.invoke(
        eval_app,
        [
            "review",
            "--cases-dir",
            str(cases_dir),
            "--case",
            "other-case",
            "--max-known-good-block-rate",
            "0.0",
        ],
    )
    assert result.exit_code == 1
    assert "known-good" in result.output
    assert "other-case" in result.output


def test_known_good_block_gate_passes_under_the_bar(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    _stub_noisy(monkeypatch, blocked=(True, False, False))
    result = runner.invoke(
        eval_app,
        [
            "review",
            "--cases-dir",
            str(cases_dir),
            "--case",
            "other-case",
            "--max-known-good-block-rate",
            "0.34",
        ],
    )
    assert result.exit_code == 0, result.output


def test_known_good_block_gate_ignores_cases_without_a_known_good_arm(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    # A catch-only case measures no false positives; a zero bar must not fail it.
    _stub_run_case(monkeypatch)
    result = runner.invoke(
        eval_app,
        [
            "review",
            "--cases-dir",
            str(cases_dir),
            "--case",
            "other-case",
            "--max-known-good-block-rate",
            "0.0",
        ],
    )
    assert result.exit_code == 0, result.output


def test_known_good_block_gate_excludes_errored_samples(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    # 1 block of 2 VALID samples = 0.5, not 1/3 — a crashed known-good sample
    # blocks by definition (an incomplete panel holds approval) and would
    # otherwise convict the arm.
    _stub_noisy(
        monkeypatch,
        noisy=(True, False, False),
        blocked=(True, True, False),
        errored=(False, True, False),
    )
    result = runner.invoke(
        eval_app,
        [
            "review",
            "--cases-dir",
            str(cases_dir),
            "--case",
            "other-case",
            "--max-known-good-block-rate",
            "0.5",
        ],
    )
    assert result.exit_code == 0, result.output


def test_known_good_block_gate_fails_closed_when_every_sample_errored(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    # The known-good arm runs AFTER the buggy one, so an exhausted quota can
    # wipe it out entirely. An explicitly requested clean-head gate must not
    # read "no evidence" as "no violation".
    _stub_noisy(
        monkeypatch,
        noisy=(False, False, False),
        blocked=(True, True, True),
        errored=(True, True, True),
    )
    result = runner.invoke(
        eval_app,
        [
            "review",
            "--cases-dir",
            str(cases_dir),
            "--case",
            "other-case",
            "--max-known-good-block-rate",
            "0.5",
        ],
    )
    assert result.exit_code == 1
    assert "other-case" in result.output


def test_all_errored_known_good_arm_still_exits_zero_without_the_gate(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    # Unchanged default: an unmeasurable FP arm is a reporting gap, not a run
    # failure (the infra-failure exit keys on the buggy side). Only an explicit
    # gate request makes it fail closed.
    _stub_noisy(
        monkeypatch,
        noisy=(False, False, False),
        blocked=(True, True, True),
        errored=(True, True, True),
    )
    result = runner.invoke(
        eval_app, ["review", "--cases-dir", str(cases_dir), "--case", "other-case"]
    )
    assert result.exit_code == 0, result.output


def test_known_good_block_gate_at_exactly_the_bar_does_not_convict(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    _stub_noisy(monkeypatch, blocked=(True, True, False))
    result = runner.invoke(
        eval_app,
        [
            "review",
            "--cases-dir",
            str(cases_dir),
            "--case",
            "other-case",
            "--max-known-good-block-rate",
            str(2 / 3),
        ],
    )
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("bad", ["-0.1", "1.1", "nan", "inf", "-inf"])
def test_out_of_range_block_rate_fails_before_any_run(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path, bad: str
) -> None:
    # A typo'd `10` for `0.10` would silently disable the gate the operator
    # explicitly asked for; a negative one would convict a silent arm.
    seen = _stub_run_case(monkeypatch)
    result = runner.invoke(
        eval_app,
        [
            "review",
            "--cases-dir",
            str(cases_dir),
            "--max-known-good-block-rate",
            bad,
        ],
    )
    assert result.exit_code == 2
    assert "max-known-good-block-rate" in _plain(result.output)
    assert seen == []


@pytest.mark.parametrize("edge", ["0.0", "1.0"])
def test_block_rate_accepts_the_closed_interval(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path, edge: str
) -> None:
    _stub_noisy(monkeypatch, blocked=(False, False, False))
    result = runner.invoke(
        eval_app,
        [
            "review",
            "--cases-dir",
            str(cases_dir),
            "--case",
            "other-case",
            "--max-known-good-block-rate",
            edge,
        ],
    )
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("bad", ["-0.1", "1.1", "nan", "inf"])
def test_out_of_range_bar_fails_before_any_run(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path, bad: str
) -> None:
    # Same class of silent failure on the pass bar: `--bar 0` would quietly
    # retire the floor regression gate.
    seen = _stub_run_case(monkeypatch)
    result = runner.invoke(
        eval_app, ["review", "--cases-dir", str(cases_dir), "--bar", bad]
    )
    assert result.exit_code == 2
    assert "--bar must be a finite rate" in _plain(result.output)
    assert seen == []


def test_help_exit_contract_names_the_block_gate() -> None:
    # The generated help must not state one exit contract in the "Exit 1 iff …"
    # sentence and a different one two paragraphs later.
    doc = eval_cli.review.__doc__ or ""
    exit_sentence = doc[doc.index("Exit 1 iff") : doc.index("Exit 1 iff") + 400]
    assert "known-good" in exit_sentence
    assert "floor case" in exit_sentence


# ── judge verdicts are auditable, and judge failure is its own signal (#307) ──


def _stub_judging_run_case(
    monkeypatch: pytest.MonkeyPatch,
    *,
    statuses: tuple[str, ...] = ("ok",),
    caught: tuple[bool, ...] = (True,),
    structured: tuple[bool, ...] = (True,),
    reply: str = "it doubles the margin\nMATCHED: f-001",
):
    """Drive the real judge sink through the CLI, without an agent.

    `run_case` is stubbed but still calls the `judge_sink` the CLI handed it, so
    the sink's on-disk layout is exercised through the public surface rather than
    by reaching for a private helper (the `tests_private_imports` budget has one
    slot spare).
    """
    expected = Expected(
        file="cli/develop.py",
        keywords=("delivery",),
        min_severity="critical",
        mechanism="exits before delivery",
    )

    def fake(case, **kwargs):
        case = replace(case, expected=(expected,))
        sink = kwargs.get("judge_sink")
        if sink is not None:
            for i, status in enumerate(statuses):
                verdict = JudgeVerdict(
                    matched_ids=("f-001",) if status == "ok" and caught[i] else (),
                    status=status,  # type: ignore[arg-type]
                    reply=reply,
                )
                sink(
                    case,
                    "buggy",
                    i,
                    RunScore(
                        caught=caught[i],
                        severity_correct=caught[i],
                        matches=[
                            MatchResult(
                                caught=caught[i],
                                severity_correct=caught[i],
                                method="judge",
                                finding_id="f-001" if caught[i] else "",
                                structured_caught=structured[i],
                                structured_finding_id="f-001",
                                judge=verdict,
                            )
                        ],
                        judge_status=status,
                        structured_caught=structured[i],
                    ),
                )
        n = len(statuses)
        return CaseResult(
            case_id=case.id,
            n=n,
            catch_rate=1.0,
            severity_correctness=1.0,
            false_positive_rate=0.0,
            passed=True,
            caught_per_sample=caught,
            severity_per_sample=caught,
            catch_rate_ci=wilson_interval(sum(caught), n),
            errored_per_sample=(False,) * n,
            judge_status_per_sample=statuses,
            structured_caught_per_sample=structured,
        )

    monkeypatch.setattr(eval_cli, "run_case", fake)


def _invoke(cases_dir: Path, out: Path | None = None, *extra: str):
    args = ["review", "--cases-dir", str(cases_dir), "--case", "180-attach-delivery"]
    if out is not None:
        args += ["--report-dir", str(out)]
    return runner.invoke(eval_app, [*args, *extra])


def test_judge_verdicts_are_written_under_the_case_judge_dir(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path, tmp_path: Path
) -> None:
    _stub_judging_run_case(monkeypatch)
    out = tmp_path / "reports"
    result = _invoke(cases_dir, out)

    assert result.exit_code == 0, result.output
    payload = json.loads(
        (out / "180-attach-delivery" / "judge" / "buggy-0.json").read_text()
    )
    assert payload["judge"] == {"tool": "claude", "model": "claude-test"}
    assert payload["judge_status"] == "ok"
    (record,) = payload["expected"]
    assert record["matched_ids"] == ["f-001"]
    assert record["mechanism"] == "exits before delivery"
    # the whole point of #307: the reasoning behind a verdict survives the run
    assert "doubles the margin" in record["reply"]


def test_judge_records_never_pollute_the_review_report_namespace(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path, tmp_path: Path
) -> None:
    """`<case>/<variant>-<i>.json` is the documented ReviewReport contract.

    Offline re-scoring globs that namespace, so a sibling `buggy-0.judge.json`
    would silently feed judge records to a finding counter. A subdirectory
    matches no `*.json` glob in the case dir.
    """
    _stub_judging_run_case(monkeypatch)
    out = tmp_path / "reports"
    _invoke(cases_dir, out)

    case_dir = out / "180-attach-delivery"
    assert [p.name for p in sorted(case_dir.glob("buggy-*.json"))] == []
    assert sorted(p.name for p in case_dir.glob("*.json")) == ["summary.json"]
    assert (case_dir / "judge" / "buggy-0.json").is_file()


def test_no_judge_writes_no_judge_dir(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path, tmp_path: Path
) -> None:
    # absence of the directory is meaningful: nothing was judged
    _stub_judging_run_case(monkeypatch)
    out = tmp_path / "reports"
    _invoke(cases_dir, out, "--no-judge")

    assert not (out / "180-attach-delivery" / "judge").exists()


def test_summary_json_separates_judge_errors_from_reviewer_errors(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path, tmp_path: Path
) -> None:
    _stub_judging_run_case(
        monkeypatch,
        statuses=("ok", "failed", "unparsed"),
        caught=(True, False, False),
        structured=(True, True, True),
    )
    out = tmp_path / "reports"
    _invoke(cases_dir, out)

    data = json.loads((out / "180-attach-delivery" / "summary.json").read_text())
    assert data["judge_errored"] == 2
    assert data["judge_status_per_sample"] == ["ok", "failed", "unparsed"]
    assert data["errored"] == 0  # the REVIEWERS were fine
    assert data["structured_caught_per_sample"] == [True, True, True]
    assert data["structured_caught"] == 3


def test_table_shows_the_structured_delta_when_it_differs(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    # judge vetoes 2 of 3 that the structured matcher accepts — the #307 shape
    _stub_judging_run_case(
        monkeypatch,
        statuses=("ok", "ok", "ok"),
        caught=(True, False, False),
        structured=(True, True, True),
    )
    result = _invoke(cases_dir)
    assert "struct 3/3" in result.output


def test_table_omits_the_structured_delta_when_it_agrees(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    _stub_judging_run_case(
        monkeypatch, statuses=("ok",) * 3, caught=(True,) * 3, structured=(True,) * 3
    )
    result = _invoke(cases_dir)
    assert "struct" not in result.output


def test_judge_failures_are_named_on_stderr(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    _stub_judging_run_case(
        monkeypatch,
        statuses=("ok", "failed"),
        caught=(True, False),
        structured=(True, True),
    )
    result = _invoke(cases_dir)
    plain = _plain(result.output)
    assert "judge gave no verdict" in plain
    assert "1 failed" in plain
    assert "+1jerr" in plain  # and it is visible in the catch cell too


def test_summary_validity_counts_judge_errors_too(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path, tmp_path: Path
) -> None:
    """summary.json must not contradict the rates it sits beside (#307 review).

    `n_valid` was reviewer-errors-only while `catch_rate`, the table and the
    block-rate gate all used the combined rule — so a report dir could record
    `n_valid: 3` for a catch rate computed over one sample.
    """
    _stub_judging_run_case(
        monkeypatch,
        statuses=("ok", "failed", "failed"),
        caught=(True, False, False),
        structured=(True, True, True),
    )
    out = tmp_path / "reports"
    _invoke(cases_dir, out)

    data = json.loads((out / "180-attach-delivery" / "summary.json").read_text())
    assert data["n_valid"] == 1  # not 3
    assert data["errored"] == 0  # reviewer-only meaning is preserved
    assert data["judge_errored"] == 2


def test_summary_known_good_block_count_excludes_judge_errors(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path, tmp_path: Path
) -> None:
    def fake(case, **kwargs):
        return CaseResult(
            case_id=case.id,
            n=2,
            catch_rate=1.0,
            severity_correctness=1.0,
            false_positive_rate=0.0,
            passed=True,
            caught_per_sample=(True, True),
            severity_per_sample=(True, True),
            catch_rate_ci=wilson_interval(2, 2),
            errored_per_sample=(False, False),
            false_positive_per_sample=(False, False),
            false_positive_errored_per_sample=(False, False),
            known_good_findings_per_sample=(1, 1),
            known_good_blocked_per_sample=(True, True),
            # the second known-good sample could not be judged at all
            false_positive_judge_status_per_sample=("ok", "failed"),
        )

    monkeypatch.setattr(eval_cli, "run_case", fake)
    out = tmp_path / "reports"
    _invoke(cases_dir, out)

    data = json.loads((out / "180-attach-delivery" / "summary.json").read_text())
    assert data["known_good_blocked"] == 1  # not 2
    assert data["false_positive_n_valid"] == 1
    assert data["false_positive_judge_errored"] == 1


def test_known_good_judge_errors_are_visible_in_the_fp_cell(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    """Otherwise the known-good denominator silently shrinks (#307 review)."""

    def fake(case, **kwargs):
        return CaseResult(
            case_id=case.id,
            n=2,
            catch_rate=1.0,
            severity_correctness=1.0,
            false_positive_rate=0.0,
            passed=True,
            caught_per_sample=(True, True),
            severity_per_sample=(True, True),
            catch_rate_ci=wilson_interval(2, 2),
            errored_per_sample=(False, False),
            false_positive_per_sample=(False, False),
            false_positive_errored_per_sample=(False, False),
            known_good_findings_per_sample=(0, 0),
            known_good_blocked_per_sample=(False, False),
            false_positive_judge_status_per_sample=("ok", "failed"),
            judge_status_per_sample=("ok", "ok"),
        )

    monkeypatch.setattr(eval_cli, "run_case", fake)
    result = _invoke(cases_dir)
    plain = _plain(result.output)

    assert "0/1 " in plain  # the fp denominator did shrink...
    assert "+1jerr" in plain  # ...and the table says why


def test_crossed_per_sample_disagreement_still_shows_the_struct_note(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    """Equal totals do not mean agreement (#307 review).

    Judge catches sample 0 and misses 1; the structured matcher does the
    reverse. Both tally 1/2, so comparing only the totals hid a case where
    *every* sample disagreed.
    """
    _stub_judging_run_case(
        monkeypatch,
        statuses=("ok", "ok"),
        caught=(True, False),
        structured=(False, True),
    )
    result = _invoke(cases_dir)
    assert "struct 1/2" in _plain(result.output)
