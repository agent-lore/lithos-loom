"""Per-tool gate adapters: tool JSON -> GateFinding + severity mapping (#132).

Each tool's parser is exercised against representative JSON fixtures (inline so
the mapping is visible next to the assertion), covering the severity table, the
field mapping, and defensive handling of empty / off-format output.
"""

from __future__ import annotations

from lithos_loom.plugins.story_develop.gate_adapters import (
    SUPPORTED_TOOLS,
    parse_findings,
)

# --- ruff (``ruff check --output-format=json``) -------------------------------

_RUFF_JSON = """[
  {"code": "F401", "filename": "/w/a.py", "location": {"row": 1, "column": 1},
   "message": "`os` imported but unused"},
  {"code": "W291", "filename": "/w/a.py", "location": {"row": 5, "column": 9},
   "message": "trailing whitespace"},
  {"code": null, "filename": "/w/b.py", "location": {"row": 2, "column": 1},
   "message": "SyntaxError: bad token"}
]"""


def test_ruff_maps_fields_and_severity() -> None:
    findings = parse_findings("lint", "ruff", _RUFF_JSON)
    assert len(findings) == 3
    by_rule = {f.rule: f for f in findings}
    assert by_rule["F401"].severity == "major"
    assert by_rule["F401"].file == "/w/a.py"
    assert by_rule["F401"].line == 1
    assert by_rule["F401"].tool == "ruff"
    # W-prefixed (warning) rules are minor.
    assert by_rule["W291"].severity == "minor"
    # a null code (e.g. a syntax error) keeps a stable placeholder rule, major.
    assert by_rule["ruff-error"].severity == "major"
    assert "SyntaxError" in by_rule["ruff-error"].message


# --- bandit (``bandit -f json``) ----------------------------------------------

_BANDIT_JSON = """{"errors": [], "results": [
  {"test_id": "B602", "issue_severity": "HIGH", "issue_text": "subprocess shell=True",
   "filename": "/w/x.py", "line_number": 10},
  {"test_id": "B113", "issue_severity": "MEDIUM", "issue_text": "no timeout",
   "filename": "/w/y.py", "line_number": 4},
  {"test_id": "B101", "issue_severity": "LOW", "issue_text": "assert used",
   "filename": "/w/z.py", "line_number": 3}
], "metrics": {}}"""


def test_bandit_severity_table_high_medium_low() -> None:
    findings = parse_findings("sast", "bandit", _BANDIT_JSON)
    by_rule = {f.rule: f for f in findings}
    assert by_rule["B602"].severity == "critical"  # HIGH -> critical
    assert by_rule["B113"].severity == "major"  # MEDIUM -> major
    assert by_rule["B101"].severity == "minor"  # LOW -> minor
    assert by_rule["B602"].file == "/w/x.py" and by_rule["B602"].line == 10
    assert all(f.tool == "bandit" for f in findings)


# --- pip-audit (``pip-audit --format=json``) ----------------------------------

_PIP_AUDIT_JSON = """{"dependencies": [
  {"name": "flask", "version": "0.5", "vulns": [
    {"id": "PYSEC-2019-179", "fix_versions": ["0.12.3"], "description": "..."}]},
  {"name": "requests", "version": "2.0", "vulns": [
    {"id": "GHSA-xxxx", "fix_versions": [], "description": "..."}]},
  {"name": "safe-pkg", "version": "1.0", "vulns": []}
]}"""


def test_pip_audit_one_finding_per_vuln_skips_clean_deps() -> None:
    findings = parse_findings("dep-audit", "pip-audit", _PIP_AUDIT_JSON)
    assert {f.rule for f in findings} == {
        "PYSEC-2019-179",
        "GHSA-xxxx",
    }  # safe-pkg dropped
    assert all(f.severity == "major" for f in findings)
    by_rule = {f.rule: f for f in findings}
    assert by_rule["PYSEC-2019-179"].package == "flask"  # locus = the package
    assert "flask 0.5" in by_rule["PYSEC-2019-179"].message
    assert "0.12.3" in by_rule["PYSEC-2019-179"].message  # fix version surfaced
    assert "no fix available" in by_rule["GHSA-xxxx"].message


_PIP_AUDIT_SHARED_CVE = """{"dependencies": [
  {"name": "pkg-a", "version": "1.0", "vulns": [{"id": "CVE-2020-1"}]},
  {"name": "pkg-b", "version": "2.0", "vulns": [{"id": "CVE-2020-1"}]}
]}"""


def test_pip_audit_same_cve_in_two_packages_is_two_distinct_findings() -> None:
    # Regression: two packages sharing one CVE id must not collapse — the package
    # is part of the finding's identity (its fingerprint).
    findings = parse_findings("dep-audit", "pip-audit", _PIP_AUDIT_SHARED_CVE)
    assert len(findings) == 2
    assert {f.package for f in findings} == {"pkg-a", "pkg-b"}
    assert all(f.rule == "CVE-2020-1" for f in findings)
    assert findings[0].fingerprint != findings[1].fingerprint


# --- defensive behaviour ------------------------------------------------------


def test_unknown_tool_yields_no_findings() -> None:
    assert parse_findings("lint", "flake8", _RUFF_JSON) == []


def test_empty_and_malformed_output_yield_no_findings() -> None:
    assert parse_findings("lint", "ruff", "") == []
    assert parse_findings("lint", "ruff", "   ") == []
    assert parse_findings("lint", "ruff", "not json at all") == []
    assert parse_findings("sast", "bandit", "{truncated") == []


def test_supported_tools_are_the_three_shipped() -> None:
    assert {"ruff", "bandit", "pip-audit"} == SUPPORTED_TOOLS
