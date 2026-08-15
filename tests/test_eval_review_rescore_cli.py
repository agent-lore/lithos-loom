"""Tests for ``lithos-loom eval rescore`` (#307).

No test here invokes a real agent: the judge factory is stubbed at the module
that builds it, so the command's flag handling, fail-closed ordering, output and
payload are all exercised for free.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lithos_loom.evals.review import app as eval_app_mod
from lithos_loom.evals.review import cli_rescore  # noqa: F401 — registers the command
from lithos_loom.evals.review.app import eval_app
from lithos_loom.evals.review.match import JudgeVerdict

runner = CliRunner()

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_BOX = re.compile(r"[│╭╮╰╯─]")


def _unwrapped(output: str) -> str:
    """Styling, box-drawing and rich's panel wrapping normalised away."""
    return " ".join(_BOX.sub(" ", _ANSI.sub("", output)).split())


_TOML = """
[case]
id = "{id}"
description = "d"
base = "aaaa"
head = "bbbb"
personas = ["correctness"]
profile = "standard"
tier = "frontier"
acceptance_criteria_file = "ac.md"

[[expected]]
file = "cli/develop.py"
keywords = ["delivery"]
min_severity = "critical"
mechanism = "{mechanism}"
"""


@pytest.fixture
def cases_dir(tmp_path: Path) -> Path:
    d = tmp_path / "cases" / "180-attach-delivery"
    d.mkdir(parents=True)
    (d / "case.toml").write_text(
        _TOML.format(id="180-attach-delivery", mechanism="exits before delivery")
    )
    (d / "ac.md").write_text("attach must wait for delivery")
    return tmp_path / "cases"


def _finding(fid: str = "f-001") -> dict:
    return {
        "reviewer": "correctness",
        "severity": "critical",
        "files": ["cli/develop.py"],
        "rationale": "exits on approved before delivery",
        "finding_id": fid,
    }


def _report(findings: list[dict] | None = None) -> dict:
    return {
        "blocking": bool(findings),
        "reviewers": [
            {
                "name": "correctness",
                "status": "FINDINGS" if findings else "LGTM",
                "passed": not findings,
                "findings": findings or [],
            }
        ],
    }


@pytest.fixture
def report_dir(tmp_path: Path) -> Path:
    root = tmp_path / "reports" / "180-attach-delivery"
    root.mkdir(parents=True)
    for i in range(2):
        (root / f"buggy-{i}.json").write_text(json.dumps(_report([_finding()])))
    return tmp_path / "reports"


@pytest.fixture(autouse=True)
def _default_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli_rescore,
        "load_tool_default_models",
        lambda: ({"codex": "gpt-test", "claude": "claude-test"}, ()),
    )


def _stub_judge(monkeypatch: pytest.MonkeyPatch, *, pattern: list[bool] | None = None):
    """Install a scripted judge; returns the call counter."""
    calls = {"n": 0}
    seq = pattern or [True]

    def build(**_kwargs):
        def judge(_mech: str, findings: list[dict]) -> JudgeVerdict:
            i = calls["n"]
            calls["n"] += 1
            if seq[min(i, len(seq) - 1)]:
                return JudgeVerdict(
                    matched_ids=tuple(f["finding_id"] for f in findings)
                )
            return JudgeVerdict()

        return judge

    monkeypatch.setattr(eval_app_mod, "build_agent_judge", build)
    return calls


def _run(report_dir: Path, cases_dir: Path, *extra: str):
    return runner.invoke(
        eval_app,
        ["rescore", str(report_dir), "--cases-dir", str(cases_dir), *extra],
    )


# ── the happy path ───────────────────────────────────────────────────────────


def test_prints_the_same_core_columns_as_review(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path
) -> None:
    _stub_judge(monkeypatch)
    result = _run(report_dir, cases_dir)

    assert result.exit_code == 0, result.output
    header = _unwrapped(result.output)
    for column in ("case", "tier", "catch (95% CI)", "sev", "fp (95% CI)", "noise"):
        assert column in header
    assert "180-attach-delivery" in result.output


