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
body = "true"
expected = "proceed"

[[finding]]
id = "f-002"
body = "false"
expected = "reject"
refutation = ["src/a.py:3-4"]
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

    def fake_live(case: TriageCase, sha: str, **kw) -> TriageVerdicts:
        seen.append({"case": case.id, "sha": sha, **kw})
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
    assert seen[0]["sha"] == _SHA
    assert seen[0]["timeout"] == 3600 and seen[0]["effort"] is None
    assert "t1" in result.output and "PASS" in result.output
    assert "2/2" in result.output  # known-false rejected 2 of 2 opportunities
    summary = json.loads((out / "t1" / "summary.json").read_text())
    assert summary["reject_rate"] == 1.0
    assert summary["over_suppression_rate"] == 0.0
    assert summary["triage"] == {
        "tool": "claude",
        "model": "m-default",
        "effort": None,
        "timeout": 3600,
    }
    assert summary["per_finding_correct"] == {"f-001": 2, "f-002": 2}
    assert summary["per_finding_ambiguous"] == {"f-001": False, "f-002": False}
    assert summary["samples_with_suppression"] == 0
    assert summary["tree"] == _SHA[:12]
    assert len(summary["expected_fingerprint"]) == 16
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
    # A blank --model is "not given", not "the empty model".
    blank = runner.invoke(
        eval_app,
        [
            "triage",
            "--cases-dir",
            str(_cases_dir(tmp_path)),
            "-k",
            "1",
            "--model",
            "  ",
        ],
    )
    assert blank.exit_code != 0

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


def test_effort_tool_and_timeout_are_validated_before_any_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _install(monkeypatch, TriageVerdicts(proceed=("f-001", "f-002")))
    cases = str(_cases_dir(tmp_path))
    bad_effort = runner.invoke(
        eval_app, ["triage", "--cases-dir", cases, "-k", "1", "--effort", "banana"]
    )
    assert bad_effort.exit_code != 0 and seen == []
    assert "effort" in bad_effort.output
    bad_tool = runner.invoke(
        eval_app, ["triage", "--cases-dir", cases, "--tool", "gpt"]
    )
    assert bad_tool.exit_code != 0 and seen == []
    bad_timeout = runner.invoke(
        eval_app, ["triage", "--cases-dir", cases, "--timeout", "0"]
    )
    assert bad_timeout.exit_code != 0 and seen == []

    ok = runner.invoke(
        eval_app,
        [
            "triage",
            "--cases-dir",
            cases,
            "-k",
            "1",
            "--effort",
            " High ",
            "--timeout",
            "42",
        ],
    )
    assert ok.exit_code == 0, ok.output
    assert seen[0]["effort"] == "high" and seen[0]["timeout"] == 42


def test_case_filter_and_pooled_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _install(
        monkeypatch,
        TriageVerdicts(proceed=("f-001",), rejections={"f-002": "src/a.py:3 x"}),
    )
    cases = _cases_dir(tmp_path)
    second = cases / "t2"
    second.mkdir()
    (second / "case.toml").write_text(
        _CASE.replace('id = "t1"', 'id = "t2"'), encoding="utf-8"
    )
    (second / "ac.md").write_text("ac", encoding="utf-8")

    only = runner.invoke(
        eval_app, ["triage", "--cases-dir", str(cases), "--case", "t2", "-k", "1"]
    )
    assert only.exit_code == 0, only.output
    assert [s["case"] for s in seen] == ["t2"]
    assert "pooled" not in only.output

    seen.clear()
    both = runner.invoke(eval_app, ["triage", "--cases-dir", str(cases), "-k", "2"])
    assert both.exit_code == 0, both.output
    assert [s["case"] for s in seen] == ["t1", "t1", "t2", "t2"]
    assert "pooled: known-false rejected 4/4" in both.output
    assert "must-proceed suppressed 0/4" in both.output


def test_errored_samples_are_marked_on_the_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def flaky(case: TriageCase, sha: str, **kw) -> TriageVerdicts:
        calls["n"] += 1
        if calls["n"] == 1:
            return TriageVerdicts(proceed=("f-001", "f-002"), note="turn failed")
        return TriageVerdicts(proceed=("f-001",), rejections={"f-002": "src/a.py:4 x"})

    monkeypatch.setattr(triage_cli, "live_triage", flaky)
    monkeypatch.setattr(
        triage_cli, "load_tool_default_models", lambda: ({"claude": "m"}, ())
    )
    result = runner.invoke(
        eval_app, ["triage", "--cases-dir", str(_cases_dir(tmp_path)), "-k", "2"]
    )
    assert result.exit_code == 0, result.output
    assert "PASS +1err" in result.output
    assert "1/1" in result.output  # one valid sample → one known-false opportunity
