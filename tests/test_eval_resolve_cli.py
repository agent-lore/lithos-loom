"""Tests for ``lithos-loom eval resolve`` — the argv goes through the real parser."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lithos_loom.evals.resolve import cli as resolve_cli
from lithos_loom.evals.resolve.case import Probe, ResolveCase
from lithos_loom.evals.resolve.harness import ProbeResult, ResolveOutcome, Trees
from lithos_loom.evals.review.app import eval_app

runner = CliRunner()

_MB, _BASE, _HEAD, _GOOD, _BAD = ("a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40)
_MERGE, _FINAL = "f" * 40, "9" * 40
_CASE = f'''
[case]
id = "r1"
description = "d"
repo = "."
title = "the PR"
merge_base = "{_MB}"
base = "{_BASE}"
head = "{_HEAD}"
known_good = "{_GOOD}"
known_bad = "{_BAD}"
personas = ["correctness"]

[[probe]]
name = "p1"
command = "true"
'''


def _cases_dir(tmp_path: Path) -> Path:
    d = tmp_path / "cases" / "r1"
    d.mkdir(parents=True, exist_ok=True)
    (d / "case.toml").write_text(_CASE, encoding="utf-8")
    (d / "ac.md").write_text("ac", encoding="utf-8")
    return tmp_path / "cases"


def _outcome(
    status: str = "converged", *, merge_sha: str = _MERGE, final_sha: str = _FINAL
) -> ResolveOutcome:
    return ResolveOutcome(
        status=status,
        message=f"{status} msg",
        rounds=1,
        cost_usd=2.0,
        conflict_paths=("a.py",),
        merge_sha=merge_sha,
        final_sha=final_sha,
        gate_green=True,
        findings_by_severity={"critical": 0, "major": 0, "minor": 0},
        retained={
            "merge.diff": "MERGE",
            "final.diff": "FINAL",
            "conversation.md": "LOG",
        },
    )


def _install(
    monkeypatch: pytest.MonkeyPatch,
    outcome: ResolveOutcome | Exception,
    *,
    fails: frozenset[tuple[str, str]] = frozenset(),
) -> tuple[list[dict], list[tuple[str, str]]]:
    seen: list[dict] = []
    probed: list[tuple[str, str]] = []

    def fake_live(case: ResolveCase, trees: Trees, **kw) -> ResolveOutcome:
        seen.append({"case": case.id, "trees": trees, **kw})
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def fake_probe(case: ResolveCase, sha: str, probe: Probe, **kw) -> ProbeResult:
        probed.append((sha, probe.name))
        passed = (sha, probe.name) not in fails and sha != _BAD
        return ProbeResult(
            name=probe.name, passed=passed, exit_code=0 if passed else 1, output=""
        )

    monkeypatch.setattr(resolve_cli, "live_resolve", fake_live)
    monkeypatch.setattr(resolve_cli, "run_probe", fake_probe)
    monkeypatch.setattr(
        resolve_cli,
        "materialise_trees",
        lambda case: (
            Trees(head=_HEAD, known_good=_GOOD, known_bad=_BAD),
            lambda: None,
        ),
    )
    monkeypatch.setattr(
        resolve_cli,
        "load_tool_default_models",
        lambda: ({"claude": "m-claude", "codex": "m-codex"}, ()),
    )
    return seen, probed


def test_runs_k_samples_prints_table_and_writes_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen, probed = _install(monkeypatch, _outcome())
    out = tmp_path / "reports"
    result = runner.invoke(
        eval_app,
        [
            "resolve",
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
    assert seen[0]["tool"] == "claude" and seen[0]["model"] == "m-claude"
    assert seen[0]["trees"].head == _HEAD
    assert seen[0]["max_rounds"] == 5
    assert seen[0]["coder_timeout"] == 3600 and seen[0]["reviewer_timeout"] == 3600
    assert seen[0]["profile"] == "standard"
    assert [s.name for s in seen[0]["reviewers"]] == ["correctness"]
    assert all(
        s.model for s in seen[0]["reviewers"]
    )  # #304: explicit, filled from defaults
    # the oracle was validated on both controls before the first run
    assert probed[:2] == [(_GOOD, "p1"), (_BAD, "p1")]
    assert "r1" in result.output and "PASS" in result.output
    assert "2/2" in result.output
    summary = json.loads((out / "r1" / "summary.json").read_text())
    assert summary["n"] == 2 and summary["n_valid"] == 2
    assert summary["correct_final"] == 2 and summary["unsafe"] == 0
    assert summary["passed"] is True
    assert summary["coder"] == {"tool": "claude", "model": "m-claude", "effort": None}
    assert summary["panel"][0]["name"] == "correctness"
    assert summary["profile"] == "standard" and summary["max_rounds"] == 5
    assert summary["develop"] == {"check_commands": {}, "check_states": {}}
    assert summary["tree"] == f"{_MB[:12]}+{_HEAD[:12]} ⇐ {_BASE[:12]}"
    assert summary["trees"] == {"head": _HEAD, "known_good": _GOOD, "known_bad": _BAD}
    assert len(summary["expected_fingerprint"]) == 16
    assert summary["status_per_sample"] == ["converged", "converged"]
    sample = json.loads((out / "r1" / "sample-0.json").read_text())
    assert sample["status"] == "converged" and sample["correct_final"] is True
    assert "retained" not in sample  # retained material lands in its own files
    assert (out / "r1" / "sample-0.merge.diff").read_text() == "MERGE"
    assert (out / "r1" / "sample-0.final.diff").read_text() == "FINAL"
    assert (out / "r1" / "sample-1.conversation.md").read_text() == "LOG"


def test_an_unsafe_approval_reads_fail_but_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, _outcome(), fails=frozenset({(_FINAL, "p1")}))
    result = runner.invoke(
        eval_app, ["resolve", "--cases-dir", str(_cases_dir(tmp_path)), "-k", "1"]
    )
    assert result.exit_code == 0, result.output
    assert "FAIL" in result.output
    assert "UNSAFE" in result.output


def test_a_correct_but_rejected_resolution_is_wasted_and_fails_the_bar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, _outcome("not_converged"))
    result = runner.invoke(
        eval_app, ["resolve", "--cases-dir", str(_cases_dir(tmp_path)), "-k", "1"]
    )
    assert result.exit_code == 0, result.output
    # pipeline-right 1/1 but approved 0/1: passes the bar, no unsafe → PASS,
    # and the wasted count is on the row
    assert "PASS" in result.output
    assert "wasted 1" in result.output


def test_all_errored_samples_exit_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, _outcome("infra_failed", merge_sha="", final_sha=""))
    result = runner.invoke(
        eval_app, ["resolve", "--cases-dir", str(_cases_dir(tmp_path)), "-k", "2"]
    )
    assert result.exit_code == 1, result.output
    assert "no valid samples" in result.output


def test_a_crashing_run_is_an_errored_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, RuntimeError("docker exploded"))
    result = runner.invoke(
        eval_app, ["resolve", "--cases-dir", str(_cases_dir(tmp_path)), "-k", "1"]
    )
    assert result.exit_code == 1, result.output
    assert "docker exploded" in result.output


def test_an_oracle_that_does_not_discriminate_aborts_before_any_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen, _ = _install(monkeypatch, _outcome(), fails=frozenset({(_GOOD, "p1")}))
    result = runner.invoke(
        eval_app, ["resolve", "--cases-dir", str(_cases_dir(tmp_path)), "-k", "1"]
    )
    assert result.exit_code == 1, result.output
    assert "known-good" in result.output
    assert seen == []


def test_a_fixture_the_intake_refuses_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen, _ = _install(monkeypatch, _outcome("no_conflict", merge_sha="", final_sha=""))
    result = runner.invoke(
        eval_app, ["resolve", "--cases-dir", str(_cases_dir(tmp_path)), "-k", "3"]
    )
    assert result.exit_code == 1, result.output
    assert "no_conflict" in result.output
    assert len(seen) == 1


def test_explicit_coder_model_is_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, _outcome())
    monkeypatch.setattr(
        resolve_cli, "load_tool_default_models", lambda: ({"codex": "m-codex"}, ())
    )
    cases = str(_cases_dir(tmp_path))
    result = runner.invoke(eval_app, ["resolve", "--cases-dir", cases, "-k", "1"])
    assert result.exit_code != 0
    assert "explicit model" in result.output.replace("\n", " ")
    blank = runner.invoke(
        eval_app, ["resolve", "--cases-dir", cases, "-k", "1", "--model", " "]
    )
    assert blank.exit_code != 0
    ok = runner.invoke(
        eval_app, ["resolve", "--cases-dir", cases, "-k", "1", "--model", "m-x"]
    )
    assert ok.exit_code == 0, ok.output


def test_the_panel_needs_explicit_models_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen, _ = _install(monkeypatch, _outcome())
    monkeypatch.setattr(
        resolve_cli, "load_tool_default_models", lambda: ({"claude": "m-claude"}, ())
    )
    result = runner.invoke(
        eval_app, ["resolve", "--cases-dir", str(_cases_dir(tmp_path)), "-k", "1"]
    )
    # the correctness persona runs on codex, which has no default here
    assert result.exit_code != 0 and seen == []
    ok = runner.invoke(
        eval_app,
        [
            "resolve",
            "--cases-dir",
            str(_cases_dir(tmp_path)),
            "-k",
            "1",
            "--reviewer-override",
            "correctness.model=m-r",
        ],
    )
    assert ok.exit_code == 0, ok.output
    assert seen[0]["reviewers"][0].model == "m-r"


@pytest.mark.parametrize(
    "argv",
    [
        ["--bar", "1.5"],
        ["-k", "0"],
        ["--effort", "banana"],
        ["--tool", "gpt"],
        ["--max-rounds", "0"],
        ["--coder-timeout", "0"],
        ["--reviewer-timeout", "0"],
        ["--probe-timeout", "0"],
        ["--case", "nope"],
        ["--reviewer-override", "nope.model=x"],
        ["--profile", "nope"],
    ],
)
def test_options_are_validated_before_any_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    seen, probed = _install(monkeypatch, _outcome())
    result = runner.invoke(
        eval_app, ["resolve", "--cases-dir", str(_cases_dir(tmp_path)), *argv]
    )
    assert result.exit_code != 0
    assert seen == [] and probed == []


def test_levers_reach_the_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen, _ = _install(monkeypatch, _outcome())
    result = runner.invoke(
        eval_app,
        [
            "resolve",
            "--cases-dir",
            str(_cases_dir(tmp_path)),
            "-k",
            "1",
            "--tool",
            "codex",
            "--effort",
            " High ",
            "--max-rounds",
            "2",
            "--coder-timeout",
            "42",
            "--reviewer-timeout",
            "43",
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen[0]["tool"] == "codex" and seen[0]["model"] == "m-codex"
    assert seen[0]["effort"] == "high" and seen[0]["max_rounds"] == 2
    assert (seen[0]["coder_timeout"], seen[0]["reviewer_timeout"]) == (42, 43)
    assert "codex/m-codex/high" in result.output