def test_writes_rescore_json_without_touching_summary_json(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path
) -> None:
    summary = report_dir / "180-attach-delivery" / "summary.json"
    summary.write_text('{"case": "180-attach-delivery"}')
    before = summary.read_bytes()
    _stub_judge(monkeypatch)

    _run(report_dir, cases_dir)

    assert summary.read_bytes() == before
    payload = json.loads((report_dir / "rescore.json").read_text())
    assert payload["schema_version"] == 1
    (case,) = payload["cases"]
    assert case["case"] == "180-attach-delivery"
    # same field names as summary.json, so drift compares field-for-field
    assert "caught_per_sample" in case["judged"]
    assert "caught_per_sample" in case["structured"]


def test_out_redirects_the_payload(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path, tmp_path: Path
) -> None:
    _stub_judge(monkeypatch)
    target = tmp_path / "elsewhere.json"
    _run(report_dir, cases_dir, "--out", str(target))

    assert target.is_file()
    assert not (report_dir / "rescore.json").exists()


def test_the_veto_audit_trail_is_recorded_at_one_repeat(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path
) -> None:
    _stub_judge(monkeypatch, pattern=[False])
    _run(report_dir, cases_dir)

    (case,) = json.loads((report_dir / "rescore.json").read_text())["cases"]
    site = case["stability"]["sites"][0]
    assert site["produced"] == ["f-001"]
    assert site["verdicts"] == [[]]  # a finding was produced and nothing matched


# ── cost is visible, and payable only on purpose ─────────────────────────────


def test_dry_run_prints_the_call_count_and_makes_no_call(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path
) -> None:
    calls = _stub_judge(monkeypatch)
    result = _run(report_dir, cases_dir, "--dry-run")

    assert result.exit_code == 0
    assert "2 judge call(s)" in _unwrapped(result.output)
    assert calls["n"] == 0
    assert not (report_dir / "rescore.json").exists()


def test_no_judge_makes_no_calls(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path
) -> None:
    calls = _stub_judge(monkeypatch)
    result = _run(report_dir, cases_dir, "--no-judge")

    assert result.exit_code == 0
    assert calls["n"] == 0


def test_case_filter_scopes_the_work(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path
) -> None:
    (report_dir / "other").mkdir()
    (report_dir / "other" / "buggy-0.json").write_text(json.dumps(_report()))
    _stub_judge(monkeypatch)

    result = _run(report_dir, cases_dir, "--case", "180-attach-delivery")
    assert result.exit_code == 0
    assert "rescoring 1 case(s)" in _unwrapped(result.output)


# ── stability, the measurement this command exists for ───────────────────────


def test_flip_and_spread_appear_only_above_one_repeat(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path
) -> None:
    def header_of(result) -> str:
        # the table header, not the whole output — tmp_path names contain the
        # test name, which would match these column labels by accident
        return next(
            line
            for line in result.output.splitlines()
            if line.startswith("case ") and "tier" in line
        )

    _stub_judge(monkeypatch)
    assert "flip" not in header_of(_run(report_dir, cases_dir))

    _stub_judge(monkeypatch)
    header = header_of(_run(report_dir, cases_dir, "--judge-repeats", "2"))
    assert "flip" in header and "spread" in header


def test_a_flipped_verdict_is_reported(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path
) -> None:
    # sample 0: confirm then veto (a flip); sample 1: confirm twice (stable)
    _stub_judge(monkeypatch, pattern=[True, False, True, True])
    result = _run(report_dir, cases_dir, "--judge-repeats", "2")
    plain = _unwrapped(result.output)

    assert "judge stability:" in plain
    assert "1/2 judged sites flipped" in plain


def test_repeats_with_no_judge_is_rejected_pre_paid(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path
) -> None:
    """A lever that cannot fire is the no-op-arm class the repo fails closed on."""
    calls = _stub_judge(monkeypatch)
    result = _run(report_dir, cases_dir, "--no-judge", "--judge-repeats", "3")

    assert result.exit_code != 0
    assert "measures nothing" in _unwrapped(result.output)
    assert calls["n"] == 0


def test_zero_repeats_is_rejected(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path
) -> None:
    _stub_judge(monkeypatch)
    result = _run(report_dir, cases_dir, "--judge-repeats", "0")
    assert result.exit_code != 0
    assert "must be at least 1" in _unwrapped(result.output)


