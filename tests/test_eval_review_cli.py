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


def _make_case(cases_dir: Path, case_id: str) -> None:
    d = cases_dir / case_id
    d.mkdir(parents=True)
    (d / "case.toml").write_text(_TOML.format(id=case_id))
    (d / "ac.md").write_text("attach must wait for delivery")


@pytest.fixture
def cases_dir(tmp_path: Path) -> Path:
    d = tmp_path / "cases"
    _make_case(d, "180-attach-delivery")
    _make_case(d, "other-case")
    return d


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


def test_failing_bar_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path
) -> None:
    _stub_run_case(monkeypatch, catch_rate=0.2, passed=False)
    result = runner.invoke(
        eval_app,
        ["review", "--cases-dir", str(cases_dir), "--case", "other-case"],
    )
    assert result.exit_code == 1


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
