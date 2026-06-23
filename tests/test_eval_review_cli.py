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
        seen.append({"case": case.id, "kwargs": kwargs})
        return CaseResult(
            case_id=case.id,
            n=kwargs.get("k", 5),
            catch_rate=catch_rate,
            severity_correctness=1.0,
            false_positive_rate=0.0,
            passed=passed,
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