# ── fail-closed preflight ────────────────────────────────────────────────────


def test_bar_out_of_range_is_rejected(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path
) -> None:
    _stub_judge(monkeypatch)
    result = _run(report_dir, cases_dir, "--bar", "1.5")
    assert result.exit_code != 0
    assert "must be a finite rate between 0 and 1" in _unwrapped(result.output)


def test_missing_report_dir_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, cases_dir: Path, tmp_path: Path
) -> None:
    _stub_judge(monkeypatch)
    result = _run(tmp_path / "nope", cases_dir)
    assert result.exit_code != 0
    assert "no report directory" in _unwrapped(result.output)


def test_a_case_missing_from_the_tree_aborts_before_any_call(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path
) -> None:
    (report_dir / "gone-case").mkdir()
    (report_dir / "gone-case" / "buggy-0.json").write_text(json.dumps(_report()))
    calls = _stub_judge(monkeypatch)

    result = _run(report_dir, cases_dir)
    assert result.exit_code != 0
    assert "gone-case" in _unwrapped(result.output)
    assert calls["n"] == 0


def test_judge_without_an_explicit_model_is_rejected(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path
) -> None:
    """#304 reaches the rescore judge too — its verdicts decide the numbers."""
    monkeypatch.setattr(cli_rescore, "load_tool_default_models", lambda: ({}, ()))
    _stub_judge(monkeypatch)

    result = _run(report_dir, cases_dir)
    assert result.exit_code != 0
    assert "resolves to no explicit model" in _unwrapped(result.output)


# ── the case-identity guard ──────────────────────────────────────────────────


def _summary_with(report_dir: Path, fingerprint: str) -> None:
    (report_dir / "180-attach-delivery" / "summary.json").write_text(
        json.dumps({"case": "180-attach-delivery", "expected_fingerprint": fingerprint})
    )


def test_absent_fingerprint_warns_loudly_and_proceeds(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path
) -> None:
    """Every dir written before the field exists lands here; refusing them would
    make the whole retained corpus unrescorable."""
    _stub_judge(monkeypatch)
    result = _run(report_dir, cases_dir)

    assert result.exit_code == 0
    assert "case identity unverifiable" in _unwrapped(result.output)


def test_a_changed_case_aborts_before_any_call(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path
) -> None:
    _summary_with(report_dir, "sha256:deadbeef")
    calls = _stub_judge(monkeypatch)

    result = _run(report_dir, cases_dir)
    assert result.exit_code != 0
    assert "has changed since these reports were written" in _unwrapped(result.output)
    assert calls["n"] == 0


def test_allow_changed_cases_proceeds_and_records_it(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path
) -> None:
    _summary_with(report_dir, "sha256:deadbeef")
    _stub_judge(monkeypatch)

    result = _run(report_dir, cases_dir, "--allow-changed-cases")
    assert result.exit_code == 0
    (case,) = json.loads((report_dir / "rescore.json").read_text())["cases"]
    assert case["case_identity"] == "changed"


def test_a_matching_fingerprint_is_silent_and_verified(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path
) -> None:
    from lithos_loom.evals.review.case import expected_fingerprint, load_case

    _summary_with(
        report_dir, expected_fingerprint(load_case(cases_dir / "180-attach-delivery"))
    )
    _stub_judge(monkeypatch)

    result = _run(report_dir, cases_dir)
    assert result.exit_code == 0
    assert "unverifiable" not in _unwrapped(result.output)
    (case,) = json.loads((report_dir / "rescore.json").read_text())["cases"]
    assert case["case_identity"] == "verified"


# ── measurement never sets the exit code ─────────────────────────────────────


def test_a_regressed_floor_case_still_exits_zero(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path
) -> None:
    """A gate that runs no reviewers would gate on the judge's mood."""
    toml = (cases_dir / "180-attach-delivery" / "case.toml").read_text()
    (cases_dir / "180-attach-delivery" / "case.toml").write_text(
        toml.replace('tier = "frontier"', 'tier = "floor"')
    )
    _stub_judge(monkeypatch, pattern=[False])  # veto everything -> 0/2

    result = _run(report_dir, cases_dir)
    assert result.exit_code == 0
    assert "REGRESSED" in result.output
