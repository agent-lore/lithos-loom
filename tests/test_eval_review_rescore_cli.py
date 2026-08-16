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


def _stub_judge(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pattern: list[bool] | None = None,
    verdicts: list[JudgeVerdict] | None = None,
):
    """Install a scripted judge; returns the call counter.

    *pattern* scripts confirm/veto; *verdicts* scripts whole verdicts, so a
    judge ERROR (a timeout, an unreadable reply) can be driven too.
    """
    calls = {"n": 0}
    seq = pattern or [True]

    def build(**_kwargs):
        def judge(_mech: str, findings: list[dict]) -> JudgeVerdict:
            i = calls["n"]
            calls["n"] += 1
            if verdicts is not None:
                return verdicts[min(i, len(verdicts) - 1)]
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
    # a finding was produced, the judge answered, and nothing matched
    assert site["verdicts"] == [
        {"status": "ok", "matched": [], "detail": "", "reply": ""}
    ]


def test_a_judge_error_is_serialised_with_its_status_and_reply(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path
) -> None:
    """Without the status a timeout is indistinguishable from a veto on disk."""
    _stub_judge(
        monkeypatch,
        verdicts=[JudgeVerdict(status="unparsed", reply="no verdict line here")],
    )
    _run(report_dir, cases_dir)

    (case,) = json.loads((report_dir / "rescore.json").read_text())["cases"]
    (verdict,) = case["stability"]["sites"][0]["verdicts"]
    assert verdict["status"] == "unparsed"
    assert verdict["reply"] == "no verdict line here"
    assert case["stability"]["unmeasured_sites"] == 2


# ── cost is visible, and payable only on purpose ─────────────────────────────


def test_dry_run_prints_the_request_count_and_makes_no_call(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path
) -> None:
    calls = _stub_judge(monkeypatch)
    result = _run(report_dir, cases_dir, "--dry-run")

    assert result.exit_code == 0
    assert "2 judge verdict request(s)" in _unwrapped(result.output)
    assert calls["n"] == 0
    assert not (report_dir / "rescore.json").exists()


def test_the_preflight_states_the_retry_ceiling_too(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path
) -> None:
    """A failed call retries once, so the request count is not the paid ceiling."""
    _stub_judge(monkeypatch)
    result = _run(report_dir, cases_dir, "--dry-run")

    plain = _unwrapped(result.output)
    assert "up to 4 agent invocations" in plain
    assert "retries once" in plain


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


def test_a_judge_error_is_never_reported_as_a_stable_verdict(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path
) -> None:
    """The failure this command must never have: an all-timed-out measurement
    reading as 100% stable because a timeout matches nothing, like a veto."""
    _stub_judge(monkeypatch, verdicts=[JudgeVerdict(status="failed", detail="boom")])
    result = _run(report_dir, cases_dir, "--judge-repeats", "2")
    plain = _unwrapped(result.output)

    assert "100.0% stable" not in plain
    assert "UNMEASURED" in plain
    assert "no site answered all 2 repeats" in plain


def test_a_site_with_one_errored_repeat_is_excluded_and_counted(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path
) -> None:
    # sample 0: confirm then a timeout (unmeasurable); sample 1: confirm twice
    _stub_judge(
        monkeypatch,
        verdicts=[
            JudgeVerdict(matched_ids=("f-001",)),
            JudgeVerdict(status="failed", detail="boom"),
            JudgeVerdict(matched_ids=("f-001",)),
            JudgeVerdict(matched_ids=("f-001",)),
        ],
    )
    result = _run(report_dir, cases_dir, "--judge-repeats", "2")
    plain = _unwrapped(result.output)

    assert "0/1 judged sites flipped" in plain  # denominator excludes the errored site
    assert "1 site(s) hit a judge error (1 unmeasurable)" in plain
    assert "+1jerr" in plain  # and the row says which case carried it


