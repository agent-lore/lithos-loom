"""Tests for ``lithos-loom eval triage`` — the argv goes through the real parser."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lithos_loom.evals.review.app import eval_app
from lithos_loom.evals.triage import cli as triage_cli
from lithos_loom.evals.triage.case import TriageCase
from lithos_loom.plugins.story_develop.external_triage import TriageVerdicts

runner = CliRunner()

_SHA = "c" * 40
_CASE = f'''
[case]
id = "t1"
description = "d"
sha = "{_SHA}"

[[finding]]
id = "f-001"
severity = "major"
rationale = "true"
expected = "proceed"

[[finding]]
id = "f-002"
severity = "major"
rationale = "false"
expected = "reject"
refutation_files = ["src/a.py"]
provenance = "synthetic"
'''


def _cases_dir(tmp_path: Path) -> Path:
    d = tmp_path / "cases" / "t1"
    d.mkdir(parents=True, exist_ok=True)
    (d / "case.toml").write_text(_CASE, encoding="utf-8")
    (d / "ac.md").write_text("ac", encoding="utf-8")
    return tmp_path / "cases"


def _install(monkeypatch: pytest.MonkeyPatch, verdicts: TriageVerdicts) -> list[dict]:
    seen: list[dict] = []

    def fake_live(case: TriageCase, **kw) -> TriageVerdicts:
        seen.append({"case": case.id, **kw})
        return verdicts

    monkeypatch.setattr(triage_cli, "live_triage", fake_live)
    monkeypatch.setattr(
        triage_cli, "load_tool_default_models", lambda: ({"claude": "m-default"}, ())
    )
    return seen


def test_runs_k_samples_prints_table_and_writes_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _install(
        monkeypatch,
        TriageVerdicts(
            proceed=("f-001",), rejections={"f-002": "src/a.py:3 x"}, cost_usd=0.2
        ),
    )
    out = tmp_path / "reports"
    result = runner.invoke(
        eval_app,
        [
            "triage",
            "--cases-dir",
            str(_cases_dir(tmp_path)),
            "-k",
            "2",
            "--report-dir",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(seen) == 2
    assert seen[0]["tool"] == "claude" and seen[0]["model"] == "m-default"
    assert "t1" in result.output and "PASS" in result.output
    assert "2/2" in result.output  # known-false rejected 2 of 2 opportunities
    summary = json.loads((out / "t1" / "summary.json").read_text())
    assert summary["reject_rate"] == 1.0
    assert summary["over_suppression_rate"] == 0.0
    assert summary["triage"] == {"tool": "claude", "model": "m-default", "effort": None}
    assert summary["per_finding_correct"] == {"f-001": 2, "f-002": 2}
    assert (out / "t1" / "sample-0.json").is_file()
    assert (out / "t1" / "sample-1.json").is_file()


def test_over_suppression_reads_fail_but_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(
        monkeypatch,
        TriageVerdicts(
            proceed=(), rejections={"f-001": "src/a.py:1 no", "f-002": "src/a.py:3 x"}
        ),
    )
    result = runner.invoke(
        eval_app, ["triage", "--cases-dir", str(_cases_dir(tmp_path)), "-k", "1"]
    )
    assert result.exit_code == 0, result.output
    assert "FAIL" in result.output
    assert "1/1" in result.output  # over-suppression 1 of 1 known-true


def test_explicit_model_is_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, TriageVerdicts(proceed=("f-001", "f-002")))
    monkeypatch.setattr(triage_cli, "load_tool_default_models", lambda: ({}, ()))
    result = runner.invoke(
        eval_app, ["triage", "--cases-dir", str(_cases_dir(tmp_path)), "-k", "1"]
    )
    assert result.exit_code != 0
    assert "explicit model" in result.output.replace("\n", " ")

    ok = runner.invoke(
        eval_app,
        [
            "triage",
            "--cases-dir",
            str(_cases_dir(tmp_path)),
            "-k",
            "1",
            "--model",
            "m-x",
        ],
    )
    assert ok.exit_code == 0, ok.output


def test_all_errored_samples_exit_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(
        monkeypatch, TriageVerdicts(proceed=("f-001", "f-002"), note="turn failed")
    )
    result = runner.invoke(
        eval_app, ["triage", "--cases-dir", str(_cases_dir(tmp_path)), "-k", "2"]
    )
    assert result.exit_code == 1, result.output
    assert "no valid samples" in result.output


@pytest.mark.parametrize("argv", [["--bar", "1.5"], ["--max-over-suppression", "-1"]])
def test_rates_are_validated_before_any_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    seen = _install(monkeypatch, TriageVerdicts(proceed=("f-001", "f-002")))
    result = runner.invoke(
        eval_app, ["triage", "--cases-dir", str(_cases_dir(tmp_path)), *argv]
    )
    assert result.exit_code != 0
    assert seen == []


def test_unknown_case_id_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, TriageVerdicts(proceed=("f-001", "f-002")))
    result = runner.invoke(
        eval_app, ["triage", "--cases-dir", str(_cases_dir(tmp_path)), "--case", "nope"]
    )
    assert result.exit_code != 0