def test_a_site_that_flips_AND_errors_still_reports_its_judge_error(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path
) -> None:
    """The flip keeps the site in the denominator, so keying the error count on
    exclusion alone would drop the failed repeat out of every aggregate."""
    # sample 0: confirm, veto, timeout — a real flip AND a real judge failure
    _stub_judge(
        monkeypatch,
        verdicts=[
            JudgeVerdict(matched_ids=("f-001",)),
            JudgeVerdict(),
            JudgeVerdict(status="failed", detail="boom"),
            JudgeVerdict(matched_ids=("f-001",)),
        ],
    )
    result = _run(report_dir, cases_dir, "--judge-repeats", "3")
    plain = _unwrapped(result.output)

    assert "1/2 judged sites flipped" in plain  # the flip counts
    assert "1 site(s) hit a judge error (0 unmeasurable)" in plain  # so does the error
    assert "+1jerr" in plain

    (case,) = json.loads((report_dir / "rescore.json").read_text())["cases"]
    assert case["stability"]["errored_sites"] == 1
    assert case["stability"]["unmeasured_sites"] == 0


def test_the_spread_denominator_tracks_each_repeats_valid_samples(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path
) -> None:
    _stub_judge(
        monkeypatch,
        verdicts=[
            JudgeVerdict(matched_ids=("f-001",)),
            JudgeVerdict(status="failed", detail="boom"),
            JudgeVerdict(matched_ids=("f-001",)),
            JudgeVerdict(matched_ids=("f-001",)),
        ],
    )
    result = _run(report_dir, cases_dir, "--judge-repeats", "2")

    # 1-2 catches over 1-2 valid samples — not a fixed K of 2, which would read
    # as a real drop in catches rather than as missing data
    assert "1-2/1-2" in _unwrapped(result.output)


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


@pytest.mark.parametrize(
    ("report", "message"),
    [
        ({"reviewers": [{"findings": [7]}]}, "is not an object"),
        # `x in frozenset(...)` on a list raises TypeError inside the scorer
        ({"reviewers": [{"status": ["invalid"], "findings": []}]}, "not a string"),
        # never raises — silently inverts the noise instrumentation instead
        ({"reviewers": [], "blocking": "false"}, "not a boolean"),
    ],
)
def test_a_malformed_report_aborts_before_any_call(
    monkeypatch: pytest.MonkeyPatch,
    report_dir: Path,
    cases_dir: Path,
    report: dict,
    message: str,
) -> None:
    """Even behind a valid case: the whole corpus is validated before paying.

    The valid case sorts first, so without load-time validation its judge
    requests are bought before the malformed report is ever opened.
    """
    other = report_dir / "zz-later"
    other.mkdir()
    (other / "buggy-0.json").write_text(json.dumps(report))
    calls = _stub_judge(monkeypatch)

    result = _run(report_dir, cases_dir)
    assert result.exit_code != 0
    assert message in _unwrapped(result.output)
    assert calls["n"] == 0


def test_out_pointing_at_a_retained_report_is_refused_pre_paid(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path
) -> None:
    """--out over an input would destroy a paid artefact for a cheap one."""
    victim = report_dir / "180-attach-delivery" / "buggy-0.json"
    before = victim.read_bytes()
    calls = _stub_judge(monkeypatch)

    result = _run(report_dir, cases_dir, "--out", str(victim))
    assert result.exit_code != 0
    assert "refusing to overwrite a paid input" in _unwrapped(result.output)
    assert calls["n"] == 0
    assert victim.read_bytes() == before


def test_out_pointing_at_a_recorded_summary_is_refused(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path
) -> None:
    summary = report_dir / "180-attach-delivery" / "summary.json"
    summary.write_text('{"case": "180-attach-delivery"}')
    _stub_judge(monkeypatch)

    result = _run(report_dir, cases_dir, "--out", str(summary))
    assert result.exit_code != 0
    assert "refusing to overwrite a paid input" in _unwrapped(result.output)


def test_out_with_a_missing_parent_is_refused_pre_paid(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path, tmp_path: Path
) -> None:
    """Otherwise the write fails only once the measurement is already bought."""
    calls = _stub_judge(monkeypatch)
    result = _run(report_dir, cases_dir, "--out", str(tmp_path / "nope" / "r.json"))

    assert result.exit_code != 0
    assert "no directory" in _unwrapped(result.output)
    assert calls["n"] == 0


def test_out_naming_a_stray_file_inside_a_case_dir_is_refused(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path
) -> None:
    """A SUCCESSFUL run must not leave the dir unloadable by the next one."""
    calls = _stub_judge(monkeypatch)
    stray = report_dir / "180-attach-delivery" / "custom.json"

    result = _run(report_dir, cases_dir, "--out", str(stray))
    assert result.exit_code != 0
    assert "unloadable by the next rescore" in _unwrapped(result.output)
    assert calls["n"] == 0


def test_out_may_still_name_rescore_json_inside_a_case_dir(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path
) -> None:
    """The one name the loader tolerates there — so a per-case payload is fine."""
    _stub_judge(monkeypatch)
    target = report_dir / "180-attach-delivery" / "rescore.json"

    assert _run(report_dir, cases_dir, "--out", str(target)).exit_code == 0
    assert target.is_file()
    _stub_judge(monkeypatch)
    assert _run(report_dir, cases_dir).exit_code == 0  # still loadable


def test_the_payload_is_written_through_a_unique_temp_file(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path
) -> None:
    """Concurrent invocations must not share one scratch file — the loser would
    fail after paying for its whole measurement."""
    seen: list[str] = []
    real_replace = cli_rescore.os.replace

    def spy(src, dst):
        seen.append(Path(src).name)
        return real_replace(src, dst)

    monkeypatch.setattr(cli_rescore.os, "replace", spy)
    _stub_judge(monkeypatch)
    _run(report_dir, cases_dir)
    _stub_judge(monkeypatch)
    _run(report_dir, cases_dir)

    assert len(seen) == 2
    assert seen[0] != seen[1]
    assert all(name.startswith(".rescore.json.tmp.") for name in seen)
    assert not list(report_dir.glob(".rescore.json.tmp.*"))  # nothing left behind


def test_out_pointing_at_a_directory_is_refused(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path, tmp_path: Path
) -> None:
    _stub_judge(monkeypatch)
    result = _run(report_dir, cases_dir, "--out", str(tmp_path))

    assert result.exit_code != 0
    assert "is a directory" in _unwrapped(result.output)


# ── the bar the run was scored at ────────────────────────────────────────────


def test_the_recorded_bar_is_honoured(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path
) -> None:
    """A run scored at --bar 0.5 must not read REGRESSED under a default 0.8."""
    (report_dir / "180-attach-delivery" / "summary.json").write_text(
        json.dumps({"case": "180-attach-delivery", "bar": 0.5})
    )
    _stub_judge(monkeypatch, pattern=[True, False])  # 1/2 catches

    result = _run(report_dir, cases_dir)
    assert result.exit_code == 0
    assert "PASS" in result.output
    (case,) = json.loads((report_dir / "rescore.json").read_text())["cases"]
    assert (case["bar"], case["bar_source"]) == (0.5, "summary")


def test_an_explicit_bar_overrides_the_recorded_one_and_says_so(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path
) -> None:
    (report_dir / "180-attach-delivery" / "summary.json").write_text(
        json.dumps({"case": "180-attach-delivery", "bar": 0.5})
    )
    _stub_judge(monkeypatch, pattern=[True, False])

    result = _run(report_dir, cases_dir, "--bar", "0.9")
    assert "FAIL" in result.output
    assert "--bar overrides the bar these runs recorded" in _unwrapped(result.output)
    (case,) = json.loads((report_dir / "rescore.json").read_text())["cases"]
    assert (case["bar"], case["bar_source"]) == (0.9, "flag")


def test_a_run_with_no_recorded_bar_says_it_is_defaulting(
    monkeypatch: pytest.MonkeyPatch, report_dir: Path, cases_dir: Path
) -> None:
    _stub_judge(monkeypatch)
    result = _run(report_dir, cases_dir)

    assert "no bar recorded by these runs" in _unwrapped(result.output)
    (case,) = json.loads((report_dir / "rescore.json").read_text())["cases"]
    assert case["bar_source"] == "default"


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
